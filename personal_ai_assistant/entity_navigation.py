from __future__ import annotations

from pathlib import Path

from .app_resolver import AppResolver, ResolveCandidate
from .file_resolver import FileResolver
from .game_resolver import GameResolver
from .name_resolver import AliasStore, Entity, NameResolver, Resolution, RankedEntity, normalize_name
from .app_profiles import app_profile
from .window_activation import ActivateOrLaunchService, WindowsWindowController, rank_matching_window


class EntityNavigator:
    def __init__(self, aliases=None, roots=(), files_enabled=False, store=None, windows=None, games=None):
        self.names = NameResolver(store or AliasStore())
        self.apps = AppResolver(aliases)
        self.files = FileResolver(self.names, roots, files_enabled)
        self._games = games if games is not None else GameResolver(self.names)
        self.windows = windows or WindowsWindowController()
        self._windows = {}
        self._recent = {}
        self.last_action = ''
        self.last_message = ''
        self._prompt_cache = (None, ())
        self.apps.refresh_background()

    @property
    def games(self):
        if self._games is None:
            self._games = GameResolver(self.names)
        return self._games

    def _exact(self, query, entity, reason):
        return Resolution(query, (RankedEntity(self.games.promote(entity), 1.0, reason),), False)

    def resolve(self, query, context=''):
        key = normalize_name(query)
        game_result = self.games.resolve(query)
        if game_result.best:
            return game_result
        remembered = self.names.store.data['aliases'].get(key)
        if remembered and remembered['kind'] == 'running' and normalize_name(remembered['name']) != key:
            return self.resolve(remembered['name'], context)
        if remembered and remembered['kind'] in {'app', 'file', 'game'}:
            entity = Entity(remembered['id'], remembered['name'], remembered['kind'], remembered['target'], remembered.get('provider', ''))
            if entity.kind == 'game':
                entity = next((e for e in self.games.candidates() if e.id == entity.id), None)
            if entity is not None and (entity.kind == 'game' or Path(entity.target).is_file()):
                return self._exact(query, entity, 'learned alias')
        learned = self.apps.learned_aliases.get(query.strip().lower())
        if learned and Path(learned).is_file():
            return self._exact(query, Entity('app:' + learned.casefold(), Path(learned).stem, 'app', learned), 'app alias')
        self._windows = {str(w.hwnd): w for w in self.windows.list_windows()}
        profile = app_profile(query)
        window = rank_matching_window(tuple(self._windows.values()), profile.name if profile else query)
        if window and (profile or normalize_name(Path(window.process_name).stem) == key or normalize_name(window.title) == key):
            return self._exact(query, Entity('running:' + str(window.hwnd), profile.name if profile else (window.client_name or Path(window.process_name).stem),
                                            'window', str(window.hwnd)), 'running window')
        if profile and ActivateOrLaunchService._client_running(profile):
            return self._exact(query, Entity('client:' + profile.name, profile.name, 'appclient', profile.name), 'running client')
        recent = self._recent.get(key)
        if recent and recent.kind == 'app' and Path(recent.target).is_file():
            return self._exact(query, recent, 'recent app')
        entities = list({e.id: e for e in (self.games.promote(app) for app in self.apps.entities())}.values())
        # Attach live process/window information to known installed targets.
        for hwnd, window in self._windows.items():
            process = normalize_name(Path(window.process_name).stem)
            if not process:
                continue
            if any(process in normalize_name(e.name) for e in entities):
                continue
            entities.append(Entity('running:' + process, Path(window.process_name).stem, 'window', hwnd,
                                   aliases=(window.title,), context=window.title))
        entities.extend(self.games.candidates())
        file_result = self.files.resolve(query, context)
        entities.extend(item.entity for item in file_result.candidates)
        result = self.names.resolve(profile.name if profile else query, entities, context)
        if result.best and result.best.kind == 'app':
            self._recent[key] = result.best
        # Temporal file requests use the cleaned filename query but keep the original alias key.
        if not result.best and file_result.candidates and any(t in query for t in ('昨天', '最新', '最近', '那個')):
            return Resolution(query, file_result.candidates, True)
        return result

    def context_names(self):
        # Read cached indexes only. Discovery runs before a command, off the UI thread.
        running = tuple(Path(w.process_name).stem for w in self.windows.list_windows())
        items = list(self.apps._index)
        games = list(self.games._entities)
        memory = self.names.store.data
        key = (running, tuple((e.name, e.path) for e in items), tuple(e.id for e in games),
               repr(memory), tuple(self._recent))
        if self._prompt_cache[0] == key:
            return self._prompt_cache[1]
        recent = sorted(memory['usage'], key=lambda k: memory['usage'][k].get('at', 0), reverse=True)
        entities = list(self._recent.values()) + games
        entities.sort(key=lambda e: recent.index(e.id) if e.id in recent else len(recent))
        names = list(running) + [e.name for e in entities]
        names += [a['query'] for a in memory['aliases'].values()]
        names += [e.name for e in items]

        result = tuple(dict.fromkeys(n[:60] for n in names if n and not any(c in n for c in '\r\n')))[:32]
        self._prompt_cache = (key, result)
        return result

    def execute(self, entity):
        entity = self.games.promote(entity)
        self.last_action = 'launched'
        self.last_message = ''
        if entity.kind == 'game':
            ok = self.games.launch(entity)
            self.last_message = f'已送出啟動 {entity.name} ({entity.provider})' if ok else f'無法啟動 {entity.name}'
        elif entity.kind == 'file':
            ok = self.files.open(entity)
        elif entity.kind == 'window':
            self.last_action = 'activated'
            window = next((w for w in self.windows.list_windows() if str(w.hwnd) == entity.target), None)
            result = ActivateOrLaunchService(self.apps, self.windows).activate_or_launch(entity.name)
            ok = result.ok
            self.last_action = result.action
            self.last_message = result.message
        elif entity.kind == 'appclient':
            result = ActivateOrLaunchService(self.apps, self.windows).activate_or_launch(entity.name)
            ok = result.ok
            self.last_action = result.action
            self.last_message = result.message
        elif entity.kind == 'app':
            result = ActivateOrLaunchService(AppResolver({entity.name: entity.target}), self.windows).activate_or_launch(entity.name)
            ok = result.ok
            self.last_action = result.action
            self.last_message = result.message
        else:
            return False
        if not ok:
            self.last_action = 'failed'
        if ok:
            self.names.store.used(entity)
        return ok

    def confirm(self, query, entity):
        if entity.kind == 'window':
            # HWNDs are ephemeral. Resolve the process afresh after restart.
            entity = Entity('running:' + normalize_name(entity.name), entity.name, 'running', entity.name)
        self.names.store.confirm(query, entity)

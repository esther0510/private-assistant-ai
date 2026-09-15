"""Game providers own discovery and launch protocols; never run game executables."""
from __future__ import annotations

import os
import re
import subprocess
import time
import threading
from dataclasses import replace
from pathlib import Path
from typing import Protocol

from .name_resolver import Entity, NameResolver


def parse_vdf(text: str) -> dict:
    tokens = re.findall(r'"((?:\\.|[^"\\])*)"|([{}])', re.sub(r'(?m)^\s*//.*$', '', text))
    values = [quoted.replace('\\\\', '\\').replace('\\"', '"') if not brace else brace for quoted, brace in tokens]
    stack = [{}]
    index = 0
    while index < len(values):
        key = values[index]
        index += 1
        if key == '}':
            if len(stack) > 1:
                stack.pop()
            continue
        if index >= len(values):
            break
        value = values[index]
        index += 1
        if value == '{':
            child = {}
            stack[-1][key] = child
            stack.append(child)
        else:
            stack[-1][key] = value
    return stack[0]


class LauncherProvider(Protocol):
    id: str
    def discover(self) -> list[Entity]: ...
    def launch(self, entity: Entity) -> bool: ...


class SteamProvider:
    id = 'steam'

    def __init__(self, root: Path | None = None, running=None, start=None, open_uri=None, ready=None, timeout=12.):
        self.root = root or self.find_root()
        self.running = running or self.is_running
        self.start = start or (lambda path: subprocess.Popen([str(path)], close_fds=True))
        self.open_uri = open_uri or os.startfile
        self.ready = ready or self.client_ready
        self.timeout = timeout
        self._cache = {}
        self._library_signature = None
        self._libraries = [self.root]
        self._discovery_lock = threading.Lock()

    @staticmethod
    def find_root():
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Software\Valve\Steam') as key:
                return Path(winreg.QueryValueEx(key, 'SteamPath')[0])
        except OSError:
            return Path(os.environ.get('ProgramFiles(x86)', 'C:/Program Files (x86)')) / 'Steam'

    @staticmethod
    def is_running():
        import psutil
        return any((p.info.get('name') or '').casefold() == 'steam.exe'
                   for p in psutil.process_iter(['name']))

    def client_ready(self):
        # ActiveProcess is populated by Steam after client initialization.
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Software\Valve\Steam\ActiveProcess') as key:
                pid = int(winreg.QueryValueEx(key, 'pid')[0])
            import psutil
            return pid > 0 and psutil.Process(pid).name().casefold() == 'steam.exe'
        except (OSError, ValueError):
            return False
        except Exception:
            return False

    def discover(self):
        with self._discovery_lock:
            return self._discover()

    def _discover(self):
        folder_file = self.root / 'steamapps/libraryfolders.vdf'
        try:
            stat = folder_file.stat()
            signature = (stat.st_mtime_ns, stat.st_size)
            if signature != self._library_signature:
                data = parse_vdf(folder_file.read_text(encoding='utf-8-sig'))
                libraries = [self.root]
                for key, entry in data.get('libraryfolders', data.get('LibraryFolders', {})).items():
                    if key.isdigit():
                        value = entry.get('path') if isinstance(entry, dict) else entry
                        if value:
                            libraries.append(Path(value))
                self._libraries, self._library_signature = libraries, signature
        except FileNotFoundError:
            self._libraries, self._library_signature = [self.root], None
        except (OSError, ValueError, AttributeError):
            pass
        libraries = self._libraries
        games = {}
        seen = set()
        for library in dict.fromkeys(libraries):
            for manifest in (library / 'steamapps').glob('appmanifest_*.acf'):
                try:
                    seen.add(manifest)
                    stat = manifest.stat()
                    signature = (stat.st_mtime_ns, stat.st_size)
                    cached = self._cache.get(manifest)
                    if cached and cached[0] == signature:
                        entity = cached[1]
                        if Path(entity.metadata['install_dir']).is_dir():
                            games[entity.metadata['appid']] = entity
                        continue
                    state = parse_vdf(manifest.read_text(encoding='utf-8-sig')).get('AppState', {})
                    appid, name = str(state.get('appid', '')), str(state.get('name', ''))
                    flags = int(state.get('StateFlags', '0'))
                    if not appid.isdigit() or not name or not flags & 4:
                        continue
                    install_dir = str(state.get('installdir', ''))
                    common = (library / 'steamapps/common').resolve()
                    installed = (common / install_dir).resolve()
                    if not install_dir or not installed.is_relative_to(common) or not installed.is_dir():
                        continue
                    uri = f'steam://rungameid/{appid}'
                    games[appid] = Entity(f'game:steam:{appid}', name, 'game', uri, 'steam',
                                           metadata={'appid': appid, 'install_state': flags, 'name': name, 'install_dir': str(installed), 'library_path': str(library), 'installed': True, 'launcher': 'Steam', 'manifest_path': str(manifest), 'manifest_mtime_ns': stat.st_mtime_ns})
                    self._cache[manifest] = (signature, games[appid])
                except (OSError, ValueError, AttributeError):
                    continue
        self._cache = {path: entry for path, entry in self._cache.items() if path in seen}
        return [replace(entity, aliases=self.local_display_names(appid)) for appid, entity in games.items()]

    @staticmethod
    def local_display_names(appid):
        # Exact Steam AppID registry keys only; never infer a translated title.
        import winreg
        names = []
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            for view in (winreg.KEY_WOW64_32KEY, winreg.KEY_WOW64_64KEY):
                try:
                    with winreg.OpenKey(hive, r'Software\Microsoft\Windows\CurrentVersion\Uninstall\Steam App ' + appid,
                                        0, winreg.KEY_READ | view) as key:
                        name = winreg.QueryValueEx(key, 'DisplayName')[0]
                        if isinstance(name, str) and name.strip():
                            names.append(name.strip())
                except OSError:
                    pass
        return tuple(dict.fromkeys(names))

    def validate(self, entity):
        # Validate only the selected manifest, never rescan inventory on launch.
        try:
            path = Path(entity.metadata['manifest_path'])
            state = parse_vdf(path.read_text(encoding='utf-8-sig')).get('AppState', {})
            appid = str(state.get('appid', ''))
            common = (path.parent / 'common').resolve()
            installed = (common / state.get('installdir', '')).resolve()
            return (entity.id == f'game:steam:{appid}' and bool(int(state.get('StateFlags', '0')) & 4)
                    and bool(state.get('installdir')) and installed.is_relative_to(common) and installed.is_dir())
        except (OSError, KeyError, ValueError, TypeError, AttributeError):
            return False

    def launch(self, entity):
        appid = str(entity.metadata.get('appid') or (entity.id.removeprefix('game:steam:') if entity.id.startswith('game:steam:') else entity.target.removeprefix('steam://rungameid/')))
        if entity.provider != self.id or not appid.isdigit():
            return False
        try:
            self.open_uri(f'steam://rungameid/{appid}')
            return True
        except OSError:
            return False


class GameResolver:
    def __init__(self, names: NameResolver, providers=None, refresh_seconds=60):
        self.names = names
        self.providers = list(providers) if providers is not None else [SteamProvider()]
        self._entities, self._at = [], 0.
        self._refresh_lock = threading.Lock()
        self._closed = threading.Event()
        self.refresh()
        self._worker = threading.Thread(target=self._background, args=(max(1, refresh_seconds),),
                                        name='game-index', daemon=True)
        self._worker.start()

    def _background(self, seconds):
        # Only this worker is lowered, never the application's process priority.
        try:
            import ctypes
            ctypes.windll.kernel32.SetThreadPriority(ctypes.windll.kernel32.GetCurrentThread(), -1)
        except (AttributeError, OSError):
            pass
        while not self._closed.wait(seconds):
            self.refresh()

    def close(self):
        self._closed.set()

    def refresh(self):
        with self._refresh_lock:
            merged = {}
            for provider in self.providers:
                try:
                    discovered = provider.discover()
                except Exception:
                    import logging
                    logging.getLogger(__name__).exception('Game index refresh failed: %s', provider.id)
                    discovered = [e for e in self._entities if e.provider == provider.id]
                for entity in discovered:
                    previous = merged.get(entity.id)
                    if previous:
                        entity = replace(previous, aliases=tuple(dict.fromkeys(
                            (*previous.aliases, entity.name, *entity.aliases))))
                    merged[entity.id] = entity
            self._entities = list(merged.values())
            self._at = time.monotonic()
        return self._entities

    def candidates(self):
        # Voice commands only read the published snapshot, never enumerate files.
        return list(self._entities)

    def promote(self, entity):
        if entity.kind != 'app':
            return entity
        target = Path(entity.target).resolve()
        for game in self.candidates():
            installed = game.metadata.get('install_dir')
            if installed and target.is_relative_to(Path(installed).resolve()):
                return game
        return entity

    def resolve(self, query):
        return self.names.resolve(query, self.candidates())

    def launch(self, entity):
        # Recheck installed inventory: stale alias cannot install a missing game.
        provider = next((p for p in self.providers if p.id == entity.provider), None)
        if provider is None:
            return False
        current = next((e for e in self._entities if e.id == entity.id), None)
        if current is None:
            return False
        validate = getattr(provider, 'validate', None)
        if callable(validate):
            if not validate(current):
                return False
        elif current.id not in {e.id for e in provider.discover()}:
            return False
        return provider.launch(current)

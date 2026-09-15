import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from personal_ai_assistant.name_resolver import AliasStore, Entity, NameResolver, normalize_name, phonetic_key
from personal_ai_assistant.file_resolver import FileResolver
from personal_ai_assistant.game_resolver import SteamProvider, GameResolver, parse_vdf
from personal_ai_assistant.entity_navigation import EntityNavigator
from personal_ai_assistant.window_activation import AppWindow, ActivateOrLaunchService
from personal_ai_assistant.app_resolver import AppResolver, ResolveCandidate
from personal_ai_assistant.command_router import route_assistant_command
from personal_ai_assistant.action_safety import classify_action_risk


class EntityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = AliasStore(self.root / 'memory.json')
        self.names = NameResolver(self.store)
        self.ps = Entity('app:ps', 'Adobe Photoshop', 'app', 'Photoshop.exe')
        self.pr = Entity('app:pr', 'Adobe Premiere Pro', 'app', 'Premiere.exe')
        self.game = Entity('game:steam:553850', 'Helldivers 2', 'game', 'steam://rungameid/553850', 'steam')

    def test_unicode_normalization(self):
        self.assertEqual(normalize_name(' ＰＳ！ '), 'ps')

    def test_split_english_and_abbreviation(self):
        self.assertEqual(self.names.resolve('PS', [self.ps]).best, self.ps)
        self.assertEqual(self.names.resolve('photo shop', [self.ps]).best, self.ps)
        self.assertEqual(self.names.resolve('PR', [self.pr]).best, self.pr)

    def test_phonetic_chinese_and_english(self):
        self.assertEqual(phonetic_key('佛頭夏普'), phonetic_key('Photoshop'))
        self.assertEqual(self.names.resolve('佛頭夏普', [self.ps]).candidates[0].entity, self.ps)
        self.assertEqual(self.names.resolve('fotoshop', [self.ps]).candidates[0].entity, self.ps)

    def test_ambiguous_candidates_require_confirmation(self):
        other = Entity('app:ps2', 'Adobe Photoshop Beta', 'app', 'beta.exe')
        result = self.names.resolve('PS', [self.ps, other])
        self.assertTrue(result.needs_confirmation)
        self.assertIsNone(result.best)

    def test_explicit_selection_learns_alias_and_survives_reload(self):
        self.store.confirm('剪影片的', self.pr)
        reloaded = NameResolver(AliasStore(self.store.path))
        self.assertEqual(reloaded.resolve('剪影片的', [self.ps, self.pr]).best, self.pr)

    def test_matching_and_usage_alone_do_not_learn(self):
        for _ in range(3):
            self.names.resolve('PS', [self.ps])
            self.store.used(self.ps)
        self.assertEqual(self.store.data['aliases'], {})

    def test_remove_and_update_alias(self):
        self.store.confirm('我的工具', self.ps)
        self.store.confirm('我的工具', self.pr)
        self.assertEqual(self.names.resolve('我的工具', [self.ps, self.pr]).best, self.pr)
        self.store.remove(normalize_name('我的工具'))
        self.assertFalse(self.store.data['aliases'])

    def test_recent_frequency_and_context_rank(self):
        first = Entity('a', 'Report v2', 'file', 'a.pdf', context='project A')
        recent = Entity('b', 'Report v3', 'file', 'b.pdf', modified=time.time(), context='project B')
        self.store.used(recent)
        result = self.names.resolve('Report', [first, recent], 'project B')
        self.assertEqual(result.candidates[0].entity, recent)
        self.assertTrue(result.needs_confirmation)

    def test_file_opt_in_and_executable_exclusion(self):
        (self.root / '報表.pdf').touch()
        (self.root / '報表.exe').touch()
        resolver = FileResolver(self.names, [self.root])
        self.assertEqual(resolver.candidates(), [])
        resolver.enabled = True
        resolver.refresh()
        self.assertEqual(len(resolver.candidates()), 1)

    def test_file_latest_ranking_asks_for_versions(self):
        old, new = self.root / '報表_v2.txt', self.root / '報表_v3.txt'
        old.touch(); new.touch()
        os.utime(old, (1, 1))
        resolver = FileResolver(self.names, [self.root], True)
        result = resolver.resolve('打開報表'.removeprefix('打開'))
        self.assertTrue(result.needs_confirmation)
        self.assertEqual(result.candidates[0].entity.target, str(new))
        self.assertTrue(resolver.resolve('昨天那個報表').candidates)

    def test_file_confirmed_alias_and_navigation(self):
        document = self.root / 'Project_v3.docx'; document.touch()
        resolver = FileResolver(self.names, [self.root], True)
        entity = resolver.candidates()[0]
        self.store.confirm('私人助理企劃書', entity)
        self.assertEqual(resolver.resolve('私人助理企劃書').best, entity)
        with patch('os.startfile') as opened:
            self.assertTrue(resolver.open(entity))
            opened.assert_called_once_with(str(document))

    def test_file_limit(self):
        for i in range(5):
            (self.root / f'{i}.txt').touch()
        self.assertEqual(len(FileResolver(self.names, [self.root], True, max_files=2).candidates()), 2)

    def test_yesterday_filters_modified_or_opened_date(self):
        yesterday = self.root / 'report old.txt'; yesterday.touch()
        today = self.root / 'report new.txt'; today.touch()
        os.utime(yesterday, (time.time() - 86400, time.time() - 86400))
        result = FileResolver(self.names, [self.root], True).resolve('昨天那個report')
        self.assertEqual(result.candidates[0].entity.target, str(yesterday))

    def steam(self, **kwargs):
        library = self.root / 'steamapps'; library.mkdir(exist_ok=True)
        (self.root / 'steam.exe').touch()
        (library / 'common/Helldivers 2').mkdir(parents=True, exist_ok=True)
        (library / 'appmanifest_553850.acf').write_text('"AppState" { "appid" "553850" "name" "Helldivers 2" "StateFlags" "4" "installdir" "Helldivers 2" }')
        return SteamProvider(self.root, **kwargs)

    def test_vdf_and_steam_manifest_uri(self):
        steam = self.steam()
        self.assertEqual(steam.discover()[0].target, 'steam://rungameid/553850')
        self.assertEqual(steam.discover()[0].metadata['appid'], '553850')
        self.assertEqual(parse_vdf('"a" { "b" "c" }'), {'a': {'b': 'c'}})

    def test_secondary_library_and_uninstalled_exclusion(self):
        steam = self.steam()
        second = self.root / 'secondary'; (second / 'steamapps').mkdir(parents=True)
        path = str(second).replace('\\', '\\\\')
        (self.root / 'steamapps/libraryfolders.vdf').write_text(f'"libraryfolders" {{ "1" {{ "path" "{path}" }} }}')
        (second / 'steamapps/common/Other').mkdir(parents=True)
        (second / 'steamapps/appmanifest_7.acf').write_text('"AppState" { "appid" "7" "name" "Other" "StateFlags" "4" "installdir" "Other" }')
        (second / 'steamapps/appmanifest_8.acf').write_text('"AppState" { "appid" "8" "name" "Uninstalled" "StateFlags" "1" }')
        self.assertEqual({e.metadata['appid'] for e in steam.discover()}, {'553850', '7'})

    def test_game_name_and_hd2(self):
        games = GameResolver(self.names, [self.steam()])
        self.assertEqual(games.resolve('Helldivers 2').best.target, self.game.target)
        self.assertEqual(games.resolve('HD2').best.target, self.game.target)
        self.assertEqual(games.resolve('絕地戰兵').candidates[0].entity.target, self.game.target)

    def test_running_steam_no_duplicate(self):
        start, uri = Mock(), Mock()
        steam = self.steam(running=lambda: True, start=start, open_uri=uri)
        self.assertTrue(steam.launch(self.game))
        start.assert_not_called()
        uri.assert_called_once_with(self.game.target)

    def test_stopped_steam_start_ready_then_launch(self):
        events = []
        steam = self.steam(running=lambda: False, start=lambda p: events.append('start'),
                           ready=lambda: events.append('ready') or True,
                           open_uri=lambda u: events.append(u))
        self.assertTrue(steam.launch(self.game))
        self.assertEqual(events, [self.game.target])

    def test_launcher_timeout_never_launches(self):
        uri = Mock()
        steam = self.steam(running=lambda: False, start=Mock(), ready=lambda: False, timeout=0, open_uri=uri)
        self.assertTrue(steam.launch(self.game)); uri.assert_called_once_with(self.game.target)

    def test_reject_arbitrary_protocol_and_missing_game(self):
        steam = self.steam(open_uri=Mock())
        self.assertFalse(steam.launch(Entity('evil', 'evil', 'game', 'steam://install/7', 'steam')))
        games = GameResolver(self.names, [steam])
        self.assertFalse(games.launch(Entity('missing', 'missing', 'game', 'steam://rungameid/7', 'steam')))

    def test_multiple_providers_ask_and_remember(self):
        epic = Entity('game:epic:hd2', 'Helldivers 2', 'game', 'example', 'epic')
        self.assertTrue(self.names.resolve('HD2', [self.game, epic]).needs_confirmation)
        self.store.confirm('HD2', epic)
        self.assertEqual(self.names.resolve('Helldivers 2', [self.game, epic]).best, epic)

    def test_app_alias_activates_existing_first(self):
        executable = self.root / 'Photoshop.exe'; executable.touch()
        windows = Mock()
        windows.list_windows.return_value = (AppWindow(1, 'Photoshop.exe', 'Photo'),)
        windows.activate.return_value = True
        windows.verify.return_value = True
        launch = Mock()
        result = ActivateOrLaunchService(AppResolver({'PS': str(executable)}), windows, launch).activate_or_launch('PS')
        self.assertTrue(result.ok); launch.assert_not_called()

    def test_entity_navigation_voice_misrecognition(self):
        windows = Mock(); windows.list_windows.return_value = ()
        navigator = EntityNavigator(store=self.store, windows=windows, games=GameResolver(self.names, []))
        navigator.apps.entities = lambda: [self.ps]
        intent = route_assistant_command('幫我開 佛頭夏普')
        result = navigator.resolve(intent.target)
        self.assertEqual(result.candidates[0].entity, self.ps)
        navigator.confirm(intent.target, self.ps)
        self.assertEqual(navigator.resolve(intent.target).best, self.ps)

    def test_router_file_name_not_misrouted_to_folder(self):
        self.assertEqual(route_assistant_command('打開私人助理企劃書').target, '私人助理企劃書')
        self.assertEqual(route_assistant_command('打開專案報表').intent, 'activate_or_launch_app')
        self.assertEqual(route_assistant_command('打開下載資料夾').intent, 'open_folder')
        self.assertEqual(route_assistant_command('打開report.docx').intent, 'activate_or_launch_app')
        self.assertEqual(route_assistant_command('打開https://example.com/report.docx').intent, 'open_url')

    def test_dangerous_actions_stay_confirmed(self):
        for action in ('delete_file', 'move_file', 'purchase', 'login', 'install_game', 'close_app', 'send_message'):
            self.assertTrue(classify_action_risk(action).requires_confirmation)
        for action in ('open_file', 'launch_game', 'activate_or_launch_app'):
            self.assertFalse(classify_action_risk(action).requires_confirmation)


if __name__ == '__main__':
    unittest.main()

import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from personal_ai_assistant.game_resolver import SteamProvider, GameResolver
from personal_ai_assistant.name_resolver import Entity, NameResolver, AliasStore
from personal_ai_assistant.voice_command import VoiceAssistantPipeline, WakeSettings, WakeRuntimeState
from personal_ai_assistant.kws import StreamingKwsWakeWordProvider

class LauncherRegression(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.install = self.root / 'steamapps/common/BongoCat'
        self.install.mkdir(parents=True)
        (self.install / 'bongocat.exe').touch()
        (self.root / 'steamapps/appmanifest_7.acf').write_text('"AppState" { "appid" "7" "name" "Bongo Cat" "StateFlags" "4" "installdir" "BongoCat" }')
        self.uri = Mock()
        self.provider = SteamProvider(self.root, running=lambda: False, start=Mock(), open_uri=self.uri)
        self.games = GameResolver(NameResolver(AliasStore(self.root / 'aliases.json')), [self.provider])
    def test_manifest_promotes_executable(self):
        app = Entity('app:cat', 'bongocat', 'app', str(self.install / 'bongocat.exe'))
        game = self.games.promote(app)
        self.assertEqual(game.provider, 'steam')
        self.assertEqual(game.metadata['install_dir'], str(self.install.resolve()))
        self.assertTrue(self.games.launch(game))
        self.uri.assert_called_once_with('steam://rungameid/7')
        self.provider.start.assert_not_called()
    def test_non_steam_preserves_executable(self):
        app = Entity('app:line', 'LINE', 'app', str(self.root / 'line.exe'))
        self.assertIs(self.games.promote(app), app)
    def test_steam_helper_is_not_client(self):
        with patch('psutil.process_iter', return_value=[Mock(info={'name': 'steamwebhelper.exe'})]):
            self.assertFalse(SteamProvider.is_running())

class SessionRegression(unittest.TestCase):
    def pipeline(self, runner=None):
        wake = Mock(spec=['configure', 'start', 'stop', 'poll'])
        wake.start.return_value = WakeRuntimeState('standby', 'ready')
        pipe = VoiceAssistantPipeline(WakeSettings('賈維斯'), wake_provider=wake, stt_provider=Mock(), intent_runner=runner)
        pipe.start()
        return pipe, wake
    def test_cancel_cleanup_once(self):
        pipe, wake = self.pipeline()
        pipe.prepare_capture()
        event = pipe._cancel_event
        pipe.cancel(); pipe.cancel(); pipe.cleanup()
        self.assertTrue(event.is_set())
        wake.stop.assert_called_once()
        self.assertEqual(wake.start.call_count, 2)
        self.assertEqual(pipe.state, 'standby')
    def test_success_immediate_rewake(self):
        pipe, wake = self.pipeline(Mock(return_value=True))
        pipe.prepare_capture()
        self.assertTrue(pipe.handle_wake('打開 LINE'))
        self.assertTrue(pipe.idle_tick())
    def test_unknown_immediate_rewake(self):
        pipe, wake = self.pipeline(Mock(return_value=False))
        pipe.prepare_capture()
        self.assertFalse(pipe.handle_wake('不明指令'))
        self.assertTrue(pipe.idle_tick())
    def test_failure_rearms(self):
        pipe, wake = self.pipeline(Mock(side_effect=ValueError('failed')))
        pipe.prepare_capture()
        with self.assertRaises(ValueError): pipe.handle_wake('打開 LINE')
        self.assertEqual(pipe.state, 'standby')
    def test_watchdog_recovers(self):
        pipe, wake = self.pipeline()
        pipe.prepare_capture()
        self.assertTrue(pipe.watchdog(pipe.last_activity + 16))
        self.assertFalse(pipe.watchdog(pipe.last_activity + 16))
        self.assertTrue(pipe.idle_tick())
    def test_stale_result_ignored(self):
        pipe, wake = self.pipeline()
        entered, release = threading.Event(), threading.Event()
        def slow(_): entered.set(); release.wait(2); return '打開 Steam'
        pipe._transcribe_after_wake = slow
        result = []
        worker = threading.Thread(target=lambda: result.append(pipe.transcribe_after_wake()))
        worker.start(); self.assertTrue(entered.wait(2))
        pipe.cancel(); release.set(); worker.join(2)
        self.assertEqual(result, [''])
        self.assertEqual(pipe.state, 'standby')
    def test_retry_invalidates_generation(self):
        pipe, wake = self.pipeline()
        pipe.prepare_capture(); old = pipe._cancel_event; generation = pipe.session_id
        pipe.cancel(); pipe._transcribe_retry = lambda _: '打開 LINE'
        self.assertEqual(pipe.transcribe_retry(), '打開 LINE')
        self.assertTrue(old.is_set())
        self.assertGreater(pipe.session_id, generation)
    def test_start_idempotent(self):
        pipe, wake = self.pipeline()
        pipe.start(); pipe.start()
        self.assertEqual(wake.start.call_count, 1)
    def test_rearm_keeps_stream_and_accepts_new_audio(self):
        provider = StreamingKwsWakeWordProvider()
        provider.engine = Mock(); provider.engine.accept.return_value = '賈維斯'
        provider._thread = Mock(); provider._thread.is_alive.return_value = True
        provider._mic = Mock()
        provider.consume(bytes(960), True, now=10)
        self.assertTrue(provider.poll())
        provider.consume(bytes(960), True, now=10.1)
        self.assertFalse(provider.poll())
        provider.rearm()
        provider.consume(bytes(960), True, now=10.2)
        self.assertTrue(provider.poll())
        provider._mic.close.assert_not_called()

if __name__ == '__main__': unittest.main()

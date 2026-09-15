import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from personal_ai_assistant.voice_text import normalize_voice, route_voice_command, VoiceDiagnostics
from personal_ai_assistant.command_router import route_assistant_command
from personal_ai_assistant.name_resolver import Entity, NameResolver, AliasStore
from personal_ai_assistant.voice_command import VoiceAssistantPipeline, WakeSettings, WakeRuntimeState

class PostSttTests(unittest.TestCase):
    def route(self, text):
        return route_voice_command(normalize_voice(text, ('賈維斯',))['normalized'])
    def test_wake_and_launch(self):
        r=self.route('賈維斯，幫我打開 Steam。')
        self.assertEqual((r.intent,r.target),('activate_or_launch_app','Steam'))
    def test_colloquial_forms(self):
        for phrase in ['我要開 Discord','我想開 Spotify','切到 Chrome','幫我開啟 Unity','可以幫我切換到 ChatGPT','幫我看 Steam','開 Steam']:
            with self.subTest(phrase=phrase):
                self.assertEqual(self.route(phrase).intent,'activate_or_launch_app')
    def test_variants(self):
        for name in ['賈維思','甲維斯','贾维斯','賈維絲']:
            self.assertTrue(normalize_voice(name+'幫我打開 Steam',('賈維斯',))['wake_removed'])
            self.assertEqual(self.route(name+'幫我打開 Steam').target,'Steam')
    def test_body_name_preserved(self):
        self.assertEqual(self.route('打開賈維斯企劃書').target,'賈維斯企劃書')
    def test_filler_repeat(self):
        self.assertEqual(self.route('欸，那個，賈維斯，麻煩幫我幫我打開打開 Steam').target,'Steam')
    def test_weak_intent_fuzzy(self):
        self.assertEqual(self.route('賈維斯幫我打凱 Steam').target,'Steam')
    def test_empty_target(self):
        r=self.route('賈維斯幫我打開')
        self.assertEqual((r.intent,r.target),('activate_or_launch_app',''))
    def test_empty_audio(self):
        self.assertEqual(self.route('').intent,'unknown')
    def test_fuzzy_target_candidates_and_alias(self):
        with tempfile.TemporaryDirectory() as d:
            store=AliasStore(Path(d)/'memory.json'); resolver=NameResolver(store)
            entities=[Entity('a','Discord','app','a'),Entity('b','Discord Beta','app','b')]
            r=self.route('幫我打開 discor')
            self.assertGreater(r.confidence,.8)
            result=resolver.resolve(r.target,entities)
            self.assertTrue(result.candidates)
            self.assertTrue(result.needs_confirmation)
            self.assertFalse(store.data['aliases'])
            store.confirm(r.target,entities[0])
            self.assertEqual(resolver.resolve(r.target,entities).best,entities[0])
    def test_game_entity(self):
        with tempfile.TemporaryDirectory() as d:
            game=Entity('steam:553850','Helldivers 2','game','steam://rungameid/553850','steam')
            r=self.route('賈維斯打開 Helldivers 2')
            self.assertEqual(NameResolver(AliasStore(Path(d)/'m.json')).resolve(r.target,[game]).best,game)
    def test_downloads(self):
        self.assertEqual(self.route('賈維斯幫我開下載資料夾').intent,'open_folder')
    def test_text_bar_not_normalized(self):
        self.assertEqual(route_assistant_command('我要開 Discord').intent,'unknown')
        self.assertEqual(route_assistant_command('幫我開 Steam').target,'Steam')
    def test_diagnostics_expire_and_clear(self):
        d=VoiceDiagnostics();d.begin({'raw':'test'});self.assertTrue(d.snapshot())
        d.expires=0;self.assertEqual(d.snapshot(),{})
        d.begin({'raw':'test'});d.clear();self.assertEqual(d.snapshot(),{})
    def test_one_shot_pipeline(self):
        wake=Mock();wake.start.return_value=WakeRuntimeState('standby','ready')
        runner=Mock(return_value=True)
        pipe=VoiceAssistantPipeline(WakeSettings('賈維斯'),wake_provider=wake,stt_provider=Mock(),intent_runner=runner)
        pipe.start()
        self.assertTrue(pipe.handle_wake(spoken_text='賈維斯幫我打開 Steam'))
        runner.assert_called_once_with('打開 Steam')
        self.assertEqual(pipe.diagnostics.snapshot()['raw'],'賈維斯幫我打開 Steam')

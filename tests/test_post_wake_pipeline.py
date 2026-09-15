import json
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from personal_ai_assistant.voice_text import normalize_voice, route_voice_command, VoiceDiagnostics
from personal_ai_assistant.voice_command import LocalWhisperTranscriber, VoiceAssistantPipeline, WakeSettings, WakeRuntimeState
from personal_ai_assistant.post_wake import SpeechEndpoint
from personal_ai_assistant.kws import StreamingKwsWakeWordProvider
from personal_ai_assistant.entity_navigation import EntityNavigator
from personal_ai_assistant.app_resolver import AppResolver
from personal_ai_assistant.name_resolver import AliasStore, Entity
from personal_ai_assistant.window_activation import ActivateOrLaunchService, AppWindow, WindowsWindowController, rank_matching_window

class PostWakePipelineTests(unittest.TestCase):
    def test_dirty_launch(self):
        for phrase in ('假如是, 幫我打開LINE', '奇怪雜訊 麻煩打開 LINE', '啊那個欸 幫我開啟LINE', '假如是 我要開 LINE', '雜訊幫我看LINE'):
            with self.subTest(phrase=phrase):
                r=route_voice_command(phrase)
                self.assertEqual((r.intent,r.target),('activate_or_launch_app','LINE'))
                self.assertGreater(r.confidence,.8)
    def test_dirty_empty_target(self):
        r=route_voice_command('假如是，幫我打開')
        self.assertEqual((r.intent,r.target),('activate_or_launch_app',''))
    def test_reminder_and_goal_preserve_launch_in_body(self):
        for phrase,intent in [('10分鐘後提醒我打開LINE','reminder'),('今天目標是打開LINE','set_goal')]:
            self.assertEqual(route_voice_command(phrase).intent,intent)
    def test_low_target_keeps_intent(self):
        r=route_voice_command('假如是幫我打開 discor')
        self.assertEqual(r.intent,'activate_or_launch_app')
        self.assertEqual(r.target,'discor')
    def test_negated_prefix_not_executed(self):
        self.assertEqual(route_voice_command('不要打開LINE').intent,'unknown')
    def test_warm_model_reused(self):
        model=Mock();model.transcribe.side_effect=lambda *a,**k: ([types.SimpleNamespace(text='幫我打開LINE')],None)
        factory=Mock(return_value=model)
        with patch.dict('sys.modules',{'faster_whisper':types.SimpleNamespace(WhisperModel=factory)}):
            transcriber=LocalWhisperTranscriber()
            for _ in range(3):
                with patch('personal_ai_assistant.asr_guard.speech_presence', return_value={'speech_detected': True}):
                    self.assertEqual(transcriber.transcribe_pcm(bytes(960)),'幫我打開LINE')
        factory.assert_called_once()
        self.assertEqual(model.transcribe.call_count,3)
    def test_eos_short_silence(self):
        vad=Mock();vad.is_speech.side_effect=[True]*10+[False]*25
        endpoint=SpeechEndpoint(vad)
        for _ in range(34): self.assertFalse(endpoint.feed(bytes(960)))
        self.assertTrue(endpoint.feed(bytes(960)))
        self.assertEqual(endpoint.quiet_ms,750)
    def test_silence_alone_never_endpoint(self):
        endpoint=SpeechEndpoint(Mock(is_speech=Mock(return_value=False)))
        for _ in range(267): self.assertFalse(endpoint.feed(bytes(960)))
    def test_capture_buffer_retained_and_not_wait_eight_seconds(self):
        provider=StreamingKwsWakeWordProvider()
        voice=b'\x01\x00'*480; quiet=bytes(960)
        original=voice*10+quiet*25
        provider._command=bytearray(original)
        provider.last_wake_at=time.monotonic()-1.05
        with patch('personal_ai_assistant.vad.create_vad',return_value=Mock(is_speech=lambda frame,rate:frame==voice)), patch.object(provider,'stop'):
            started=time.monotonic();pcm=provider.capture_command(8)
        self.assertLess(time.monotonic()-started,.3)
        self.assertEqual(pcm,original)
    def test_pre_wake_pause_does_not_truncate_command(self):
        provider=StreamingKwsWakeWordProvider()
        voice=b'\x01\x00'*480;quiet=bytes(960)
        original=voice*10+quiet*20+voice*12+quiet*25
        provider._command=bytearray(original)
        provider.last_wake_at=time.monotonic()-.75
        with patch('personal_ai_assistant.vad.create_vad',return_value=Mock(is_speech=lambda frame,rate:frame==voice)), patch.object(provider,'stop'):
            self.assertEqual(provider.capture_command(8),original)
    def test_trace_survives_worker_to_ui(self):
        wake=Mock();wake.capture_command.return_value=bytes(960);wake.last_wake_at=time.monotonic()
        stt=Mock();stt.transcriber.transcribe_pcm.return_value='假如是, 幫我打開LINE'
        pipe=VoiceAssistantPipeline(WakeSettings('賈維斯'),wake_provider=wake,stt_provider=stt,intent_runner=lambda t:True)
        pipe.active=True
        text=pipe.transcribe_after_wake();pipe.handle_wake(text)
        data=pipe.diagnostics.snapshot()
        self.assertEqual(data['normalized'],'打開 LINE')
        for key in ('wake_event_at','recording_start','end_of_speech_detected','stt_start','stt_done','normalize_done','action_done'):
            self.assertIn(key,data['timing'])
        self.assertIn('after_eos',data['latency_ms'])
        self.assertNotIn('pcm',json.dumps(data))
    def test_stage_latency_arithmetic(self):
        d=VoiceDiagnostics();d.begin({})
        for stage,at in [('wake_event_at',10),('recording_start',10),('speech_ended_at',11),('end_of_speech_detected',11),('stt_start',11),('stt_done',11.2),('normalize_done',11.21),('intent_done',11.22),('resolver_done',11.23),('action_start',11.23),('action_done',11.25)]:d.mark(stage,at)
        self.assertEqual(d.snapshot()['latency_ms']['after_speech'],250)
        self.assertEqual(d.snapshot()['latency_ms']['total'],1250)
    def test_each_profile_activates_before_resolver(self):
        for name,alias in [('LINE','賴'),('Steam','Steam'),('Telegram','電報')]:
            with self.subTest(name=name):
                windows=Mock();windows.list_windows.return_value=(AppWindow(1,name+'.exe',name,True,0,True),)
                windows.activate.return_value=True;windows.verify.return_value=True
                resolver=Mock();launcher=Mock()
                result=ActivateOrLaunchService(resolver,windows,launcher).activate_or_launch(alias)
                self.assertEqual(result.action,'activated');resolver.resolve_app.assert_not_called();launcher.assert_not_called()
    def test_steam_helper_excluded(self):
        self.assertIsNone(rank_matching_window((AppWindow(1,'steamwebhelper.exe','Steam'),),'Steam'))
    def test_telegram_wrapper_excluded(self):
        self.assertIsNone(rank_matching_window((AppWindow(1,'Update.exe','Telegram'),),'Telegram'))
    def test_running_client_without_hwnd_no_duplicate(self):
        for name in ('LINE','Telegram'):
            windows=Mock();windows.list_windows.return_value=()
            launcher=Mock();resolver=Mock()
            from personal_ai_assistant.app_resolver import ResolveResult
            resolver.resolve_app.return_value=ResolveResult(name, (), False, 'not found')
            with patch.object(ActivateOrLaunchService,'_client_running',return_value=True):
                result=ActivateOrLaunchService(resolver,windows,launcher).activate_or_launch(name)
            self.assertFalse(result.ok);launcher.assert_not_called();resolver.resolve_app.assert_called_once()
    def test_steam_tray_protocol_fallback(self):
        windows=Mock();windows.list_windows.return_value=()
        with patch.object(ActivateOrLaunchService,'_client_running',return_value=True),patch('os.startfile') as opened:
            from personal_ai_assistant.app_resolver import ResolveResult
            resolver=Mock();resolver.resolve_app.return_value=ResolveResult('Steam', (), False, 'not found')
            self.assertFalse(ActivateOrLaunchService(resolver,windows).activate_or_launch('Steam').ok)
        opened.assert_called_once_with('steam://open/main')
    def test_navigator_running_fast_path_skips_slow_index(self):
        with tempfile.TemporaryDirectory() as d,patch.object(AppResolver,'refresh_background'):
            windows=Mock();windows.list_windows.return_value=(AppWindow(1,'LINE.exe','LINE'),)
            navigator=EntityNavigator(store=AliasStore(Path(d)/'m.json'),windows=windows)
            navigator.apps.entities=Mock(side_effect=AssertionError('slow index used'))
            self.assertEqual(navigator.resolve('LINE').best.target,'1')
    def test_navigator_learned_fast_path(self):
        with tempfile.TemporaryDirectory() as d,patch.object(AppResolver,'refresh_background'):
            path=Path(d)/'custom.exe';path.touch()
            store=AliasStore(Path(d)/'m.json');store.confirm('我的工具',Entity('a','Custom','app',str(path)))
            navigator=EntityNavigator(store=store,windows=Mock())
            navigator.apps.entities=Mock(side_effect=AssertionError('slow index used'))
            self.assertEqual(navigator.resolve('我的工具').best.target,str(path))
            navigator.windows.list_windows.assert_not_called()

if __name__=='__main__':unittest.main()

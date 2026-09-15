import threading
import types
import unittest
from concurrent.futures import Future
from unittest.mock import Mock, patch
from personal_ai_assistant.post_wake import SpeechEndpoint, capture_post_wake
from personal_ai_assistant.voice_text import command_transcript_state
from personal_ai_assistant.voice_command import VoiceAssistantPipeline, WakeSettings
from personal_ai_assistant.window_activation import WindowsWindowController

VOICE = b'\x01\x00' * 480
QUIET = bytes(960)

class ImmediatePool:
    def __init__(self, **kw): pass
    def submit(self, fn, *args):
        future = Future()
        try: future.set_result(fn(*args))
        except Exception as exc: future.set_exception(exc)
        return future
    def shutdown(self, **kw): pass

class CommandOnsetTests(unittest.TestCase):
    def capture(self, frames, transcripts, initial=0, timeout=8, during_stt=None):
        clock = [100.0]
        pending = list(frames)
        p = types.SimpleNamespace(_lock=threading.Lock(), _command=bytearray(b''.join(pending[:initial])), last_wake_at=100.0)
        del pending[:initial]
        stopped = [False]
        def wait(seconds):
            clock[0] += .03
            if pending: p._command.extend(pending.pop(0))
        p._stop = types.SimpleNamespace(is_set=lambda: stopped[0], wait=wait)
        p.stop = lambda: stopped.__setitem__(0, True)
        results = iter(transcripts)
        def decode(pcm):
            if during_stt:
                during_stt(p, pcm)
            return next(results)
        decoder = Mock(side_effect=decode)
        marks = []
        with patch('time.monotonic', side_effect=lambda: clock[0]), patch('concurrent.futures.ThreadPoolExecutor', ImmediatePool), patch('personal_ai_assistant.vad.create_vad', return_value=Mock(is_speech=lambda f,r: f==VOICE)):
            result = capture_post_wake(p, decoder, ('賈維斯',), timeout, mark=lambda *a: marks.append(a))
        return result, decoder, clock[0]-100, marks

    def test_wake_700ms_then_command_not_cut(self):
        text, decoder, elapsed, _ = self.capture([VOICE]*10+[QUIET]*24+[VOICE]*15+[QUIET]*25, ['賈維斯幫我打開 LINE'])
        self.assertEqual(text, '賈維斯幫我打開 LINE')
        self.assertGreaterEqual(elapsed, 2.1)
        self.assertEqual(decoder.call_count, 1)

    def test_residue_keeps_microphone_until_command(self):
        text, decoder, elapsed, _ = self.capture([VOICE]*10+[QUIET]*35+[VOICE]*15+[QUIET]*25, ['假尾思', '幫我打開LINE'])
        self.assertEqual(text, '幫我打開LINE')
        self.assertEqual(decoder.call_count, 2)
        self.assertLess(elapsed, 4)

    def test_one_shot_keeps_pre_wake_audio(self):
        text, decoder, elapsed, _ = self.capture([VOICE]*20+[QUIET]*25, ['賈維斯幫我打開 LINE'], initial=10)
        self.assertEqual(text, '賈維斯幫我打開 LINE')
        self.assertTrue(decoder.call_args.args[0].startswith(VOICE*20))
        self.assertLess(elapsed, 1.2)

    def test_partial_waits_for_target(self):
        text, decoder, elapsed, _ = self.capture([VOICE]*15+[QUIET]*27+[VOICE]*8+[QUIET]*25, ['幫我打開', '幫我打開 LINE'])
        self.assertEqual(text, '幫我打開 LINE')
        self.assertEqual(decoder.call_count, 2)

    def test_short_complete_command_fast(self):
        text, decoder, elapsed, marks = self.capture([VOICE]*10+[QUIET]*25, ['開 LINE'])
        self.assertEqual(text, '開 LINE')
        self.assertLess(elapsed, 1.2)
        self.assertIn('speech_onset', [x[0] for x in marks])

    def test_speech_during_stt_invalidates_old_complete_result(self):
        calls = []
        def arriving(provider, pcm):
            if not calls:
                provider._command.extend(VOICE*10 + QUIET*25)
            calls.append(pcm)
        text, decoder, _, _ = self.capture([VOICE]*10+[QUIET]*25,
            ['開 LINE', '開 Telegram'], during_stt=arriving)
        self.assertEqual(text, '開 Telegram')
        self.assertEqual(decoder.call_count, 2)

    def test_warmup_once_and_reused_for_real_commands(self):
        import numpy  # Native modules must load before patch.dict restores sys.modules.
        from personal_ai_assistant.voice_command import LocalWhisperTranscriber
        model=Mock(); model.transcribe.side_effect=lambda *a,**k: ([types.SimpleNamespace(text='開 LINE')], None)
        factory=Mock(return_value=model)
        with patch.dict('sys.modules', {'faster_whisper':types.SimpleNamespace(WhisperModel=factory)}):
            service=LocalWhisperTranscriber()
            service.warm_background()
            service._warm_thread.join(3)
            service.warm_background()
            with patch('personal_ai_assistant.asr_guard.speech_presence', return_value={'speech_detected': True}):
                for _ in range(2): service.transcribe_pcm(VOICE)
        factory.assert_called_once()
        self.assertEqual(model.transcribe.call_count, 2)

    def test_simple_voice_commands_do_not_call_advisor(self):
        from personal_ai_assistant.main_window import MainWindow
        fake=types.SimpleNamespace(_executing_voice=True, current_snapshot=None,
            settings=types.SimpleNamespace(ai_provider='chatgpt', ai_handoff_auto_send=False),
            store=Mock(), voice_listening_overlay=Mock(), voice_pipeline=Mock(), goal_engine=Mock(),
            refresh_advisor=Mock(side_effect=AssertionError('Advisor on local voice path')),
            _command_notice=Mock(), update_top_status_bar=Mock(), _shorten=lambda text,n:text[:n],
            _execute_launch_app=Mock(return_value=True), _add_parsed_reminder=Mock(return_value=True))
        for name in ['_execute_set_mode','set_intent_work','set_intent_private_time','_set_explicit_intent']:
            setattr(fake,name,types.MethodType(getattr(MainWindow,name),fake))
        for text in ['假如是，幫我打開LINE','今天目標是完成報告','回到工作','私人時間','10分鐘後提醒我喝水']:
            self.assertTrue(MainWindow.execute_assistant_command(fake,text), text)
        fake.refresh_advisor.assert_not_called()

    def test_running_tray_client_navigator_skips_index(self):
        import tempfile
        from pathlib import Path
        from personal_ai_assistant.entity_navigation import EntityNavigator
        from personal_ai_assistant.name_resolver import AliasStore
        with tempfile.TemporaryDirectory() as d, patch('personal_ai_assistant.app_resolver.AppResolver.refresh_background'), patch('personal_ai_assistant.window_activation.ActivateOrLaunchService._client_running',return_value=True):
            windows=Mock(); windows.list_windows.return_value=()
            nav=EntityNavigator(store=AliasStore(Path(d)/'aliases.json'),windows=windows)
            nav.apps.entities=Mock(side_effect=AssertionError('slow index'))
            entity=nav.resolve('Steam').best
            self.assertEqual(entity.kind,'appclient')
            with patch('os.startfile') as opened:
                from personal_ai_assistant.app_resolver import ResolveResult
                nav.apps.resolve_app=Mock(return_value=ResolveResult('Steam',(),False,'not found'))
                self.assertFalse(nav.execute(entity))
            opened.assert_called_once_with('steam://open/main')

    def test_only_residue_times_out_without_routing(self):
        text, decoder, elapsed, _ = self.capture([VOICE]*10+[QUIET]*25, ['假尾思'])
        self.assertEqual(text, '')
        self.assertLess(elapsed, 8.1)
        self.assertEqual(decoder.call_count, 1)

    def test_semantic_states(self):
        for text in ['賈維斯', '假尾思', '假如是', '賈維思', '欸假尾思 賈維斯']:
            self.assertEqual(command_transcript_state(text)[0], 'awaiting_command', text)
        for text in ['幫我打開', '幫我開', '切到']:
            self.assertEqual(command_transcript_state(text)[0], 'awaiting_target', text)
        self.assertEqual(command_transcript_state('開 LINE')[0], 'complete')
        self.assertIn('假如是', command_transcript_state('幫我打開假如是筆記')[1])

    def test_empty_result_never_calls_intent_or_processing_ui(self):
        runner=Mock(); status=Mock()
        pipe=VoiceAssistantPipeline(WakeSettings('賈維斯'), wake_provider=Mock(), stt_provider=Mock(), intent_runner=runner, status_callback=status)
        self.assertFalse(pipe.handle_wake('假尾思'))
        runner.assert_not_called()
        self.assertFalse(any('正在處理' in str(c) for c in status.call_args_list))

    def test_hidden_localized_chat_title_enumerated(self):
        for process in ['LINE.exe','Telegram.exe','steam.exe']:
            gui=Mock()
            gui.EnumWindows.side_effect=lambda callback,arg: callback(123,arg)
            gui.IsWindowVisible.return_value=False
            gui.GetWindowText.return_value='朋友的對話'
            gui.GetWindow.return_value=0
            gui.GetWindowLong.return_value=0
            gui.GetClassName.return_value='QtQWindowIcon'
            gui.GetWindowRect.return_value=(0,0,600,400)
            ps=Mock(); ps.Process.return_value.name.return_value=process
            wp=Mock(); wp.GetWindowThreadProcessId.return_value=(1,2)
            with patch.dict('sys.modules', {'win32gui':gui,'win32process':wp,'psutil':ps}):
                windows=WindowsWindowController().list_windows()
            self.assertEqual(len(windows),1)
            self.assertTrue(windows[0].hidden)

if __name__ == '__main__': unittest.main()

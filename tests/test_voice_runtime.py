import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from personal_ai_assistant.voice_command import VoiceAssistantPipeline, WakeSettings, WakeRuntimeState, VoiceInstallState, install_voice_dependencies
from personal_ai_assistant.vad import create_vad
from personal_ai_assistant.kws import StreamingKwsWakeWordProvider

class VoiceRuntimeTests(unittest.TestCase):
    def install(self, runner, local='ready'):
        with tempfile.TemporaryDirectory() as d, patch('personal_ai_assistant.voice_command.detect_voice_components', return_value=VoiceInstallState(local)):
            return install_voice_dependencies(Path(d), runner=runner)

    def test_exit_zero_import_failure_never_ready(self):
        runner=Mock(side_effect=[subprocess.CompletedProcess([],0), subprocess.CompletedProcess([],1)])
        self.assertEqual(self.install(runner).state, 'error_dependency')

    def test_installer_explicit_current_python(self):
        runner=Mock(return_value=subprocess.CompletedProcess([],0))
        self.assertEqual(self.install(runner).state,'verified')
        args=runner.call_args_list[0].args[0]
        self.assertEqual(args[args.index('--python')+1],sys.executable)
        self.assertEqual(runner.call_args_list[1].args[0][0],sys.executable)

    def test_pythonw_uses_sibling(self):
        runner=Mock(return_value=subprocess.CompletedProcess([],0))
        with patch('personal_ai_assistant.voice_command.sys.executable',str(Path(sys.executable).with_name('pythonw.exe'))):
            self.install(runner)
        self.assertEqual(runner.call_args_list[0].args[0][4],sys.executable)

    def test_restart_explicit_when_local_import_fails(self):
        self.assertEqual(self.install(Mock(return_value=subprocess.CompletedProcess([],0)), 'not_installed').state,'restart_required')

    def test_repeated_install_same_environment_no_venv_creation(self):
        runner=Mock(return_value=subprocess.CompletedProcess([],0))
        self.install(runner); self.install(runner)
        self.assertEqual(runner.call_args_list[0].args[0],runner.call_args_list[2].args[0])
        self.assertNotIn('venv',runner.call_args_list[0].args[0])

    def test_wake_blocked_for_every_nonready_state(self):
        provider=Mock()
        pipeline=VoiceAssistantPipeline(WakeSettings('賈維斯'),wake_provider=provider)
        for state in ('disabled','missing_components','installing','verifying','loading_models','opening_microphone','error_dependency','error_model','error_microphone','error_runtime','restart_required'):
            pipeline.runtime_state=provider.runtime_state=WakeRuntimeState(state,state)
            self.assertFalse(pipeline.test_wake_word().matched)
        provider.test_wake_word.assert_not_called()

    def test_provider_itself_blocks_countdown(self):
        provider=StreamingKwsWakeWordProvider()
        with patch('personal_ai_assistant.kws.time.monotonic') as clock:
            provider.test_wake_word()
        clock.assert_not_called()

    def test_real_native_vad_fallback(self):
        from personal_ai_assistant.vad import NativeWebRtcVad
        failed=Mock(side_effect=ImportError('wrapper unavailable'))
        self.assertFalse(create_vad(providers=(failed,NativeWebRtcVad)).is_speech(bytes(960),16000))

    def test_missing_all_vad_providers_fails(self):
        with self.assertRaises(RuntimeError):
            create_vad(providers=(Mock(side_effect=ImportError()),))

    def test_mic_open_failure_classified(self):
        provider=StreamingKwsWakeWordProvider(engine_factory=Mock(return_value=Mock()))
        with patch('personal_ai_assistant.voice_command.detect_voice_components',return_value=VoiceInstallState('ready')), patch('personal_ai_assistant.kws.ensure_model'), patch('personal_ai_assistant.vad.create_vad',return_value=Mock()), patch('personal_ai_assistant.voice_command._sounddevice_module',return_value=Mock(RawInputStream=Mock(side_effect=OSError('device unavailable')))):
            provider._run(provider._stop)
        self.assertEqual(provider.runtime_state.state,'error_microphone')

    def test_install_state_overrides_stale_provider_ready(self):
        provider=Mock(runtime_state=WakeRuntimeState('standby','old ready'))
        pipeline=VoiceAssistantPipeline(WakeSettings('賈維斯'),wake_provider=provider)
        pipeline.runtime_state=WakeRuntimeState('installing','正在安裝語音元件…')
        self.assertEqual(pipeline.readiness().state,'installing')

    def test_readiness_requires_first_audio_frame(self):
        provider=StreamingKwsWakeWordProvider(engine_factory=Mock(return_value=Mock()))
        audio=Mock()
        states=[]
        def start():
            states.append(provider.runtime_state.state)
            provider._stop.set()
        audio.RawInputStream.return_value.start.side_effect=start
        with patch('personal_ai_assistant.voice_command.detect_voice_components',return_value=VoiceInstallState('ready')), patch('personal_ai_assistant.kws.ensure_model'), patch('personal_ai_assistant.vad.create_vad',return_value=Mock()), patch('personal_ai_assistant.voice_command._sounddevice_module',return_value=audio):
            provider._run(provider._stop)
        self.assertEqual(states,['opening_microphone'])
        self.assertNotEqual(provider.runtime_state.state,'standby')

    def test_loaded_native_update_requires_restart(self):
        runner=Mock(return_value=subprocess.CompletedProcess([],0))
        calls=[0]
        def version(package):
            calls[0]+=1
            return 'old' if calls[0]<=5 else 'new'
        with patch('importlib.metadata.version',side_effect=version), patch.dict(sys.modules,{'_webrtcvad':Mock()}):
            self.assertEqual(self.install(runner).state,'restart_required')


    def test_verifying_live_pipeline_advances_to_ready_without_timer(self):
        provider=Mock(runtime_state=WakeRuntimeState('standby','待機中'))
        pipeline=VoiceAssistantPipeline(WakeSettings('賈維斯'),wake_provider=provider)
        pipeline.runtime_state=WakeRuntimeState('verifying','驗證中')
        pipeline.active=True
        self.assertEqual(pipeline.readiness().state,'ready')


class VoiceUiRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import os
        os.environ['QT_QPA_PLATFORM']='offscreen'
        from PySide6.QtWidgets import QApplication
        cls.app=QApplication.instance() or QApplication([])

    def setUp(self):
        from personal_ai_assistant.main_window import MainWindow
        self.temp=tempfile.TemporaryDirectory()
        with patch.object(MainWindow,'maybe_check_updates'), patch.object(MainWindow,'start_monitoring'), patch.object(MainWindow,'configure_quick_add_hotkey'):
            self.window=MainWindow(Path(self.temp.name)/'clean.sqlite3')

    def tearDown(self):
        self.window.exiting=True
        self.window.close()
        self.app.processEvents()
        self.temp.cleanup()

    def test_settings_toast_dialog_share_authoritative_failure(self):
        from personal_ai_assistant.main_window import AssistantCommandDialog
        w=self.window
        error=VoiceInstallState('error_dependency','VAD 元件載入失敗')
        with patch.object(w,'_command_notice') as notice:
            w._finish_voice_components_install(error)
        self.assertEqual(w.voice_command_label.text(),error.message)
        notice.assert_called_once_with(error.message)
        dialog=AssistantCommandDialog(w)
        with patch('personal_ai_assistant.main_window.QMessageBox.information') as info:
            dialog.show_voice_status()
        self.assertEqual(info.call_args.args[-1],error.message)
        dialog.close()
        self.assertFalse(w.voice_wake_test_button.isEnabled())
        with patch.object(w,'_start_voice_job') as job:
            w.test_voice_wake_word_now()
        job.assert_not_called()

    def test_verified_install_recreates_backend_before_ready(self):
        w=self.window
        old=w.voice_pipeline.wake_provider
        w.settings=w.settings_service.update(voice_command_enabled=True,assistant_name='賈維斯')
        w.voice_microphones_available=True
        provider=Mock(runtime_state=WakeRuntimeState('standby','語音助理：待機中'))
        provider.start.return_value=provider.runtime_state
        with patch('personal_ai_assistant.kws.StreamingKwsWakeWordProvider',return_value=provider):
            w._finish_voice_components_install(VoiceInstallState('verified'))
        self.assertIsNot(old,w.voice_pipeline.wake_provider)
        provider.configure_settings.assert_called()
        provider.start.assert_called_once()
        self.assertEqual(w.voice_pipeline.readiness().state,'ready')
        self.assertTrue(w.voice_wake_test_button.isEnabled())
        w.voice_pipeline.wake_provider=old

    def test_command_bar_stays_usable_with_voice_error(self):
        from personal_ai_assistant.main_window import AssistantCommandDialog
        self.window.voice_pipeline.runtime_state=WakeRuntimeState('error_dependency','VAD 元件載入失敗')
        self.window._update_voice_status_label()
        dialog=AssistantCommandDialog(self.window)
        dialog.input_box.setText('十分鐘後提醒我喝水')
        self.assertTrue(dialog.input_box.isEnabled())
        self.assertEqual(dialog.text(),'十分鐘後提醒我喝水')
        dialog.close()

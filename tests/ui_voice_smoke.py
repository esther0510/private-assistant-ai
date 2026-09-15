import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
profile = tempfile.TemporaryDirectory()
os.environ['LOCALAPPDATA'] = profile.name
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
from PySide6.QtWidgets import QApplication, QMessageBox
from PySide6.QtCore import QTimer
from personal_ai_assistant.main_window import MainWindow
from personal_ai_assistant.voice_command import WakeRuntimeState, WakeSettings
app = QApplication([])
app.setQuitOnLastWindowClosed(False)

class BackgroundUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with patch.object(MainWindow, 'maybe_check_updates'), patch.object(MainWindow, 'start_monitoring'), patch.object(MainWindow, 'configure_quick_add_hotkey'):
            cls.window = MainWindow(Path(profile.name) / 'test.sqlite3')
        cls.window.voice_pipeline.stop()
        cls.provider = Mock(spec=['configure', 'start', 'stop', 'poll', 'runtime_state'])
        cls.provider.runtime_state = WakeRuntimeState('standby', 'ready')
        cls.provider.start.return_value = cls.provider.runtime_state
        cls.provider.poll.return_value = False
        cls.window.voice_pipeline.wake_provider = cls.provider
        cls.window.voice_pipeline.settings = WakeSettings("賈維斯")
        cls.window.voice_pipeline.stt_provider = Mock()
        cls.window.voice_pipeline.active = False
        cls.window.voice_pipeline.start()
        cls.window.voice_wake_timer.start()
    def test_microphone_gain_setting_round_trip(self):
        with patch.object(MainWindow, 'maybe_check_updates'), patch.object(MainWindow, 'start_monitoring'), patch.object(MainWindow, 'configure_quick_add_hotkey'):
            window = MainWindow(Path(profile.name) / 'gain-test.sqlite3')
        try:
            combo = window.voice_microphone_gain_combo
            self.assertEqual(combo.currentData(), 'auto')
            combo.setCurrentIndex(combo.findData('off'))
            self.assertEqual(window.settings.voice_microphone_gain, 'off')
            self.assertEqual(window.voice_pipeline.settings.microphone_gain, 'off')
            combo.setCurrentIndex(combo.findData('auto'))
            self.assertEqual(window.voice_pipeline.settings.microphone_gain, 'auto')
        finally:
            window.voice_pipeline.stop()
            window.hide()
            window.deleteLater()

    def setUp(self):
        self.window.show(); app.processEvents()
        self.provider.stop.reset_mock(); self.provider.start.reset_mock()
    def assertAlive(self):
        app.processEvents()
        self.assertTrue(self.window.voice_pipeline.active)
        self.assertTrue(self.window.voice_wake_timer.isActive())
        self.provider.stop.assert_not_called()
    def test_hide(self):
        self.window.hide(); self.assertAlive()
    def test_minimize(self):
        self.window.showMinimized(); self.assertAlive()
    def test_close_to_tray(self):
        self.window.close(); self.assertAlive()
    def test_reopen_no_duplicate(self):
        self.window.hide(); self.window.show_main_window(); self.assertAlive()
        self.provider.start.assert_not_called()
    def test_dialog_x_cancel(self):
        self.window.voice_pipeline.prepare_capture()
        self.window._voice_retry('重說測試')
        self.window._voice_retry_dialog.close(); app.processEvents()
        self.assertIsNone(self.window._voice_retry_dialog)
        self.assertEqual(self.window.voice_pipeline.state, 'standby')
        self.provider.stop.assert_called_once()
    def test_retry_button(self):
        self.window._voice_retry('重說測試')
        dialog = self.window._voice_retry_dialog
        with patch.object(self.window, '_start_voice_job') as start:
            next(b for b in dialog.buttons() if b.text() == '重說').click()
            app.processEvents()
            start.assert_called_once_with('command', self.window.voice_pipeline.transcribe_retry)
        self.assertIsNone(self.window._voice_retry_dialog)
    def test_idle_monitoring_pause_keeps_voice(self):
        from datetime import datetime
        from personal_ai_assistant.models import ForegroundSnapshot
        self.window.monitoring = True
        snapshot = ForegroundSnapshot(datetime.now(), 'idle', '', '閒置', 3600)
        with patch('personal_ai_assistant.main_window.get_foreground_snapshot', return_value=snapshot), patch('personal_ai_assistant.main_window.is_windows_locked_or_unavailable', return_value=False):
            self.window.monitor_tick()
        self.assertTrue(self.window.auto_idle_paused)
        self.assertAlive()
        self.window.monitoring = False

    def test_status_render_does_not_rearm(self):
        with patch.object(self.window.voice_pipeline, 'watchdog') as watchdog:
            self.window._update_voice_status_label()
            watchdog.assert_not_called()

    def test_service_timer_active_hidden(self):
        self.window.hide()
        self.assertTrue(self.window.voice_state_timer.isActive())
        self.assertIs(self.window.voice_state_timer.parent(), app)
        self.assertAlive()

    def test_sensitivity_ui_defaults_auto_and_has_calibration(self):
        self.assertEqual(self.window.voice_wake_sensitivity_combo.currentData(), 'auto')
        self.assertEqual(self.window.voice_wake_sensitivity_combo.count(), 4)
        self.assertIn('正常說話音量', self.window.voice_calibrate_button.text())

    def test_privacy_stops_voice_and_watchdog_stays_off(self):
        with patch.object(self.window, 'refresh_lists'), patch.object(self.window, 'update_top_status_bar'):
            try:
                self.window.set_privacy_mode_enabled(True)
                self.assertFalse(self.window.voice_pipeline.active)
                self.assertFalse(self.window.voice_pipeline.watchdog())
                self.provider.stop.assert_called_once()
            finally:
                self.window.set_privacy_mode_enabled(False)
                self.window.voice_pipeline.start()
                self.window.voice_wake_timer.start()

    def test_voice_off_stops_worker(self):
        try:
            self.window.set_voice_command_enabled(False)
            self.assertFalse(self.window.voice_pipeline.active)
            self.assertFalse(self.window.voice_pipeline.watchdog())
            self.assertGreaterEqual(self.provider.stop.call_count,1)
        finally:
            self.window.voice_pipeline.start()
            self.window.voice_wake_timer.start()

    def test_z_true_quit(self):
        self.window.exit_application()
        self.assertFalse(self.window.voice_pipeline.active)
        self.provider.stop.assert_called_once()

unittest.main()

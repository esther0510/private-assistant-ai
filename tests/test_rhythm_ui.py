import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PySide6.QtWidgets import QApplication, QDialog
from personal_ai_assistant.rhythm_ui import RhythmDiagnosticWidget


class RhythmUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.widget = RhythmDiagnosticWidget(Path(self.temp.name), lambda: SimpleNamespace(app_name="game.exe"))
        self.addCleanup(self.widget.deleteLater)
        self.addCleanup(self.widget.cancel)
        self.widget.profile.setText("Test game; positive late")
        self.widget.chart.setText("same chart")
        self.widget.controls.setChecked(True)
        self.target = {"hwnd": 12, "pid": 123456, "app": "game.exe", "title": "game", "activity_mode": "game"}
        self.env = {**self.target, "client_px": [1920, 1080], "display_px": [1920, 1080],
                    "refresh_hz": 144, "dpi_percent": 100, "presentation": "windowed", "monitor": "one"}

    def start(self):
        self.widget.arm()
        self.widget.arm_timer.stop()
        with patch("personal_ai_assistant.rhythm_ui.foreground_target", return_value=self.target), \
             patch("personal_ai_assistant.rhythm_ui.capture_environment", return_value=dict(self.env)), \
             patch("personal_ai_assistant.rhythm_ui.keyboard_devices", return_value=["keyboard"]), \
             patch("personal_ai_assistant.rhythm_ui.CpuSampler", side_effect=RuntimeError("unavailable")):
            self.widget.begin()

    def test_record_save_and_reopen(self):
        self.start()
        self.assertTrue(self.widget.timer.isActive())
        self.assertFalse(self.widget.chart.isEnabled())
        with patch("personal_ai_assistant.rhythm_ui.ResultDialog") as dialog:
            instance = dialog.return_value
            instance.exec.return_value = QDialog.Accepted
            instance.metrics = {"accuracy": 98}
            instance.samples = {}
            instance.source.text.return_value = "manual"
            self.widget.finish()
        self.assertIsNone(self.widget.active)
        self.assertFalse(self.widget.timer.isActive())
        self.assertEqual(len(self.widget.session["runs"]), 1)
        saved = self.widget.path
        self.assertTrue(saved.exists())
        self.widget.new_session()
        with patch("personal_ai_assistant.rhythm_ui.QFileDialog.getOpenFileName", return_value=(str(saved), "")):
            self.widget.load()
        self.assertEqual(self.widget.chart.text(), "same chart")
        self.assertEqual(len(self.widget.session["runs"]), 1)
        self.assertIn("暫時趨勢", self.widget.output.toPlainText())

    def test_assistant_foreground_rejected(self):
        self.target["pid"] = os.getpid()
        self.start()
        self.assertIsNone(self.widget.active)
        self.assertFalse(self.widget.timer.isActive())
        self.assertTrue(self.widget.start_button.isEnabled())

    def test_environment_change_uses_locked_target(self):
        self.start()
        changed = {**self.env, "refresh_hz": 60}
        with patch("personal_ai_assistant.rhythm_ui.capture_environment", return_value=changed) as capture:
            self.widget.tick()
        self.assertEqual(capture.call_args.args[0]["hwnd"], 12)
        self.assertTrue(self.widget.active["environment_changed"])

    def test_cancel_pending_and_recording(self):
        self.widget.arm()
        self.widget.cancel()
        self.assertFalse(self.widget.arm_timer.isActive())
        self.start()
        self.widget.cancel()
        self.assertIsNone(self.widget.active)
        self.assertFalse(self.widget.timer.isActive())
        self.assertEqual(self.widget.session["runs"], [])

    def test_alt_tab_desktop_mode_not_game_setting_change(self):
        self.start()
        desktop = {**self.env, "refresh_hz": 60, "is_foreground": False, "minimized": True}
        with patch("personal_ai_assistant.rhythm_ui.capture_environment", return_value=desktop):
            self.widget.tick()
        self.assertFalse(self.widget.active["environment_changed"])

    def test_cancel_result_keeps_run_stopped(self):
        self.start()
        with patch("personal_ai_assistant.rhythm_ui.ResultDialog") as dialog:
            dialog.return_value.exec.return_value = QDialog.Rejected
            self.widget.finish()
        self.assertIsNotNone(self.widget.active)
        self.assertFalse(self.widget.timer.isActive())
        self.assertEqual(self.widget.session["runs"], [])


if __name__ == "__main__":
    unittest.main()

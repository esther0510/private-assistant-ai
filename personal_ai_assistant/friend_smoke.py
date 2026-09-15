"""Explicit packaged diagnostic; all state lives under a caller supplied empty directory."""
import os
import sys
import json
import traceback
from pathlib import Path
from datetime import datetime, timedelta


def main():
    root = Path(sys.argv[2]).resolve()
    root.mkdir(parents=True, exist_ok=True)
    os.environ['LOCALAPPDATA'] = str(root / 'local')
    os.environ['APPDATA'] = str(root / 'roaming')
    os.environ['HF_HOME'] = str(root / 'hf')
    checks = {}
    def record(name):
        checks[name] = 'PASS'
        (root / 'smoke.json').write_text(json.dumps(checks, ensure_ascii=False, indent=2), encoding='utf-8')
    try:
        from PySide6.QtWidgets import QApplication
        from PySide6.QtCore import QTimer
        from .storage import default_db_path
        from .db import AssistantStore
        from .settings import SettingsService
        from .voice_command import detect_voice_components, install_voice_dependencies
        from .reminders import parse_natural_reminder
        app = QApplication([])
        record('Qt Windows platform / QApplication')
        store = AssistantStore(default_db_path())
        settings = SettingsService(store).load()
        assert not settings.advisor_api_enabled and not settings.advisor_endpoint_url
        assert not settings.voice_microphone_id and not settings.app_aliases
        assert not store.list_reminders()
        record('clean defaults and empty reminders')
        state = detect_voice_components()
        assert state.state == 'ready', state
        assert install_voice_dependencies(Path.cwd()).state == 'verified'
        record('bundled voice imports / native VAD / frozen bootstrap')
        now = datetime.now()
        parsed = parse_natural_reminder('十二點半提醒我睡覺', now=now)
        assert parsed.due_at.hour == 0 and parsed.due_at.minute == 30
        reminder = store.add_reminder(parsed.text, parsed.due_at)
        store.update_reminder(reminder.id, now + timedelta(minutes=2))
        store.delete_reminder(reminder.id)
        assert not store.list_reminders()
        store.undo_reminder_operation(store.last_reminder_operation()['id'])
        assert store.get_reminder(reminder.id).status == 'pending'
        assert store.due_reminders(now + timedelta(minutes=3))
        record('midnight / add / edit / delete / Undo / due reminders')
        SettingsService(store).update(auto_start_monitoring_enabled=False, update_checks_enabled=False, quick_add_enabled=False)
        from .main_window import MainWindow
        window = MainWindow(default_db_path())
        window.show()
        def finish():
            record('MainWindow show / event loop')
            window.exit_application()
        QTimer.singleShot(1500, finish)
        app.exec()
        record('complete application exit')
        if '--models' in sys.argv:
            from .kws import ensure_model, SherpaKeywordEngine
            engine = SherpaKeywordEngine(ensure_model())
            engine.configure(('賈維斯',))
            engine.accept(bytes(32000))
            record('fresh KWS download / Chinese keyword compile / CPU inference')
            from faster_whisper import WhisperModel
            import numpy as np
            model = WhisperModel('small', device='cpu', compute_type='int8', download_root=str(root / 'whisper'))
            segments, info = model.transcribe(np.zeros(16000, dtype=np.float32), vad_filter=True)
            list(segments)
            record('fresh Whisper small download / CPU model / VAD inference')
        record('overall')
        return 0
    except Exception:
        checks['ERROR'] = traceback.format_exc()
        (root / 'smoke.json').write_text(json.dumps(checks, ensure_ascii=False, indent=2), encoding='utf-8')
        return 1

import io
import json
import logging
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from personal_ai_assistant.db import AssistantStore
from personal_ai_assistant.main_window import MainWindow
from personal_ai_assistant.reminders import parse_natural_reminder
from personal_ai_assistant.command_router import route_assistant_command
from personal_ai_assistant.voice_text import route_voice_command
from personal_ai_assistant.ui_formatters import format_reminder_card_text


class MidnightChainTests(unittest.TestCase):
    now = datetime(2026, 9, 14, 0, 19)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = AssistantStore(Path(self.temp.name) / 'fixture.db')
        self.window = Mock()
        self.window.store = self.store
        self.window._confirm_ambiguous_time_if_needed.side_effect = lambda p: p

    def test_all_routes_storage_and_ui(self):
        cases = [('十二點睡覺', 0, 0), ('十二點半睡覺', 0, 30),
                 ('12:30睡覺', 0, 30), ('12:00睡覺', 0, 0),
                 ('晚上十二點睡覺', 0, 0), ('凌晨十二點睡覺', 0, 0),
                 ('晚上十二點半睡覺', 0, 30), ('凌晨十二點半睡覺', 0, 30),
                 ('中午十二點吃飯', 12, 0)]
        output = io.StringIO()
        logger = logging.getLogger('personal_ai_assistant.reminder_trace')
        handler = logging.StreamHandler(output)
        previous = logger.level
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        try:
            for router in (parse_natural_reminder, route_assistant_command, route_voice_command):
                for text, hour, minute in cases:
                    with self.subTest(router=router.__name__, text=text):
                        result = router(text, now=self.now)
                        parsed = getattr(result, 'parsed_reminder', result)
                        self.assertFalse(parsed.needs_confirmation)
                        self.assertEqual((parsed.due_at.hour, parsed.due_at.minute), (hour, minute))
                        self.assertTrue(MainWindow._add_parsed_reminder(self.window, parsed))
                        with self.store.connect() as conn:
                            row = conn.execute('SELECT * FROM reminders WHERE text=? AND due_at=?',
                                               (parsed.text, parsed.due_at.isoformat())).fetchone()
                        self.assertIsNotNone(row)
                        stored = self.store.get_reminder(row['id'])
                        self.assertEqual(stored.due_at, parsed.due_at)
                        self.assertIn(f'{hour:02d}:{minute:02d}', format_reminder_card_text(stored))
        finally:
            logger.removeHandler(handler)
            logger.setLevel(previous)
        trace = output.getvalue()
        for stage in ('input', 'voice_input', 'voice_normalized', 'time_resolution', 'parsed_datetime',
                      'intent', 'creator', 'storage_input', 'storage_written', 'storage_read', 'ui'):
            self.assertIn('"stage": "' + stage + '"', trace)
        Path('work').mkdir(exist_ok=True)
        Path('work/midnight-chain-trace.jsonl').write_text(trace, encoding='utf-8')

    def test_ambiguous_never_writes_without_choice(self):
        for text in ('十二點開會', '十二點半開會', '12:00開會', '12:30開會'):
            for router in (route_assistant_command, route_voice_command):
                parsed = router(text, now=self.now).parsed_reminder
                self.assertTrue(parsed.needs_confirmation)
                self.window._confirm_ambiguous_time_if_needed.side_effect = lambda p: MainWindow._confirm_ambiguous_time_if_needed(self.window, p)
                with patch('personal_ai_assistant.main_window.QMessageBox') as box:
                    box.return_value.clickedButton.return_value = None
                    self.assertFalse(MainWindow._add_parsed_reminder(self.window, parsed))
        self.assertEqual(self.store.list_reminders(), [])

    def test_retry_and_concurrent_connections(self):
        due = datetime(2026, 9, 15)
        with ThreadPoolExecutor(max_workers=8) as pool:
            ids = list(pool.map(lambda _: self.store.add_reminder('睡覺', due).id, range(16)))
        self.assertEqual(len(set(ids)), 1)
        self.assertEqual(len(self.store.list_reminders()), 1)
        with self.store.connect() as conn:
            count = conn.execute("SELECT COUNT(*) FROM assistant_events WHERE event_type='reminder_created'").fetchone()[0]
        self.assertEqual(count, 1)
        self.assertNotEqual(self.store.add_reminder('睡覺', due + timedelta(minutes=30)).id, ids[0])
        self.assertNotEqual(self.store.add_reminder('吃藥', due).id, ids[0])
        with self.store.connect() as conn:
            conn.execute('UPDATE reminders SET created_at=? WHERE id=?',
                         ((datetime.now() - timedelta(minutes=1)).isoformat(), ids[0]))
        self.assertNotEqual(self.store.add_reminder('睡覺', due).id, ids[0])
        self.assertIsNotNone(self.store.get_reminder(ids[0]))


if __name__ == '__main__':
    unittest.main()

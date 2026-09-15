import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from personal_ai_assistant.db import AssistantStore
from personal_ai_assistant.command_router import route_assistant_command
from personal_ai_assistant.voice_text import route_voice_command
from personal_ai_assistant.reminder_operations import parse_operation, operation_candidates, parse_replacement
from personal_ai_assistant.main_window import MainWindow


class ReminderOperationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = AssistantStore(Path(self.temp.name) / 'test.sqlite3')
        self.now = datetime.now()
        self.window = SimpleNamespace(store=self.store, deferred_reminders={}, active_popups={},
            refresh_lists=Mock(), _command_notice=Mock(), _confirm_ambiguous_time_if_needed=lambda p: p)
        self.window._execute_reminder_operation = lambda op, selected=None: MainWindow._execute_reminder_operation(self.window, op, selected)

    def add(self, title='睡覺', minutes=90, rule=None):
        return self.store.add_reminder(title, self.now + timedelta(minutes=minutes), recurrence_rule=rule)

    def execute(self, text, yes=True):
        with patch('personal_ai_assistant.main_window.QMessageBox') as box:
            box.question.return_value = box.StandardButton.Yes if yes else box.StandardButton.No
            return self.window._execute_reminder_operation(parse_operation(text))

    def test_intents_both_routes(self):
        for route in (route_assistant_command, route_voice_command):
            for text, intent in [('刪掉剛剛那個提醒', 'delete'), ('取消剛剛那個', 'delete'),
                    ('剛剛時間說錯了', 'update'), ('把睡覺提醒刪掉', 'delete'),
                    ('把十二點半的睡覺提醒改成一點', 'update'), ('復原剛剛的操作', 'undo')]:
                self.assertEqual(route(text).intent, 'reminder_' + intent, (route, text))

    def test_recent_only_latest_and_no_fallthrough(self):
        old = self.add('喝水')
        latest = self.add()
        self.assertTrue(self.execute('取消剛剛那個'))
        self.assertEqual([r.id for r in self.store.list_reminders()], [old.id])
        self.assertEqual(self.store.get_reminder(latest.id).status, 'deleted')
        self.assertFalse(self.execute('取消剛剛那個'))
        self.assertEqual(self.store.get_reminder(old.id).status, 'pending')

    def test_recent_window(self):
        reminder = self.add()
        for seconds, count in [(30, 1), (59, 1), (61, 0)]:
            self.assertEqual(len(operation_candidates(self.store, parse_operation('取消'),
                             reminder.created_at + timedelta(seconds=seconds))), count)

    def test_update_in_place(self):
        original = self.add()
        self.assertTrue(self.execute('剛剛時間說錯了，改成一點'))
        rows = self.store.list_reminders()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].id, original.id)
        self.assertEqual(rows[0].created_at, original.created_at)
        self.assertEqual(rows[0].due_at.hour, 1)
        self.assertEqual(rows[0].due_at.minute, 0)

    def test_unique_delete_confirmation(self):
        reminder = self.add()
        self.assertFalse(self.execute('把睡覺提醒刪掉', yes=False))
        self.assertEqual(self.store.get_reminder(reminder.id).status, 'pending')
        self.assertTrue(self.execute('把睡覺提醒刪掉'))
        self.assertEqual(self.store.list_reminders(), [])

    def test_multiple_require_choice_and_confirmation(self):
        one, two = self.add(), self.add(minutes=150)
        with patch('personal_ai_assistant.main_window.QInputDialog.getItem', return_value=('', False)) as chooser:
            self.assertFalse(self.execute('把睡覺提醒刪掉'))
            labels = chooser.call_args.args[3]
            self.assertTrue(all('睡覺' in label and ':' in label for label in labels))
        self.assertEqual(len(self.store.list_reminders()), 2)
        with patch('personal_ai_assistant.main_window.QInputDialog.getItem', side_effect=lambda *args: (args[3][1], True)):
            self.assertTrue(self.execute('把睡覺提醒刪掉'))
        self.assertEqual([r.id for r in self.store.list_reminders()], [one.id])

    def test_missing_time_uses_existing_dialog(self):
        self.add()
        with patch('personal_ai_assistant.main_window.QInputDialog.getText', return_value=('凌晨一點', True)):
            self.assertTrue(self.execute('剛剛時間說錯了'))
        self.assertEqual(self.store.list_reminders()[0].due_at.hour, 1)

    def test_recurring_preserves_all_other_rule_fields(self):
        for rule in [dict(freq='daily', time='21:00'), dict(freq='weekly', time='21:00', days=[0, 2, 4]),
                     dict(freq='interval', time='21:00', start_date=self.now.date().isoformat(), interval_days=3),
                     dict(freq='cycle', time='21:00', start_date=self.now.date().isoformat(), active_days=3,
                          break_days=2, interval_days=1, repeat=True)]:
            reminder = self.add(str(rule), rule=json.dumps(rule))
            updated = self.store.update_reminder(reminder.id, self.now + timedelta(days=1))
            expected = dict(rule, time=(self.now + timedelta(days=1)).strftime('%H:%M'))
            self.assertEqual(json.loads(updated.recurrence_rule), expected)
            self.assertEqual(updated.id, reminder.id)
            self.assertIsNotNone(updated.next_due_at)

    def test_undo_delete_and_update_persist(self):
        original = self.add()
        for action in ('delete', 'update'):
            if action == 'delete':
                self.store.delete_reminder(original.id)
            else:
                self.store.update_reminder(original.id, original.due_at + timedelta(hours=1))
            reopened = AssistantStore(self.store.path)
            event = reopened.last_reminder_operation()
            restored = reopened.undo_reminder_operation(event['id'])
            self.assertEqual(restored, original)
            self.assertIsNone(reopened.last_reminder_operation())

    def test_stale_confirmation_and_undo_rejected(self):
        reminder = self.add()
        self.store.update_reminder(reminder.id, reminder.due_at + timedelta(hours=1))
        with self.assertRaises(ValueError):
            self.store.delete_reminder(reminder.id, expected=reminder)
        event = self.store.last_reminder_operation()
        with self.store.connect() as conn:
            conn.execute("UPDATE reminders SET status='notified' WHERE id=?", (reminder.id,))
        with self.assertRaises(ValueError):
            self.store.undo_reminder_operation(event['id'])

    def test_selected_ui_actions_storage_and_cleanup(self):
        reminder = self.add()
        item = Mock()
        item.data.return_value = reminder.id
        self.window.reminders_list = Mock()
        self.window.reminders_list.currentItem.return_value = item
        popup = Mock()
        self.window.active_popups[reminder.id] = popup
        self.window.deferred_reminders[reminder.id] = object()
        with patch('personal_ai_assistant.main_window.QInputDialog.getText', return_value=('凌晨一點', True)), \
             patch('personal_ai_assistant.main_window.QMessageBox') as box:
            box.question.return_value = box.StandardButton.Yes
            self.assertTrue(MainWindow._selected_reminder_operation(self.window, 'update'))
            self.assertEqual(self.store.get_reminder(reminder.id).due_at.hour, 1)
            self.assertTrue(MainWindow._selected_reminder_operation(self.window, 'delete'))
        self.assertEqual(self.store.list_reminders(), [])
        self.assertEqual(self.window.deferred_reminders, {})
        popup.close.assert_called_once()
        self.assertEqual(self.window.refresh_lists.call_count, 2)

    def test_deleted_reminder_not_deduplicated_or_fired(self):
        reminder = self.add(minutes=-1)
        self.store.delete_reminder(reminder.id)
        self.assertEqual(self.store.due_reminders(self.now), [])
        replacement = self.add(minutes=-1)
        self.assertNotEqual(replacement.id, reminder.id)

    def test_title_and_old_clock_filter(self):
        morning = self.store.add_reminder('睡覺', (self.now + timedelta(days=1)).replace(hour=0, minute=30))
        self.store.add_reminder('睡覺', morning.due_at.replace(hour=12))
        self.assertEqual(len(operation_candidates(self.store, parse_operation('把十二點半的睡覺提醒改成一點'))), 2)
        self.assertEqual([r.id for r in operation_candidates(self.store, parse_operation('把凌晨十二點半的睡覺提醒改成一點'))], [morning.id])
        title = self.add('買媽媽的藥')
        self.assertEqual([r.id for r in operation_candidates(self.store, parse_operation('把買媽媽的藥提醒刪掉'))], [title.id])

    def test_update_decline_and_time_dialog_cancel_no_write(self):
        original = self.add()
        self.assertFalse(self.execute('剛剛時間說錯了，改成一點', yes=False))
        with patch('personal_ai_assistant.main_window.QInputDialog.getText', return_value=('', False)):
            self.assertFalse(self.execute('剛剛時間說錯了'))
        self.assertEqual(self.store.get_reminder(original.id), original)
        self.assertIsNone(self.store.last_reminder_operation())

    def test_ui_undo_and_entry_points(self):
        original = self.add()
        self.assertTrue(MainWindow.execute_assistant_command(self.window, '取消剛剛那個'))
        self.assertTrue(self.execute('復原剛剛的操作'))
        self.assertEqual(self.store.get_reminder(original.id), original)
        self.window.reminder_input = Mock()
        self.window.reminder_input.text.return_value = '取消剛剛那個'
        MainWindow.add_reminder(self.window)
        self.assertEqual(self.store.list_reminders(), [])
        self.window.reminder_input.clear.assert_called_once()


if __name__ == '__main__':
    unittest.main()

import json
import unittest
from datetime import datetime
from unittest.mock import Mock, patch
from personal_ai_assistant.reminders import parse_natural_reminder, format_due_at
from personal_ai_assistant.command_router import route_assistant_command
from personal_ai_assistant.voice_text import route_voice_command


class TwelveTimeTests(unittest.TestCase):
    now = datetime(2026, 9, 14, 10)

    def test_context(self):
        for task in ('睡覺', '晚安', '關電腦', '宵夜', '夜間吃藥'):
            for twelve in ('十二點', '12點'):
                with self.subTest(task=task, twelve=twelve):
                    result = parse_natural_reminder(twelve + '提醒我' + task, self.now)
                    self.assertEqual(result.due_at, datetime(2026, 9, 15))
                    self.assertFalse(result.needs_confirmation)
                    self.assertIsNone(result.recurrence_rule)
        for text in ('十二點吃午餐', '十二點白天開會', '中午十二點吃飯'):
            self.assertEqual(parse_natural_reminder(text, self.now).due_at.hour, 12)

    def test_explicit_times_win(self):
        for text, hour, minute, day in (
            ('中午12點睡覺', 12, 0, 14), ('晚上十二點睡覺', 0, 0, 15),
            ('凌晨12點吃午餐', 0, 0, 15), ('12:30睡覺', 0, 30, 15),
            ('十二點半睡覺', 0, 30, 15), ('明天中午12點吃飯', 12, 0, 15),
            ('晚上12:00睡覺', 0, 0, 15), ('明天晚上12點睡覺', 0, 0, 15),
            ('12點 PM 睡覺', 12, 0, 14), ('12點 AM 開會', 0, 0, 15),
            ('00:00開會', 0, 0, 15),
        ):
            with self.subTest(text=text):
                result = parse_natural_reminder(text, self.now)
                self.assertEqual(result.due_at, datetime(2026, 9, day, hour, minute))
                self.assertFalse(result.needs_confirmation)

    def test_uncertain_is_always_confirmed(self):
        for text in ('十二點開會', '十二點吃飯', '十二點吃藥', '十二點午餐後睡覺'):
            for hour in (0, 10, 23):
                result = parse_natural_reminder(text, self.now.replace(hour=hour), active_hour_scores={12: 100})
                self.assertTrue(result.needs_confirmation)
                self.assertEqual({d.hour for d in result.ambiguous_time_options}, {0, 12})
                self.assertIn('午夜12點', result.confirmation_summary)
        result = parse_natural_reminder('明天十二點開會', self.now)
        self.assertEqual({d.day for d in result.ambiguous_time_options}, {15})

    def test_recurrence(self):
        for prefix, freq in (('每天', 'daily'), ('每週一、三 ', 'weekly'), ('每2天', 'interval'), ('從今天開始連續吃3天停2天循環 ', 'cycle')):
            result = parse_natural_reminder(prefix + '十二點提醒我睡覺', self.now)
            rule = json.loads(result.recurrence_rule)
            self.assertEqual(rule['freq'], freq)
            self.assertEqual(rule['time'], '00:00')
            self.assertEqual(result.due_at.hour, 0)
            self.assertIsNotNone(result.next_due_at)
            uncertain = parse_natural_reminder(prefix + '十二點開會', self.now)
            self.assertTrue(uncertain.needs_confirmation)
            self.assertEqual(len(uncertain.ambiguous_time_options), 2)

    def test_voice_router_and_display(self):
        for text, confirm in (('十二點提醒我睡覺', False), ('十二點開會', True)):
            for router in (route_assistant_command, route_voice_command):
                intent = router(text, now=self.now)
                self.assertEqual(intent.intent, 'reminder')
                self.assertEqual(intent.needs_confirmation, confirm)
        result = parse_natural_reminder('十二點提醒我睡覺', self.now)
        self.assertIn('00:00（午夜12點）', result.understood_message)
        self.assertEqual(format_due_at(result.due_at), '2026-09-15 00:00（午夜12點）')
        self.assertEqual(format_due_at(self.now), '2026-09-14 10:00')

    def test_confirmation_choice_and_cancel(self):
        from personal_ai_assistant.main_window import MainWindow
        for prefix in ('', '每天', '每2天', '連續吃3天循環 '):
            parsed = parse_natural_reminder(prefix + '十二點開會', self.now)
            for choice in (0, 1, None):
                with self.subTest(prefix=prefix, choice=choice):
                    buttons = [object(), object(), object()]
                    with patch('personal_ai_assistant.main_window.QMessageBox') as box_type:
                        box = box_type.return_value
                        box.addButton.side_effect = buttons
                        box.clickedButton.return_value = buttons[choice] if choice is not None else None
                        result = MainWindow._confirm_ambiguous_time_if_needed(Mock(), parsed)
                    if choice is None:
                        self.assertIsNone(result)
                        continue
                    selected = parsed.ambiguous_time_options[choice]
                    self.assertEqual(result.due_at.hour, selected.hour)
                    self.assertFalse(result.ambiguous_time_options)
                    self.assertEqual(result.missing_fields, parsed.missing_fields)
                    if parsed.recurrence_rule:
                        before = json.loads(parsed.recurrence_rule)
                        after = json.loads(result.recurrence_rule)
                        before['time'] = selected.strftime('%H:%M')
                        self.assertEqual(after, before)
                        self.assertEqual(result.needs_confirmation, after['freq'] in {'cycle', 'interval'})
                    else:
                        self.assertEqual(result.due_at, selected)
                        self.assertFalse(result.needs_confirmation)

    def test_cancel_does_not_create(self):
        from personal_ai_assistant.main_window import MainWindow
        window = Mock()
        window._confirm_ambiguous_time_if_needed.return_value = None
        result = MainWindow._add_parsed_reminder(window, parse_natural_reminder('十二點開會', self.now))
        self.assertFalse(result)
        window.store.add_reminder.assert_not_called()


if __name__ == '__main__':
    unittest.main()

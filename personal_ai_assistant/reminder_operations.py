"""Reminder edits, transactional snapshots and conservative command matching."""
import json
import re
from datetime import datetime, timedelta

from .reminders import _find_time, next_occurrence, parse_natural_reminder


ACTIVE = ('pending', 'snoozed', 'pending_due', 'notified', 'today_inbox')


def parse_operation(text):
    value = re.sub(r'\s+', '', text).strip('，,。！!')
    value = re.sub(r'^(?:請|幫我)', '', value)
    if value.lower() in ('undo', '復原剛剛的操作', '撤銷上次操作', '復原上次提醒操作'):
        return dict(action='undo')
    recent = bool(re.search(r'剛剛|剛才', value))
    update = bool(re.search(r'改成|改到|修改|時間說錯|時間錯了', value))
    delete = bool(re.search(r'刪掉|刪除|取消', value))
    if not (recent or '提醒' in value or value == '取消') or not (update or delete):
        return None
    if re.search(r'不要|別|不想', value):
        return None
    action = 'update' if update else 'delete'
    parts = re.split(r'改成|改到', value, maxsplit=1)
    target = parts[0]
    new_time = parts[1] if len(parts) == 2 else ''
    old = _find_time(target)
    old_clock = (old.hour, old.minute, old.explicit_daypart or old.twenty_four_hour) if old else None
    if old:
        target = target[:old.start] + target[old.end:]
    title = re.sub(r'^(?:把|刪掉|刪除|取消|修改)', '', target)
    title = re.sub(r'(?:提醒)?(?:刪掉|刪除|取消|修改)$', '', title)
    title = re.sub(r'提醒$', '', title).strip('的，, ')
    return dict(action=action, recent=recent or value == '取消', title=title,
                new_time=new_time, old_clock=old_clock)


def operation_candidates(store, operation, now=None):
    now = now or datetime.now()
    with store.connect() as conn:
        if operation.get('recent'):
            row = conn.execute('SELECT * FROM reminders ORDER BY created_at DESC, id DESC LIMIT 1').fetchone()
            if not row or not 0 <= (now - datetime.fromisoformat(row['created_at'])).total_seconds() <= 60:
                return []
            rows = [row]
        else:
            rows = conn.execute('SELECT * FROM reminders ORDER BY due_at, id').fetchall()
    results = []
    for row in rows:
        if row['status'] not in ACTIVE:
            continue
        if not operation.get('recent'):
            title = operation.get('title', '')
            if title and title != row['text'].strip():
                continue
            old = operation.get('old_clock')
            due = datetime.fromisoformat(row['due_at'])
            if old and ((due.hour, due.minute) != old[:2] if old[2]
                        else (due.hour % 12, due.minute) != (old[0] % 12, old[1])):
                continue
        results.append(store._row_to_reminder(row))
    return results


def parse_replacement(reminder, text, now=None):
    # Keep the existing item's date for a clock-only correction.
    now = now or datetime.now()
    parsed = parse_natural_reminder(text + ' 提醒我' + reminder.text, now)
    if parsed.recurrence_rule or parsed.kind != 'scheduled':
        raise ValueError('這裡只能修改時間，不能更換提醒類型或週期。')
    from dataclasses import replace
    if not re.search(r'今天|明天|後天|月|日|號|/|後', text):
        def on_original_date(value):
            due = reminder.due_at.replace(hour=value.hour, minute=value.minute, second=0, microsecond=0)
            if due <= now:
                due = due.replace(year=now.year, month=now.month, day=now.day)
                if due <= now:
                    due += timedelta(days=1)
            return due
        due = on_original_date(parsed.due_at)
        parsed = replace(parsed, due_at=due,
                         ambiguous_time_options=tuple(on_original_date(d) for d in parsed.ambiguous_time_options))
    return parsed


class ReminderOperationsStore:
    def _operation_table(self, conn):
        conn.execute('''CREATE TABLE IF NOT EXISTS reminder_operation_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT, reminder_id INTEGER NOT NULL,
            action TEXT NOT NULL, timestamp TEXT NOT NULL,
            before_json TEXT NOT NULL, after_json TEXT NOT NULL, undone INTEGER NOT NULL DEFAULT 0)''')

    def _edit_reminder(self, reminder_id, due_at=None, expected=None):
        with self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            self._operation_table(conn)
            row = conn.execute('SELECT * FROM reminders WHERE id=?', (reminder_id,)).fetchone()
            if not row or row['status'] not in ACTIVE:
                raise ValueError('提醒已取消或完成，請重新選擇。')
            if expected is not None and self._row_to_reminder(row) != expected:
                raise ValueError('提醒已變更，請重新選擇並確認。')
            before = dict(row)
            after = dict(row)
            if due_at is None:
                after['status'] = 'deleted'
            else:
                rule_text = row['recurrence_rule']
                if rule_text:
                    rule = json.loads(rule_text)
                    rule['time'] = due_at.strftime('%H:%M')
                    rule_text = json.dumps(rule, ensure_ascii=False)
                    due_at = next_occurrence(rule_text, due_at - timedelta(microseconds=1))
                    if due_at is None:
                        raise ValueError('這個週期已結束，沒有可修改的下一次提醒。')
                following = next_occurrence(rule_text, due_at) if rule_text else None
                after.update(due_at=due_at.isoformat(), recurrence_rule=rule_text,
                             next_due_at=following.isoformat() if following else None,
                             status='pending', fired_at=None, notified_at=None, snoozed_until=None,
                             time_inferred=0, inference_reason=None)
            fields = [key for key in after if key != 'id']
            conn.execute('UPDATE reminders SET ' + ','.join(key+'=?' for key in fields) + ' WHERE id=?',
                         [after[key] for key in fields] + [reminder_id])
            conn.execute('INSERT INTO reminder_operation_log (reminder_id,action,timestamp,before_json,after_json) VALUES (?,?,?,?,?)',
                         (reminder_id, 'update' if due_at else 'delete', datetime.now().isoformat(),
                          json.dumps(before), json.dumps(after)))
        return self.get_reminder(reminder_id)

    def update_reminder(self, reminder_id, due_at, expected=None):
        return self._edit_reminder(reminder_id, due_at=due_at, expected=expected)

    def delete_reminder(self, reminder_id, expected=None):
        return self._edit_reminder(reminder_id, expected=expected)

    def last_reminder_operation(self):
        with self.connect() as conn:
            self._operation_table(conn)
            row = conn.execute('SELECT * FROM reminder_operation_log ORDER BY id DESC LIMIT 1').fetchone()
        return dict(row) if row and not row['undone'] else None

    def undo_reminder_operation(self, operation_id):
        with self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            self._operation_table(conn)
            event = conn.execute('SELECT * FROM reminder_operation_log ORDER BY id DESC LIMIT 1').fetchone()
            if not event or event['id'] != operation_id or event['undone']:
                raise ValueError('最近操作已變更或復原，請重新確認。')
            row = conn.execute('SELECT * FROM reminders WHERE id=?', (event['reminder_id'],)).fetchone()
            if not row or dict(row) != json.loads(event['after_json']):
                raise ValueError('提醒在操作後已變更，無法安全復原。')
            before = json.loads(event['before_json'])
            fields = [key for key in before if key != 'id']
            conn.execute('UPDATE reminders SET ' + ','.join(key+'=?' for key in fields) + ' WHERE id=?',
                         [before[key] for key in fields] + [event['reminder_id']])
            conn.execute('UPDATE reminder_operation_log SET undone=1 WHERE id=?', (operation_id,))
        return self.get_reminder(event['reminder_id'])

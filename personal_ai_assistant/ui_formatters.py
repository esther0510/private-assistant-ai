from __future__ import annotations

from .reminder_trace import trace_reminder

from .models import AssistantEvent, Reminder
from .reminders import cycle_status, describe_recurrence_rule, format_due_at


def status_label(status: str) -> str:
    return {
        "pending": "等待中",
        "pending_due": "等你回來",
        "today_inbox": "Today Inbox",
        "notified": "已通知",
        "completed": "已完成",
        "ignored": "已忽略",
        "snoozed": "已延後",
    }.get(status, status)


def format_reminder_card_text(reminder: Reminder, learning_summary: str = "尚未學習") -> str:
    trace_reminder("ui", due_at=reminder.due_at, formatted=format_due_at(reminder.due_at))
    recurrence = describe_recurrence_rule(reminder.recurrence_rule)
    cycle = cycle_status(reminder.recurrence_rule, reminder.due_at)
    second_line_parts = [format_due_at(reminder.due_at), "正式設定", recurrence]
    if cycle:
        second_line_parts.append(cycle)
    if learning_summary:
        second_line_parts.append(learning_summary)
    return f"{reminder.text}  [{status_label(reminder.status)}]\n" + "｜".join(second_line_parts)


def human_event_title(event: AssistantEvent) -> str:
    app_name = event.app_name or ""
    title = event.window_title_summary or ""
    if event.event_type == "foreground_changed":
        if app_name and title:
            return f"切換到 {app_name}：{title}"
        if app_name:
            return f"切換到 {app_name}"
        return "切換活動視窗"
    if event.event_type == "idle_started":
        return "你暫時離開電腦"
    if event.event_type == "idle_ended":
        return "你回到電腦"
    if event.event_type.startswith("reminder_"):
        return "提醒事件"
    return {
        "context_reminder_triggered": "情境提醒觸發",
        "dead_loop_detected": "可能陷入重複處理",
    }.get(event.event_type, "助理事件")


def format_event_card_text(event: AssistantEvent) -> str:
    mode = event.context_mode or "-"
    detail = f"{event.timestamp.strftime('%H:%M:%S')}｜{mode}"
    return f"{human_event_title(event)}\n{detail}"

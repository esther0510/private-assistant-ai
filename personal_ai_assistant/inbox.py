from __future__ import annotations

from datetime import datetime, time


TODAY_INBOX_STATUS = "today_inbox"


def looks_like_today_inbox(text: str) -> bool:
    value = " ".join((text or "").split())
    if not value:
        return False
    return any(token in value for token in ("今天記得", "有空提醒", "有空", "今天要", "記得"))


def strip_inbox_prefix(text: str) -> str:
    value = " ".join((text or "").split())
    for prefix in ("今天記得", "今天要", "有空提醒我", "有空提醒", "提醒我", "記得"):
        if value.startswith(prefix):
            value = value[len(prefix) :].strip(" ，,：:")
            break
    return value or "Today Inbox"


def today_inbox_due_at(now: datetime | None = None) -> datetime:
    now = now or datetime.now()
    return datetime.combine(now.date(), time(23, 59))


def should_suggest_today_inbox(
    active: bool,
    mode: str | None,
    has_higher_priority_due: bool,
    minutes_since_last_suggestion: int | None,
) -> bool:
    if not active or has_higher_priority_due:
        return False
    if mode in {"遊戲", "工作", "看片", "閒置"}:
        return False
    if minutes_since_last_suggestion is not None and minutes_since_last_suggestion < 45:
        return False
    return True

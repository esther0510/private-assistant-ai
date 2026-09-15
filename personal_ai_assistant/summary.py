from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime

from .models import ActivitySession, DailySummaryBucket, UsageEvent


SUMMARY_LABELS = {
    "工作": "工作",
    "遊戲": "遊戲",
    "看片": "看片",
    "聊天": "聊天",
    "閒置": "離席",
    "private": "private/ignored",
    "ignored": "private/ignored",
}


def build_daily_summary(events: list[UsageEvent], target_date: date, end_at: datetime | None = None) -> list[DailySummaryBucket]:
    same_day = sorted((event for event in events if event.timestamp.date() == target_date), key=lambda item: item.timestamp)
    if not same_day:
        return []
    end_at = end_at or datetime.now()
    totals: dict[str, int] = defaultdict(int)
    for index, event in enumerate(same_day):
        next_time = same_day[index + 1].timestamp if index + 1 < len(same_day) else min(end_at, event.timestamp.replace(hour=23, minute=59, second=59))
        seconds = max(0, int((next_time - event.timestamp).total_seconds()))
        label = SUMMARY_LABELS.get(event.mode, "一般")
        totals[label] += min(seconds, 4 * 60 * 60)
    return [
        DailySummaryBucket(label=label, seconds=seconds)
        for label, seconds in sorted(totals.items(), key=lambda item: item[1], reverse=True)
        if seconds > 0
    ]


def build_daily_summary_from_sessions(
    sessions: list[ActivitySession],
    target_date: date,
    end_at: datetime | None = None,
) -> list[DailySummaryBucket]:
    same_day = [session for session in sessions if session.started_at.date() <= target_date and session.last_seen_at.date() >= target_date]
    if not same_day:
        return []
    end_at = end_at or datetime.now()
    day_start = datetime.combine(target_date, datetime.min.time())
    day_end = min(end_at, datetime.combine(target_date, datetime.max.time()))
    totals: dict[str, int] = defaultdict(int)
    for session in same_day:
        started_at = max(session.started_at, day_start)
        ended_at = min(session.ended_at or session.last_seen_at or end_at, day_end)
        seconds = max(0, int((ended_at - started_at).total_seconds()))
        label = SUMMARY_LABELS.get(session.mode, "一般")
        totals[label] += min(seconds, 4 * 60 * 60)
    return [
        DailySummaryBucket(label=label, seconds=seconds)
        for label, seconds in sorted(totals.items(), key=lambda item: item[1], reverse=True)
        if seconds > 0
    ]


def format_summary_bucket(bucket: DailySummaryBucket) -> str:
    minutes = round(bucket.seconds / 60)
    hours, mins = divmod(minutes, 60)
    if hours:
        return f"{bucket.label} {hours} 小時 {mins} 分"
    return f"{bucket.label} {mins} 分"

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class ForegroundSnapshot:
    timestamp: datetime
    app_name: str
    window_title: str
    mode: str
    idle_seconds: int = 0


@dataclass(frozen=True)
class Reminder:
    id: int
    text: str
    due_at: datetime
    created_at: datetime
    fired_at: datetime | None = None
    status: str = "pending"
    notified_at: datetime | None = None
    completed_at: datetime | None = None
    ignored_at: datetime | None = None
    snoozed_until: datetime | None = None
    recurrence_rule: str | None = None
    next_due_at: datetime | None = None
    snooze_count: int = 0
    notification_count: int = 0
    completed_count: int = 0
    ignored_count: int = 0
    last_snoozed_at: datetime | None = None
    importance: str = "normal"
    category: str = "general"
    context_rule_id: int | None = None
    time_inferred: bool = False
    inference_reason: str | None = None


@dataclass(frozen=True)
class ReminderInteraction:
    id: int
    reminder_id: int
    event_type: str
    timestamp: datetime
    response_seconds: int | None = None
    current_mode: str | None = None
    source: str = "assistant_reminder"


@dataclass(frozen=True)
class ReminderHabitSummary:
    reminder_id: int
    text: str
    recurrence_rule: str | None
    notification_count: int
    completed_count: int
    snooze_count: int
    ignored_count: int
    average_response_seconds: int | None
    tendency: str


@dataclass(frozen=True)
class UsageEvent:
    id: int
    timestamp: datetime
    app_name: str
    window_title: str
    mode: str
    idle_seconds: int


@dataclass(frozen=True)
class ActivitySession:
    id: int
    started_at: datetime
    last_seen_at: datetime
    ended_at: datetime | None
    app_name: str
    mode: str
    category: str
    title_summary: str
    duration_seconds: int
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True)
class AssistantEvent:
    id: int
    timestamp: datetime
    event_type: str
    source: str
    context_mode: str | None = None
    app_name: str | None = None
    window_title_summary: str | None = None
    related_reminder_id: int | None = None
    series_id: str | None = None
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True)
class LearnedPattern:
    id: int
    pattern_type: str
    scope: str
    key: str
    value: str
    score: float
    sample_count: int
    confidence: float
    first_seen: datetime
    last_updated: datetime
    status: str
    protected: bool = False
    reason: str | None = None


@dataclass(frozen=True)
class AdaptiveDecision:
    strategy: str
    reason: str
    policy_level: int = 1
    defer_seconds: int = 0
    requires_confirmation: bool = False
    preference_key: str | None = None


@dataclass(frozen=True)
class AppSetting:
    key: str
    value: str


@dataclass(frozen=True)
class NotificationEvent:
    id: int
    source_app: str
    title: str
    body: str
    received_at: datetime
    metadata: str | None = None


@dataclass(frozen=True)
class NotificationInteraction:
    id: int
    notification_event_id: int
    event_type: str
    timestamp: datetime
    response_seconds: int | None = None
    target_app: str | None = None


@dataclass(frozen=True)
class ContextReminder:
    id: int
    text: str
    app_pattern: str
    trigger: str
    window_title_pattern: str | None
    created_at: datetime
    status: str = "waiting"
    triggered_at: datetime | None = None
    reminder_id: int | None = None
    last_seen_active: bool = False


@dataclass(frozen=True)
class DisplayGeometry:
    name: str
    index: int
    is_primary: bool
    x: int
    y: int
    width: int
    height: int
    available_x: int
    available_y: int
    available_width: int
    available_height: int
    device_pixel_ratio: float = 1.0


@dataclass(frozen=True)
class DailySummaryBucket:
    label: str
    seconds: int


@dataclass(frozen=True)
class CurrentState:
    current_mode: str
    foreground_app: str
    window_title_summary: str
    activity_summary: str
    activity_started_at: datetime
    updated_at: datetime
    intent: str = "unknown"
    intent_source: str = "inferred"
    session_category: str = "general"
    session_started_at: datetime | None = None
    session_duration_seconds: int = 0
    previous_work_session_seconds: int = 0
    break_pattern: dict[str, Any] | None = None
    recent_apps: tuple[str, ...] = ()
    recent_events: tuple[str, ...] = ()
    active_reminders: tuple[str, ...] = ()
    away: bool = False
    manual_stop: bool = False
    privacy_mode: bool = False


@dataclass(frozen=True)
class Goal:
    id: int
    goal_type: str
    text: str
    source: str
    confidence: float
    status: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class NextAction:
    action_text: str
    reason: str
    confidence: float
    source: str
    should_interrupt: bool = False
    intervention_level: str = "silent"
    understanding: str = ""
    questions: tuple[str, ...] = ()
    provider_label: str = ""
    health_state: str = "unknown"
    health_message: str = ""

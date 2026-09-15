from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .db import AssistantStore
from .activity_sessions import session_identity
from .models import AssistantEvent, ForegroundSnapshot, Reminder


@dataclass(frozen=True)
class EventContext:
    mode: str | None = None
    app_name: str | None = None
    window_title: str | None = None


class EventBus:
    """Small durable event pipeline for meaningful state changes and interactions."""

    def __init__(self, store: AssistantStore) -> None:
        self.store = store
        self._last_foreground_key: tuple[str, str, str] | None = None
        self._last_idle_state = False

    def publish_foreground_snapshot(self, snapshot: ForegroundSnapshot) -> AssistantEvent | None:
        if self.store.privacy_mode_enabled() or not self.store.should_record_app(snapshot.app_name, snapshot.window_title):
            return None

        key = session_identity(snapshot)
        if key == self._last_foreground_key:
            return None

        previous = self._last_foreground_key
        self._last_foreground_key = key
        event_type = "foreground_changed" if previous else "foreground_observed"
        return self.store.record_event(
            event_type=event_type,
            source="foreground_monitor",
            timestamp=snapshot.timestamp,
            context_mode=snapshot.mode,
            app_name=snapshot.app_name,
            window_title=snapshot.window_title,
            metadata={
                "idle_seconds": snapshot.idle_seconds,
                "previous_mode": previous[0] if previous else None,
                "previous_app": previous[1] if previous else None,
            },
        )

    def publish_idle_state(self, snapshot: ForegroundSnapshot, paused: bool, reason: str) -> AssistantEvent | None:
        if paused == self._last_idle_state:
            return None
        self._last_idle_state = paused
        event_type = "user_away" if paused else "user_returned"
        return self.store.record_event(
            event_type=event_type,
            source="foreground_monitor",
            timestamp=snapshot.timestamp,
            context_mode=snapshot.mode,
            app_name=snapshot.app_name,
            window_title=snapshot.window_title,
            metadata={"reason": reason, "idle_seconds": snapshot.idle_seconds},
        )

    def publish_reminder_event(
        self,
        event_type: str,
        reminder: Reminder,
        context: EventContext | None = None,
        metadata: dict[str, object] | None = None,
    ) -> AssistantEvent:
        context = context or EventContext()
        return self.store.record_event(
            event_type=event_type,
            source="reminder_service",
            timestamp=datetime.now(),
            context_mode=context.mode,
            app_name=context.app_name,
            window_title=context.window_title,
            related_reminder_id=reminder.id,
            series_id=self.store.series_id_for(reminder.id, reminder.text, reminder.recurrence_rule),
            metadata=metadata or {},
        )

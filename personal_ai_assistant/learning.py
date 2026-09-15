from __future__ import annotations

import json
from statistics import median
from dataclasses import dataclass
from datetime import datetime, timedelta

from .db import AssistantStore
from .models import ForegroundSnapshot, LearnedPattern, NotificationEvent, Reminder


OBSERVING_MIN_SAMPLES = 3
PROVISIONAL_MIN_SAMPLES = 5
CONFIRMED_MIN_SAMPLES = 12
NOTIFICATION_INTERVENTION_MIN_SAMPLES = 5
NOTIFICATION_FAST_SECONDS = 120
NOTIFICATION_MIN_DELAY_SECONDS = 180


@dataclass(frozen=True)
class AdvisorResult:
    should_intervene: bool
    suggestion: str
    confidence: float
    reason: str


class LearningEngine:
    """Transparent rules for Observe -> Outcome -> Learn."""

    def __init__(self, store: AssistantStore) -> None:
        self.store = store

    def observe_foreground_change(self, snapshot: ForegroundSnapshot) -> None:
        if self.store.privacy_mode_enabled() or not self.store.learning_enabled():
            return
        hour_key = f"{snapshot.mode}:{snapshot.timestamp.hour:02d}"
        self._increment_pattern(
            "mode_seen_by_hour",
            "time",
            hour_key,
            value=snapshot.mode,
            reason=f"{snapshot.timestamp.hour:02d}:00 常見模式：{snapshot.mode}",
        )
        app_key = snapshot.app_name.lower()
        self._increment_pattern(
            "app_seen",
            "app",
            app_key,
            value=snapshot.app_name,
            reason=f"常用 App：{snapshot.app_name}",
        )

    def learn_from_reminder_outcome(self, reminder_id: int, outcome: str, current_mode: str | None) -> None:
        if self.store.privacy_mode_enabled() or not self.store.learning_enabled():
            return
        reminder = self.store.get_reminder(reminder_id)
        if not reminder:
            return

        interactions = [item for item in self.store.reminder_interactions(reminder_id) if item.event_type != "notified"]
        sample_count = max(1, len(interactions))
        responses = [item.response_seconds for item in interactions if item.response_seconds is not None]
        average_response = int(sum(responses) / len(responses)) if responses else 0
        completed = sum(1 for item in interactions if item.event_type == "completed")
        snoozed = sum(1 for item in interactions if item.event_type == "snoozed")
        ignored = sum(1 for item in interactions if item.event_type == "ignored")

        self.store.upsert_pattern(
            "reminder_response_seconds",
            "reminder",
            self._reminder_key(reminder),
            str(average_response),
            float(average_response),
            sample_count,
            self._confidence(sample_count),
            self._status(sample_count),
            reason=f"{reminder.text} 平均約 {average_response} 秒回應",
        )
        self.store.upsert_pattern(
            "reminder_outcome_rate",
            "reminder",
            self._reminder_key(reminder),
            f"completed={completed};snoozed={snoozed};ignored={ignored}",
            self._dominant_rate(completed, snoozed, ignored),
            sample_count,
            self._confidence(sample_count),
            self._status(sample_count),
            reason=self._reminder_reason(reminder.text, completed, snoozed, ignored),
        )

        mode = current_mode or "未知"
        mode_interactions = [item for item in interactions if (item.current_mode or "未知") == mode]
        if mode_interactions:
            mode_sample_count = len(mode_interactions)
            mode_snoozed = sum(1 for item in mode_interactions if item.event_type == "snoozed")
            mode_ignored = sum(1 for item in mode_interactions if item.event_type == "ignored")
            mode_completed = sum(1 for item in mode_interactions if item.event_type == "completed")
            key = f"{mode}:{reminder.category}:{reminder.importance}"
            status = self._status(mode_sample_count)
            reason = self._mode_reason(mode, reminder.category, mode_completed, mode_snoozed, mode_ignored)
            pattern = self.store.upsert_pattern(
                "mode_reminder_outcome",
                "context",
                key,
                f"completed={mode_completed};snoozed={mode_snoozed};ignored={mode_ignored}",
                self._dominant_rate(mode_completed, mode_snoozed, mode_ignored),
                mode_sample_count,
                self._confidence(mode_sample_count),
                status,
                reason=reason,
            )
            if mode_sample_count >= OBSERVING_MIN_SAMPLES:
                self.store.upsert_hypothesis(
                    "mode_reminder_outcome",
                    "context",
                    key,
                    reason,
                    pattern.confidence,
                    mode_sample_count,
                    status,
                    reason="由提醒 outcome 自動建立，後續資料衝突時會降低信心或回到觀察",
                )
            self._maybe_create_level2_preference(pattern, mode, reminder)
            self._maybe_create_level3_suggestion(reminder, sample_count, snoozed, ignored, average_response)

    def learned_patterns(self) -> list[LearnedPattern]:
        return self.store.learned_patterns()

    def record_external_notification(
        self,
        source_app: str,
        received_at: datetime | None = None,
        title: str = "",
        body: str = "",
        context_key: str | None = None,
        current_mode: str | None = None,
        away_state: str | None = None,
    ) -> NotificationEvent | None:
        if not self._notification_learning_allowed(source_app, title):
            return None
        received_at = received_at or datetime.now()
        normalized_context = self._notification_context_key(source_app, context_key, current_mode)
        return self.store.add_notification_event(
            source_app=source_app,
            received_at=received_at,
            title=title,
            body=body,
            context_key=normalized_context,
            current_mode=current_mode,
            away_state=away_state,
            metadata={
                "beta": True,
                "content_policy": "metadata_only_by_default",
                "context_key": normalized_context,
            },
        )

    def observe_notification_app_visit(self, snapshot: ForegroundSnapshot) -> list[NotificationEvent]:
        if not self._notification_learning_allowed(snapshot.app_name, snapshot.window_title):
            return []
        visited: list[NotificationEvent] = []
        app_key = snapshot.app_name.lower()
        for event in self.store.pending_notification_events(snapshot.timestamp):
            if event.source_app.lower() != app_key:
                continue
            interaction = self.store.mark_notification_viewed(event.id, snapshot.timestamp, snapshot.app_name)
            if interaction:
                visited.append(event)
                self._update_notification_pattern(event, interaction.response_seconds or 0, handled=True)
        return visited

    def notification_intervention_candidate(
        self,
        now: datetime | None = None,
        away: bool = False,
    ) -> tuple[NotificationEvent, LearnedPattern] | None:
        if away or self.store.privacy_mode_enabled() or not self.store.learning_enabled():
            return None
        if self.store.get_setting("notification_listener_beta_enabled", "0") != "1":
            return None
        now = now or datetime.now()
        for event in self.store.pending_notification_events(now):
            pattern = self.store.get_pattern("notification_response_seconds", "notification", self._event_pattern_key(event))
            if not pattern or pattern.sample_count < NOTIFICATION_INTERVENTION_MIN_SAMPLES or pattern.confidence < 0.35:
                continue
            stats = self._parse_notification_stats(pattern.value)
            median_seconds = int(stats.get("median", 0))
            threshold = max(NOTIFICATION_MIN_DELAY_SECONDS, median_seconds * 3)
            age_seconds = int((now - event.received_at).total_seconds())
            if stats.get("fast_rate", 0.0) >= 0.6 and age_seconds >= threshold:
                return event, pattern
        return None

    def handle_notification_prompt_action(
        self,
        notification_event_id: int,
        action: str,
        at: datetime | None = None,
    ) -> None:
        at = at or datetime.now()
        event = self.store.get_notification_event(notification_event_id)
        if action == "snooze":
            self.store.snooze_notification_event(notification_event_id, at + timedelta(minutes=10))
            self.store.log_notification_interaction(notification_event_id, "snoozed", at)
            return
        if action == "ignore":
            self.store.dismiss_notification_event(notification_event_id)
            self.store.log_notification_interaction(notification_event_id, "ignored", at)
            if event:
                self._update_notification_pattern(event, 0, handled=False)
            return
        if action == "open":
            self.store.dismiss_notification_event(notification_event_id)
            self.store.log_notification_interaction(notification_event_id, "open_requested", at)

    def _notification_learning_allowed(self, source_app: str | None, context: str | None = None) -> bool:
        if self.store.privacy_mode_enabled() or not self.store.learning_enabled():
            return False
        if self.store.get_setting("notification_listener_beta_enabled", "0") != "1":
            return False
        return self.store.should_record_app(source_app, context)

    def _notification_context_key(self, source_app: str, context_key: str | None, current_mode: str | None) -> str:
        app = (source_app or "unknown").lower()
        context = (context_key or current_mode or "general").strip().lower() or "general"
        return f"{app}:{context}"

    def _event_pattern_key(self, event: NotificationEvent) -> str:
        metadata: dict[str, object] = {}
        if event.metadata:
            try:
                raw = json.loads(event.metadata)
                if isinstance(raw, dict):
                    metadata = raw
            except json.JSONDecodeError:
                metadata = {}
        context = str(metadata.get("context_key") or "")
        if not context:
            context = self._notification_context_key(event.source_app, None, None)
        return context

    def _update_notification_pattern(self, event: NotificationEvent, response_seconds: int, handled: bool) -> LearnedPattern:
        key = self._event_pattern_key(event)
        existing = self.store.get_pattern("notification_response_seconds", "notification", key)
        stats = self._parse_notification_stats(existing.value if existing else "")
        responses = list(stats.get("responses", []))
        if handled:
            responses.append(max(0, int(response_seconds)))
        ignored = int(stats.get("ignored", 0)) + (0 if handled else 1)
        total = len(responses) + ignored
        average = int(sum(responses) / len(responses)) if responses else 0
        median_seconds = int(median(responses)) if responses else 0
        fast_rate = (sum(1 for item in responses if item <= NOTIFICATION_FAST_SECONDS) / total) if total else 0.0
        ignore_rate = ignored / total if total else 0.0
        confidence = max(0.0, min(0.9, (total / 12 * 0.9) * (1 - min(0.5, ignore_rate))))
        status = self._status(total)
        value = json.dumps(
            {
                "average": average,
                "median": median_seconds,
                "fast_rate": round(fast_rate, 3),
                "ignore_rate": round(ignore_rate, 3),
                "ignored": ignored,
                "responses": responses[-20:],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        reason = self._notification_reason(event.source_app, key, median_seconds, total, confidence, ignore_rate)
        return self.store.upsert_pattern(
            "notification_response_seconds",
            "notification",
            key,
            value,
            float(median_seconds),
            total,
            confidence,
            status,
            reason=reason,
        )

    def _parse_notification_stats(self, value: str) -> dict[str, object]:
        if not value:
            return {"responses": [], "ignored": 0, "fast_rate": 0.0, "ignore_rate": 0.0, "median": 0}
        try:
            raw = json.loads(value)
        except json.JSONDecodeError:
            return {"responses": [], "ignored": 0, "fast_rate": 0.0, "ignore_rate": 0.0, "median": 0}
        if not isinstance(raw, dict):
            return {"responses": [], "ignored": 0, "fast_rate": 0.0, "ignore_rate": 0.0, "median": 0}
        responses = [int(item) for item in raw.get("responses", []) if isinstance(item, (int, float))]
        return {
            "responses": responses,
            "ignored": int(raw.get("ignored", 0) or 0),
            "fast_rate": float(raw.get("fast_rate", 0.0) or 0.0),
            "ignore_rate": float(raw.get("ignore_rate", 0.0) or 0.0),
            "median": int(raw.get("median", 0) or 0),
        }

    def _notification_reason(
        self,
        source_app: str,
        key: str,
        median_seconds: int,
        sample_count: int,
        confidence: float,
        ignore_rate: float,
    ) -> str:
        if ignore_rate >= 0.5:
            return f"{source_app} 這類通知：通常晚點處理（樣本 {sample_count}，信心 {self._confidence_word(confidence)}）"
        if median_seconds <= NOTIFICATION_FAST_SECONDS and sample_count:
            return f"{source_app} 這類通知：通常 {median_seconds} 秒內查看（樣本 {sample_count}，信心 {self._confidence_word(confidence)}）"
        return f"{source_app} 這類通知：持續觀察中（樣本 {sample_count}，信心 {self._confidence_word(confidence)}）"

    def _confidence_word(self, confidence: float) -> str:
        if confidence >= 0.7:
            return "高"
        if confidence >= 0.35:
            return "中"
        return "低"

    def _increment_pattern(self, pattern_type: str, scope: str, key: str, value: str, reason: str) -> None:
        existing = self.store.get_pattern(pattern_type, scope, key)
        sample_count = (existing.sample_count if existing else 0) + 1
        self.store.upsert_pattern(
            pattern_type,
            scope,
            key,
            value,
            float(sample_count),
            sample_count,
            self._confidence(sample_count),
            self._status(sample_count),
            reason=reason,
        )

    def _maybe_create_level2_preference(self, pattern: LearnedPattern, mode: str, reminder: Reminder) -> None:
        if pattern.status == "observing" or reminder.importance in {"critical", "important"} or reminder.category == "health":
            return
        if mode != "遊戲":
            return
        value = pattern.value
        if "snoozed=" not in value and "ignored=" not in value:
            return
        parts = self._parse_rate_value(value)
        disruptions = parts.get("snoozed", 0) + parts.get("ignored", 0)
        if disruptions < max(3, parts.get("completed", 0) + 1):
            return
        self.store.set_adaptive_preference(
            preference_key=f"presentation:{mode}:{reminder.category}:{reminder.importance}",
            value="defer_until_game_end_or_app_switch",
            policy_level=2,
            reason=pattern.reason or "遊戲中這類提醒常被延後或忽略",
            pattern_id=pattern.id,
        )

    def _maybe_create_level3_suggestion(
        self,
        reminder: Reminder,
        sample_count: int,
        snoozed: int,
        ignored: int,
        average_response: int,
    ) -> None:
        if sample_count < CONFIRMED_MIN_SAMPLES:
            return
        if reminder.category == "health":
            return
        if snoozed >= max(8, sample_count * 0.7) and average_response >= 15 * 60:
            self.store.add_suggestion(
                suggestion_type="reschedule_reminder",
                reminder_id=reminder.id,
                title=f"建議調整：{reminder.text}",
                body=f"這個提醒最近常延後，平均約 {round(average_response / 60)} 分鐘才處理。要不要之後改晚一點？",
                proposed_change={"kind": "reschedule", "minutes_later": min(30, max(5, round(average_response / 300) * 5))},
                confidence=self._confidence(sample_count),
                reason="Level 3：改正式時間必須先問你",
            )
        if ignored >= max(8, sample_count * 0.75):
            self.store.add_suggestion(
                suggestion_type="review_reminder",
                reminder_id=reminder.id,
                title=f"建議檢查：{reminder.text}",
                body="這個提醒最近常被忽略。要不要保留、改時間，或改成比較安靜的提醒？",
                proposed_change={"kind": "review"},
                confidence=self._confidence(sample_count),
                reason="Level 3：停用或改正式規則必須先問你",
            )

    def _confidence(self, sample_count: int) -> float:
        if sample_count <= 0:
            return 0.0
        return min(0.9, sample_count / 12 * 0.9)

    def _status(self, sample_count: int) -> str:
        if sample_count >= CONFIRMED_MIN_SAMPLES:
            return "confirmed"
        if sample_count >= PROVISIONAL_MIN_SAMPLES:
            return "provisional"
        return "observing"

    def _dominant_rate(self, completed: int, snoozed: int, ignored: int) -> float:
        total = max(1, completed + snoozed + ignored)
        return max(completed, snoozed, ignored) / total

    def _reminder_key(self, reminder: Reminder) -> str:
        return self.store.series_id_for(reminder.id, reminder.text, reminder.recurrence_rule)

    def _reminder_reason(self, text: str, completed: int, snoozed: int, ignored: int) -> str:
        if snoozed >= max(2, completed, ignored):
            return f"{text}：常延後"
        if ignored >= max(2, completed, snoozed):
            return f"{text}：常忽略"
        if completed >= max(1, snoozed, ignored):
            return f"{text}：通常會完成"
        return f"{text}：持續觀察中"

    def _mode_reason(self, mode: str, category: str, completed: int, snoozed: int, ignored: int) -> str:
        if snoozed + ignored >= max(2, completed + 1):
            return f"{mode} 模式下 {category} 提醒常被延後或忽略"
        if completed >= max(1, snoozed + ignored):
            return f"{mode} 模式下 {category} 提醒通常會處理"
        return f"{mode} 模式下 {category} 提醒持續觀察中"

    def _parse_rate_value(self, value: str) -> dict[str, int]:
        result: dict[str, int] = {}
        for part in value.split(";"):
            name, _, raw_count = part.partition("=")
            try:
                result[name] = int(raw_count)
            except ValueError:
                result[name] = 0
        return result


class RuleBasedAdvisor:
    """Future LLM slot: current_state + recent_events + patterns -> advice."""

    def __init__(self, store: AssistantStore) -> None:
        self.store = store

    def evaluate(self, current_state: ForegroundSnapshot, reminder: Reminder | None = None) -> AdvisorResult:
        patterns = self.store.learned_patterns(limit=20)
        if reminder and current_state.mode == "遊戲" and reminder.importance != "important":
            return AdvisorResult(
                should_intervene=False,
                suggestion="遊戲中先降低干擾",
                confidence=0.5,
                reason="規則版 advisor：遊戲模式下的一般提醒先採取較安靜策略",
            )
        active = [item for item in patterns if item.status in ("provisional", "confirmed")]
        return AdvisorResult(
            should_intervene=False,
            suggestion="持續觀察",
            confidence=max((item.confidence for item in active), default=0.0),
            reason="目前沒有需要主動介入的高信心模式",
        )

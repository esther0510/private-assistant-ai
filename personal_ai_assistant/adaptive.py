from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .db import AssistantStore
from .models import AdaptiveDecision, ForegroundSnapshot, Reminder


PRESENTATION_IMMEDIATE_POPUP = "immediate_popup"
PRESENTATION_WINDOWS_ONLY = "windows_notification_only"
PRESENTATION_STATUS_ONLY = "status_bar_only"
PRESENTATION_DEFER_ACTIVE = "defer_until_active"
PRESENTATION_DEFER_GAME_END = "defer_until_game_end_or_app_switch"
PRESENTATION_REPEAT = "repeat_if_unconfirmed"


@dataclass(frozen=True)
class PolicyCheck:
    level: int
    allowed: bool
    reason: str


class AdaptivePolicy:
    """Classifies adaptive changes into permission levels."""

    LEVEL3_KEYWORDS = ("time", "schedule", "recurrence", "disable", "health", "medicine", "sleep")

    def classify_change(self, change_type: str, reminder: Reminder | None = None) -> PolicyCheck:
        category = reminder.category if reminder else "general"
        if category == "health":
            return PolicyCheck(3, False, "健康/藥物提醒只允許產生建議，正式規則必須先確認")
        if category == "life" and reminder and any(token in reminder.text for token in ("睡", "起床")):
            return PolicyCheck(3, False, "睡眠時間屬於正式生活設定，必須先確認")
        if any(token in change_type for token in self.LEVEL3_KEYWORDS):
            return PolicyCheck(3, False, "正式時間、週期、停用或重要判定都必須先確認")
        if change_type in {
            "presentation_strategy",
            "quiet_notification",
            "status_bar_only",
            "game_defer",
            "message_detail",
            "intervention_frequency",
        }:
            return PolicyCheck(2, True, "低風險顯示策略可自動微調，並保留原因與復原紀錄")
        return PolicyCheck(1, True, "純觀察統計不改正式設定")


class AdaptiveReminderStrategy:
    """Uses current state and learned preferences without overwriting formal reminders."""

    def __init__(self, store: AssistantStore, policy: AdaptivePolicy | None = None) -> None:
        self.store = store
        self.policy = policy or AdaptivePolicy()

    def decide(
        self,
        reminder: Reminder,
        snapshot: ForegroundSnapshot,
        auto_idle_paused: bool = False,
    ) -> AdaptiveDecision:
        if reminder.category == "health" or reminder.importance in {"critical", "important"}:
            decision = AdaptiveDecision(
                strategy=PRESENTATION_REPEAT,
                reason="重要/健康類提醒按正式時間提醒，不讓低層策略危險延後",
                policy_level=1,
            )
            self._log(reminder, decision, snapshot)
            return decision

        if auto_idle_paused or snapshot.mode == "閒置":
            decision = AdaptiveDecision(
                strategy=PRESENTATION_DEFER_ACTIVE,
                reason="目前離席或閒置，先等回到電腦後提醒",
                policy_level=2,
                defer_seconds=60,
                preference_key="presentation:idle",
            )
            self._log(reminder, decision, snapshot)
            return decision

        preference_key = f"presentation:{snapshot.mode}:{reminder.category}:{reminder.importance}"
        learned_strategy = self.store.get_adaptive_preference(preference_key)
        if learned_strategy:
            check = self.policy.classify_change("presentation_strategy", reminder)
            decision = AdaptiveDecision(
                strategy=learned_strategy,
                reason=f"AI 推測：{self._preference_reason(preference_key)}",
                policy_level=check.level,
                preference_key=preference_key,
            )
            self._log(reminder, decision, snapshot)
            return decision

        if snapshot.mode == "遊戲" and reminder.importance in {"low", "casual"}:
            decision = AdaptiveDecision(
                strategy=PRESENTATION_STATUS_ONLY,
                reason="遊戲中且提醒標記為隨意，先降低打擾",
                policy_level=2,
                preference_key=preference_key,
            )
            self._log(reminder, decision, snapshot)
            return decision

        if snapshot.mode == "遊戲" and reminder.importance == "normal":
            decision = AdaptiveDecision(
                strategy=PRESENTATION_WINDOWS_ONLY,
                reason="遊戲中一般提醒先用 Windows 通知，避免直接搶畫面",
                policy_level=2,
                preference_key=preference_key,
            )
            self._log(reminder, decision, snapshot)
            return decision

        decision = AdaptiveDecision(
            strategy=PRESENTATION_IMMEDIATE_POPUP,
            reason="沒有高信心推測，按正式提醒設定正常顯示",
            policy_level=1,
        )
        self._log(reminder, decision, snapshot)
        return decision

    def should_release_deferred(self, decision: AdaptiveDecision, previous_snapshot: ForegroundSnapshot | None, current: ForegroundSnapshot) -> bool:
        if decision.strategy == PRESENTATION_DEFER_ACTIVE:
            return current.idle_seconds < 60 and current.mode != "閒置"
        if decision.strategy == PRESENTATION_DEFER_GAME_END:
            return previous_snapshot is None or current.mode != "遊戲" or current.app_name != previous_snapshot.app_name
        return True

    def _preference_reason(self, preference_key: str) -> str:
        with self.store.connect() as conn:
            row = conn.execute(
                "SELECT reason FROM adaptive_preferences WHERE preference_key = ?",
                (preference_key,),
            ).fetchone()
        return str(row["reason"]) if row else "過往反應顯示這樣比較少打擾"

    def _log(self, reminder: Reminder, decision: AdaptiveDecision, snapshot: ForegroundSnapshot) -> None:
        self.store.log_adaptive_decision(
            reminder_id=reminder.id,
            strategy=decision.strategy,
            policy_level=decision.policy_level,
            reason=decision.reason,
            metadata={
                "mode": snapshot.mode,
                "app_name": snapshot.app_name,
                "importance": reminder.importance,
                "category": reminder.category,
                "decided_at": datetime.now().isoformat(),
            },
        )

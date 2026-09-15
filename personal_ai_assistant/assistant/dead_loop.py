from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta

from ..models import AssistantEvent, ForegroundSnapshot


@dataclass(frozen=True)
class DeadLoopAssessment:
    risk_score: float
    should_alert: bool
    reasons: tuple[str, ...]
    repeated_patterns: tuple[str, ...]
    prompt: str


class DeadLoopDetector:
    """Transparent local rules for spotting repeated, low-progress work loops."""

    def __init__(self, threshold: float = 0.72, cooldown_minutes: int = 20) -> None:
        self.threshold = threshold
        self.cooldown = timedelta(minutes=cooldown_minutes)
        self.last_alert_at: datetime | None = None

    def assess(
        self,
        current_snapshot: ForegroundSnapshot,
        recent_events: list[AssistantEvent],
        now: datetime | None = None,
    ) -> DeadLoopAssessment:
        now = now or datetime.now()
        window_start = now - timedelta(minutes=30)
        events = [event for event in recent_events if event.timestamp >= window_start]
        reasons: list[str] = []
        score = 0.0

        keys = [
            f"{event.context_mode or '-'}|{event.app_name or '-'}|{event.window_title_summary or '-'}"
            for event in events
            if event.event_type in {"foreground_changed", "foreground_observed"}
        ]
        counts = Counter(keys)
        repeated = [key for key, count in counts.items() if count >= 3]
        if repeated:
            score += min(0.35, 0.12 * len(repeated))
            reasons.append("最近 30 分鐘反覆回到相同 App/視窗組合")

        app_sequence = [event.app_name or "-" for event in events if event.app_name]
        switches = sum(1 for before, after in zip(app_sequence, app_sequence[1:]) if before != after)
        if switches >= 8 and len(set(app_sequence)) <= 4:
            score += 0.25
            reasons.append("在少數 App 之間頻繁切換")

        title_tokens = Counter()
        for event in events:
            title = (event.window_title_summary or "").lower()
            for token in ("error", "failed", "exception", "traceback", "錯誤", "失敗", "exception"):
                if token in title:
                    title_tokens[token] += 1
        if sum(title_tokens.values()) >= 3:
            score += 0.25
            reasons.append("視窗標題多次出現錯誤或失敗訊號")

        if current_snapshot.idle_seconds < 60 and len(events) >= 8 and not repeated:
            score += 0.08
            reasons.append("長時間持續活動但沒有明顯狀態收斂")

        if self.last_alert_at and now - self.last_alert_at < self.cooldown:
            score = min(score, self.threshold - 0.01)

        repeated_patterns = tuple(item.replace("|", " / ") for item in repeated[:5])
        should_alert = score >= self.threshold
        prompt = self.build_prompt(current_snapshot, events, tuple(reasons), repeated_patterns, score)
        return DeadLoopAssessment(round(score, 2), should_alert, tuple(reasons), repeated_patterns, prompt)

    def mark_alerted(self, at: datetime | None = None) -> None:
        self.last_alert_at = at or datetime.now()

    def build_prompt(
        self,
        current_snapshot: ForegroundSnapshot,
        events: list[AssistantEvent],
        reasons: tuple[str, ...],
        repeated_patterns: tuple[str, ...],
        risk_score: float,
    ) -> str:
        event_lines = []
        for event in list(reversed(events[-20:])):
            event_lines.append(
                f"- {event.timestamp.strftime('%H:%M')} {event.event_type}: "
                f"{event.context_mode or '-'} / {event.app_name or '-'} / {event.window_title_summary or '-'}"
            )
        repeated_text = "\n".join(f"- {item}" for item in repeated_patterns) or "- 尚未整理出單一重複項，但整體行為接近循環"
        reason_text = "\n".join(f"- {item}" for item in reasons) or "- 本機規則判定工作流可能停滯"
        events_text = "\n".join(event_lines) or "- 沒有可安全分享的近期事件"
        return (
            "我可能陷入重複處理同一個問題，請幫我重新檢查。\n\n"
            "請不要沿用我目前的既有假設。請重新審視問題來源、替代方向、最小驗證步驟，"
            "並指出我可能反覆嘗試但沒有進展的地方。\n\n"
            f"目前活動推測：{current_snapshot.mode} / {current_snapshot.app_name} / {current_snapshot.window_title[:120] or '-'}\n"
            f"本機 dead-loop risk score：{risk_score:.2f}\n\n"
            "為何懷疑卡住：\n"
            f"{reason_text}\n\n"
            "重複模式：\n"
            f"{repeated_text}\n\n"
            "最近 10-30 分鐘重要事件：\n"
            f"{events_text}\n\n"
            "請先列出 3 個不同假設，再建議下一個最小可測試動作。"
        )

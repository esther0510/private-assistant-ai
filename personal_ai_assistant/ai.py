from __future__ import annotations

from .models import ForegroundSnapshot, Reminder


class AssistantAnalyzer:
    """Stable interface for future model-backed analysis.

    The MVP intentionally uses deterministic wording so it works without an API key.
    Replace this class or call out from here when a model is available later.
    """

    def build_reminder_message(self, reminder: Reminder, snapshot: ForegroundSnapshot) -> tuple[str, str]:
        title = f"提醒：{reminder.text}"
        mode = snapshot.mode

        if mode == "遊戲":
            body = f"你現在像是在遊戲中。這件事到時間了：{reminder.text}。可以在這局或這段結束後處理。"
        elif mode == "看片":
            body = f"你正在看片。先提醒你：{reminder.text}。這支結束前可以先不要開下一支。"
        elif mode == "工作":
            body = f"你正在工作。到時間了：{reminder.text}。先停一下處理，回來再接續目前工作。"
        elif mode == "聊天":
            body = f"你正在聊天。提醒事項到時間了：{reminder.text}。"
        elif mode == "閒置":
            body = f"提醒事項到時間了：{reminder.text}。"
        else:
            body = f"現在是 {mode} 模式。提醒事項到時間了：{reminder.text}。"

        return title, body

    def should_intervene(self, snapshot: ForegroundSnapshot) -> bool:
        return False

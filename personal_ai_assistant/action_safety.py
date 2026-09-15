from __future__ import annotations

from dataclasses import dataclass


LOW_RISK_ACTIONS = {
    "reminder",
    "activate_or_launch_app",
    "launch_app",
    "open_folder",
    "open_file",
    "launch_game",
    "open_url",
    "ask_ai",
    "set_goal",
    "set_mode",
}

MEDIUM_RISK_ACTIONS = {
    "close_app",
    "send_message",
    "auto_send_ai",
    "shell",
}

HIGH_RISK_ACTIONS = {
    "delete_file",
    "modify_system",
    "purchase",
}


@dataclass(frozen=True)
class ActionRisk:
    level: str
    requires_confirmation: bool
    reason: str


def classify_action_risk(intent: str, auto_send: bool = False) -> ActionRisk:
    normalized = (intent or "unknown").strip().lower()
    if normalized == "ask_ai" and auto_send:
        return ActionRisk("medium", True, "自動送出 AI prompt 需要先確認")
    if normalized in LOW_RISK_ACTIONS:
        return ActionRisk("low", False, "低風險本機動作")
    if normalized in MEDIUM_RISK_ACTIONS:
        return ActionRisk("medium", True, "這個動作可能替你送出或執行外部操作")
    if normalized in HIGH_RISK_ACTIONS:
        return ActionRisk("high", True, "這個動作可能造成不可逆變更")
    return ActionRisk("medium", True, "未知動作先確認")

from __future__ import annotations

from .reminder_trace import trace_reminder

import re
from dataclasses import dataclass
from datetime import datetime

from .action_safety import ActionRisk, classify_action_risk
from .integrations.ai_handoff import PROVIDER_URLS
from .reminders import ParsedReminder, parse_natural_reminder
from .file_resolver import DOCUMENT_EXTENSIONS


INTENTS = ("reminder_delete", "reminder_update", "reminder_undo", "reminder", "activate_or_launch_app", "launch_app", "ask_ai", "set_goal", "set_mode", "open_folder", "open_url", "unknown")


@dataclass(frozen=True)
class AssistantIntent:
    intent: str
    raw_text: str
    confidence: float
    target: str = ""
    payload: dict[str, object] | None = None
    parsed_reminder: ParsedReminder | None = None
    needs_confirmation: bool = False
    confirmation_summary: str = ""
    risk: ActionRisk = ActionRisk("medium", True, "未知動作先確認")


def route_assistant_command(
    raw_text: str,
    now: datetime | None = None,
    default_ai_provider: str = "chatgpt",
    active_hour_scores: dict[int, float] | None = None,
    user_is_active: bool = True,
    auto_send_ai: bool = False,
) -> AssistantIntent:
    text = " ".join((raw_text or "").strip().split())
    if not text:
        return _intent("unknown", text, 0.0, confirmation_summary="請輸入助理指令")

    from .reminder_operations import parse_operation
    operation = parse_operation(text)
    if operation:
        return _intent('reminder_' + operation['action'], text, .96, payload=operation)

    ask_ai = _parse_ask_ai(text, default_ai_provider)
    if ask_ai:
        provider, prompt = ask_ai
        return _intent(
            "ask_ai",
            text,
            0.94,
            target=provider,
            payload={"provider": provider, "prompt": prompt, "url": PROVIDER_URLS.get(provider, PROVIDER_URLS["chatgpt"])},
            risk=classify_action_risk("ask_ai", auto_send=auto_send_ai),
        )

    mode = _parse_mode(text)
    if mode:
        mode_name, minutes = mode
        return _intent("set_mode", text, 0.91, target=mode_name, payload={"mode": mode_name, "minutes": minutes})

    goal = _parse_goal(text)
    if goal:
        return _intent("set_goal", text, 0.91, target=goal, payload={"goal": goal})

    url = _parse_url(text)
    if url:
        return _intent("open_url", text, 0.92, target=url, payload={"url": url})

    folder = _parse_folder(text)
    if folder:
        return _intent("open_folder", text, 0.9, target=folder, payload={"query": folder})

    app = _parse_launch_app(text)
    if app:
        return _intent("activate_or_launch_app", text, 0.88, target=app, payload={"query": app})

    try:
        parsed = parse_natural_reminder(text, now, active_hour_scores, user_is_active)
        trace_reminder("intent", due_at=parsed.due_at, needs_confirmation=parsed.needs_confirmation)
        confidence = 0.72 if parsed.needs_confirmation else 0.9
        return _intent(
            "reminder",
            text,
            confidence,
            target=parsed.text,
            parsed_reminder=parsed,
            needs_confirmation=parsed.needs_confirmation,
            confirmation_summary=parsed.confirmation_summary or "",
        )
    except ValueError:
        pass

    return _intent(
        "unknown",
        text,
        0.15,
        needs_confirmation=True,
        confirmation_summary="我還不確定你要提醒、開程式、切模式、設定目標，還是交給 AI。",
    )


def _intent(
    intent: str,
    raw_text: str,
    confidence: float,
    target: str = "",
    payload: dict[str, object] | None = None,
    parsed_reminder: ParsedReminder | None = None,
    needs_confirmation: bool = False,
    confirmation_summary: str = "",
    risk: ActionRisk | None = None,
) -> AssistantIntent:
    return AssistantIntent(
        intent=intent if intent in INTENTS else "unknown",
        raw_text=raw_text,
        confidence=confidence,
        target=target,
        payload=payload or {},
        parsed_reminder=parsed_reminder,
        needs_confirmation=needs_confirmation or confidence < 0.7,
        confirmation_summary=confirmation_summary,
        risk=risk or classify_action_risk(intent),
    )


def _parse_ask_ai(text: str, default_provider: str = "chatgpt") -> tuple[str, str] | None:
    match = re.match(r"^(?:幫我)?問\s*(?P<provider>gpt|chatgpt|grok|claude|gemini)?\s*(?P<prompt>.+)$", text, re.I)
    if not match:
        match = re.match(r"^幫我問\s*(?P<provider>gpt|chatgpt|grok|claude|gemini)?\s*(?P<prompt>.+)$", text, re.I)
    if not match:
        return None
    prompt = (match.group("prompt") or "").strip(" ：:")
    if not prompt:
        return None
    provider = _normalize_provider(match.group("provider") or default_provider)
    return provider, prompt


def _normalize_provider(value: str) -> str:
    provider = value.strip().lower()
    if provider in {"gpt", "chatgpt"}:
        return "chatgpt"
    if provider in {"grok", "claude", "gemini"}:
        return provider
    return "chatgpt"


def _parse_launch_app(text: str) -> str | None:
    match = re.match(r"^(?:幫我)?(?:開|打開|啟動|切到|切換到|看)\s*(?P<app>.+?)(?:程式|app|App)?$", text, re.I)
    if not match:
        return None
    target = match.group("app").strip(" ：:")
    if target in {"網址", "網站", "資料夾"}:
        return None
    if _looks_like_folder(target) or _looks_like_url(target):
        return None
    return target or None


def _parse_folder(text: str) -> str | None:
    match = re.match(r"^(?:幫我)?(?:開|打開|啟動)\s*(?P<folder>.+?(?:資料夾|folder|Folder)?)$", text, re.I)
    if not match:
        return None
    target = match.group("folder").strip(" ：:")
    return target if _looks_like_folder(target) else None


def _looks_like_folder(target: str) -> bool:
    value = target.lower()
    return value in {"下載", "桌面", "文件", "documents", "downloads", "desktop", "專案"} or value.endswith(("資料夾", "folder"))


def _parse_url(text: str) -> str | None:
    match = re.match(r"^(?:開|打開)\s*(?P<url>https?://\S+|[\w.-]+\.[a-z]{2,}\S*)$", text, re.I)
    if not match:
        return None
    url = match.group("url").strip()
    if not _looks_like_url(url):
        return None
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url


def _looks_like_url(target: str) -> bool:
    if not target.lower().startswith(('http://', 'https://')) and any(target.lower().endswith(ext) for ext in DOCUMENT_EXTENSIONS):
        return False
    return bool(re.search(r"^https?://|[\w.-]+\.[a-z]{2,}", target, re.I))


def _parse_goal(text: str) -> str | None:
    patterns = (
        r"^今天目標是(?P<goal>.+)$",
        r"^目前目標是(?P<goal>.+)$",
        r"^設定目標(?P<goal>.+)$",
        r"^目標[:：]\s*(?P<goal>.+)$",
    )
    for pattern in patterns:
        match = re.match(pattern, text)
        if match:
            return match.group("goal").strip(" ：:")
    return None


def _parse_mode(text: str) -> tuple[str, int | None] | None:
    if re.search(r"(回到|開始|進入).*(工作|專注)|回來工作|回到工作", text):
        return ("work", None)
    if "私人時間" in text or "private time" in text.lower():
        return ("private_time", None)
    if "休息" in text or "暫不打擾" in text:
        minutes_match = re.search(r"(\d+)\s*分鐘", text)
        return ("intentional_break", int(minutes_match.group(1)) if minutes_match else None)
    return None

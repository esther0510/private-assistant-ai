from __future__ import annotations

import json
import random
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any

import requests

from .activity_sessions import (
    break_pattern_payload,
    break_threshold_seconds,
    infer_intent,
    is_break_session,
    is_private_session,
)
from .assistant.dead_loop import DeadLoopDetector
from .db import AssistantStore
from .models import ActivitySession, CurrentState, ForegroundSnapshot, Goal, NextAction, Reminder, UsageEvent
from .settings import AssistantSettings


GOAL_TYPES = ("long_term", "today", "current_session", "immediate_next")
ADVISOR_PROVIDER_PRESETS = {
    "openai": {
        "label": "OpenAI",
        "endpoint": "https://api.openai.com/v1",
        "key_url": "https://platform.openai.com/api-keys",
        "docs_url": "https://platform.openai.com/docs",
    },
    "groq": {
        "label": "Groq",
        "endpoint": "https://api.groq.com/openai/v1",
        "key_url": "https://console.groq.com/keys",
        "docs_url": "https://console.groq.com/docs",
    },
    "openrouter": {
        "label": "OpenRouter",
        "endpoint": "https://openrouter.ai/api/v1",
        "key_url": "https://openrouter.ai/settings/keys",
        "docs_url": "https://openrouter.ai/docs",
    },
    "custom": {
        "label": "Custom",
        "endpoint": "",
        "key_url": "",
        "docs_url": "",
    },
}
ADVISOR_CUSTOM_MODEL = "__custom_model__"
ADVISOR_DISABLED_SELECTION = "__advisor_disabled__"
ADVISOR_MODEL_RECOMMENDATIONS = {
    "openai": (
        ("GPT-4.1 mini｜通用/省額度", "gpt-4.1-mini"),
        ("GPT-4o mini｜快速日常分析", "gpt-4o-mini"),
    ),
    "groq": (
        ("GPT OSS 20B｜文字 Advisor 推薦", "openai/gpt-oss-20b"),
        ("Qwen 3.8 27B｜中文/推理推薦", "qwen/qwen3.8-27b"),
        ("Qwen 3.6 27B｜中文/推理備選", "qwen/qwen3.6-27b"),
        ("GPT OSS 120B｜高品質推理", "openai/gpt-oss-120b"),
    ),
    "openrouter": (
        ("Qwen 3.5 32B｜中文/推理推薦", "qwen/qwen-2.5-32b-instruct"),
        ("GPT OSS 120B｜通用推理", "openai/gpt-oss-120b"),
        ("Llama 3.3 70B｜快速省額度", "meta-llama/llama-3.3-70b-instruct"),
    ),
    "custom": (
        ("自訂服務預設模型", ""),
    ),
}
ADVISOR_HTTP_USER_AGENT = "PrivateAssistantAI/0.1.0-alpha"
ADVISOR_HEALTH_STATES = {"available", "degraded", "rate_limited", "auth_error", "disabled", "unknown"}
ADVISOR_TRANSIENT_DEGRADED_FAILURES = 2
ADVISOR_BACKOFF_BASE_SECONDS = 15
ADVISOR_BACKOFF_MAX_SECONDS = 300


@dataclass(frozen=True)
class AdvisorModelOption:
    label: str
    model_id: str


@dataclass(frozen=True)
class AdvisorHealth:
    state: str = "unknown"
    consecutive_failures: int = 0
    last_success_at: datetime | None = None
    last_failure_at: datetime | None = None
    retry_after_at: datetime | None = None
    message: str = "尚未測試"

    @property
    def can_try_now(self) -> bool:
        return not self.retry_after_at or self.retry_after_at <= datetime.now()


def advisor_recommended_models(preset: str) -> tuple[AdvisorModelOption, ...]:
    raw = ADVISOR_MODEL_RECOMMENDATIONS.get(preset, ADVISOR_MODEL_RECOMMENDATIONS["custom"])
    return tuple(AdvisorModelOption(label, model_id) for label, model_id in raw if label)


def advisor_recommended_model_labels(preset: str) -> dict[str, str]:
    return {option.model_id: option.label for option in advisor_recommended_models(preset) if option.model_id}


def advisor_models_url(endpoint_url: str) -> str:
    base = endpoint_url.strip().rstrip("/")
    if base.endswith("/models"):
        return base
    if base.endswith("/chat/completions"):
        base = base[: -len("/chat/completions")]
    return f"{base}/models"


def advisor_chat_completions_url(endpoint_url: str) -> str:
    base = endpoint_url.strip().rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    return f"{base}/chat/completions"


def sanitize_advisor_message(text: str, api_key: str = "") -> str:
    cleaned = text or ""
    if api_key:
        cleaned = cleaned.replace(api_key, "[API key hidden]")
    cleaned = re.sub(r"Bearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [API key hidden]", cleaned, flags=re.I)
    return cleaned[:500]


def advisor_request_headers(api_key: str = "") -> dict[str, str]:
    headers = {
        "Accept": "application/json",
        "User-Agent": ADVISOR_HTTP_USER_AGENT,
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def advisor_http_error_message(exc: Exception, api_key: str = "") -> str:
    if isinstance(exc, requests.HTTPError):
        response = exc.response
        status_code = response.status_code if response is not None else None
        body = response.text if response is not None else ""
        detail = ""
        if body:
            try:
                payload = json.loads(body)
                if isinstance(payload, dict):
                    error = payload.get("error")
                    if isinstance(error, dict):
                        detail = str(error.get("message") or error.get("code") or error)
                    else:
                        detail = str(payload.get("message") or payload)
                else:
                    detail = str(payload)
            except json.JSONDecodeError:
                detail = body
        detail = sanitize_advisor_message(detail or str(exc), api_key)
        status_text = f"HTTP {status_code}" if status_code else "HTTP 錯誤"
        return status_text + (f"：{detail}" if detail else "")
    if isinstance(exc, requests.RequestException):
        return f"網路錯誤：{sanitize_advisor_message(str(exc), api_key)}"
    return sanitize_advisor_message(str(exc) or type(exc).__name__, api_key)


def advisor_health_label(state: str) -> str:
    return {
        "available": "可用",
        "degraded": "連線不穩，使用本機規則",
        "rate_limited": "已限流，稍後重試",
        "auth_error": "API 驗證/權限問題，請檢查 Key 或服務權限",
        "disabled": "未啟用",
        "unknown": "尚未測試",
    }.get(state, "尚未測試")


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def advisor_load_health(store: AssistantStore, enabled: bool = True) -> AdvisorHealth:
    if not enabled:
        return AdvisorHealth(state="disabled", message=advisor_health_label("disabled"))
    raw_state = str(store.get_setting("advisor_health_state", "") or "unknown")
    state = raw_state if raw_state in ADVISOR_HEALTH_STATES else "unknown"
    try:
        failures = int(store.get_setting("advisor_consecutive_failures", "0") or "0")
    except ValueError:
        failures = 0
    message = store.get_setting("advisor_health_message", "") or advisor_health_label(state)
    return AdvisorHealth(
        state=state,
        consecutive_failures=max(0, failures),
        last_success_at=_parse_datetime(store.get_setting("advisor_last_success_at", "")),
        last_failure_at=_parse_datetime(store.get_setting("advisor_last_failure_at", "")),
        retry_after_at=_parse_datetime(store.get_setting("advisor_retry_after_at", "")),
        message=message,
    )


def advisor_record_success(store: AssistantStore, now: datetime | None = None, message: str = "最近呼叫成功") -> AdvisorHealth:
    now = now or datetime.now()
    store.set_setting("advisor_health_state", "available")
    store.set_setting("advisor_consecutive_failures", "0")
    store.set_setting("advisor_last_success_at", now.isoformat())
    store.delete_setting("advisor_retry_after_at")
    store.set_setting("advisor_health_message", message)
    store.set_setting("advisor_api_last_ok", "1")
    return advisor_load_health(store, True)


def advisor_reset_health(store: AssistantStore) -> None:
    for key in (
        "advisor_health_state",
        "advisor_consecutive_failures",
        "advisor_last_failure_at",
        "advisor_retry_after_at",
        "advisor_health_message",
        "advisor_api_last_ok",
    ):
        store.delete_setting(key)


def _http_status_code(exc: Exception) -> int | None:
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        return int(exc.response.status_code)
    return None


def _retry_after_from_response(response: requests.Response | None, now: datetime) -> datetime | None:
    if response is None:
        return None
    raw = response.headers.get("Retry-After", "").strip()
    if not raw:
        return None
    if raw.isdigit():
        return now + timedelta(seconds=max(1, int(raw)))
    try:
        parsed = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed


def advisor_record_failure(
    store: AssistantStore,
    exc: Exception,
    api_key: str = "",
    now: datetime | None = None,
) -> AdvisorHealth:
    now = now or datetime.now()
    previous = advisor_load_health(store, True)
    status_code = _http_status_code(exc)
    message = advisor_http_error_message(exc, api_key)

    if status_code in {401, 403}:
        state = "auth_error"
        failures = previous.consecutive_failures + 1
        retry_at = None
        health_message = "API 驗證/權限問題，請檢查 Key 或服務權限"
        store.set_setting("advisor_api_last_ok", "0")
    elif status_code == 429:
        state = "rate_limited"
        failures = previous.consecutive_failures + 1
        retry_at = _retry_after_from_response(exc.response if isinstance(exc, requests.HTTPError) else None, now)
        if not retry_at:
            retry_at = now + timedelta(seconds=60)
        health_message = "已達速率限制，稍後自動重試"
    else:
        failures = previous.consecutive_failures + 1
        state = "degraded" if failures >= ADVISOR_TRANSIENT_DEGRADED_FAILURES else (previous.state if previous.state == "available" else "unknown")
        backoff = min(ADVISOR_BACKOFF_MAX_SECONDS, ADVISOR_BACKOFF_BASE_SECONDS * (2 ** max(0, failures - 1)))
        jitter = random.uniform(0, min(5, backoff * 0.2))
        retry_at = now + timedelta(seconds=backoff + jitter)
        health_message = "連線不穩，使用本機規則" if state == "degraded" else "暫時異常，稍後自動重試"

    store.set_setting("advisor_health_state", state)
    store.set_setting("advisor_consecutive_failures", str(failures))
    store.set_setting("advisor_last_failure_at", now.isoformat())
    store.set_setting("advisor_health_message", health_message if not message else f"{health_message}：{message}")
    if retry_at:
        store.set_setting("advisor_retry_after_at", retry_at.isoformat())
    else:
        store.delete_setting("advisor_retry_after_at")
    return advisor_load_health(store, True)


def _state_payload(state: CurrentState) -> dict[str, Any]:
    return {
        "current_mode": state.current_mode,
        "foreground_app": state.foreground_app,
        "window_title_summary": state.window_title_summary,
        "activity_summary": state.activity_summary,
        "activity_started_at": state.activity_started_at.isoformat(),
        "updated_at": state.updated_at.isoformat(),
        "intent": state.intent,
        "intent_source": state.intent_source,
        "session_category": state.session_category,
        "session_started_at": state.session_started_at.isoformat() if state.session_started_at else None,
        "session_duration_seconds": state.session_duration_seconds,
        "previous_work_session_seconds": state.previous_work_session_seconds,
        "break_pattern": state.break_pattern or {},
        "recent_apps": list(state.recent_apps),
        "recent_events": list(state.recent_events),
        "active_reminders": list(state.active_reminders),
        "away": state.away,
        "manual_stop": state.manual_stop,
        "privacy_mode": state.privacy_mode,
    }


def _goal_payload(goal: Goal | None) -> dict[str, Any] | None:
    if not goal:
        return None
    return {
        "goal_type": goal.goal_type,
        "text": goal.text,
        "source": goal.source,
        "confidence": goal.confidence,
        "status": goal.status,
        "created_at": goal.created_at.isoformat(),
        "updated_at": goal.updated_at.isoformat(),
    }


def summarize_activity(app_name: str, window_title: str, mode: str) -> str:
    app = (app_name or "").lower()
    title = " ".join((window_title or "").split())
    lower = f"{app} {title.lower()}"

    if "chatgpt" in lower:
        if any(token in lower for token in ("assistant", "助理", "private_assistant", "mvp", "codex", "開發", "bug")):
            return "正在使用 ChatGPT 討論私人助理開發"
        return "正在使用 ChatGPT 討論問題"
    if "telegram" in app or "telegram" in lower:
        if any(token in lower for token in ("stock", "股票", "台股", "美股", "投資", "crypto", "幣")):
            return "正在 Telegram 看股票或投資相關資訊"
        return "正在 Telegram 查看訊息"
    if any(token in app for token in ("steam", "epicgameslauncher", "riotclient", "helldivers", "game")) or mode == "遊戲":
        return "正在玩遊戲"
    if any(token in app for token in ("code", "pycharm", "cursor", "devenv")):
        if any(token in lower for token in ("error", "failed", "exception", "traceback", "錯誤", "失敗")):
            return "正在開發並處理錯誤"
        return "正在寫程式或檢查專案"
    if any(token in app for token in ("chrome", "edge", "firefox")):
        if "youtube" in lower:
            return "正在 YouTube 看影片"
        return "正在瀏覽網頁"
    if mode == "閒置":
        return "目前離席或閒置"
    return f"正在{mode}：{app_name or '未知 App'}"


class CurrentStateBuilder:
    def __init__(self, store: AssistantStore) -> None:
        self.store = store
        self._last_signature: tuple[object, ...] | None = None

    def build(
        self,
        snapshot: ForegroundSnapshot,
        activity_started_at: datetime,
        away: bool,
        manual_stop: bool,
        privacy_mode: bool,
        current_session: ActivitySession | None = None,
        explicit_intent: str | None = None,
        explicit_intent_until: datetime | None = None,
    ) -> CurrentState:
        title = self.store.summarize_text(snapshot.window_title, 80)
        safe_app = snapshot.app_name if self.store.should_record_app(snapshot.app_name, snapshot.window_title) and not privacy_mode else "[隱私 App]"
        safe_title = title if safe_app != "[隱私 App]" else "[隱私內容已略過]"
        recent_events = self.store.recent_assistant_events(12)
        recent_apps = []
        for event in recent_events:
            if event.app_name and event.app_name not in recent_apps and self.store.should_record_app(event.app_name, event.window_title_summary):
                recent_apps.append(event.app_name)
        active_reminders = [
            self.store.summarize_text(reminder.text, 60)
            for reminder in self.store.list_reminders(20)
            if reminder.status in {"pending", "notified", "pending_due", "snoozed"}
        ][:5]
        event_summaries = [
            f"{event.timestamp.strftime('%H:%M')} {event.event_type} {event.app_name or ''}".strip()
            for event in recent_events[:8]
        ]
        activity = "隱私模式中，暫停分析活動內容" if privacy_mode else summarize_activity(snapshot.app_name, snapshot.window_title, snapshot.mode)
        session_started_at = current_session.started_at if current_session else activity_started_at
        session_duration_seconds = (
            current_session.duration_seconds
            if current_session
            else max(0, int((snapshot.timestamp - activity_started_at).total_seconds()))
        )
        previous_work = self.store.previous_activity_session(before=session_started_at, modes=("工作",)) if current_session else None
        previous_work_seconds = previous_work.duration_seconds if previous_work else 0
        pattern_payload = break_pattern_payload(self.store.learned_pattern("break_pattern", "global", "work_break"))
        intent, intent_source = infer_intent(
            current_session,
            explicit_intent,
            explicit_intent_until,
            snapshot.timestamp,
            previous_work_seconds=previous_work_seconds,
        )
        if current_session and current_session.mode == "工作" and intent_source != "explicit":
            previous_any = self.store.previous_activity_session(before=current_session.started_at)
            if (is_break_session(previous_any) or is_private_session(previous_any)) and previous_any and previous_any.ended_at:
                if current_session.started_at - previous_any.ended_at <= timedelta(minutes=10):
                    intent = "returning_to_work"
        return CurrentState(
            current_mode=snapshot.mode,
            foreground_app=safe_app,
            window_title_summary=safe_title,
            activity_summary=activity,
            activity_started_at=activity_started_at,
            updated_at=snapshot.timestamp,
            intent=intent,
            intent_source=intent_source,
            session_category=current_session.category if current_session else "general",
            session_started_at=session_started_at,
            session_duration_seconds=session_duration_seconds,
            previous_work_session_seconds=previous_work_seconds,
            break_pattern=pattern_payload,
            recent_apps=tuple(recent_apps[:8]),
            recent_events=tuple(event_summaries),
            active_reminders=tuple(active_reminders),
            away=away,
            manual_stop=manual_stop,
            privacy_mode=privacy_mode,
        )

    def save_if_changed(self, state: CurrentState) -> bool:
        signature = (
            state.current_mode,
            state.foreground_app,
            state.window_title_summary,
            state.activity_summary,
            state.activity_started_at.replace(microsecond=0),
            state.intent,
            state.intent_source,
            state.session_category,
            state.session_started_at.replace(microsecond=0) if state.session_started_at else None,
            int(state.session_duration_seconds / 30),
            state.previous_work_session_seconds,
            state.away,
            state.manual_stop,
            state.privacy_mode,
            state.recent_apps,
            state.active_reminders,
        )
        if signature == self._last_signature:
            return False
        self._last_signature = signature
        self.store.save_current_state(state)
        return True


class GoalEngine:
    def __init__(self, store: AssistantStore) -> None:
        self.store = store

    def set_manual_goal(self, text: str, goal_type: str = "current_session") -> Goal:
        if goal_type not in GOAL_TYPES:
            goal_type = "current_session"
        return self.store.upsert_goal(goal_type, text, "manual", 1.0)

    def clear_manual_goal(self, goal_type: str = "current_session") -> None:
        self.store.clear_goal(goal_type, "manual")

    def infer_goal(self, state: CurrentState) -> Goal | None:
        if state.privacy_mode:
            return None
        if state.active_reminders:
            return self.store.upsert_inferred_goal("immediate_next", f"處理提醒：{state.active_reminders[0]}", 0.55)
        if "私人助理" in state.activity_summary or "開發" in state.activity_summary:
            return self.store.upsert_inferred_goal("current_session", "推測正在處理私人助理專案", 0.45)
        if "股票" in state.activity_summary:
            return self.store.upsert_inferred_goal("current_session", "推測正在查看投資資訊", 0.35)
        return None


class ContextBuilder:
    def __init__(self, store: AssistantStore) -> None:
        self.store = store

    def build(
        self,
        state: CurrentState,
        goal: Goal | None,
        settings: AssistantSettings,
        minutes: int | None = None,
    ) -> dict[str, Any]:
        window_minutes = minutes or settings.advisor_context_minutes
        if state.privacy_mode:
            recent_usage: list[UsageEvent] = []
            recent_sessions: list[dict[str, Any]] = []
            recent_events = []
        else:
            reference_now = state.updated_at
            start = reference_now - timedelta(minutes=max(10, min(30, window_minutes)))
            recent_usage = [
                event
                for event in self.store.usage_events_between(start, reference_now)
                if self.store.should_record_app(event.app_name, event.window_title)
            ]
            recent_sessions = [
                {
                    "started_at": session.started_at.strftime("%H:%M"),
                    "last_seen_at": session.last_seen_at.strftime("%H:%M"),
                    "mode": session.mode,
                    "category": session.category,
                    "app": session.app_name,
                    "duration_seconds": session.duration_seconds,
                    "title_summary": session.title_summary,
                }
                for session in self.store.activity_sessions_between(start, reference_now)
                if self.store.should_record_app(session.app_name, session.title_summary)
            ]
            recent_events = [
                {
                    "time": event.timestamp.strftime("%H:%M"),
                    "type": event.event_type,
                    "mode": event.context_mode,
                    "app": event.app_name if self.store.should_record_app(event.app_name, event.window_title_summary) else "[excluded]",
                    "title_summary": event.window_title_summary
                    if self.store.should_record_app(event.app_name, event.window_title_summary)
                    else "[excluded]",
                }
                for event in self.store.recent_assistant_events(30)
                if self.store.should_record_app(event.app_name, event.window_title_summary)
            ]
        compact_usage = []
        last_key = None
        for event in recent_usage:
            key = (event.mode, event.app_name, self.store.summarize_text(event.window_title, 60))
            if key == last_key:
                continue
            last_key = key
            compact_usage.append(
                {
                    "time": event.timestamp.strftime("%H:%M"),
                    "mode": event.mode,
                    "app": event.app_name,
                    "title_summary": self.store.summarize_text(event.window_title, 60),
                }
            )
        next_reminders = [
            {
                "text": self.store.summarize_text(reminder.text, 60),
                "due_at": reminder.due_at.isoformat(),
                "status": reminder.status,
                "importance": reminder.importance,
            }
            for reminder in self.store.list_reminders(20)
            if reminder.status in {"pending", "snoozed", "pending_due", "notified"}
        ][:8]
        learned_patterns = [
            {
                "type": pattern.pattern_type,
                "summary": pattern.reason or pattern.key,
                "confidence": round(pattern.confidence, 2),
                "samples": pattern.sample_count,
            }
            for pattern in self.store.learned_patterns(limit=12)
            if pattern.status != "rejected"
        ]
        loop_snapshot = ForegroundSnapshot(datetime.now(), state.foreground_app, state.window_title_summary, state.current_mode, 0)
        dead_loop_score = DeadLoopDetector().assess(loop_snapshot, self.store.recent_assistant_events(80)).risk_score
        return {
            "current_state": _state_payload(state),
            "goal": _goal_payload(goal),
            "recent_events": recent_events[:20],
            "recent_activity": compact_usage[-20:],
            "recent_sessions": recent_sessions[-20:],
            "current_intent": state.intent,
            "current_session_duration_seconds": state.session_duration_seconds,
            "previous_work_session_duration_seconds": state.previous_work_session_seconds,
            "typical_break_pattern": state.break_pattern or {},
            "learned_patterns": learned_patterns,
            "next_reminders": next_reminders,
            "dead_loop_score": dead_loop_score,
            "privacy": {
                "privacy_mode": state.privacy_mode,
                "excluded_apps_respected": True,
                "content_policy": "metadata_and_summaries_only",
            },
        }


class InterventionPolicy:
    def evaluate(
        self,
        action: NextAction,
        state: CurrentState,
        pending_reminders: list[Reminder],
        dead_loop_score: float,
    ) -> NextAction:
        urgent = any(reminder.importance == "critical" and reminder.status in {"pending_due", "notified"} for reminder in pending_reminders)
        score = action.confidence
        if dead_loop_score >= 0.72:
            score += 0.18
        if urgent:
            score += 0.12
        if state.away or state.manual_stop or state.current_mode == "遊戲":
            score -= 0.18
        if state.intent in {"intentional_break", "private_time"}:
            score -= 0.16
        if action.should_interrupt:
            score += 0.1
        if score >= 0.9 and not state.away and not state.privacy_mode:
            level = "popup"
        elif score >= 0.55:
            level = "status_only"
        else:
            level = "silent"
        return NextAction(
            action_text=action.action_text,
            reason=action.reason,
            confidence=max(0.0, min(1.0, action.confidence)),
            source=action.source,
            should_interrupt=level == "popup",
            intervention_level=level,
            understanding=action.understanding,
            questions=action.questions,
            provider_label=action.provider_label,
        )


class NextActionEngine:
    def __init__(self, store: AssistantStore, dead_loop_detector: DeadLoopDetector | None = None) -> None:
        self.store = store
        self.dead_loop_detector = dead_loop_detector or DeadLoopDetector()
        self.policy = InterventionPolicy()
        self._last_return_goal_signature: tuple[str, str] | None = None
        self._last_return_goal_at: datetime | None = None

    def evaluate(self, state: CurrentState, goal: Goal | None, now: datetime | None = None) -> NextAction:
        now = now or datetime.now()
        pending = [reminder for reminder in self.store.list_reminders(20) if reminder.status in {"pending", "snoozed", "pending_due", "notified"}]
        completed_recently = [
            reminder
            for reminder in self.store.list_reminders(20)
            if reminder.status == "completed" and reminder.completed_at and now - reminder.completed_at <= timedelta(minutes=30)
        ]
        snapshot = ForegroundSnapshot(now, state.foreground_app, state.window_title_summary, state.current_mode, 0)
        dead_loop = self.dead_loop_detector.assess(snapshot, self.store.recent_assistant_events(80), now)

        action = NextAction(
            action_text="持續觀察，目前沒有需要打斷你的建議",
            reason="本機規則沒有偵測到高信心偏離、重複錯誤或待處理提醒",
            confidence=0.25,
            source="rule",
            understanding=state.activity_summary,
        )
        due_pending = [reminder for reminder in pending if reminder.status in {"notified", "pending_due"}]
        upcoming = [reminder for reminder in pending if reminder.due_at <= now + timedelta(minutes=15)]
        if due_pending:
            next_due = sorted(due_pending, key=lambda item: item.due_at)[0]
            action = NextAction(
                action_text=f"確認提醒是否已處理：{next_due.text}",
                reason="提醒已送出或離席期間到期，尚未看到完成紀錄",
                confidence=0.72,
                source="rule",
                understanding=state.activity_summary,
            )
        elif upcoming:
            next_due = sorted(upcoming, key=lambda item: item.due_at)[0]
            action = NextAction(
                action_text=f"準備處理接下來的提醒：{next_due.text}",
                reason="有正式提醒即將到期",
                confidence=0.58,
                source="rule",
                understanding=state.activity_summary,
            )
        elif dead_loop.risk_score >= 0.72:
            action = NextAction(
                action_text="先停下重複嘗試，重新確認問題假設與最小驗證步驟",
                reason="；".join(dead_loop.reasons) or "本機死循環分數偏高",
                confidence=max(0.78, dead_loop.risk_score),
                source="rule",
                should_interrupt=False,
                understanding=state.activity_summary,
            )
        elif state.intent == "intentional_break":
            threshold = break_threshold_seconds(state.break_pattern or {})
            urgent_goal = goal and (goal.goal_type in {"immediate_next", "today"} or goal.confidence >= 0.85)
            if state.session_duration_seconds >= threshold and urgent_goal:
                action = NextAction(
                    action_text=f"如果休息夠了，可以回到目前目標：{goal.text}" if goal else "如果休息夠了，可以回到下一個小步驟",
                    reason="休息已超過平常或預設區間，且目前目標有較高優先度",
                    confidence=0.56,
                    source="rule",
                    understanding=state.activity_summary,
                )
            else:
                action = NextAction(
                    action_text="休息中，暫不打擾",
                    reason="目前像是正常休息或私人緩衝，沒有足夠 urgency 需要催回目標",
                    confidence=0.36,
                    source="rule",
                    understanding=state.activity_summary,
                )
        elif state.intent == "private_time":
            action = NextAction(
                action_text="目前像是私人時間，暫不催回工作",
                reason="使用者明確或長時間處於娛樂/私人活動，降低目標追蹤干擾",
                confidence=0.5,
                source="rule",
                understanding=state.activity_summary,
            )
        elif completed_recently and not pending:
            action = NextAction(
                action_text="剛完成提醒，可以回到原本目標或設定下一個小步驟",
                reason="最近 30 分鐘有提醒已完成，且沒有同一事項仍待處理",
                confidence=0.62,
                source="rule",
                understanding=state.activity_summary,
            )
        elif state.intent == "returning_to_work" and goal:
            action = NextAction(
                action_text=f"已回到工作狀態，可以接續目前目標：{goal.text}",
                reason="偵測到已回到工作 app，恢復一般目標追蹤",
                confidence=0.52,
                source="rule",
                understanding=state.activity_summary,
            )
        elif goal and self._deviates_from_goal(goal.text, state):
            if self._return_goal_on_cooldown(goal.text, state, now):
                action = NextAction(
                    action_text="持續觀察，目前沒有新的目標提醒",
                    reason="同一個回到目標建議剛出現過，先避免重複刷新",
                    confidence=0.3,
                    source="rule",
                    understanding=state.activity_summary,
                )
            else:
                action = NextAction(
                    action_text=f"回到目前目標：{goal.text}",
                    reason=f"已設定目標，但目前活動看起來偏向「{state.activity_summary}」",
                    confidence=0.66 if goal.source == "manual" else 0.48,
                    source="rule",
                    understanding=state.activity_summary,
                )
                self._mark_return_goal_notice(goal.text, state, now)

        result = self.policy.evaluate(action, state, pending, dead_loop.risk_score)
        self.store.log_advisor_intervention(result, {"dead_loop_score": dead_loop.risk_score})
        return result

    def _deviates_from_goal(self, goal_text: str, state: CurrentState) -> bool:
        goal = goal_text.lower()
        activity = state.activity_summary.lower()
        if state.away or state.privacy_mode or state.intent in {"intentional_break", "private_time"}:
            return False
        if state.session_category.startswith(("video_", "social_", "chat_")):
            threshold = break_threshold_seconds(state.break_pattern or {})
            if state.session_duration_seconds < threshold:
                return False
        if any(token in goal for token in ("提醒", "通知", "助理", "bug", "開發", "測試")):
            return not any(token in activity for token in ("助理", "開發", "程式", "錯誤", "chatgpt"))
        if any(token in goal for token in ("股票", "投資")):
            return "股票" not in activity and "投資" not in activity
        if any(token in goal for token in ("遊戲", "休息")):
            return "遊戲" not in activity
        return False

    def _return_goal_signature(self, goal_text: str, state: CurrentState) -> tuple[str, str]:
        return (goal_text.strip().lower(), state.session_category or state.activity_summary)

    def _return_goal_on_cooldown(self, goal_text: str, state: CurrentState, now: datetime) -> bool:
        signature = self._return_goal_signature(goal_text, state)
        return bool(
            self._last_return_goal_signature == signature
            and self._last_return_goal_at
            and now - self._last_return_goal_at < timedelta(minutes=10)
        )

    def _mark_return_goal_notice(self, goal_text: str, state: CurrentState, now: datetime) -> None:
        self._last_return_goal_signature = self._return_goal_signature(goal_text, state)
        self._last_return_goal_at = now


class AdvisorProvider:
    source = "rule"

    def advise(self, context: dict[str, Any]) -> NextAction:
        raise NotImplementedError


class OpenAICompatibleAdvisorProvider(AdvisorProvider):
    source = "ai"

    def __init__(self, endpoint_url: str, model: str, api_key: str, timeout_seconds: int = 20) -> None:
        self.endpoint_url = endpoint_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds

    def advise(self, context: dict[str, Any]) -> NextAction:
        url = advisor_chat_completions_url(self.endpoint_url)
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Return JSON only with keys: understanding, next_action, reason, "
                        "confidence, should_interrupt, questions. Be conservative about interruption. "
                        "Use Traditional Chinese for understanding, next_action, reason, and questions "
                        "unless the user's current language is clearly different. "
                        "Do not push the user back to a goal during normal intentional_break. "
                        "Only suggest returning to work when evidence shows the break is longer than usual "
                        "or a reminder/deadline has real urgency. Keep private_time low-interruption."
                    ),
                },
                {"role": "user", "content": json.dumps(context, ensure_ascii=False)},
            ],
        }
        if self._supports_temperature():
            payload["temperature"] = 0.2
        response = requests.post(
            url,
            json=payload,
            headers={**advisor_request_headers(self.api_key), "Content-Type": "application/json"},
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        raw = response.json()
        content = raw["choices"][0]["message"]["content"]
        return parse_advisor_response(content, source="ai")

    def test_connection(self) -> tuple[bool, str]:
        url = advisor_chat_completions_url(self.endpoint_url)
        payload = {
            "model": self.model,
            "messages": [
                {"role": "user", "content": "ping"},
            ],
            "max_tokens": 8,
        }
        try:
            response = requests.post(
                url,
                json=payload,
                headers={**advisor_request_headers(self.api_key), "Content-Type": "application/json"},
                timeout=min(10, self.timeout_seconds),
            )
            response.raise_for_status()
            response.json()
            return True, "連線成功"
        except Exception as exc:
            return False, f"連線失敗：{advisor_http_error_message(exc, self.api_key)}"

    def list_models(self) -> tuple[AdvisorModelOption, ...]:
        response = requests.get(
            advisor_models_url(self.endpoint_url),
            headers=advisor_request_headers(self.api_key),
            timeout=min(10, self.timeout_seconds),
        )
        response.raise_for_status()
        raw = response.json()
        data = raw.get("data") if isinstance(raw, dict) else None
        if not isinstance(data, list):
            return ()
        options = []
        for item in data:
            if isinstance(item, dict):
                model_id = str(item.get("id") or "").strip()
            else:
                model_id = str(item).strip()
            if model_id:
                options.append(AdvisorModelOption(model_id, model_id))
        return tuple(options)

    def _supports_temperature(self) -> bool:
        lower = self.endpoint_url.lower()
        return "groq.com" not in lower


def parse_advisor_response(raw: str | dict[str, Any], source: str = "ai") -> NextAction:
    payload: dict[str, Any]
    if isinstance(raw, dict):
        payload = raw
    else:
        text = raw.strip()
        match = re.search(r"\{.*\}", text, flags=re.S)
        payload = json.loads(match.group(0) if match else text)
    questions = payload.get("questions") or ()
    if isinstance(questions, str):
        questions = (questions,)
    return NextAction(
        action_text=str(payload.get("next_action") or payload.get("action_text") or "持續觀察"),
        reason=str(payload.get("reason") or "AI advisor 回傳結構化建議"),
        confidence=max(0.0, min(1.0, float(payload.get("confidence") or 0))),
        source=source,
        should_interrupt=bool(payload.get("should_interrupt", False)),
        understanding=str(payload.get("understanding") or ""),
        questions=tuple(str(item) for item in questions if str(item).strip()),
    )


def advisor_provider_label(preset: str, endpoint: str = "", model: str = "") -> str:
    base = ADVISOR_PROVIDER_PRESETS.get(preset, ADVISOR_PROVIDER_PRESETS["custom"])["label"]
    if preset == "custom" and endpoint:
        base = "Custom"
    return f"{base} / {model}" if model else base


def advisor_key_url(preset: str) -> str:
    return str(ADVISOR_PROVIDER_PRESETS.get(preset, ADVISOR_PROVIDER_PRESETS["custom"]).get("key_url") or "")


def advisor_preset_endpoint(preset: str) -> str:
    return str(ADVISOR_PROVIDER_PRESETS.get(preset, ADVISOR_PROVIDER_PRESETS["custom"]).get("endpoint") or "")


class AdvisorService:
    def __init__(
        self,
        store: AssistantStore,
        settings_service: object,
        next_action: NextActionEngine,
        context_builder: ContextBuilder,
        provider_factory: object | None = None,
    ) -> None:
        self.store = store
        self.settings_service = settings_service
        self.next_action = next_action
        self.context_builder = context_builder
        self.provider_factory = provider_factory

    def analyze(self, state: CurrentState, goal: Goal | None, force_ai: bool = False) -> NextAction:
        settings = self.settings_service.load()
        fallback = self.next_action.evaluate(state, goal)
        provider_label = advisor_provider_label(
            settings.advisor_provider_preset,
            settings.advisor_endpoint_url,
            settings.advisor_model_name,
        )
        if not settings.advisor_api_enabled and not force_ai:
            return fallback
        api_key = self.store.get_setting("advisor_api_key", "") or ""
        if not api_key.strip() or not settings.advisor_endpoint_url.strip() or not settings.advisor_model_name.strip():
            return self._unavailable(fallback, "缺少 endpoint、model 或 API key", provider_label, "unknown")
        health = advisor_load_health(self.store, True)
        if health.state == "auth_error" and not force_ai:
            return self._unavailable(fallback, health.message, provider_label, health.state)
        if health.retry_after_at and health.retry_after_at > datetime.now() and not force_ai:
            return self._unavailable(fallback, health.message, provider_label, health.state)
        context = self.context_builder.build(state, goal, settings)
        try:
            provider = (
                self.provider_factory(settings.advisor_endpoint_url, settings.advisor_model_name, api_key)
                if self.provider_factory
                else OpenAICompatibleAdvisorProvider(settings.advisor_endpoint_url, settings.advisor_model_name, api_key)
            )
            ai_action = provider.advise(context)
        except (OSError, KeyError, ValueError, json.JSONDecodeError, requests.RequestException) as exc:
            health = advisor_record_failure(self.store, exc, api_key)
            return self._unavailable(fallback, health.message, provider_label, health.state)
        policy = InterventionPolicy()
        pending = [reminder for reminder in self.store.list_reminders(20) if reminder.status in {"pending", "snoozed", "pending_due", "notified"}]
        result = policy.evaluate(ai_action, state, pending, float(context.get("dead_loop_score", 0) or 0))
        result = NextAction(
            result.action_text,
            result.reason,
            result.confidence,
            "ai",
            result.should_interrupt,
            result.intervention_level,
            result.understanding,
            result.questions,
            provider_label,
            "available",
            "最近呼叫成功",
        )
        advisor_record_success(self.store)
        self.store.log_advisor_intervention(result, {"provider": "openai_compatible"})
        return result

    def _unavailable(self, fallback: NextAction, reason: str, provider_label: str, health_state: str = "unknown") -> NextAction:
        return NextAction(
            fallback.action_text,
            f"AI 暫時不可用，暫用本機規則：{reason}；{fallback.reason}",
            fallback.confidence,
            "ai_unavailable",
            fallback.should_interrupt,
            fallback.intervention_level,
            fallback.understanding,
            fallback.questions,
            provider_label,
            health_state,
            reason,
        )

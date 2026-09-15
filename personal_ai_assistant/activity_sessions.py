from __future__ import annotations

import json
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from .models import ActivitySession, ForegroundSnapshot, LearnedPattern


WORK_MODES = {"工作"}
BREAK_MODES = {"看片", "瀏覽"}
PRIVATE_MODES = {"遊戲"}
CHAT_MODES = {"聊天"}
DEFAULT_BREAK_MIN_SECONDS = 15 * 60
DEFAULT_BREAK_MAX_SECONDS = 25 * 60
PRIVATE_TIME_AFTER_SECONDS = 90 * 60
PATTERN_MIN_SAMPLES = 3
PATTERN_MIN_CONFIDENCE = 0.6


def activity_category(app_name: str, window_title: str, mode: str) -> str:
    app = (app_name or "").lower()
    title = (window_title or "").lower()
    combined = f"{app} {title}"
    if mode == "工作":
        if "chatgpt" in combined:
            return "work_ai"
        if any(token in combined for token in ("code", "pycharm", "cursor", "github", "docs", "stackoverflow", "jira")):
            return "work_development"
        return "work"
    if mode == "看片":
        if any(token in combined for token in ("youtube", "shorts", "short")):
            return "video_youtube"
        if any(token in combined for token in ("twitch", "netflix", "bilibili")):
            return "video_streaming"
        return "video"
    if mode == "聊天":
        return "chat"
    if mode == "遊戲":
        return "game"
    if mode == "閒置":
        return "away"
    if mode == "瀏覽":
        return "browsing"
    return "general"


def session_identity(snapshot: ForegroundSnapshot) -> tuple[str, str, str]:
    return (
        (snapshot.app_name or "unknown").lower(),
        snapshot.mode,
        activity_category(snapshot.app_name, snapshot.window_title, snapshot.mode),
    )


def is_work_session(session: ActivitySession | None) -> bool:
    return bool(session and session.mode in WORK_MODES)


def is_break_session(session: ActivitySession | None) -> bool:
    return bool(session and (session.mode in BREAK_MODES or session.category.startswith("video")))


def is_private_session(session: ActivitySession | None) -> bool:
    return bool(session and session.mode in PRIVATE_MODES)


@dataclass(frozen=True)
class SessionUpdate:
    session: ActivitySession
    started: bool = False
    ended: ActivitySession | None = None
    resumed: bool = False


class ActivitySessionManager:
    def __init__(self, store: Any, grace_period_seconds: int = 90) -> None:
        self.store = store
        self.grace_period_seconds = max(30, min(120, int(grace_period_seconds)))
        self.current_session: ActivitySession | None = None
        self.last_ended_session: ActivitySession | None = None

    def observe(self, snapshot: ForegroundSnapshot, privacy_mode: bool = False) -> SessionUpdate:
        safe_title = "[隱私內容已略過]" if privacy_mode else self.store.summarize_text(snapshot.window_title, 80)
        identity = session_identity(snapshot)
        if self.current_session and self._identity_for_session(self.current_session) == identity:
            session = self.store.touch_activity_session(self.current_session.id, snapshot.timestamp, safe_title, snapshot.idle_seconds)
            self.current_session = session
            return SessionUpdate(session=session)

        ended = self.current_session
        if ended:
            ended = self.store.end_activity_session(ended.id, snapshot.timestamp)
            self.last_ended_session = ended
            self._learn_break_pattern_if_ready(ended, snapshot)

        if self._can_resume_last(identity, snapshot.timestamp):
            session = self.store.reopen_activity_session(self.last_ended_session.id, snapshot.timestamp, safe_title, snapshot.idle_seconds)
            self.current_session = session
            return SessionUpdate(session=session, ended=ended, resumed=True)

        session = self.store.start_activity_session(
            started_at=snapshot.timestamp,
            app_name=snapshot.app_name,
            mode=snapshot.mode,
            category=identity[2],
            title_summary=safe_title,
            metadata={"idle_seconds": snapshot.idle_seconds},
        )
        self.current_session = session
        return SessionUpdate(session=session, started=True, ended=ended)

    def _identity_for_session(self, session: ActivitySession) -> tuple[str, str, str]:
        return ((session.app_name or "unknown").lower(), session.mode, session.category)

    def _can_resume_last(self, identity: tuple[str, str, str], now: datetime) -> bool:
        if not self.last_ended_session:
            return False
        ended_at = self.last_ended_session.ended_at or self.last_ended_session.last_seen_at
        return self._identity_for_session(self.last_ended_session) == identity and now - ended_at <= timedelta(seconds=self.grace_period_seconds)

    def _learn_break_pattern_if_ready(self, ended: ActivitySession, next_snapshot: ForegroundSnapshot) -> None:
        if not is_break_session(ended) or next_snapshot.mode not in WORK_MODES:
            return
        previous_work = self.store.previous_activity_session(before=ended.started_at, modes=tuple(WORK_MODES))
        self.update_break_pattern(self.store, ended, previous_work)

    @staticmethod
    def update_break_pattern(store: Any, break_session: ActivitySession, previous_work: ActivitySession | None = None) -> LearnedPattern:
        existing = store.learned_pattern("break_pattern", "global", "work_break")
        samples: list[int] = []
        if existing:
            try:
                payload = json.loads(existing.value)
                raw_samples = payload.get("samples", [])
                if isinstance(raw_samples, list):
                    samples = [int(item) for item in raw_samples if int(item) > 0][-20:]
            except (ValueError, TypeError, json.JSONDecodeError):
                samples = []
        samples.append(max(60, int(break_session.duration_seconds)))
        median = int(statistics.median(samples))
        average = int(sum(samples) / len(samples))
        confidence = min(0.9, len(samples) / 5)
        payload = {
            "median_break_seconds": median,
            "average_break_seconds": average,
            "sample_count": len(samples),
            "confidence": confidence,
            "time_of_day": break_session.started_at.strftime("%H"),
            "previous_work_seconds": previous_work.duration_seconds if previous_work else 0,
            "samples": samples[-20:],
        }
        return store.upsert_pattern(
            "break_pattern",
            "global",
            "work_break",
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
            score=confidence,
            sample_count=len(samples),
            confidence=confidence,
            status="observing",
            reason="工作後休息長度模式",
        )


def break_pattern_payload(pattern: LearnedPattern | None) -> dict[str, Any]:
    if not pattern:
        return {
            "median_break_seconds": None,
            "average_break_seconds": None,
            "sample_count": 0,
            "confidence": 0.0,
            "uses_personal_threshold": False,
            "default_range_seconds": [DEFAULT_BREAK_MIN_SECONDS, DEFAULT_BREAK_MAX_SECONDS],
        }
    try:
        payload = json.loads(pattern.value)
    except json.JSONDecodeError:
        payload = {}
    confidence = float(payload.get("confidence") or pattern.confidence or 0)
    sample_count = int(payload.get("sample_count") or pattern.sample_count or 0)
    payload["confidence"] = confidence
    payload["sample_count"] = sample_count
    payload["uses_personal_threshold"] = confidence >= PATTERN_MIN_CONFIDENCE and sample_count >= PATTERN_MIN_SAMPLES
    payload["default_range_seconds"] = [DEFAULT_BREAK_MIN_SECONDS, DEFAULT_BREAK_MAX_SECONDS]
    return payload


def break_threshold_seconds(pattern_payload: dict[str, Any]) -> int:
    if pattern_payload.get("uses_personal_threshold"):
        median = int(pattern_payload.get("median_break_seconds") or DEFAULT_BREAK_MAX_SECONDS)
        return max(DEFAULT_BREAK_MIN_SECONDS, int(median * 1.35))
    return DEFAULT_BREAK_MAX_SECONDS


def infer_intent(
    current_session: ActivitySession | None,
    explicit_intent: str | None,
    explicit_until: datetime | None,
    now: datetime,
    previous_work_seconds: int = 0,
) -> tuple[str, str]:
    if explicit_intent and explicit_intent != "auto" and (explicit_until is None or explicit_until > now):
        return explicit_intent, "explicit"
    if not current_session:
        return "unknown", "inferred"
    if current_session.mode == "閒置" or current_session.category == "away":
        return "away", "inferred"
    if current_session.mode in WORK_MODES:
        return "working", "inferred"
    if is_private_session(current_session):
        if current_session.duration_seconds >= PRIVATE_TIME_AFTER_SECONDS:
            return "private_time", "inferred"
        return "intentional_break" if previous_work_seconds >= 25 * 60 else "private_time", "inferred"
    if is_break_session(current_session):
        return "intentional_break" if previous_work_seconds >= 25 * 60 else "private_time", "inferred"
    if current_session.mode in CHAT_MODES:
        return "private_time" if current_session.duration_seconds >= 45 * 60 else "unknown", "inferred"
    return "unknown", "inferred"

from __future__ import annotations

from .reminder_trace import trace_reminder

import sqlite3
import json
from datetime import datetime, timedelta
from pathlib import Path

from .models import (
    AssistantEvent,
    ActivitySession,
    ContextReminder,
    CurrentState,
    ForegroundSnapshot,
    Goal,
    LearnedPattern,
    NextAction,
    NotificationEvent,
    NotificationInteraction,
    Reminder,
    ReminderHabitSummary,
    ReminderInteraction,
    UsageEvent,
)
from .reminders import recurrence_next_after, recurrence_occurrence


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


from .reminder_operations import ReminderOperationsStore


class AssistantStore(ReminderOperationsStore):
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, factory=ClosingConnection)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS reminders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    text TEXT NOT NULL,
                    due_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    fired_at TEXT,
                    status TEXT NOT NULL DEFAULT 'pending',
                    notified_at TEXT,
                    completed_at TEXT,
                    ignored_at TEXT,
                    snoozed_until TEXT
                )
                """
            )
            self._ensure_reminder_columns(conn)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS assistant_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    source TEXT NOT NULL,
                    context_mode TEXT,
                    app_name TEXT,
                    window_title_summary TEXT,
                    related_reminder_id INTEGER,
                    series_id TEXT,
                    metadata TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS usage_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    app_name TEXT NOT NULL,
                    window_title TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    idle_seconds INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS activity_sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    started_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    ended_at TEXT,
                    app_name TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    category TEXT NOT NULL,
                    title_summary TEXT NOT NULL,
                    duration_seconds INTEGER NOT NULL DEFAULT 0,
                    metadata TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS reminder_interactions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reminder_id INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    response_seconds INTEGER,
                    current_mode TEXT,
                    source TEXT NOT NULL DEFAULT 'assistant_reminder',
                    FOREIGN KEY(reminder_id) REFERENCES reminders(id)
                )
                """
            )
            self._ensure_interaction_columns(conn)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS app_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS notification_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_app TEXT NOT NULL,
                    title TEXT NOT NULL,
                    body TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    metadata TEXT,
                    context_key TEXT,
                    current_mode TEXT,
                    away_state TEXT,
                    first_view_at TEXT,
                    status TEXT NOT NULL DEFAULT 'observing',
                    intervention_count INTEGER NOT NULL DEFAULT 0,
                    last_intervention_at TEXT,
                    snoozed_until TEXT
                )
                """
            )
            self._ensure_notification_event_columns(conn)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS notification_interactions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    notification_event_id INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    response_seconds INTEGER,
                    target_app TEXT,
                    FOREIGN KEY(notification_event_id) REFERENCES notification_events(id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS learned_patterns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    pattern_type TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    key TEXT NOT NULL,
                    value TEXT NOT NULL,
                    score REAL NOT NULL DEFAULT 0,
                    sample_count INTEGER NOT NULL DEFAULT 0,
                    confidence REAL NOT NULL DEFAULT 0,
                    first_seen TEXT NOT NULL,
                    last_updated TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'observing',
                    protected INTEGER NOT NULL DEFAULT 0,
                    reason TEXT,
                    UNIQUE(pattern_type, scope, key)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS behavior_hypotheses (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    pattern_type TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    key TEXT NOT NULL,
                    hypothesis TEXT NOT NULL,
                    confidence REAL NOT NULL DEFAULT 0,
                    sample_count INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'observing',
                    first_seen TEXT NOT NULL,
                    last_updated TEXT NOT NULL,
                    reason TEXT,
                    UNIQUE(pattern_type, scope, key)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS adaptive_preferences (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    preference_key TEXT NOT NULL UNIQUE,
                    value TEXT NOT NULL,
                    policy_level INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'provisional',
                    reason TEXT NOT NULL,
                    previous_value TEXT,
                    pattern_id INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS adaptive_decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    reminder_id INTEGER,
                    strategy TEXT NOT NULL,
                    policy_level INTEGER NOT NULL,
                    decision_reason TEXT NOT NULL,
                    metadata TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS adaptive_suggestions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    suggestion_type TEXT NOT NULL,
                    reminder_id INTEGER,
                    title TEXT NOT NULL,
                    body TEXT NOT NULL,
                    proposed_change TEXT NOT NULL,
                    policy_level INTEGER NOT NULL DEFAULT 3,
                    status TEXT NOT NULL DEFAULT 'pending',
                    confidence REAL NOT NULL DEFAULT 0,
                    reason TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    resolved_at TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS context_reminders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    text TEXT NOT NULL,
                    app_pattern TEXT NOT NULL,
                    trigger TEXT NOT NULL,
                    window_title_pattern TEXT,
                    created_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'waiting',
                    triggered_at TEXT,
                    reminder_id INTEGER,
                    last_seen_active INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS reminder_feedback (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reminder_id INTEGER NOT NULL,
                    value INTEGER NOT NULL,
                    timestamp TEXT NOT NULL,
                    current_mode TEXT,
                    reason TEXT,
                    FOREIGN KEY(reminder_id) REFERENCES reminders(id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS current_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    current_mode TEXT NOT NULL,
                    foreground_app TEXT NOT NULL,
                    window_title_summary TEXT NOT NULL,
                    activity_summary TEXT NOT NULL,
                    activity_started_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    recent_apps TEXT NOT NULL DEFAULT '[]',
                    recent_events TEXT NOT NULL DEFAULT '[]',
                    active_reminders TEXT NOT NULL DEFAULT '[]',
                    away INTEGER NOT NULL DEFAULT 0,
                    manual_stop INTEGER NOT NULL DEFAULT 0,
                    privacy_mode INTEGER NOT NULL DEFAULT 0,
                    intent TEXT NOT NULL DEFAULT 'unknown',
                    intent_source TEXT NOT NULL DEFAULT 'inferred',
                    session_category TEXT NOT NULL DEFAULT 'general',
                    session_started_at TEXT,
                    session_duration_seconds INTEGER NOT NULL DEFAULT 0,
                    previous_work_session_seconds INTEGER NOT NULL DEFAULT 0,
                    break_pattern TEXT
                )
                """
            )
            self._ensure_current_state_columns(conn)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS goals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    goal_type TEXT NOT NULL,
                    text TEXT NOT NULL,
                    source TEXT NOT NULL,
                    confidence REAL NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(goal_type, source)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS advisor_interventions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    action_text TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    source TEXT NOT NULL,
                    intervention_level TEXT NOT NULL,
                    should_interrupt INTEGER NOT NULL DEFAULT 0,
                    metadata TEXT
                )
                """
            )
            self._repair_recurring_schedule_cache(conn, datetime.now())

    def _ensure_reminder_columns(self, conn: sqlite3.Connection) -> None:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(reminders)").fetchall()}
        migrations = {
            "status": "ALTER TABLE reminders ADD COLUMN status TEXT NOT NULL DEFAULT 'pending'",
            "notified_at": "ALTER TABLE reminders ADD COLUMN notified_at TEXT",
            "completed_at": "ALTER TABLE reminders ADD COLUMN completed_at TEXT",
            "ignored_at": "ALTER TABLE reminders ADD COLUMN ignored_at TEXT",
            "snoozed_until": "ALTER TABLE reminders ADD COLUMN snoozed_until TEXT",
            "recurrence_rule": "ALTER TABLE reminders ADD COLUMN recurrence_rule TEXT",
            "next_due_at": "ALTER TABLE reminders ADD COLUMN next_due_at TEXT",
            "snooze_count": "ALTER TABLE reminders ADD COLUMN snooze_count INTEGER NOT NULL DEFAULT 0",
            "notification_count": "ALTER TABLE reminders ADD COLUMN notification_count INTEGER NOT NULL DEFAULT 0",
            "completed_count": "ALTER TABLE reminders ADD COLUMN completed_count INTEGER NOT NULL DEFAULT 0",
            "ignored_count": "ALTER TABLE reminders ADD COLUMN ignored_count INTEGER NOT NULL DEFAULT 0",
            "last_snoozed_at": "ALTER TABLE reminders ADD COLUMN last_snoozed_at TEXT",
            "importance": "ALTER TABLE reminders ADD COLUMN importance TEXT NOT NULL DEFAULT 'normal'",
            "category": "ALTER TABLE reminders ADD COLUMN category TEXT NOT NULL DEFAULT 'general'",
            "context_rule_id": "ALTER TABLE reminders ADD COLUMN context_rule_id INTEGER",
            "time_inferred": "ALTER TABLE reminders ADD COLUMN time_inferred INTEGER NOT NULL DEFAULT 0",
            "inference_reason": "ALTER TABLE reminders ADD COLUMN inference_reason TEXT",
        }
        for column, sql in migrations.items():
            if column not in columns:
                conn.execute(sql)

        conn.execute(
            """
            UPDATE reminders
            SET status = CASE WHEN fired_at IS NULL THEN 'pending' ELSE 'notified' END,
                notified_at = COALESCE(notified_at, fired_at)
            WHERE status IS NULL OR status = '' OR (status = 'pending' AND fired_at IS NOT NULL)
            """
        )
        conn.execute("UPDATE reminders SET importance = 'critical' WHERE importance = 'important'")
        conn.execute("UPDATE reminders SET importance = 'low' WHERE importance = 'casual'")

    def _repair_recurring_schedule_cache(self, conn: sqlite3.Connection, now: datetime) -> int:
        rows = conn.execute(
            """
            SELECT * FROM reminders
            WHERE recurrence_rule IS NOT NULL
              AND recurrence_rule != ''
              AND status IN ('pending', 'snoozed', 'pending_due', 'notified')
            """
        ).fetchall()
        repaired = 0
        for row in rows:
            rule_text = str(row["recurrence_rule"] or "")
            try:
                rule = json.loads(rule_text)
            except json.JSONDecodeError:
                continue
            if rule.get("freq") != "cycle":
                continue

            current_due_at = _dt(str(row["due_at"]))
            completed_at = _dt(str(row["completed_at"])) if row["completed_at"] else None
            ignored_at = _dt(str(row["ignored_at"])) if row["ignored_at"] else None
            snoozed_until = _dt(str(row["snoozed_until"])) if row["snoozed_until"] else None
            status = str(row["status"] or "")

            occurrence = recurrence_occurrence(rule_text, now, keep_missed=True)
            if occurrence is None:
                continue
            target_due_at = occurrence.due_at
            if occurrence.is_missed and any(done_at and done_at >= target_due_at for done_at in (completed_at, ignored_at)):
                following = recurrence_next_after(rule_text, target_due_at)
                if following is None:
                    continue
                occurrence = following
                target_due_at = following.due_at

            preserve_snooze = bool(snoozed_until and snoozed_until > now and current_due_at == snoozed_until)
            if preserve_snooze:
                target_due_at = current_due_at
                next_source = occurrence.due_at if occurrence.due_at <= now else current_due_at
                following = recurrence_next_after(rule_text, next_source)
                next_due_at = following.due_at if following else occurrence.next_due_at
            else:
                next_due_at = occurrence.next_due_at

            if current_due_at == target_due_at and (row["next_due_at"] or None) == (next_due_at.isoformat() if next_due_at else None):
                continue

            conn.execute(
                """
                UPDATE reminders
                SET due_at = ?,
                    next_due_at = ?,
                    fired_at = CASE WHEN due_at = ? THEN fired_at ELSE NULL END,
                    notified_at = CASE WHEN due_at = ? THEN notified_at ELSE NULL END,
                    snoozed_until = CASE WHEN ? = 1 THEN snoozed_until ELSE NULL END,
                    status = CASE WHEN status = 'snoozed' AND ? = 1 THEN status ELSE 'pending' END
                WHERE id = ?
                """,
                (
                    target_due_at.isoformat(),
                    next_due_at.isoformat() if next_due_at else None,
                    target_due_at.isoformat(),
                    target_due_at.isoformat(),
                    1 if preserve_snooze else 0,
                    1 if preserve_snooze else 0,
                    int(row["id"]),
                ),
            )
            repaired += 1
        return repaired

    def _ensure_interaction_columns(self, conn: sqlite3.Connection) -> None:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(reminder_interactions)").fetchall()}
        if "snooze_count" not in columns:
            conn.execute("ALTER TABLE reminder_interactions ADD COLUMN snooze_count INTEGER NOT NULL DEFAULT 0")

    def _ensure_notification_event_columns(self, conn: sqlite3.Connection) -> None:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(notification_events)").fetchall()}
        migrations = {
            "context_key": "ALTER TABLE notification_events ADD COLUMN context_key TEXT",
            "current_mode": "ALTER TABLE notification_events ADD COLUMN current_mode TEXT",
            "away_state": "ALTER TABLE notification_events ADD COLUMN away_state TEXT",
            "first_view_at": "ALTER TABLE notification_events ADD COLUMN first_view_at TEXT",
            "status": "ALTER TABLE notification_events ADD COLUMN status TEXT NOT NULL DEFAULT 'observing'",
            "intervention_count": "ALTER TABLE notification_events ADD COLUMN intervention_count INTEGER NOT NULL DEFAULT 0",
            "last_intervention_at": "ALTER TABLE notification_events ADD COLUMN last_intervention_at TEXT",
            "snoozed_until": "ALTER TABLE notification_events ADD COLUMN snoozed_until TEXT",
        }
        for column, sql in migrations.items():
            if column not in columns:
                conn.execute(sql)

    def _ensure_current_state_columns(self, conn: sqlite3.Connection) -> None:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(current_state)").fetchall()}
        migrations = {
            "intent": "ALTER TABLE current_state ADD COLUMN intent TEXT NOT NULL DEFAULT 'unknown'",
            "intent_source": "ALTER TABLE current_state ADD COLUMN intent_source TEXT NOT NULL DEFAULT 'inferred'",
            "session_category": "ALTER TABLE current_state ADD COLUMN session_category TEXT NOT NULL DEFAULT 'general'",
            "session_started_at": "ALTER TABLE current_state ADD COLUMN session_started_at TEXT",
            "session_duration_seconds": "ALTER TABLE current_state ADD COLUMN session_duration_seconds INTEGER NOT NULL DEFAULT 0",
            "previous_work_session_seconds": "ALTER TABLE current_state ADD COLUMN previous_work_session_seconds INTEGER NOT NULL DEFAULT 0",
            "break_pattern": "ALTER TABLE current_state ADD COLUMN break_pattern TEXT",
        }
        for column, sql in migrations.items():
            if column not in columns:
                conn.execute(sql)

    def get_setting(self, key: str, default: str | None = None) -> str | None:
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO app_settings (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (key, value, datetime.now().isoformat()),
            )

    def delete_setting(self, key: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM app_settings WHERE key = ?", (key,))

    def list_settings(self) -> dict[str, str]:
        with self.connect() as conn:
            rows = conn.execute("SELECT key, value FROM app_settings ORDER BY key").fetchall()
        return {str(row["key"]): str(row["value"]) for row in rows}

    def reset_learning_data(self) -> None:
        with self.connect() as conn:
            for table in (
                "learned_patterns",
                "behavior_hypotheses",
                "adaptive_preferences",
                "adaptive_decisions",
                "adaptive_suggestions",
                "usage_events",
                "assistant_events",
                "reminder_interactions",
                "notification_events",
                "notification_interactions",
                "reminder_feedback",
                "advisor_interventions",
                "activity_sessions",
            ):
                conn.execute(f"DELETE FROM {table}")

    def reset_all_personal_data(self) -> None:
        with self.connect() as conn:
            for table in (
                "reminders",
                "assistant_events",
                "usage_events",
                "reminder_interactions",
                "notification_events",
                "notification_interactions",
                "learned_patterns",
                "behavior_hypotheses",
                "adaptive_preferences",
                "adaptive_decisions",
                "adaptive_suggestions",
                "context_reminders",
                "reminder_feedback",
                "current_state",
                "goals",
                "advisor_interventions",
                "activity_sessions",
            ):
                conn.execute(f"DELETE FROM {table}")

    def save_current_state(self, state: CurrentState) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO current_state (
                    id, current_mode, foreground_app, window_title_summary, activity_summary,
                    activity_started_at, updated_at, recent_apps, recent_events, active_reminders,
                    away, manual_stop, privacy_mode, intent, intent_source, session_category,
                    session_started_at, session_duration_seconds, previous_work_session_seconds,
                    break_pattern
                )
                VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    current_mode = excluded.current_mode,
                    foreground_app = excluded.foreground_app,
                    window_title_summary = excluded.window_title_summary,
                    activity_summary = excluded.activity_summary,
                    activity_started_at = excluded.activity_started_at,
                    updated_at = excluded.updated_at,
                    recent_apps = excluded.recent_apps,
                    recent_events = excluded.recent_events,
                    active_reminders = excluded.active_reminders,
                    away = excluded.away,
                    manual_stop = excluded.manual_stop,
                    privacy_mode = excluded.privacy_mode,
                    intent = excluded.intent,
                    intent_source = excluded.intent_source,
                    session_category = excluded.session_category,
                    session_started_at = excluded.session_started_at,
                    session_duration_seconds = excluded.session_duration_seconds,
                    previous_work_session_seconds = excluded.previous_work_session_seconds,
                    break_pattern = excluded.break_pattern
                """,
                (
                    state.current_mode,
                    state.foreground_app,
                    state.window_title_summary,
                    state.activity_summary,
                    state.activity_started_at.isoformat(),
                    state.updated_at.isoformat(),
                    json.dumps(list(state.recent_apps), ensure_ascii=False),
                    json.dumps(list(state.recent_events), ensure_ascii=False),
                    json.dumps(list(state.active_reminders), ensure_ascii=False),
                    1 if state.away else 0,
                    1 if state.manual_stop else 0,
                    1 if state.privacy_mode else 0,
                    state.intent,
                    state.intent_source,
                    state.session_category,
                    state.session_started_at.isoformat() if state.session_started_at else None,
                    int(state.session_duration_seconds),
                    int(state.previous_work_session_seconds),
                    json.dumps(state.break_pattern or {}, ensure_ascii=False, sort_keys=True),
                ),
            )

    def load_current_state(self) -> CurrentState | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM current_state WHERE id = 1").fetchone()
        return self._row_to_current_state(row) if row else None

    def upsert_goal(
        self,
        goal_type: str,
        text: str,
        source: str = "manual",
        confidence: float = 1.0,
        status: str = "active",
        now: datetime | None = None,
    ) -> Goal:
        now = now or datetime.now()
        text = self.summarize_text(text, 160)
        with self.connect() as conn:
            existing = conn.execute(
                "SELECT created_at FROM goals WHERE goal_type = ? AND source = ?",
                (goal_type, source),
            ).fetchone()
            created_at = str(existing["created_at"]) if existing else now.isoformat()
            conn.execute(
                """
                INSERT INTO goals (goal_type, text, source, confidence, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(goal_type, source) DO UPDATE SET
                    text = excluded.text,
                    confidence = excluded.confidence,
                    status = excluded.status,
                    updated_at = excluded.updated_at
                """,
                (
                    goal_type,
                    text,
                    source,
                    max(0.0, min(1.0, float(confidence))),
                    status,
                    created_at,
                    now.isoformat(),
                ),
            )
            row = conn.execute(
                "SELECT * FROM goals WHERE goal_type = ? AND source = ?",
                (goal_type, source),
            ).fetchone()
        return self._row_to_goal(row)

    def clear_goal(self, goal_type: str, source: str = "manual") -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE goals
                SET status = 'cleared', updated_at = ?
                WHERE goal_type = ? AND source = ?
                """,
                (datetime.now().isoformat(), goal_type, source),
            )

    def active_goals(self) -> list[Goal]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM goals
                WHERE status = 'active'
                ORDER BY
                    CASE source WHEN 'manual' THEN 0 ELSE 1 END,
                    CASE goal_type
                        WHEN 'immediate_next' THEN 0
                        WHEN 'current_session' THEN 1
                        WHEN 'today' THEN 2
                        WHEN 'long_term' THEN 3
                        ELSE 4
                    END,
                    updated_at DESC
                """
            ).fetchall()
        return [self._row_to_goal(row) for row in rows]

    def current_goal(self) -> Goal | None:
        goals = self.active_goals()
        manual = [goal for goal in goals if goal.source == "manual"]
        return (manual or goals or [None])[0]

    def upsert_inferred_goal(self, goal_type: str, text: str, confidence: float) -> Goal | None:
        if any(goal.goal_type == goal_type and goal.source == "manual" for goal in self.active_goals()):
            return None
        return self.upsert_goal(goal_type, text, "inferred", confidence)

    def log_advisor_intervention(self, action: NextAction, metadata: dict[str, object] | None = None) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO advisor_interventions (
                    timestamp, action_text, reason, confidence, source,
                    intervention_level, should_interrupt, metadata
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    datetime.now().isoformat(),
                    action.action_text,
                    action.reason,
                    max(0.0, min(1.0, float(action.confidence))),
                    action.source,
                    action.intervention_level,
                    1 if action.should_interrupt else 0,
                    json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True),
                ),
            )

    def start_activity_session(
        self,
        started_at: datetime,
        app_name: str,
        mode: str,
        category: str,
        title_summary: str,
        metadata: dict[str, object] | None = None,
    ) -> ActivitySession:
        payload = metadata or {}
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO activity_sessions (
                    started_at, last_seen_at, ended_at, app_name, mode, category,
                    title_summary, duration_seconds, metadata
                )
                VALUES (?, ?, NULL, ?, ?, ?, ?, 0, ?)
                """,
                (
                    started_at.isoformat(),
                    started_at.isoformat(),
                    app_name,
                    mode,
                    category,
                    title_summary,
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                ),
            )
            session_id = int(cursor.lastrowid)
            row = conn.execute("SELECT * FROM activity_sessions WHERE id = ?", (session_id,)).fetchone()
        self.record_event(
            "activity_session_started",
            "activity_session_manager",
            started_at,
            mode,
            app_name,
            title_summary,
            metadata={"category": category},
        )
        return self._row_to_activity_session(row)

    def touch_activity_session(
        self,
        session_id: int,
        seen_at: datetime,
        title_summary: str | None = None,
        idle_seconds: int | None = None,
    ) -> ActivitySession:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM activity_sessions WHERE id = ?", (session_id,)).fetchone()
            if not row:
                raise ValueError(f"activity session not found: {session_id}")
            started_at = _dt(str(row["started_at"]))
            metadata = {}
            if row["metadata"]:
                try:
                    metadata = json.loads(str(row["metadata"]))
                except json.JSONDecodeError:
                    metadata = {}
            if title_summary:
                metadata["latest_title_summary"] = title_summary
            if idle_seconds is not None:
                metadata["idle_seconds"] = idle_seconds
            duration_seconds = max(0, int((seen_at - started_at).total_seconds()))
            conn.execute(
                """
                UPDATE activity_sessions
                SET last_seen_at = ?, title_summary = COALESCE(?, title_summary),
                    duration_seconds = ?, metadata = ?
                WHERE id = ?
                """,
                (
                    seen_at.isoformat(),
                    title_summary,
                    duration_seconds,
                    json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                    session_id,
                ),
            )
            row = conn.execute("SELECT * FROM activity_sessions WHERE id = ?", (session_id,)).fetchone()
        return self._row_to_activity_session(row)

    def end_activity_session(self, session_id: int, ended_at: datetime) -> ActivitySession:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM activity_sessions WHERE id = ?", (session_id,)).fetchone()
            if not row:
                raise ValueError(f"activity session not found: {session_id}")
            started_at = _dt(str(row["started_at"]))
            duration_seconds = max(0, int((ended_at - started_at).total_seconds()))
            conn.execute(
                """
                UPDATE activity_sessions
                SET ended_at = ?, last_seen_at = ?, duration_seconds = ?
                WHERE id = ?
                """,
                (ended_at.isoformat(), ended_at.isoformat(), duration_seconds, session_id),
            )
            row = conn.execute("SELECT * FROM activity_sessions WHERE id = ?", (session_id,)).fetchone()
        session = self._row_to_activity_session(row)
        self.record_event(
            "activity_session_ended",
            "activity_session_manager",
            ended_at,
            session.mode,
            session.app_name,
            session.title_summary,
            metadata={"category": session.category, "duration_seconds": session.duration_seconds},
        )
        return session

    def reopen_activity_session(
        self,
        session_id: int,
        seen_at: datetime,
        title_summary: str | None = None,
        idle_seconds: int | None = None,
    ) -> ActivitySession:
        with self.connect() as conn:
            conn.execute("UPDATE activity_sessions SET ended_at = NULL WHERE id = ?", (session_id,))
        return self.touch_activity_session(session_id, seen_at, title_summary, idle_seconds)

    def previous_activity_session(
        self,
        before: datetime,
        modes: tuple[str, ...] | None = None,
    ) -> ActivitySession | None:
        params: list[object] = [before.isoformat()]
        mode_clause = ""
        if modes:
            placeholders = ",".join("?" for _ in modes)
            mode_clause = f"AND mode IN ({placeholders})"
            params.extend(modes)
        with self.connect() as conn:
            row = conn.execute(
                f"""
                SELECT * FROM activity_sessions
                WHERE started_at < ? {mode_clause}
                ORDER BY last_seen_at DESC
                LIMIT 1
                """,
                tuple(params),
            ).fetchone()
        return self._row_to_activity_session(row) if row else None

    def activity_sessions_between(self, start: datetime, end: datetime) -> list[ActivitySession]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM activity_sessions
                WHERE started_at <= ? AND last_seen_at >= ?
                ORDER BY started_at ASC
                """,
                (end.isoformat(), start.isoformat()),
            ).fetchall()
        return [self._row_to_activity_session(row) for row in rows]

    def learned_pattern(self, pattern_type: str, scope: str, key: str) -> LearnedPattern | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM learned_patterns
                WHERE pattern_type = ? AND scope = ? AND key = ?
                """,
                (pattern_type, scope, key),
            ).fetchone()
        return self._row_to_pattern(row) if row else None

    def next_pending_reminder(self, now: datetime | None = None) -> Reminder | None:
        now = now or datetime.now()
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM reminders
                WHERE status IN ('pending', 'snoozed') AND due_at >= ?
                ORDER BY due_at ASC
                LIMIT 1
                """,
                (now.isoformat(),),
            ).fetchone()
        return self._row_to_reminder(row) if row else None

    def next_pending_reminder_within(
        self,
        within: timedelta,
        now: datetime | None = None,
    ) -> Reminder | None:
        now = now or datetime.now()
        until = now + within
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM reminders
                WHERE status IN ('pending', 'snoozed') AND due_at >= ? AND due_at <= ?
                ORDER BY due_at ASC
                LIMIT 1
                """,
                (now.isoformat(), until.isoformat()),
            ).fetchone()
        return self._row_to_reminder(row) if row else None

    def add_reminder(
        self,
        text: str,
        due_at: datetime,
        recurrence_rule: str | None = None,
        next_due_at: datetime | None = None,
        importance: str | None = None,
        category: str | None = None,
        status: str = "pending",
        context_rule_id: int | None = None,
        time_inferred: bool = False,
        inference_reason: str | None = None,
    ) -> Reminder:
        created_at = datetime.now()
        importance = importance or self.infer_reminder_importance(text)
        category = category or self.infer_reminder_category(text)
        trace_reminder("storage_input", due_at=due_at)
        with self.connect() as conn:
            # Serialize check + insert across connections/processes, including voice/text retries.
            conn.execute("BEGIN IMMEDIATE")
            candidates = conn.execute(
                """SELECT * FROM reminders WHERE due_at = ? AND created_at >= ?
                   AND recurrence_rule IS ? AND context_rule_id IS ?
                   AND status NOT IN ('completed', 'ignored', 'deleted') ORDER BY id""",
                (due_at.isoformat(), (created_at - timedelta(seconds=30)).isoformat(),
                 recurrence_rule, context_rule_id),
            ).fetchall()
            key = " ".join(text.split()).casefold()
            for row in candidates:
                if " ".join(row["text"].split()).casefold() == key:
                    trace_reminder("storage_duplicate", due_at=row["due_at"], reminder_id=row["id"])
                    return self._row_to_reminder(row)
            cursor = conn.execute(
                """
                INSERT INTO reminders (
                    text, due_at, created_at, recurrence_rule, next_due_at, importance, category,
                    status, context_rule_id, time_inferred, inference_reason
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    text,
                    due_at.isoformat(),
                    created_at.isoformat(),
                    recurrence_rule,
                    next_due_at.isoformat() if next_due_at else None,
                    importance,
                    category,
                    status,
                    context_rule_id,
                    1 if time_inferred else 0,
                    inference_reason,
                ),
            )
            reminder_id = int(cursor.lastrowid)
        trace_reminder("storage_written", due_at=due_at.isoformat(), reminder_id=reminder_id)
        self.record_event(
            event_type="reminder_created",
            source="reminder_service",
            related_reminder_id=reminder_id,
            series_id=self.series_id_for(reminder_id, text, recurrence_rule),
            metadata={
                "text_summary": self.summarize_text(text),
                "importance": importance,
                "category": category,
                "time_inferred": time_inferred,
                "inference_reason": inference_reason,
            },
        )
        return Reminder(
            id=reminder_id,
            text=text,
            due_at=due_at,
            created_at=created_at,
            recurrence_rule=recurrence_rule,
            next_due_at=next_due_at,
            status=status,
            importance=importance,
            category=category,
            context_rule_id=context_rule_id,
            time_inferred=time_inferred,
            inference_reason=inference_reason,
        )

    def active_hour_scores(self) -> dict[int, float]:
        scores: dict[int, float] = {}
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT key, confidence, sample_count, status FROM learned_patterns
                WHERE pattern_type = 'mode_seen_by_hour'
                """
            ).fetchall()
        for row in rows:
            try:
                hour = int(str(row["key"]).rsplit(":", 1)[-1])
            except ValueError:
                continue
            if str(row["status"] or "") == "rejected":
                continue
            confidence = float(row["confidence"] or 0)
            sample_weight = min(1.0, int(row["sample_count"] or 0) / 12)
            scores[hour] = max(scores.get(hour, 0.0), confidence * sample_weight)
        return scores

    def repair_misparsed_garbage_reminder(
        self,
        wrong_due_at: datetime,
        intended_due_at: datetime,
        now: datetime | None = None,
    ) -> Reminder | None:
        now = now or datetime.now()
        created_cutoff = now - timedelta(hours=6)
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM reminders
                WHERE status IN ('pending', 'snoozed')
                  AND due_at = ?
                  AND created_at >= ?
                  AND notified_at IS NULL
                  AND completed_at IS NULL
                  AND ignored_at IS NULL
                ORDER BY created_at DESC
                """,
                (wrong_due_at.isoformat(), created_cutoff.isoformat()),
            ).fetchall()
            candidates = [row for row in rows if "垃圾" in str(row["text"]) or "收拾" in str(row["text"])]
            if len(candidates) != 1:
                return None
            row = candidates[0]
            conn.execute(
                """
                UPDATE reminders
                SET due_at = ?,
                    snoozed_until = NULL,
                    status = 'pending',
                    time_inferred = 1,
                    inference_reason = ?
                WHERE id = ?
                """,
                (
                    intended_due_at.isoformat(),
                    "修復無上午/下午的 5:30 誤判：依建立時下午活躍情境改為今天 17:30",
                    int(row["id"]),
                ),
            )
            reminder_id = int(row["id"])
        self.record_event(
            event_type="reminder_time_repaired",
            source="reminder_service",
            related_reminder_id=reminder_id,
            metadata={
                "from_due_at": wrong_due_at.isoformat(),
                "to_due_at": intended_due_at.isoformat(),
                "reason": "ambiguous_5_30_pm_inferred",
            },
        )
        return self.get_reminder(reminder_id)

    def due_reminders(self, now: datetime | None = None) -> list[Reminder]:
        now = now or datetime.now()
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM reminders
                WHERE status IN ('pending', 'snoozed') AND due_at <= ?
                ORDER BY due_at ASC
                """,
                (now.isoformat(),),
            ).fetchall()
        return [self._row_to_reminder(row) for row in rows]

    def today_inbox_items(self, now: datetime | None = None, limit: int = 20) -> list[Reminder]:
        now = now or datetime.now()
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = now.replace(hour=23, minute=59, second=59, microsecond=999999)
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM reminders
                WHERE status = 'today_inbox' AND due_at >= ? AND due_at <= ?
                ORDER BY created_at ASC
                LIMIT ?
                """,
                (start.isoformat(), end.isoformat(), limit),
            ).fetchall()
        return [self._row_to_reminder(row) for row in rows]

    def promote_today_inbox_item(self, reminder_id: int, due_at: datetime) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE reminders
                SET status = 'pending', due_at = ?
                WHERE id = ? AND status = 'today_inbox'
                """,
                (due_at.isoformat(), reminder_id),
            )

    def mark_reminder_pending_due(self, reminder_id: int, reason: str, at: datetime | None = None) -> bool:
        at = at or datetime.now()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE reminders
                SET status = 'pending_due'
                WHERE id = ? AND status IN ('pending', 'snoozed')
                """,
                (reminder_id,),
            )
            changed = cursor.rowcount > 0
        if not changed:
            return False
        self.record_event(
            event_type="reminder_pending_due",
            source="reminder_service",
            related_reminder_id=reminder_id,
            metadata={"reason": reason, "timestamp": at.isoformat()},
        )
        return True

    def pending_due_reminders(self) -> list[Reminder]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM reminders WHERE status = 'pending_due' ORDER BY due_at ASC"
            ).fetchall()
        return [self._row_to_reminder(row) for row in rows]

    def release_pending_due(self, reminder_id: int) -> bool:
        with self.connect() as conn:
            cursor = conn.execute("UPDATE reminders SET status = 'pending' WHERE id = ? AND status = 'pending_due'", (reminder_id,))
        return cursor.rowcount > 0

    def notified_reminders_older_than(self, cutoff: datetime) -> list[Reminder]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM reminders
                WHERE status = 'notified' AND notified_at IS NOT NULL AND notified_at <= ?
                ORDER BY notified_at ASC
                """,
                (cutoff.isoformat(),),
            ).fetchall()
        return [self._row_to_reminder(row) for row in rows]

    def mark_reminder_fired(self, reminder_id: int, fired_at: datetime | None = None) -> None:
        self.mark_reminder_notified(reminder_id, fired_at)

    def mark_reminder_notified(
        self,
        reminder_id: int,
        notified_at: datetime | None = None,
        expected_due_at: datetime | None = None,
    ) -> bool:
        notified_at = notified_at or datetime.now()
        with self.connect() as conn:
            params: list[str | int] = [
                notified_at.isoformat(),
                notified_at.isoformat(),
                reminder_id,
            ]
            due_guard = ""
            if expected_due_at is not None:
                due_guard = " AND due_at = ?"
                params.append(expected_due_at.isoformat())
            cursor = conn.execute(
                """
                UPDATE reminders
                SET status = 'notified',
                    fired_at = COALESCE(fired_at, ?),
                    notified_at = ?,
                    notification_count = COALESCE(notification_count, 0) + 1
                WHERE id = ? AND status IN ('pending', 'snoozed', 'pending_due')
                """ + due_guard,
                tuple(params),
            )
            changed = cursor.rowcount > 0
        if not changed:
            return False
        reminder = self.get_reminder(reminder_id)
        self.record_event(
            event_type="reminder_notified",
            source="reminder_service",
            context_mode=None,
            related_reminder_id=reminder_id,
            series_id=self.series_id_for(reminder_id, reminder.text, reminder.recurrence_rule) if reminder else None,
            metadata={"notified_at": notified_at.isoformat()},
        )
        return True

    def escalate_unconfirmed_reminders(self, cutoff: datetime, now: datetime | None = None) -> list[Reminder]:
        now = now or datetime.now()
        reminders = self.notified_reminders_older_than(cutoff)
        escalated: list[Reminder] = []
        with self.connect() as conn:
            for reminder in reminders:
                if reminder.status != "notified":
                    continue
                conn.execute(
                    """
                    UPDATE reminders
                    SET status = 'pending', due_at = ?
                    WHERE id = ? AND status = 'notified'
                    """,
                    (now.isoformat(), reminder.id),
                )
                escalated.append(reminder)
        for reminder in escalated:
            self.record_event(
                event_type="reminder_escalated",
                source="reminder_service",
                related_reminder_id=reminder.id,
                metadata={"reason": "未確認自動升級", "timestamp": now.isoformat()},
            )
        return escalated

    def complete_reminder(
        self,
        reminder_id: int,
        completed_at: datetime | None = None,
        expected_due_at: datetime | None = None,
    ) -> bool:
        completed_at = completed_at or datetime.now()
        return self._finish_reminder(reminder_id, "completed", completed_at, expected_due_at)

    def ignore_reminder(
        self,
        reminder_id: int,
        ignored_at: datetime | None = None,
        expected_due_at: datetime | None = None,
    ) -> bool:
        ignored_at = ignored_at or datetime.now()
        return self._finish_reminder(reminder_id, "ignored", ignored_at, expected_due_at)

    def add_reminder_feedback(
        self,
        reminder_id: int,
        helpful: bool,
        current_mode: str | None = None,
        reason: str | None = None,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO reminder_feedback (reminder_id, value, timestamp, current_mode, reason)
                VALUES (?, ?, ?, ?, ?)
                """,
                (reminder_id, 1 if helpful else -1, datetime.now().isoformat(), current_mode, reason),
            )
        reminder = self.get_reminder(reminder_id)
        if reminder:
            self.upsert_pattern(
                "reminder_feedback",
                "reminder",
                self.series_id_for(reminder.id, reminder.text, reminder.recurrence_rule),
                "helpful" if helpful else "not_helpful",
                0.2 if helpful else -0.2,
                max(1, len(self.reminder_feedback(reminder_id))),
                min(0.4, max(1, len(self.reminder_feedback(reminder_id))) / 20),
                "observing",
                reason=f"{reminder.text} 的主觀回饋",
            )

    def reminder_feedback(self, reminder_id: int) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM reminder_feedback WHERE reminder_id = ? ORDER BY timestamp DESC",
                (reminder_id,),
            ).fetchall()

    def add_context_reminder(
        self,
        text: str,
        app_pattern: str,
        trigger: str,
        window_title_pattern: str | None = None,
    ) -> ContextReminder:
        created_at = datetime.now()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO context_reminders (text, app_pattern, trigger, window_title_pattern, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (text, app_pattern.lower(), trigger, window_title_pattern, created_at.isoformat()),
            )
            context_id = int(cursor.lastrowid)
        return ContextReminder(context_id, text, app_pattern.lower(), trigger, window_title_pattern, created_at)

    def context_reminders(self, active_only: bool = True) -> list[ContextReminder]:
        sql = "SELECT * FROM context_reminders"
        if active_only:
            sql += " WHERE status = 'waiting'"
        sql += " ORDER BY created_at ASC"
        with self.connect() as conn:
            rows = conn.execute(sql).fetchall()
        return [self._row_to_context_reminder(row) for row in rows]

    def update_context_reminder_seen(self, context_id: int, active: bool) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE context_reminders SET last_seen_active = ? WHERE id = ?",
                (1 if active else 0, context_id),
            )

    def trigger_context_reminder(self, context_id: int, due_at: datetime | None = None) -> Reminder | None:
        context = next((item for item in self.context_reminders(True) if item.id == context_id), None)
        if not context:
            return None
        due_at = due_at or datetime.now()
        reminder = self.add_reminder(context.text, due_at, context_rule_id=context.id)
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE context_reminders
                SET status = 'triggered', triggered_at = ?, reminder_id = ?
                WHERE id = ?
                """,
                (due_at.isoformat(), reminder.id, context.id),
            )
        return reminder

    def evaluate_context_reminders(self, snapshot: ForegroundSnapshot) -> list[Reminder]:
        triggered: list[Reminder] = []
        for rule in self.context_reminders(True):
            active = self._context_rule_matches(rule, snapshot)
            should_trigger = (rule.trigger == "open" and active and not rule.last_seen_active) or (
                rule.trigger == "close" and not active and rule.last_seen_active
            )
            self.update_context_reminder_seen(rule.id, active)
            if should_trigger:
                reminder = self.trigger_context_reminder(rule.id, snapshot.timestamp)
                if reminder:
                    triggered.append(reminder)
        return triggered

    def snooze_reminder(
        self,
        reminder_id: int,
        due_at: datetime,
        snoozed_at: datetime | None = None,
        expected_due_at: datetime | None = None,
    ) -> bool:
        snoozed_at = snoozed_at or datetime.now()
        with self.connect() as conn:
            params: list[str | int] = [
                due_at.isoformat(),
                due_at.isoformat(),
                snoozed_at.isoformat(),
                reminder_id,
            ]
            due_guard = ""
            if expected_due_at is not None:
                due_guard = " AND due_at = ?"
                params.append(expected_due_at.isoformat())
            cursor = conn.execute(
                """
                UPDATE reminders
                SET status = 'pending',
                    due_at = ?,
                    snoozed_until = ?,
                    last_snoozed_at = ?,
                    snooze_count = COALESCE(snooze_count, 0) + 1
                WHERE id = ? AND status IN ('notified', 'pending_due', 'pending', 'snoozed')
                """ + due_guard,
                tuple(params),
            )
            changed = cursor.rowcount > 0
        if not changed:
            return False
        reminder = self.get_reminder(reminder_id)
        self.record_event(
            event_type="reminder_snoozed",
            source="reminder_popup",
            related_reminder_id=reminder_id,
            series_id=self.series_id_for(reminder_id, reminder.text, reminder.recurrence_rule) if reminder else None,
            metadata={"snoozed_until": due_at.isoformat()},
        )
        return True

    def log_reminder_interaction(
        self,
        reminder_id: int,
        event_type: str,
        timestamp: datetime | None = None,
        current_mode: str | None = None,
        source: str = "assistant_reminder",
    ) -> ReminderInteraction:
        timestamp = timestamp or datetime.now()
        response_seconds = self._response_seconds(reminder_id, timestamp)
        if event_type == "notified":
            response_seconds = None

        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO reminder_interactions (
                    reminder_id, event_type, timestamp, response_seconds, current_mode, source, snooze_count
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    reminder_id,
                    event_type,
                    timestamp.isoformat(),
                    response_seconds,
                    current_mode,
                    source,
                    self.get_reminder(reminder_id).snooze_count if self.get_reminder(reminder_id) else 0,
                ),
            )
            interaction_id = int(cursor.lastrowid)
        return ReminderInteraction(
            id=interaction_id,
            reminder_id=reminder_id,
            event_type=event_type,
            timestamp=timestamp,
            response_seconds=response_seconds,
            current_mode=current_mode,
            source=source,
        )

    def record_event(
        self,
        event_type: str,
        source: str,
        timestamp: datetime | None = None,
        context_mode: str | None = None,
        app_name: str | None = None,
        window_title: str | None = None,
        related_reminder_id: int | None = None,
        series_id: str | None = None,
        metadata: dict[str, object] | None = None,
    ) -> AssistantEvent:
        timestamp = timestamp or datetime.now()
        metadata = metadata or {}
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO assistant_events (
                    timestamp, event_type, source, context_mode, app_name, window_title_summary,
                    related_reminder_id, series_id, metadata
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    timestamp.isoformat(),
                    event_type,
                    source,
                    context_mode,
                    app_name,
                    self.summarize_text(window_title or "") if window_title else None,
                    related_reminder_id,
                    series_id,
                    json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                ),
            )
            event_id = int(cursor.lastrowid)
        return AssistantEvent(
            id=event_id,
            timestamp=timestamp,
            event_type=event_type,
            source=source,
            context_mode=context_mode,
            app_name=app_name,
            window_title_summary=self.summarize_text(window_title or "") if window_title else None,
            related_reminder_id=related_reminder_id,
            series_id=series_id,
            metadata=metadata,
        )

    def recent_assistant_events(self, limit: int = 50) -> list[AssistantEvent]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM assistant_events ORDER BY timestamp DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._row_to_assistant_event(row) for row in rows]

    def upsert_pattern(
        self,
        pattern_type: str,
        scope: str,
        key: str,
        value: str,
        score: float,
        sample_count: int,
        confidence: float,
        status: str,
        reason: str | None = None,
    ) -> LearnedPattern:
        now = datetime.now()
        with self.connect() as conn:
            existing = conn.execute(
                """
                SELECT * FROM learned_patterns
                WHERE pattern_type = ? AND scope = ? AND key = ?
                """,
                (pattern_type, scope, key),
            ).fetchone()
            first_seen = str(existing["first_seen"]) if existing else now.isoformat()
            protected = int(existing["protected"] or 0) if existing else 0
            if protected:
                status = "rejected"
            conn.execute(
                """
                INSERT INTO learned_patterns (
                    pattern_type, scope, key, value, score, sample_count, confidence,
                    first_seen, last_updated, status, protected, reason
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(pattern_type, scope, key) DO UPDATE SET
                    value = excluded.value,
                    score = excluded.score,
                    sample_count = excluded.sample_count,
                    confidence = excluded.confidence,
                    last_updated = excluded.last_updated,
                    status = CASE WHEN learned_patterns.protected = 1 THEN 'rejected' ELSE excluded.status END,
                    reason = excluded.reason
                """,
                (
                    pattern_type,
                    scope,
                    key,
                    value,
                    float(score),
                    int(sample_count),
                    max(0.0, min(1.0, float(confidence))),
                    first_seen,
                    now.isoformat(),
                    status,
                    protected,
                    reason,
                ),
            )
            row = conn.execute(
                """
                SELECT * FROM learned_patterns
                WHERE pattern_type = ? AND scope = ? AND key = ?
                """,
                (pattern_type, scope, key),
            ).fetchone()
        return self._row_to_pattern(row)

    def learned_patterns(self, include_rejected: bool = False, limit: int = 100) -> list[LearnedPattern]:
        where = "" if include_rejected else "WHERE status != 'rejected'"
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM learned_patterns {where} ORDER BY last_updated DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._row_to_pattern(row) for row in rows]

    def upsert_hypothesis(
        self,
        pattern_type: str,
        scope: str,
        key: str,
        hypothesis: str,
        confidence: float,
        sample_count: int,
        status: str,
        reason: str | None = None,
    ) -> None:
        now = datetime.now()
        with self.connect() as conn:
            existing = conn.execute(
                """
                SELECT first_seen FROM behavior_hypotheses
                WHERE pattern_type = ? AND scope = ? AND key = ?
                """,
                (pattern_type, scope, key),
            ).fetchone()
            first_seen = str(existing["first_seen"]) if existing else now.isoformat()
            conn.execute(
                """
                INSERT INTO behavior_hypotheses (
                    pattern_type, scope, key, hypothesis, confidence, sample_count,
                    status, first_seen, last_updated, reason
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(pattern_type, scope, key) DO UPDATE SET
                    hypothesis = excluded.hypothesis,
                    confidence = excluded.confidence,
                    sample_count = excluded.sample_count,
                    status = excluded.status,
                    last_updated = excluded.last_updated,
                    reason = excluded.reason
                """,
                (
                    pattern_type,
                    scope,
                    key,
                    hypothesis,
                    max(0.0, min(1.0, confidence)),
                    sample_count,
                    status,
                    first_seen,
                    now.isoformat(),
                    reason,
                ),
            )

    def behavior_hypotheses_count(self) -> int:
        with self.connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS count FROM behavior_hypotheses").fetchone()
        return int(row["count"] or 0)

    def get_pattern(self, pattern_type: str, scope: str, key: str) -> LearnedPattern | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM learned_patterns
                WHERE pattern_type = ? AND scope = ? AND key = ?
                """,
                (pattern_type, scope, key),
            ).fetchone()
        return self._row_to_pattern(row) if row else None

    def reject_pattern(self, pattern_id: int) -> None:
        now = datetime.now().isoformat()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE learned_patterns
                SET status = 'rejected', protected = 1, last_updated = ?
                WHERE id = ?
                """,
                (now, pattern_id),
            )

    def reset_pattern(self, pattern_id: int) -> None:
        now = datetime.now().isoformat()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE learned_patterns
                SET value = '', score = 0, sample_count = 0, confidence = 0,
                    status = 'observing', protected = 0, reason = NULL, last_updated = ?
                WHERE id = ?
                """,
                (now, pattern_id),
            )

    def set_adaptive_preference(
        self,
        preference_key: str,
        value: str,
        policy_level: int,
        reason: str,
        pattern_id: int | None = None,
        status: str = "provisional",
    ) -> None:
        now = datetime.now()
        previous = self.get_adaptive_preference(preference_key)
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO adaptive_preferences (
                    preference_key, value, policy_level, status, reason, previous_value,
                    pattern_id, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(preference_key) DO UPDATE SET
                    previous_value = adaptive_preferences.value,
                    value = excluded.value,
                    policy_level = excluded.policy_level,
                    status = excluded.status,
                    reason = excluded.reason,
                    pattern_id = excluded.pattern_id,
                    updated_at = excluded.updated_at
                """,
                (
                    preference_key,
                    value,
                    policy_level,
                    status,
                    reason,
                    previous,
                    pattern_id,
                    now.isoformat(),
                    now.isoformat(),
                ),
            )

    def get_adaptive_preference(self, preference_key: str) -> str | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT value FROM adaptive_preferences WHERE preference_key = ? AND status != 'reverted'",
                (preference_key,),
            ).fetchone()
        return str(row["value"]) if row else None

    def revert_adaptive_preference(self, preference_key: str) -> None:
        now = datetime.now().isoformat()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE adaptive_preferences
                SET value = COALESCE(previous_value, value), status = 'reverted', updated_at = ?
                WHERE preference_key = ?
                """,
                (now, preference_key),
            )

    def log_adaptive_decision(
        self,
        reminder_id: int | None,
        strategy: str,
        policy_level: int,
        reason: str,
        metadata: dict[str, object] | None = None,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO adaptive_decisions (timestamp, reminder_id, strategy, policy_level, decision_reason, metadata)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    datetime.now().isoformat(),
                    reminder_id,
                    strategy,
                    policy_level,
                    reason,
                    json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True),
                ),
            )

    def pending_suggestions(self) -> list[sqlite3.Row]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM adaptive_suggestions WHERE status = 'pending' ORDER BY created_at DESC"
            ).fetchall()
        return rows

    def add_suggestion(
        self,
        suggestion_type: str,
        title: str,
        body: str,
        proposed_change: dict[str, object],
        reason: str,
        confidence: float,
        reminder_id: int | None = None,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO adaptive_suggestions (
                    suggestion_type, reminder_id, title, body, proposed_change,
                    policy_level, status, confidence, reason, created_at
                )
                VALUES (?, ?, ?, ?, ?, 3, 'pending', ?, ?, ?)
                """,
                (
                    suggestion_type,
                    reminder_id,
                    title,
                    body,
                    json.dumps(proposed_change, ensure_ascii=False, sort_keys=True),
                    confidence,
                    reason,
                    datetime.now().isoformat(),
                ),
            )

    def resolve_suggestion(self, suggestion_id: int, status: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE adaptive_suggestions
                SET status = ?, resolved_at = ?
                WHERE id = ?
                """,
                (status, datetime.now().isoformat(), suggestion_id),
            )

    def reminder_interactions(self, reminder_id: int | None = None) -> list[ReminderInteraction]:
        with self.connect() as conn:
            if reminder_id is None:
                rows = conn.execute(
                    "SELECT * FROM reminder_interactions ORDER BY timestamp DESC"
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM reminder_interactions
                    WHERE reminder_id = ?
                    ORDER BY timestamp DESC
                    """,
                    (reminder_id,),
                ).fetchall()
        return [self._row_to_interaction(row) for row in rows]

    def habit_summaries(self) -> list[ReminderHabitSummary]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    r.id,
                    r.text,
                    r.recurrence_rule,
                    COALESCE(r.notification_count, 0) AS notification_count,
                    COALESCE(r.completed_count, 0) AS completed_count,
                    COALESCE(r.snooze_count, 0) AS snooze_count,
                    COALESCE(r.ignored_count, 0) AS ignored_count,
                    AVG(
                        CASE
                            WHEN i.event_type IN ('completed', 'snoozed', 'ignored')
                            THEN i.response_seconds
                        END
                    ) AS average_response_seconds
                FROM reminders r
                LEFT JOIN reminder_interactions i ON i.reminder_id = r.id
                GROUP BY r.id
                ORDER BY r.due_at ASC
                """
            ).fetchall()
        return [
            ReminderHabitSummary(
                reminder_id=int(row["id"]),
                text=str(row["text"]),
                recurrence_rule=row["recurrence_rule"],
                notification_count=int(row["notification_count"] or 0),
                completed_count=int(row["completed_count"] or 0),
                snooze_count=int(row["snooze_count"] or 0),
                ignored_count=int(row["ignored_count"] or 0),
                average_response_seconds=(
                    int(row["average_response_seconds"]) if row["average_response_seconds"] is not None else None
                ),
                tendency=self._habit_tendency(
                    int(row["notification_count"] or 0),
                    int(row["completed_count"] or 0),
                    int(row["snooze_count"] or 0),
                    int(row["ignored_count"] or 0),
                ),
            )
            for row in rows
        ]

    def list_reminders(self, limit: int = 50) -> list[Reminder]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM reminders WHERE status != 'deleted' ORDER BY due_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._row_to_reminder(row) for row in rows]

    def get_reminder(self, reminder_id: int) -> Reminder | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM reminders WHERE id = ?", (reminder_id,)).fetchone()
        if row:
            trace_reminder("storage_read", due_at=row["due_at"], reminder_id=row["id"])
        return self._row_to_reminder(row) if row else None

    def add_usage_event(self, snapshot: ForegroundSnapshot) -> UsageEvent:
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO usage_events (timestamp, app_name, window_title, mode, idle_seconds)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    snapshot.timestamp.isoformat(),
                    snapshot.app_name,
                    snapshot.window_title,
                    snapshot.mode,
                    snapshot.idle_seconds,
                ),
            )
            event_id = int(cursor.lastrowid)
        return UsageEvent(
            id=event_id,
            timestamp=snapshot.timestamp,
            app_name=snapshot.app_name,
            window_title=snapshot.window_title,
            mode=snapshot.mode,
            idle_seconds=snapshot.idle_seconds,
        )

    def recent_events(self, limit: int = 30) -> list[UsageEvent]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM usage_events ORDER BY timestamp DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._row_to_event(row) for row in rows]

    def usage_events_between(self, start: datetime, end: datetime) -> list[UsageEvent]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM usage_events
                WHERE timestamp >= ? AND timestamp <= ?
                ORDER BY timestamp ASC
                """,
                (start.isoformat(), end.isoformat()),
            ).fetchall()
        return [self._row_to_event(row) for row in rows]

    def add_notification_event(
        self,
        source_app: str,
        received_at: datetime,
        title: str = "",
        body: str = "",
        context_key: str | None = None,
        current_mode: str | None = None,
        away_state: str | None = None,
        metadata: dict[str, object] | None = None,
    ) -> NotificationEvent:
        safe_title = self.summarize_text(title, 80) if title else ""
        safe_body = self.summarize_text(body, 80) if body and self.get_setting("notification_learning_store_body", "0") == "1" else ""
        payload = metadata or {}
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO notification_events (
                    source_app, title, body, received_at, metadata, context_key,
                    current_mode, away_state, status
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'observing')
                """,
                (
                    source_app,
                    safe_title,
                    safe_body,
                    received_at.isoformat(),
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    context_key,
                    current_mode,
                    away_state,
                ),
            )
            event_id = int(cursor.lastrowid)
        return NotificationEvent(event_id, source_app, safe_title, safe_body, received_at, json.dumps(payload, ensure_ascii=False))

    def pending_notification_events(self, now: datetime | None = None, limit: int = 50) -> list[NotificationEvent]:
        now = now or datetime.now()
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM notification_events
                WHERE status IN ('observing', 'snoozed')
                  AND first_view_at IS NULL
                  AND (snoozed_until IS NULL OR snoozed_until <= ?)
                ORDER BY received_at ASC
                LIMIT ?
                """,
                (now.isoformat(), limit),
            ).fetchall()
        return [self._row_to_notification_event(row) for row in rows]

    def get_notification_event(self, notification_event_id: int) -> NotificationEvent | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM notification_events WHERE id = ?",
                (notification_event_id,),
            ).fetchone()
        return self._row_to_notification_event(row) if row else None

    def mark_notification_viewed(
        self,
        notification_event_id: int,
        viewed_at: datetime,
        target_app: str | None = None,
    ) -> NotificationInteraction | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM notification_events WHERE id = ? AND first_view_at IS NULL",
                (notification_event_id,),
            ).fetchone()
            if not row:
                return None
            response_seconds = max(0, int((viewed_at - _dt(str(row["received_at"]))).total_seconds()))
            cursor = conn.execute(
                """
                UPDATE notification_events
                SET first_view_at = ?, status = 'visited'
                WHERE id = ? AND first_view_at IS NULL
                """,
                (viewed_at.isoformat(), notification_event_id),
            )
            if cursor.rowcount == 0:
                return None
            cursor = conn.execute(
                """
                INSERT INTO notification_interactions (
                    notification_event_id, event_type, timestamp, response_seconds, target_app
                )
                VALUES (?, 'visited', ?, ?, ?)
                """,
                (notification_event_id, viewed_at.isoformat(), response_seconds, target_app),
            )
            interaction_id = int(cursor.lastrowid)
        return NotificationInteraction(interaction_id, notification_event_id, "visited", viewed_at, response_seconds, target_app)

    def log_notification_interaction(
        self,
        notification_event_id: int,
        event_type: str,
        timestamp: datetime | None = None,
        response_seconds: int | None = None,
        target_app: str | None = None,
    ) -> NotificationInteraction:
        timestamp = timestamp or datetime.now()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO notification_interactions (
                    notification_event_id, event_type, timestamp, response_seconds, target_app
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (notification_event_id, event_type, timestamp.isoformat(), response_seconds, target_app),
            )
            interaction_id = int(cursor.lastrowid)
        return NotificationInteraction(interaction_id, notification_event_id, event_type, timestamp, response_seconds, target_app)

    def mark_notification_intervened(self, notification_event_id: int, at: datetime | None = None) -> bool:
        at = at or datetime.now()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE notification_events
                SET status = 'intervened',
                    intervention_count = COALESCE(intervention_count, 0) + 1,
                    last_intervention_at = ?
                WHERE id = ? AND status IN ('observing', 'snoozed')
                """,
                (at.isoformat(), notification_event_id),
            )
        return cursor.rowcount > 0

    def snooze_notification_event(self, notification_event_id: int, until: datetime) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE notification_events
                SET status = 'snoozed', snoozed_until = ?
                WHERE id = ? AND status IN ('observing', 'intervened', 'snoozed')
                """,
                (until.isoformat(), notification_event_id),
            )

    def dismiss_notification_event(self, notification_event_id: int) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE notification_events SET status = 'dismissed' WHERE id = ?",
                (notification_event_id,),
            )

    def _json_tuple(self, raw: str | None) -> tuple[str, ...]:
        if not raw:
            return ()
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            return ()
        if not isinstance(value, list):
            return ()
        return tuple(str(item) for item in value if str(item).strip())

    def _row_to_current_state(self, row: sqlite3.Row) -> CurrentState:
        break_pattern: dict[str, object] = {}
        if "break_pattern" in row.keys() and row["break_pattern"]:
            try:
                raw = json.loads(str(row["break_pattern"]))
                if isinstance(raw, dict):
                    break_pattern = raw
            except json.JSONDecodeError:
                break_pattern = {}
        return CurrentState(
            current_mode=str(row["current_mode"]),
            foreground_app=str(row["foreground_app"]),
            window_title_summary=str(row["window_title_summary"]),
            activity_summary=str(row["activity_summary"]),
            activity_started_at=_dt(str(row["activity_started_at"])),
            updated_at=_dt(str(row["updated_at"])),
            intent=str(row["intent"]) if "intent" in row.keys() else "unknown",
            intent_source=str(row["intent_source"]) if "intent_source" in row.keys() else "inferred",
            session_category=str(row["session_category"]) if "session_category" in row.keys() else "general",
            session_started_at=_dt(str(row["session_started_at"])) if "session_started_at" in row.keys() and row["session_started_at"] else None,
            session_duration_seconds=int(row["session_duration_seconds"] or 0) if "session_duration_seconds" in row.keys() else 0,
            previous_work_session_seconds=int(row["previous_work_session_seconds"] or 0) if "previous_work_session_seconds" in row.keys() else 0,
            break_pattern=break_pattern,
            recent_apps=self._json_tuple(str(row["recent_apps"] or "[]")),
            recent_events=self._json_tuple(str(row["recent_events"] or "[]")),
            active_reminders=self._json_tuple(str(row["active_reminders"] or "[]")),
            away=bool(row["away"]),
            manual_stop=bool(row["manual_stop"]),
            privacy_mode=bool(row["privacy_mode"]),
        )

    def _row_to_activity_session(self, row: sqlite3.Row) -> ActivitySession:
        metadata: dict[str, object] = {}
        if row["metadata"]:
            try:
                raw = json.loads(str(row["metadata"]))
                if isinstance(raw, dict):
                    metadata = raw
            except json.JSONDecodeError:
                metadata = {}
        return ActivitySession(
            id=int(row["id"]),
            started_at=_dt(str(row["started_at"])),
            last_seen_at=_dt(str(row["last_seen_at"])),
            ended_at=_dt(str(row["ended_at"])) if row["ended_at"] else None,
            app_name=str(row["app_name"]),
            mode=str(row["mode"]),
            category=str(row["category"]),
            title_summary=str(row["title_summary"] or ""),
            duration_seconds=int(row["duration_seconds"] or 0),
            metadata=metadata,
        )

    def _row_to_goal(self, row: sqlite3.Row) -> Goal:
        return Goal(
            id=int(row["id"]),
            goal_type=str(row["goal_type"]),
            text=str(row["text"]),
            source=str(row["source"]),
            confidence=float(row["confidence"] or 0),
            status=str(row["status"] or "active"),
            created_at=_dt(str(row["created_at"])),
            updated_at=_dt(str(row["updated_at"])),
        )

    def _row_to_reminder(self, row: sqlite3.Row) -> Reminder:
        return Reminder(
            id=int(row["id"]),
            text=str(row["text"]),
            due_at=_dt(str(row["due_at"])),
            created_at=_dt(str(row["created_at"])),
            fired_at=_dt(str(row["fired_at"])) if row["fired_at"] else None,
            status=str(row["status"] or "pending"),
            notified_at=_dt(str(row["notified_at"])) if row["notified_at"] else None,
            completed_at=_dt(str(row["completed_at"])) if row["completed_at"] else None,
            ignored_at=_dt(str(row["ignored_at"])) if row["ignored_at"] else None,
            snoozed_until=_dt(str(row["snoozed_until"])) if row["snoozed_until"] else None,
            recurrence_rule=str(row["recurrence_rule"]) if row["recurrence_rule"] else None,
            next_due_at=_dt(str(row["next_due_at"])) if row["next_due_at"] else None,
            snooze_count=int(row["snooze_count"] or 0),
            notification_count=int(row["notification_count"] or 0),
            completed_count=int(row["completed_count"] or 0),
            ignored_count=int(row["ignored_count"] or 0),
            last_snoozed_at=_dt(str(row["last_snoozed_at"])) if row["last_snoozed_at"] else None,
            importance=str(row["importance"] or "normal"),
            category=str(row["category"] or "general"),
            context_rule_id=int(row["context_rule_id"]) if "context_rule_id" in row.keys() and row["context_rule_id"] is not None else None,
            time_inferred=bool(row["time_inferred"]) if "time_inferred" in row.keys() else False,
            inference_reason=str(row["inference_reason"]) if "inference_reason" in row.keys() and row["inference_reason"] else None,
        )

    def _row_to_context_reminder(self, row: sqlite3.Row) -> ContextReminder:
        return ContextReminder(
            id=int(row["id"]),
            text=str(row["text"]),
            app_pattern=str(row["app_pattern"]),
            trigger=str(row["trigger"]),
            window_title_pattern=str(row["window_title_pattern"]) if row["window_title_pattern"] else None,
            created_at=_dt(str(row["created_at"])),
            status=str(row["status"] or "waiting"),
            triggered_at=_dt(str(row["triggered_at"])) if row["triggered_at"] else None,
            reminder_id=int(row["reminder_id"]) if row["reminder_id"] is not None else None,
            last_seen_active=bool(row["last_seen_active"]),
        )

    def _context_rule_matches(self, rule: ContextReminder, snapshot: ForegroundSnapshot) -> bool:
        app = (snapshot.app_name or "").lower()
        title = (snapshot.window_title or "").lower()
        app_match = rule.app_pattern in app
        title_match = True
        if rule.window_title_pattern:
            title_match = rule.window_title_pattern.lower() in title
        return app_match and title_match

    def _row_to_event(self, row: sqlite3.Row) -> UsageEvent:
        return UsageEvent(
            id=int(row["id"]),
            timestamp=_dt(str(row["timestamp"])),
            app_name=str(row["app_name"]),
            window_title=str(row["window_title"]),
            mode=str(row["mode"]),
            idle_seconds=int(row["idle_seconds"]),
        )

    def _row_to_notification_event(self, row: sqlite3.Row) -> NotificationEvent:
        return NotificationEvent(
            id=int(row["id"]),
            source_app=str(row["source_app"]),
            title=str(row["title"] or ""),
            body=str(row["body"] or ""),
            received_at=_dt(str(row["received_at"])),
            metadata=str(row["metadata"]) if row["metadata"] else None,
        )

    def _row_to_assistant_event(self, row: sqlite3.Row) -> AssistantEvent:
        metadata: dict[str, object] = {}
        if row["metadata"]:
            try:
                raw = json.loads(str(row["metadata"]))
                if isinstance(raw, dict):
                    metadata = raw
            except json.JSONDecodeError:
                metadata = {}
        return AssistantEvent(
            id=int(row["id"]),
            timestamp=_dt(str(row["timestamp"])),
            event_type=str(row["event_type"]),
            source=str(row["source"]),
            context_mode=str(row["context_mode"]) if row["context_mode"] else None,
            app_name=str(row["app_name"]) if row["app_name"] else None,
            window_title_summary=str(row["window_title_summary"]) if row["window_title_summary"] else None,
            related_reminder_id=int(row["related_reminder_id"]) if row["related_reminder_id"] is not None else None,
            series_id=str(row["series_id"]) if row["series_id"] else None,
            metadata=metadata,
        )

    def _row_to_pattern(self, row: sqlite3.Row) -> LearnedPattern:
        return LearnedPattern(
            id=int(row["id"]),
            pattern_type=str(row["pattern_type"]),
            scope=str(row["scope"]),
            key=str(row["key"]),
            value=str(row["value"]),
            score=float(row["score"] or 0),
            sample_count=int(row["sample_count"] or 0),
            confidence=float(row["confidence"] or 0),
            first_seen=_dt(str(row["first_seen"])),
            last_updated=_dt(str(row["last_updated"])),
            status=str(row["status"] or "observing"),
            protected=bool(row["protected"]),
            reason=str(row["reason"]) if row["reason"] else None,
        )

    def _row_to_interaction(self, row: sqlite3.Row) -> ReminderInteraction:
        return ReminderInteraction(
            id=int(row["id"]),
            reminder_id=int(row["reminder_id"]),
            event_type=str(row["event_type"]),
            timestamp=_dt(str(row["timestamp"])),
            response_seconds=int(row["response_seconds"]) if row["response_seconds"] is not None else None,
            current_mode=str(row["current_mode"]) if row["current_mode"] else None,
            source=str(row["source"] or "assistant_reminder"),
        )

    def _finish_reminder(
        self,
        reminder_id: int,
        status: str,
        timestamp: datetime,
        expected_due_at: datetime | None = None,
    ) -> bool:
        status_column = "completed_at" if status == "completed" else "ignored_at"
        count_column = "completed_count" if status == "completed" else "ignored_count"
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM reminders WHERE id = ?", (reminder_id,)).fetchone()
            if not row:
                return False
            current_due_at = _dt(str(row["due_at"]))
            if expected_due_at is not None and current_due_at != expected_due_at:
                return False
            if str(row["status"] or "") not in {"notified", "pending_due", "pending", "snoozed"}:
                return False
            if status == "completed" and expected_due_at is None and str(row["status"] or "") in {"pending", "snoozed"} and current_due_at > timestamp:
                return False

            recurrence_rule = row["recurrence_rule"]
            if recurrence_rule:
                next_schedule = recurrence_next_after(str(recurrence_rule), current_due_at)
                next_due_at = next_schedule.due_at if next_schedule else None
                following_due_at = next_schedule.next_due_at if next_schedule else None
                if following_due_at is None:
                    cursor = conn.execute(
                        f"""
                        UPDATE reminders
                        SET status = ?,
                            {status_column} = ?,
                            fired_at = NULL,
                            notified_at = NULL,
                            snoozed_until = NULL,
                            next_due_at = NULL,
                            {count_column} = COALESCE({count_column}, 0) + 1
                        WHERE id = ? AND status IN ('notified', 'pending_due', 'pending', 'snoozed') AND due_at = ?
                        """,
                        (status, timestamp.isoformat(), reminder_id, current_due_at.isoformat()),
                    )
                else:
                    cursor = conn.execute(
                        f"""
                        UPDATE reminders
                        SET status = 'pending',
                            due_at = ?,
                            next_due_at = ?,
                            {status_column} = ?,
                            fired_at = NULL,
                            notified_at = NULL,
                            snoozed_until = NULL,
                            {count_column} = COALESCE({count_column}, 0) + 1
                        WHERE id = ? AND status IN ('notified', 'pending_due', 'pending', 'snoozed') AND due_at = ?
                        """,
                        (
                            next_due_at.isoformat(),
                            following_due_at.isoformat(),
                            timestamp.isoformat(),
                            reminder_id,
                            current_due_at.isoformat(),
                        ),
                    )
            else:
                cursor = conn.execute(
                    f"""
                    UPDATE reminders
                    SET status = ?,
                        {status_column} = ?,
                        fired_at = NULL,
                        notified_at = NULL,
                        snoozed_until = NULL,
                        {count_column} = COALESCE({count_column}, 0) + 1
                    WHERE id = ? AND status IN ('notified', 'pending_due', 'pending', 'snoozed') AND due_at = ?
                    """,
                    (status, timestamp.isoformat(), reminder_id, current_due_at.isoformat()),
                )
            changed = cursor.rowcount > 0
        if not changed:
            return False
        reminder = self.get_reminder(reminder_id)
        self.record_event(
            event_type=f"reminder_{status}",
            source="reminder_popup",
            related_reminder_id=reminder_id,
            series_id=self.series_id_for(reminder_id, reminder.text, reminder.recurrence_rule) if reminder else None,
            metadata={"timestamp": timestamp.isoformat()},
        )
        return True

    def _response_seconds(self, reminder_id: int, timestamp: datetime) -> int | None:
        reminder = self.get_reminder(reminder_id)
        if not reminder or not reminder.notified_at:
            return None
        return max(0, int((timestamp - reminder.notified_at).total_seconds()))

    def summarize_text(self, text: str, max_chars: int = 80) -> str:
        value = " ".join((text or "").split())
        blacklist = self._privacy_blacklist()
        if any(token and token.lower() in value.lower() for token in blacklist):
            return "[隱私內容已略過]"
        if len(value) <= max_chars:
            return value
        return value[: max_chars - 1] + "…"

    def should_record_app(self, app_name: str | None, window_title: str | None = None) -> bool:
        app = (app_name or "").lower()
        title = (window_title or "").lower()
        combined = f"{app} {title}"
        for token in self._privacy_blacklist():
            value = token.lower()
            if not value:
                continue
            if value.endswith(".exe") and app == value:
                return False
            if value in combined:
                return False
        return True

    def learning_enabled(self) -> bool:
        return self.get_setting("learning_enabled", "1") == "1"

    def privacy_mode_enabled(self) -> bool:
        return self.get_setting("privacy_mode_enabled", "0") == "1"

    def _privacy_blacklist(self) -> list[str]:
        raw = self.get_setting("excluded_apps", None) or self.get_setting(
            "privacy_app_blacklist",
            "1password.exe,bitwarden.exe,keepass.exe,lastpass.exe,dashlane.exe,bank",
        )
        if raw.strip().startswith("["):
            try:
                values = json.loads(raw)
                if isinstance(values, list):
                    return [str(item).strip() for item in values if str(item).strip()]
            except json.JSONDecodeError:
                pass
        return [item.strip() for item in (raw or "").split(",") if item.strip()]

    def infer_reminder_category(self, text: str) -> str:
        lower = text.lower()
        if any(token in lower for token in ("藥", "服藥", "健康", "醫", "血壓")):
            return "health"
        if any(token in lower for token in ("睡", "起床", "洗澡", "吃飯")):
            return "life"
        if any(token in lower for token in ("工作", "開會", "回信", "報告", "專案")):
            return "work"
        return "general"

    def infer_reminder_importance(self, text: str) -> str:
        category = self.infer_reminder_category(text)
        lower = text.lower()
        if category == "health" or any(token in lower for token in ("必要", "重要", "不能忘")):
            return "critical"
        if any(token in lower for token in ("有空", "隨便", "不急")):
            return "low"
        return "normal"

    def series_id_for(self, reminder_id: int, text: str, recurrence_rule: str | None) -> str:
        if recurrence_rule:
            return f"series:{self.summarize_text(text, 40)}:{recurrence_rule}"
        return f"reminder:{reminder_id}"

    def _habit_tendency(
        self,
        notification_count: int,
        completed_count: int,
        snooze_count: int,
        ignored_count: int,
    ) -> str:
        if notification_count == 0:
            return "尚未累積"
        if snooze_count >= max(2, completed_count + ignored_count):
            return "常延後"
        if completed_count and ignored_count == 0 and snooze_count == 0:
            return "通常很快處理"
        if completed_count >= notification_count and snooze_count == 0 and ignored_count == 0:
            return "通常會完成"
        if ignored_count >= max(2, completed_count):
            return "常忽略"
        return "持續學習中"

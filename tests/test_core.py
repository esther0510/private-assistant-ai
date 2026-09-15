from __future__ import annotations

import json
import math
import subprocess
import tempfile
import unittest
import sqlite3
from unittest.mock import patch
from datetime import datetime, timedelta
from pathlib import Path

import requests

from personal_ai_assistant.ai import AssistantAnalyzer
from personal_ai_assistant.action_safety import classify_action_risk
from personal_ai_assistant.audio_devices import (
    AudioInputDevice,
    SoundDeviceAudioInputProvider,
    SYSTEM_DEFAULT_MICROPHONE_ID,
    build_sounddevice_identifier,
    recoverable_sounddevice_identifier,
    sounddevice_index_from_identifier,
)
from personal_ai_assistant.adaptive import (
    PRESENTATION_DEFER_ACTIVE,
    PRESENTATION_DEFER_GAME_END,
    PRESENTATION_REPEAT,
    AdaptivePolicy,
    AdaptiveReminderStrategy,
)
from personal_ai_assistant.app_resolver import AppResolver, ResolveCandidate, ResolveResult
from personal_ai_assistant.advisor_core import (
    ADVISOR_CUSTOM_MODEL,
    ADVISOR_DISABLED_SELECTION,
    ADVISOR_PROVIDER_PRESETS,
    AdvisorModelOption,
    AdvisorService,
    ContextBuilder,
    CurrentStateBuilder,
    GoalEngine,
    InterventionPolicy,
    NextActionEngine,
    OpenAICompatibleAdvisorProvider,
    advisor_chat_completions_url,
    advisor_load_health,
    advisor_record_failure,
    advisor_http_error_message,
    advisor_key_url,
    advisor_preset_endpoint,
    advisor_record_success,
    advisor_request_headers,
    advisor_recommended_models,
    parse_advisor_response,
    summarize_activity,
)
from personal_ai_assistant.command_router import route_assistant_command
from personal_ai_assistant.activity_sessions import (
    ActivitySessionManager,
    break_pattern_payload,
    break_threshold_seconds,
)
from personal_ai_assistant.assistant.dead_loop import DeadLoopDetector
from personal_ai_assistant.classifier import classify_mode
from personal_ai_assistant.db import AssistantStore
from personal_ai_assistant.display import (
    choose_popup_screen,
    choose_status_screen,
    classify_window_presentation,
    scaled_size,
    status_bar_geometry,
)
from personal_ai_assistant.events import EventBus
from personal_ai_assistant.hotkeys import hotkey_error_message, parse_hotkey
from personal_ai_assistant.inbox import looks_like_today_inbox, should_suggest_today_inbox, strip_inbox_prefix, today_inbox_due_at
from personal_ai_assistant.integrations.ai_handoff import AIHandoffProvider, PROVIDER_URLS
from personal_ai_assistant.learning import LearningEngine
from personal_ai_assistant.models import AssistantEvent, CurrentState, DisplayGeometry, ForegroundSnapshot, NextAction, Reminder
from personal_ai_assistant.reminders import cycle_status, next_occurrence, parse_natural_reminder, recurrence_occurrence
from personal_ai_assistant.settings import SettingsService
from personal_ai_assistant.startup import startup_command
from personal_ai_assistant.summary import build_daily_summary
from personal_ai_assistant.ui_formatters import format_event_card_text, format_reminder_card_text
from personal_ai_assistant.updates import check_latest_release, should_check_for_updates
from personal_ai_assistant.voice_command import (
    BaselineAcousticKeywordMatcher,
    FallbackWakeWordProvider,
    FixedKeywordWakeWordProvider,
    PrefixWakeWordProvider,
    VoiceAssistantPipeline,
    WakeTemplateStore,
    WakeSettings,
    WakeTestResult,
    acoustic_features_from_pcm,
    calibrated_baseline_threshold,
    enrollment_sample_from_pcm,
    enrollment_quality,
    first_voice_prefix,
    install_voice_dependencies,
    multi_template_similarity,
    sensitivity_threshold,
    sequence_similarity,
    strip_wake_phrase,
    validate_assistant_name,
    wake_phrase_matches,
    wake_aliases_for_settings,
)
from personal_ai_assistant.window_activation import ActivateOrLaunchService, AppWindow, rank_matching_window


class CoreTests(unittest.TestCase):
    def test_parse_reminder_same_day(self) -> None:
        parsed = parse_natural_reminder("18:30 喝水", datetime(2026, 9, 7, 12, 0))
        self.assertEqual(parsed.text, "喝水")
        self.assertEqual(parsed.due_at, datetime(2026, 9, 7, 18, 30))

    def test_parse_reminder_rolls_to_tomorrow(self) -> None:
        parsed = parse_natural_reminder("00:00 閱讀", datetime(2026, 9, 7, 12, 0))
        self.assertEqual(parsed.text, "閱讀")
        self.assertEqual(parsed.due_at, datetime(2026, 9, 8, 0, 0))

    def test_parse_relative_minutes_and_chinese_numbers(self) -> None:
        now = datetime(2026, 9, 7, 14, 45)
        cases = {
            "10分鐘後提醒我吃飯": (datetime(2026, 9, 7, 14, 55), "吃飯"),
            "十分鐘後提醒我吃飯": (datetime(2026, 9, 7, 14, 55), "吃飯"),
            "十五分鐘後提醒我喝水": (datetime(2026, 9, 7, 15, 0), "喝水"),
            "半小時後提醒我洗衣服": (datetime(2026, 9, 7, 15, 15), "洗衣服"),
            "1小時後提醒我出門": (datetime(2026, 9, 7, 15, 45), "出門"),
            "一小時後提醒我出門": (datetime(2026, 9, 7, 15, 45), "出門"),
            "兩個小時後提醒我睡覺": (datetime(2026, 9, 7, 16, 45), "睡覺"),
            "90分鐘後提醒我": (datetime(2026, 9, 7, 16, 15), "提醒"),
            "一個半小時後提醒我": (datetime(2026, 9, 7, 16, 15), "提醒"),
        }
        for text, (due_at, label) in cases.items():
            with self.subTest(text=text):
                parsed = parse_natural_reminder(text, now)
                self.assertEqual(parsed.due_at, due_at)
                self.assertEqual(parsed.text, label)

    def test_parse_natural_dates_and_periods(self) -> None:
        now = datetime(2026, 9, 7, 14, 45)
        cases = {
            "下午3點提醒我": datetime(2026, 9, 7, 15, 0),
            "下午3點半提醒我": datetime(2026, 9, 7, 15, 30),
            "今晚10點半提醒我吃藥": datetime(2026, 9, 7, 22, 30),
            "今天晚上10點提醒我": datetime(2026, 9, 7, 22, 0),
            "明天早上8點提醒我": datetime(2026, 9, 8, 8, 0),
            "明天下午3點提醒我": datetime(2026, 9, 8, 15, 0),
            "明天晚上10點半提醒我": datetime(2026, 9, 8, 22, 30),
            "後天晚上9點提醒我": datetime(2026, 9, 9, 21, 0),
            "9月10日晚上10點半提醒我": datetime(2026, 9, 10, 22, 30),
            "9/10 22:30提醒我": datetime(2026, 9, 10, 22, 30),
        }
        for text, due_at in cases.items():
            with self.subTest(text=text):
                parsed = parse_natural_reminder(text, now)
                self.assertEqual(parsed.due_at, due_at)

    def test_ambiguous_clock_time_prefers_nearby_pm(self) -> None:
        parsed = parse_natural_reminder("5點半提醒我收垃圾", datetime(2026, 9, 10, 17, 20))
        self.assertEqual(parsed.due_at, datetime(2026, 9, 10, 17, 30))
        self.assertTrue(parsed.time_inferred)
        self.assertFalse(parsed.needs_confirmation)

        parsed = parse_natural_reminder("5點提醒我", datetime(2026, 9, 10, 16, 0))
        self.assertEqual(parsed.due_at, datetime(2026, 9, 10, 17, 0))
        self.assertTrue(parsed.time_inferred)

    def test_ambiguous_clock_time_recent_pm_is_late_not_tomorrow_am(self) -> None:
        parsed = parse_natural_reminder("5:30提醒我收垃圾", datetime(2026, 9, 10, 17, 32))
        self.assertEqual(parsed.due_at, datetime(2026, 9, 10, 17, 30))
        self.assertTrue(parsed.time_inferred)
        self.assertIn("剛過", parsed.inference_reason or "")

    def test_explicit_daypart_and_24_hour_time_override_ambiguous_heuristic(self) -> None:
        now = datetime(2026, 9, 10, 17, 20)
        cases = {
            "凌晨5點提醒我": datetime(2026, 9, 11, 5, 0),
            "下午5點提醒我": datetime(2026, 9, 11, 17, 0),
            "17:30提醒我": datetime(2026, 9, 10, 17, 30),
        }
        for text, due_at in cases.items():
            with self.subTest(text=text):
                parsed = parse_natural_reminder(text, now, active_hour_scores={17: 1.0})
                self.assertEqual(parsed.due_at, due_at)
                self.assertFalse(parsed.time_inferred)

    def test_ambiguous_low_confidence_requires_confirmation(self) -> None:
        parsed = parse_natural_reminder("5點提醒我", datetime(2026, 9, 10, 1, 0), user_is_active=False)
        self.assertTrue(parsed.needs_confirmation)
        self.assertEqual(
            parsed.ambiguous_time_options,
            (datetime(2026, 9, 10, 5, 0), datetime(2026, 9, 10, 17, 0)),
        )

    def test_learned_active_hours_bias_ambiguous_time_but_not_explicit_daypart(self) -> None:
        parsed = parse_natural_reminder(
            "5點提醒我",
            datetime(2026, 9, 10, 1, 0),
            active_hour_scores={17: 1.0},
            user_is_active=False,
        )
        self.assertEqual(parsed.due_at, datetime(2026, 9, 10, 17, 0))
        self.assertTrue(parsed.time_inferred)

        explicit = parse_natural_reminder(
            "凌晨5點提醒我",
            datetime(2026, 9, 10, 1, 0),
            active_hour_scores={17: 1.0},
        )
        self.assertEqual(explicit.due_at, datetime(2026, 9, 10, 5, 0))
        self.assertFalse(explicit.time_inferred)

    def test_parse_past_natural_time_rolls_forward(self) -> None:
        parsed = parse_natural_reminder("下午3點提醒我", datetime(2026, 9, 7, 15, 1))
        self.assertEqual(parsed.due_at, datetime(2026, 9, 8, 15, 0))

    def test_parse_daily_recurrence(self) -> None:
        parsed = parse_natural_reminder("每天 21:00 伸展", datetime(2026, 9, 7, 12, 0))
        self.assertEqual(parsed.text, "伸展")
        self.assertEqual(parsed.due_at, datetime(2026, 9, 7, 21, 0))
        self.assertEqual(parsed.next_due_at, datetime(2026, 9, 8, 21, 0))
        self.assertIn('"freq": "daily"', parsed.recurrence_rule or "")

    def test_parse_daily_natural_language_recurrence(self) -> None:
        parsed = parse_natural_reminder("每天晚上10點半吃藥", datetime(2026, 9, 7, 12, 0))
        self.assertEqual(parsed.text, "吃藥")
        self.assertEqual(parsed.due_at, datetime(2026, 9, 7, 22, 30))
        self.assertEqual(parsed.next_due_at, datetime(2026, 9, 8, 22, 30))
        self.assertIn('"freq": "daily"', parsed.recurrence_rule or "")

        compact = parse_natural_reminder("每天21:00提醒我伸展", datetime(2026, 9, 7, 12, 0))
        self.assertEqual(compact.text, "伸展")
        self.assertEqual(compact.due_at, datetime(2026, 9, 7, 21, 0))

    def test_parse_weekly_multi_day_recurrence(self) -> None:
        parsed = parse_natural_reminder("每週二、四、六 18:30 喝水", datetime(2026, 9, 7, 12, 0))
        self.assertEqual(parsed.text, "喝水")
        self.assertEqual(parsed.due_at, datetime(2026, 9, 8, 18, 30))
        self.assertEqual(parsed.next_due_at, datetime(2026, 9, 10, 18, 30))
        self.assertIn('"days": [1, 3, 5]', parsed.recurrence_rule or "")

    def test_parse_weekly_natural_language_recurrence(self) -> None:
        parsed = parse_natural_reminder("每週二、四、六晚上6點半丟垃圾", datetime(2026, 9, 7, 12, 0))
        self.assertEqual(parsed.text, "丟垃圾")
        self.assertEqual(parsed.due_at, datetime(2026, 9, 8, 18, 30))
        self.assertIn('"days": [1, 3, 5]', parsed.recurrence_rule or "")

        compact = parse_natural_reminder("每週一三五早上9點提醒我運動", datetime(2026, 9, 7, 12, 0))
        self.assertEqual(compact.text, "運動")
        self.assertEqual(compact.due_at, datetime(2026, 9, 9, 9, 0))

    def test_parse_cycle_reminder_requires_confirmation(self) -> None:
        parsed = parse_natural_reminder(
            "從 9/10 開始，每天 22:30 保養任務，活動 21 天休息 7 天循環",
            datetime(2026, 9, 7, 12, 0),
        )
        self.assertTrue(parsed.needs_confirmation)
        self.assertEqual(parsed.text, "保養任務")
        self.assertEqual(parsed.due_at, datetime(2026, 9, 10, 22, 30))
        self.assertEqual(parsed.next_due_at, datetime(2026, 9, 11, 22, 30))
        self.assertEqual(parsed.missing_fields, ())
        self.assertIn('"active_days": 21', parsed.recurrence_rule or "")
        self.assertIn("是否循環：是", parsed.confirmation_summary or "")

    def test_parse_complex_missing_break_does_not_assume(self) -> None:
        parsed = parse_natural_reminder(
            "我每天要保養任務，週期 21 天，晚上 10點半，9/10 開始",
            datetime(2026, 9, 7, 12, 0),
        )
        self.assertTrue(parsed.needs_confirmation)
        self.assertIn("休息天數", parsed.missing_fields)
        self.assertIn("休息天數：未指定", parsed.confirmation_summary or "")

    def test_parse_finite_active_days_cycle(self) -> None:
        parsed = parse_natural_reminder(
            "每天清潔設備，從明天開始，連續 7 天，晚上 9 點",
            datetime(2026, 9, 7, 12, 0),
        )
        self.assertTrue(parsed.needs_confirmation)
        self.assertEqual(parsed.text, "清潔設備")
        self.assertEqual(parsed.due_at, datetime(2026, 9, 8, 21, 0))
        self.assertEqual(parsed.next_due_at, datetime(2026, 9, 9, 21, 0))

    def test_parse_interval_reminder(self) -> None:
        parsed = parse_natural_reminder(
            "每 3 天提醒我換一次東西，從 9 月 15 日開始 08:00",
            datetime(2026, 9, 7, 12, 0),
        )
        self.assertTrue(parsed.needs_confirmation)
        self.assertEqual(parsed.text, "換一次東西")
        self.assertEqual(parsed.due_at, datetime(2026, 9, 15, 8, 0))
        self.assertEqual(parsed.next_due_at, datetime(2026, 9, 18, 8, 0))

    def test_parse_interval_missing_time_requires_confirmation(self) -> None:
        parsed = parse_natural_reminder(
            "每3天提醒我換一次東西，從9月15日開始",
            datetime(2026, 9, 7, 12, 0),
        )
        self.assertTrue(parsed.needs_confirmation)
        self.assertEqual(parsed.text, "換一次東西")
        self.assertEqual(parsed.missing_fields, ("提醒時間",))

    def test_parse_every_two_days_natural_language(self) -> None:
        parsed = parse_natural_reminder("每兩天晚上9點提醒我", datetime(2026, 9, 7, 12, 0))
        self.assertEqual(parsed.text, "提醒")
        self.assertEqual(parsed.due_at, datetime(2026, 9, 7, 21, 0))
        self.assertIn('"interval_days": 2', parsed.recurrence_rule or "")

    def test_parse_context_and_today_inbox_reminders(self) -> None:
        now = datetime(2026, 9, 7, 10, 0)
        context = parse_natural_reminder("遊戲關掉後提醒我洗澡", now)
        self.assertEqual(context.kind, "context")
        self.assertEqual(context.context_name, "遊戲")
        self.assertEqual(context.context_trigger, "close")
        self.assertEqual(context.text, "洗澡")

        opened = parse_natural_reminder("下次打開Unity提醒我改碰撞", now)
        self.assertEqual(opened.kind, "context")
        self.assertEqual(opened.context_name, "Unity")
        self.assertEqual(opened.context_trigger, "open")
        self.assertEqual(opened.text, "改碰撞")

        inbox = parse_natural_reminder("今天有空提醒我洗衣服", now)
        self.assertEqual(inbox.kind, "today_inbox")
        self.assertEqual(inbox.text, "洗衣服")
        self.assertEqual(inbox.due_at, datetime(2026, 9, 7, 23, 59))

    def test_quick_add_phrase_uses_shared_parser(self) -> None:
        parsed = parse_natural_reminder("十分鐘後提醒我吃飯", datetime(2026, 9, 7, 14, 45))
        self.assertEqual(parsed.text, "吃飯")
        self.assertEqual(parsed.due_at, datetime(2026, 9, 7, 14, 55))

        ambiguous = parse_natural_reminder("5點半提醒我收垃圾", datetime(2026, 9, 10, 17, 20))
        self.assertEqual(ambiguous.due_at, datetime(2026, 9, 10, 17, 30))

    def test_classifier_modes(self) -> None:
        self.assertEqual(classify_mode("Code.exe", "project - Visual Studio Code"), "工作")
        self.assertEqual(classify_mode("chrome.exe", "YouTube - video"), "看片")
        self.assertEqual(classify_mode("Discord.exe", "general"), "聊天")
        self.assertEqual(classify_mode("whatever.exe", "anything", idle_seconds=301), "閒置")

    def test_current_state_update_uses_readable_activity_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = AssistantStore(Path(temp_dir) / "assistant.sqlite3")
            builder = CurrentStateBuilder(store)
            snapshot = ForegroundSnapshot(
                datetime(2026, 9, 8, 10, 0),
                "ChatGPT.exe",
                "私人助理 bug - ChatGPT",
                "工作",
                0,
            )

            state = builder.build(snapshot, snapshot.timestamp, False, False, False)
            saved = builder.save_if_changed(state)
            duplicate = builder.save_if_changed(state)

            self.assertTrue(saved)
            self.assertFalse(duplicate)
            self.assertIn("私人助理開發", state.activity_summary)
            self.assertEqual(store.load_current_state().activity_summary, state.activity_summary)
            self.assertIn("Telegram", summarize_activity("Telegram.exe", "台股 股票", "聊天"))

    def test_manual_goal_is_not_overwritten_by_inferred_goal(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = AssistantStore(Path(temp_dir) / "assistant.sqlite3")
            goals = GoalEngine(store)
            goals.set_manual_goal("今天測試提醒系統")
            state = CurrentStateBuilder(store).build(
                ForegroundSnapshot(datetime(2026, 9, 8, 10, 0), "Telegram.exe", "台股 股票", "聊天", 0),
                datetime(2026, 9, 8, 10, 0),
                False,
                False,
                False,
            )

            inferred = goals.infer_goal(state)

            self.assertIsNone(inferred)
            current = store.current_goal()
            self.assertIsNotNone(current)
            self.assertEqual(current.text, "今天測試提醒系統")
            self.assertEqual(current.source, "manual")

    def test_next_action_rule_fallback_goal_deviation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = AssistantStore(Path(temp_dir) / "assistant.sqlite3")
            goal = GoalEngine(store).set_manual_goal("把私人助理的通知 bug 修好")
            state = CurrentStateBuilder(store).build(
                ForegroundSnapshot(datetime(2026, 9, 8, 10, 0), "Telegram.exe", "一般聊天", "聊天", 0),
                datetime(2026, 9, 8, 9, 30),
                False,
                False,
                False,
            )

            action = NextActionEngine(store).evaluate(state, goal, datetime(2026, 9, 8, 10, 0))

            self.assertEqual(action.source, "rule")
            self.assertIn("回到目前目標", action.action_text)
            self.assertEqual(action.intervention_level, "status_only")

    def test_youtube_shorts_title_changes_do_not_reset_session(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = AssistantStore(Path(temp_dir) / "assistant.sqlite3")
            manager = ActivitySessionManager(store, grace_period_seconds=90)
            first = manager.observe(ForegroundSnapshot(datetime(2026, 9, 8, 10, 0), "chrome.exe", "Short A - YouTube", "看片", 0))
            second = manager.observe(ForegroundSnapshot(datetime(2026, 9, 8, 10, 4), "chrome.exe", "Short B - YouTube", "看片", 0))

            self.assertEqual(first.session.id, second.session.id)
            self.assertEqual(second.session.started_at, datetime(2026, 9, 8, 10, 0))
            self.assertEqual(second.session.duration_seconds, 240)
            self.assertEqual(len(store.activity_sessions_between(datetime(2026, 9, 8, 9, 0), datetime(2026, 9, 8, 11, 0))), 1)

    def test_real_app_or_mode_change_switches_session(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = AssistantStore(Path(temp_dir) / "assistant.sqlite3")
            manager = ActivitySessionManager(store)
            first = manager.observe(ForegroundSnapshot(datetime(2026, 9, 8, 10, 0), "chrome.exe", "YouTube", "看片", 0))
            second = manager.observe(ForegroundSnapshot(datetime(2026, 9, 8, 10, 5), "Code.exe", "main.py", "工作", 0))

            self.assertNotEqual(first.session.id, second.session.id)
            ended = store.activity_sessions_between(datetime(2026, 9, 8, 9, 0), datetime(2026, 9, 8, 11, 0))[0]
            self.assertEqual(ended.ended_at, datetime(2026, 9, 8, 10, 5))

    def test_break_duration_ignores_video_title_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = AssistantStore(Path(temp_dir) / "assistant.sqlite3")
            manager = ActivitySessionManager(store)
            work = manager.observe(ForegroundSnapshot(datetime(2026, 9, 8, 9, 0), "Code.exe", "main.py", "工作", 0)).session
            manager.observe(ForegroundSnapshot(datetime(2026, 9, 8, 10, 0), "chrome.exe", "Short A - YouTube", "看片", 0))
            session = manager.observe(ForegroundSnapshot(datetime(2026, 9, 8, 10, 12), "chrome.exe", "Short B - YouTube", "看片", 0)).session
            state = CurrentStateBuilder(store).build(
                ForegroundSnapshot(datetime(2026, 9, 8, 10, 12), "chrome.exe", "Short B - YouTube", "看片", 0),
                session.started_at,
                False,
                False,
                False,
                current_session=session,
            )

            self.assertEqual(work.duration_seconds, 0)
            self.assertEqual(state.intent, "intentional_break")
            self.assertEqual(state.session_duration_seconds, 12 * 60)

    def test_normal_break_does_not_suggest_return_to_goal(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = AssistantStore(Path(temp_dir) / "assistant.sqlite3")
            goal = GoalEngine(store).set_manual_goal("把私人助理的通知 bug 修好")
            work = store.start_activity_session(datetime(2026, 9, 8, 9, 0), "Code.exe", "工作", "work_development", "main.py")
            store.end_activity_session(work.id, datetime(2026, 9, 8, 10, 0))
            session = store.start_activity_session(datetime(2026, 9, 8, 10, 0), "chrome.exe", "看片", "video_youtube", "YouTube")
            session = store.touch_activity_session(session.id, datetime(2026, 9, 8, 10, 12), "Short B", 0)
            state = CurrentStateBuilder(store).build(
                ForegroundSnapshot(datetime(2026, 9, 8, 10, 12), "chrome.exe", "Short B - YouTube", "看片", 0),
                session.started_at,
                False,
                False,
                False,
                current_session=session,
            )

            action = NextActionEngine(store).evaluate(state, goal, datetime(2026, 9, 8, 10, 12))

            self.assertIn("暫不打擾", action.action_text)
            self.assertNotIn("回到目前目標", action.action_text)

    def test_long_break_suggests_return_after_threshold(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = AssistantStore(Path(temp_dir) / "assistant.sqlite3")
            goal = GoalEngine(store).set_manual_goal("把私人助理的通知 bug 修好")
            work = store.start_activity_session(datetime(2026, 9, 8, 9, 0), "Code.exe", "工作", "work_development", "main.py")
            work = store.end_activity_session(work.id, datetime(2026, 9, 8, 10, 0))
            session = store.start_activity_session(datetime(2026, 9, 8, 10, 0), "chrome.exe", "看片", "video_youtube", "YouTube")
            session = store.touch_activity_session(session.id, datetime(2026, 9, 8, 10, 31), "Short", 0)
            state = CurrentStateBuilder(store).build(
                ForegroundSnapshot(datetime(2026, 9, 8, 10, 31), "chrome.exe", "Short - YouTube", "看片", 0),
                session.started_at,
                False,
                False,
                False,
                current_session=session,
            )

            action = NextActionEngine(store).evaluate(state, goal, datetime(2026, 9, 8, 10, 31))

            self.assertIn("如果休息夠了", action.action_text)

    def test_long_break_without_urgent_goal_stays_low_distraction(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = AssistantStore(Path(temp_dir) / "assistant.sqlite3")
            goal = store.upsert_goal("current_session", "整理之後可以看的資料", "inferred", 0.45)
            work = store.start_activity_session(datetime(2026, 9, 8, 9, 0), "Code.exe", "工作", "work_development", "main.py")
            store.end_activity_session(work.id, datetime(2026, 9, 8, 10, 0))
            session = store.start_activity_session(datetime(2026, 9, 8, 10, 0), "chrome.exe", "看片", "video_youtube", "YouTube")
            session = store.touch_activity_session(session.id, datetime(2026, 9, 8, 10, 31), "Short", 0)
            state = CurrentStateBuilder(store).build(
                ForegroundSnapshot(datetime(2026, 9, 8, 10, 31), "chrome.exe", "Short - YouTube", "看片", 0),
                session.started_at,
                False,
                False,
                False,
                current_session=session,
            )

            action = NextActionEngine(store).evaluate(state, goal, datetime(2026, 9, 8, 10, 31))

            self.assertIn("暫不打擾", action.action_text)
            self.assertNotIn("回到目前目標", action.action_text)

    def test_repeated_return_to_goal_uses_cooldown(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = AssistantStore(Path(temp_dir) / "assistant.sqlite3")
            goal = GoalEngine(store).set_manual_goal("把私人助理的通知 bug 修好")
            state = CurrentStateBuilder(store).build(
                ForegroundSnapshot(datetime(2026, 9, 8, 10, 0), "Telegram.exe", "一般聊天", "聊天", 0),
                datetime(2026, 9, 8, 9, 30),
                False,
                False,
                False,
            )
            engine = NextActionEngine(store)

            first = engine.evaluate(state, goal, datetime(2026, 9, 8, 10, 0))
            second = engine.evaluate(state, goal, datetime(2026, 9, 8, 10, 5))
            third = engine.evaluate(state, goal, datetime(2026, 9, 8, 10, 11))

            self.assertIn("回到目前目標", first.action_text)
            self.assertNotIn("回到目前目標", second.action_text)
            self.assertIn("回到目前目標", third.action_text)

    def test_explicit_private_time_overrides_goal_pressure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = AssistantStore(Path(temp_dir) / "assistant.sqlite3")
            goal = GoalEngine(store).set_manual_goal("把私人助理的通知 bug 修好")
            session = store.start_activity_session(datetime(2026, 9, 8, 20, 0), "Steam.exe", "遊戲", "game", "game")
            session = store.touch_activity_session(session.id, datetime(2026, 9, 8, 20, 10), "game", 0)
            state = CurrentStateBuilder(store).build(
                ForegroundSnapshot(datetime(2026, 9, 8, 20, 10), "Steam.exe", "game", "遊戲", 0),
                session.started_at,
                False,
                False,
                False,
                current_session=session,
                explicit_intent="private_time",
                explicit_intent_until=datetime(2026, 9, 8, 23, 0),
            )

            action = NextActionEngine(store).evaluate(state, goal, datetime(2026, 9, 8, 20, 10))

            self.assertEqual(state.intent, "private_time")
            self.assertIn("私人時間", action.action_text)
            self.assertNotIn("回到目前目標", action.action_text)

    def test_returning_to_work_restores_goal_tracking(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = AssistantStore(Path(temp_dir) / "assistant.sqlite3")
            goal = GoalEngine(store).set_manual_goal("把私人助理的通知 bug 修好")
            break_session = store.start_activity_session(datetime(2026, 9, 8, 10, 0), "chrome.exe", "看片", "video_youtube", "YouTube")
            store.end_activity_session(break_session.id, datetime(2026, 9, 8, 10, 20))
            work = store.start_activity_session(datetime(2026, 9, 8, 10, 21), "Code.exe", "工作", "work_development", "main.py")
            state = CurrentStateBuilder(store).build(
                ForegroundSnapshot(datetime(2026, 9, 8, 10, 21), "Code.exe", "main.py", "工作", 0),
                work.started_at,
                False,
                False,
                False,
                current_session=work,
            )

            action = NextActionEngine(store).evaluate(state, goal, datetime(2026, 9, 8, 10, 21))

            self.assertEqual(state.intent, "returning_to_work")
            self.assertIn("接續目前目標", action.action_text)

    def test_learned_break_pattern_requires_samples_and_confidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = AssistantStore(Path(temp_dir) / "assistant.sqlite3")
            low = break_pattern_payload(None)
            self.assertFalse(low["uses_personal_threshold"])
            self.assertEqual(break_threshold_seconds(low), 25 * 60)

            for index in range(3):
                started = datetime(2026, 9, 8, 10 + index, 0)
                session = store.start_activity_session(started, "chrome.exe", "看片", "video_youtube", "YouTube")
                session = store.touch_activity_session(session.id, started + timedelta(minutes=20), "YouTube", 0)
                ActivitySessionManager.update_break_pattern(store, session)
            learned = break_pattern_payload(store.learned_pattern("break_pattern", "global", "work_break"))

            self.assertTrue(learned["uses_personal_threshold"])
            self.assertGreaterEqual(learned["confidence"], 0.6)

    def test_context_builder_respects_privacy_exclusions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = AssistantStore(Path(temp_dir) / "assistant.sqlite3")
            SettingsService(store).update(excluded_apps=("bank",))
            store.add_usage_event(ForegroundSnapshot(datetime(2026, 9, 8, 10, 0), "Chrome.exe", "bank balance secret", "一般", 0))
            state = CurrentStateBuilder(store).build(
                ForegroundSnapshot(datetime(2026, 9, 8, 10, 5), "Code.exe", "main.py", "工作", 0),
                datetime(2026, 9, 8, 10, 5),
                False,
                False,
                False,
            )

            context = ContextBuilder(store).build(state, None, SettingsService(store).load(), minutes=10)
            encoded = json.dumps(context, ensure_ascii=False).lower()

            self.assertNotIn("balance", encoded)
            self.assertNotIn("secret", encoded)

    def test_advisor_context_includes_session_intent_pattern_and_respects_privacy(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = AssistantStore(Path(temp_dir) / "assistant.sqlite3")
            work = store.start_activity_session(datetime(2026, 9, 8, 9, 0), "Code.exe", "工作", "work_development", "main.py")
            store.end_activity_session(work.id, datetime(2026, 9, 8, 10, 0))
            session = store.start_activity_session(datetime(2026, 9, 8, 10, 0), "chrome.exe", "看片", "video_youtube", "YouTube")
            session = store.touch_activity_session(session.id, datetime(2026, 9, 8, 10, 10), "Short", 0)
            ActivitySessionManager.update_break_pattern(store, session, work)
            state = CurrentStateBuilder(store).build(
                ForegroundSnapshot(datetime(2026, 9, 8, 10, 10), "chrome.exe", "Short - YouTube", "看片", 0),
                session.started_at,
                False,
                False,
                False,
                current_session=session,
            )

            context = ContextBuilder(store).build(state, None, SettingsService(store).load(), minutes=10)
            private_state = CurrentStateBuilder(store).build(
                ForegroundSnapshot(datetime(2026, 9, 8, 10, 11), "chrome.exe", "secret bank video", "看片", 0),
                session.started_at,
                False,
                False,
                True,
                current_session=session,
            )
            private_context = ContextBuilder(store).build(private_state, None, SettingsService(store).load(), minutes=10)

            self.assertEqual(context["current_intent"], "intentional_break")
            self.assertEqual(context["current_session_duration_seconds"], 600)
            self.assertIn("typical_break_pattern", context)
            self.assertTrue(context["recent_sessions"])
            self.assertEqual(private_context["recent_sessions"], [])

    def test_groq_preset_and_connection_test_do_not_expose_key(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = AssistantStore(Path(temp_dir) / "assistant.sqlite3")
            settings = SettingsService(store).update(
                advisor_provider_preset="groq",
                advisor_endpoint_url="https://api.groq.com/openai/v1",
                advisor_model_name="qwen/example",
            )
            store.set_setting("advisor_api_key", "dummy-test-key")

            exported = SettingsService(store).export_public_settings()
            ok, message = OpenAICompatibleAdvisorProvider(
                settings.advisor_endpoint_url,
                settings.advisor_model_name,
                "dummy-test-key",
                timeout_seconds=1,
            ).test_connection()

            self.assertEqual(settings.advisor_provider_preset, "groq")
            self.assertEqual(settings.advisor_endpoint_url, "https://api.groq.com/openai/v1")
            self.assertNotIn("dummy-test-key", json.dumps(exported))
            self.assertNotIn("dummy-test-key", message)
            self.assertIsInstance(ok, bool)

    def test_advisor_disabled_still_returns_rule_action(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = AssistantStore(Path(temp_dir) / "assistant.sqlite3")
            SettingsService(store).update(ai_provider="chatgpt", ai_handoff_enabled=True)
            service = AdvisorService(store, SettingsService(store), NextActionEngine(store), ContextBuilder(store))
            state = CurrentStateBuilder(store).build(
                ForegroundSnapshot(datetime(2026, 9, 8, 10, 0), "Code.exe", "main.py", "工作", 0),
                datetime(2026, 9, 8, 10, 0),
                False,
                False,
                False,
            )

            action = service.analyze(state, None)

            self.assertEqual(action.source, "rule")
            self.assertIn("持續觀察", action.action_text)

    def test_advisor_enabled_missing_key_reports_unavailable_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = AssistantStore(Path(temp_dir) / "assistant.sqlite3")
            SettingsService(store).update(
                advisor_api_enabled=True,
                advisor_provider_preset="openai",
                advisor_endpoint_url="https://api.openai.com/v1",
                advisor_model_name="gpt-test",
            )
            service = AdvisorService(store, SettingsService(store), NextActionEngine(store), ContextBuilder(store))
            state = CurrentStateBuilder(store).build(
                ForegroundSnapshot(datetime(2026, 9, 8, 10, 0), "Code.exe", "main.py", "工作", 0),
                datetime(2026, 9, 8, 10, 0),
                False,
                False,
                False,
            )

            action = service.analyze(state, None)

            self.assertEqual(action.source, "ai_unavailable")
            self.assertIn("AI 暫時不可用", action.reason)
            self.assertIn("OpenAI", action.provider_label)

    def test_advisor_health_one_timeout_does_not_mark_degraded(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = AssistantStore(Path(temp_dir) / "assistant.sqlite3")

            with patch("personal_ai_assistant.advisor_core.random.uniform", return_value=0):
                health = advisor_record_failure(store, requests.Timeout("slow"), now=datetime(2026, 9, 8, 10, 0))

            self.assertEqual(health.state, "unknown")
            self.assertEqual(health.consecutive_failures, 1)
            self.assertEqual(health.retry_after_at, datetime(2026, 9, 8, 10, 0, 15))

    def test_advisor_health_consecutive_failures_enter_degraded_with_backoff(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = AssistantStore(Path(temp_dir) / "assistant.sqlite3")

            with patch("personal_ai_assistant.advisor_core.random.uniform", return_value=0):
                advisor_record_failure(store, requests.ConnectionError("offline"), now=datetime(2026, 9, 8, 10, 0))
                health = advisor_record_failure(store, requests.Timeout("slow"), now=datetime(2026, 9, 8, 10, 1))

            self.assertEqual(health.state, "degraded")
            self.assertEqual(health.consecutive_failures, 2)
            self.assertEqual(health.retry_after_at, datetime(2026, 9, 8, 10, 1, 30))
            self.assertIn("連線不穩", health.message)

    def test_advisor_health_429_uses_retry_after(self) -> None:
        response = requests.Response()
        response.status_code = 429
        response._content = b'{"error":{"message":"too many"}}'
        response.headers["Retry-After"] = "45"
        error = requests.HTTPError("rate limited", response=response)
        with tempfile.TemporaryDirectory() as temp_dir:
            store = AssistantStore(Path(temp_dir) / "assistant.sqlite3")

            health = advisor_record_failure(store, error, now=datetime(2026, 9, 8, 10, 0))

            self.assertEqual(health.state, "rate_limited")
            self.assertEqual(health.retry_after_at, datetime(2026, 9, 8, 10, 0, 45))
            self.assertIn("已達速率限制", health.message)

    def test_advisor_health_401_403_do_not_schedule_retry(self) -> None:
        for status in (401, 403):
            response = requests.Response()
            response.status_code = status
            response._content = b'{"error":{"message":"bad key"}}'
            error = requests.HTTPError("auth", response=response)
            with tempfile.TemporaryDirectory() as temp_dir:
                store = AssistantStore(Path(temp_dir) / "assistant.sqlite3")

                health = advisor_record_failure(store, error, now=datetime(2026, 9, 8, 10, 0))

                self.assertEqual(health.state, "auth_error")
                self.assertIsNone(health.retry_after_at)
                self.assertIn("驗證", health.message)

    def test_advisor_5xx_failure_falls_back_then_recovers_to_ai(self) -> None:
        class FlakyProvider:
            calls = 0

            def __init__(self, *_args: object) -> None:
                pass

            def advise(self, _context: dict[str, object]) -> NextAction:
                FlakyProvider.calls += 1
                if FlakyProvider.calls == 1:
                    response = requests.Response()
                    response.status_code = 500
                    response._content = b'{"error":{"message":"server busy"}}'
                    raise requests.HTTPError("server busy", response=response)
                return NextAction("繼續目前工作", "已恢復", 0.8, "ai")

        with tempfile.TemporaryDirectory() as temp_dir:
            store = AssistantStore(Path(temp_dir) / "assistant.sqlite3")
            SettingsService(store).update(
                advisor_api_enabled=True,
                advisor_provider_preset="groq",
                advisor_endpoint_url="https://api.groq.com/openai/v1",
                advisor_model_name="openai/gpt-oss-20b",
            )
            store.set_setting("advisor_api_key", "dummy-test-key")
            state = CurrentStateBuilder(store).build(
                ForegroundSnapshot(datetime(2026, 9, 8, 10, 0), "Code.exe", "main.py", "工作", 0),
                datetime(2026, 9, 8, 10, 0),
                False,
                False,
                False,
            )
            service = AdvisorService(store, SettingsService(store), NextActionEngine(store), ContextBuilder(store), provider_factory=FlakyProvider)

            with patch("personal_ai_assistant.advisor_core.random.uniform", return_value=0):
                first = service.analyze(state, None)

            self.assertEqual(first.source, "ai_unavailable")
            self.assertEqual(advisor_load_health(store).consecutive_failures, 1)

            advisor_record_success(store, now=datetime(2026, 9, 8, 10, 2))
            second = service.analyze(state, None)

            self.assertEqual(second.source, "ai")
            self.assertEqual(advisor_load_health(store).state, "available")

    def test_advisor_success_takes_priority_over_rule_fallback(self) -> None:
        class FakeProvider:
            def __init__(self, *_args: object) -> None:
                pass

            def advise(self, _context: dict[str, object]) -> NextAction:
                return NextAction("AI 建議：繼續休息", "正常休息中", 0.8, "ai", understanding="AI 判斷正在休息")

        with tempfile.TemporaryDirectory() as temp_dir:
            store = AssistantStore(Path(temp_dir) / "assistant.sqlite3")
            SettingsService(store).update(
                advisor_api_enabled=True,
                advisor_provider_preset="groq",
                advisor_endpoint_url="https://api.groq.com/openai/v1",
                advisor_model_name="qwen/example",
            )
            store.set_setting("advisor_api_key", "dummy-test-key")
            goal = GoalEngine(store).set_manual_goal("把私人助理的通知 bug 修好")
            state = CurrentState(
                current_mode="聊天",
                foreground_app="Telegram.exe",
                window_title_summary="一般聊天",
                activity_summary="正在 Telegram 查看訊息",
                activity_started_at=datetime(2026, 9, 8, 9, 30),
                updated_at=datetime(2026, 9, 8, 10, 0),
                intent="working",
            )
            service = AdvisorService(
                store,
                SettingsService(store),
                NextActionEngine(store),
                ContextBuilder(store),
                provider_factory=FakeProvider,
            )

            action = service.analyze(state, goal)

            self.assertEqual(action.source, "ai")
            self.assertEqual(action.action_text, "AI 建議：繼續休息")
            self.assertNotIn("回到目前目標", action.action_text)
            self.assertIn("Groq / qwen/example", action.provider_label)

    def test_auth_error_health_reports_unavailable_until_settings_change(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = AssistantStore(Path(temp_dir) / "assistant.sqlite3")
            SettingsService(store).update(
                advisor_api_enabled=True,
                advisor_provider_preset="groq",
                advisor_endpoint_url="https://api.groq.com/openai/v1",
                advisor_model_name="qwen/example",
            )
            store.set_setting("advisor_api_key", "dummy-test-key")
            store.set_setting("advisor_health_state", "auth_error")
            store.set_setting("advisor_health_message", "API 驗證/權限問題，請檢查 Key 或服務權限")
            service = AdvisorService(store, SettingsService(store), NextActionEngine(store), ContextBuilder(store))
            state = CurrentStateBuilder(store).build(
                ForegroundSnapshot(datetime(2026, 9, 8, 10, 0), "Code.exe", "main.py", "工作", 0),
                datetime(2026, 9, 8, 10, 0),
                False,
                False,
                False,
            )

            action = service.analyze(state, None)

            self.assertEqual(action.source, "ai_unavailable")
            self.assertIn("驗證", action.reason)

    def test_provider_preset_endpoint_and_key_url_mapping(self) -> None:
        self.assertEqual(advisor_preset_endpoint("openai"), "https://api.openai.com/v1")
        self.assertEqual(advisor_preset_endpoint("groq"), "https://api.groq.com/openai/v1")
        self.assertEqual(advisor_preset_endpoint("openrouter"), "https://openrouter.ai/api/v1")
        self.assertEqual(advisor_preset_endpoint("custom"), "")
        self.assertEqual(advisor_key_url("openai"), "https://platform.openai.com/api-keys")
        self.assertEqual(advisor_key_url("groq"), "https://console.groq.com/keys")
        self.assertEqual(advisor_key_url("openrouter"), "https://openrouter.ai/settings/keys")
        self.assertEqual(ADVISOR_PROVIDER_PRESETS["custom"]["key_url"], "")

    def test_provider_aware_model_recommendations_include_custom_escape_hatch(self) -> None:
        groq_models = advisor_recommended_models("groq")

        self.assertIn("openai/gpt-oss-20b", [item.model_id for item in groq_models])
        self.assertIn("qwen/qwen3.6-27b", [item.model_id for item in groq_models])
        self.assertNotIn("llama-3.3-70b-versatile", [item.model_id for item in groq_models])
        self.assertTrue(any("中文" in item.label for item in groq_models))
        self.assertEqual(ADVISOR_CUSTOM_MODEL, "__custom_model__")

    def test_openai_compatible_chat_url_and_minimal_test_payload_for_groq(self) -> None:
        captured: dict[str, object] = {}

        class FakeResponse:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict[str, object]:
                return {"choices": [{"message": {"content": "pong"}}]}

        def fake_post(url, json=None, headers=None, timeout=0):
            captured["url"] = url
            captured["headers"] = dict(headers or {})
            captured["payload"] = json or {}
            captured["timeout"] = timeout
            return FakeResponse()

        with patch("personal_ai_assistant.advisor_core.requests.post", fake_post):
            ok, message = OpenAICompatibleAdvisorProvider(
                "https://api.groq.com/openai/v1",
                "qwen/qwen3.6-27b",
                "dummy-test-key",
            ).test_connection()

        self.assertTrue(ok)
        self.assertEqual(message, "連線成功")
        self.assertEqual(captured["url"], "https://api.groq.com/openai/v1/chat/completions")
        self.assertEqual(advisor_chat_completions_url("https://api.groq.com/openai/v1/chat/completions"), captured["url"])
        self.assertEqual(captured["payload"]["model"], "qwen/qwen3.6-27b")
        self.assertNotIn("response_format", captured["payload"])
        self.assertNotIn("reasoning", captured["payload"])
        self.assertNotIn("temperature", captured["payload"])
        self.assertEqual(captured["headers"]["Authorization"], "Bearer dummy-test-key")
        self.assertEqual(captured["headers"]["Accept"], "application/json")
        self.assertEqual(captured["headers"]["Content-Type"], "application/json")
        self.assertEqual(captured["headers"]["User-Agent"], "PrivateAssistantAI/0.1.0-alpha")
        self.assertEqual(advisor_request_headers()["User-Agent"], "PrivateAssistantAI/0.1.0-alpha")

    def test_http_error_status_body_parsing_hides_api_key(self) -> None:
        response = requests.Response()
        response.status_code = 403
        response.url = "https://api.groq.com/openai/v1/chat/completions"
        response._content = b'{"error":{"message":"Bad key dummy-test-key or error code: 1010"}}'
        exc = requests.HTTPError("403 Client Error dummy-test-key", response=response)

        message = advisor_http_error_message(exc, "dummy-test-key")

        self.assertIn("HTTP 403", message)
        self.assertIn("error code: 1010", message)
        self.assertNotIn("dummy-test-key", message)

    def test_structured_advisor_response_parsing(self) -> None:
        raw = '{"understanding":"正在測試","next_action":"先跑測試","reason":"確認狀態","confidence":0.82,"should_interrupt":false,"questions":["要不要打包？"]}'

        action = parse_advisor_response(raw)

        self.assertEqual(action.source, "ai")
        self.assertEqual(action.action_text, "先跑測試")
        self.assertAlmostEqual(action.confidence, 0.82)
        self.assertEqual(action.questions, ("要不要打包？",))

    def test_intervention_policy_levels(self) -> None:
        state = CurrentStateBuilder(AssistantStore(Path(tempfile.mkdtemp()) / "assistant.sqlite3")).build(
            ForegroundSnapshot(datetime(2026, 9, 8, 10, 0), "Code.exe", "main.py", "工作", 0),
            datetime(2026, 9, 8, 10, 0),
            False,
            False,
            False,
        )
        policy = InterventionPolicy()

        silent = policy.evaluate(NextAction("觀察", "低信心", 0.2, "rule"), state, [], 0)
        status = policy.evaluate(NextAction("回到目標", "偏離", 0.65, "rule"), state, [], 0)
        popup = policy.evaluate(NextAction("立刻處理", "高信心", 0.92, "rule", True), state, [], 0)

        self.assertEqual(silent.intervention_level, "silent")
        self.assertEqual(status.intervention_level, "status_only")
        self.assertEqual(popup.intervention_level, "popup")

    def test_completed_reminder_is_not_suggested_again(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = AssistantStore(Path(temp_dir) / "assistant.sqlite3")
            reminder = store.add_reminder("吃藥", datetime(2026, 9, 8, 9, 55))
            store.mark_reminder_notified(reminder.id, datetime(2026, 9, 8, 9, 55))
            store.complete_reminder(reminder.id, datetime(2026, 9, 8, 9, 56))
            state = CurrentStateBuilder(store).build(
                ForegroundSnapshot(datetime(2026, 9, 8, 10, 0), "Code.exe", "main.py", "工作", 0),
                datetime(2026, 9, 8, 10, 0),
                False,
                False,
                False,
            )

            action = NextActionEngine(store).evaluate(state, None, datetime(2026, 9, 8, 10, 0))

            self.assertNotIn("吃藥", action.action_text)
            self.assertIn("剛完成提醒", action.action_text)

    def test_dead_loop_suggests_rechecking_direction(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = AssistantStore(Path(temp_dir) / "assistant.sqlite3")
            now = datetime(2026, 9, 8, 10, 0)
            for index in range(10):
                app = "Code.exe" if index % 2 == 0 else "PowerShell.exe"
                store.record_event(
                    "foreground_changed",
                    "foreground_monitor",
                    now - timedelta(minutes=index),
                    "工作",
                    app,
                    "error failed traceback",
                )
            state = CurrentStateBuilder(store).build(
                ForegroundSnapshot(now, "Code.exe", "error failed traceback", "工作", 0),
                now - timedelta(minutes=20),
                False,
                False,
                False,
            )

            action = NextActionEngine(store).evaluate(state, None, now)

            self.assertIn("重新確認", action.action_text)
            self.assertGreaterEqual(action.confidence, 0.78)

    def test_store_reminder_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = AssistantStore(Path(temp_dir) / "assistant.sqlite3")
            due_at = datetime.now() - timedelta(minutes=1)
            reminder = store.add_reminder("伸展", due_at)

            due = store.due_reminders()
            self.assertEqual([item.id for item in due], [reminder.id])

            store.mark_reminder_fired(reminder.id)
            self.assertEqual(store.due_reminders(), [])
            self.assertEqual(store.list_reminders()[0].status, "notified")

            store.complete_reminder(reminder.id)
            self.assertEqual(store.list_reminders()[0].status, "completed")

    def test_store_reminder_snooze_and_ignore(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = AssistantStore(Path(temp_dir) / "assistant.sqlite3")
            due_at = datetime.now() - timedelta(minutes=1)
            reminder = store.add_reminder("整理桌面", due_at)

            snoozed_until = datetime.now() + timedelta(minutes=10)
            store.snooze_reminder(reminder.id, snoozed_until)
            self.assertEqual(store.due_reminders(), [])
            snoozed = store.list_reminders()[0]
            self.assertEqual(snoozed.status, "pending")
            self.assertEqual(snoozed.snooze_count, 1)
            self.assertIsNotNone(snoozed.last_snoozed_at)

            store.ignore_reminder(reminder.id)
            self.assertEqual(store.due_reminders(), [])
            self.assertEqual(store.list_reminders()[0].status, "ignored")

    def test_store_snoozed_reminder_triggers_again_at_new_time(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = AssistantStore(Path(temp_dir) / "assistant.sqlite3")
            reminder = store.add_reminder("整理桌面", datetime(2026, 9, 7, 18, 30))

            store.snooze_reminder(reminder.id, datetime(2026, 9, 7, 18, 40), datetime(2026, 9, 7, 18, 30))

            self.assertEqual(store.due_reminders(datetime(2026, 9, 7, 18, 39)), [])
            self.assertEqual(
                [item.id for item in store.due_reminders(datetime(2026, 9, 7, 18, 40))],
                [reminder.id],
            )

    def test_store_next_pending_reminder_within_thirty_minutes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = AssistantStore(Path(temp_dir) / "assistant.sqlite3")
            now = datetime(2026, 9, 7, 18, 0)
            far = store.add_reminder("遠期私事", now + timedelta(minutes=31))
            near = store.add_reminder("伸展", now + timedelta(minutes=30))

            upcoming = store.next_pending_reminder_within(timedelta(minutes=30), now)

            self.assertIsNotNone(upcoming)
            self.assertEqual(upcoming.id, near.id)
            self.assertNotEqual(upcoming.id, far.id)

    def test_store_snoozed_reminder_follows_status_bar_lookahead(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = AssistantStore(Path(temp_dir) / "assistant.sqlite3")
            now = datetime(2026, 9, 7, 18, 0)
            reminder = store.add_reminder("整理桌面", now - timedelta(minutes=1))

            store.snooze_reminder(reminder.id, now + timedelta(minutes=35), now)
            self.assertIsNone(store.next_pending_reminder_within(timedelta(minutes=30), now))

            store.snooze_reminder(reminder.id, now + timedelta(minutes=10), now)
            upcoming = store.next_pending_reminder_within(timedelta(minutes=30), now)
            self.assertIsNotNone(upcoming)
            self.assertEqual(upcoming.id, reminder.id)

    def test_recurring_reminder_reschedules_after_complete(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = AssistantStore(Path(temp_dir) / "assistant.sqlite3")
            parsed = parse_natural_reminder("每天 21:00 伸展", datetime(2026, 9, 7, 12, 0))
            reminder = store.add_reminder(
                parsed.text,
                parsed.due_at,
                parsed.recurrence_rule,
                parsed.next_due_at,
            )

            store.mark_reminder_notified(reminder.id, datetime(2026, 9, 7, 21, 0))
            store.log_reminder_interaction(
                reminder.id,
                "completed",
                datetime(2026, 9, 7, 21, 2),
                current_mode="一般",
            )
            store.complete_reminder(reminder.id, datetime(2026, 9, 7, 21, 2))

            updated = store.get_reminder(reminder.id)
            self.assertIsNotNone(updated)
            self.assertEqual(updated.status, "pending")
            self.assertEqual(updated.due_at, datetime(2026, 9, 8, 21, 0))
            self.assertEqual(updated.next_due_at, datetime(2026, 9, 9, 21, 0))
            self.assertEqual(updated.completed_count, 1)

    def test_completed_occurrence_is_idempotent_and_not_renotified(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = AssistantStore(Path(temp_dir) / "assistant.sqlite3")
            reminder = store.add_reminder("吃藥", datetime(2026, 9, 7, 22, 30))

            due = store.due_reminders(datetime(2026, 9, 7, 22, 31))[0]
            self.assertTrue(store.mark_reminder_notified(due.id, datetime(2026, 9, 7, 22, 31), expected_due_at=due.due_at))
            self.assertTrue(store.complete_reminder(due.id, datetime(2026, 9, 7, 22, 32), expected_due_at=due.due_at))

            self.assertFalse(store.mark_reminder_notified(due.id, datetime(2026, 9, 7, 22, 33), expected_due_at=due.due_at))
            self.assertFalse(store.complete_reminder(due.id, datetime(2026, 9, 7, 22, 34), expected_due_at=due.due_at))
            self.assertEqual(store.due_reminders(datetime(2026, 9, 7, 22, 35)), [])
            self.assertEqual(store.escalate_unconfirmed_reminders(datetime(2026, 9, 7, 22, 34), datetime(2026, 9, 7, 22, 35)), [])
            updated = store.get_reminder(due.id)
            self.assertIsNotNone(updated)
            self.assertEqual(updated.status, "completed")
            self.assertEqual(updated.completed_count, 1)

    def test_recurring_completed_occurrence_only_schedules_next_occurrence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = AssistantStore(Path(temp_dir) / "assistant.sqlite3")
            parsed = parse_natural_reminder("每天 22:30 吃藥", datetime(2026, 9, 7, 12, 0))
            reminder = store.add_reminder(parsed.text, parsed.due_at, parsed.recurrence_rule, parsed.next_due_at)
            due = store.due_reminders(datetime(2026, 9, 7, 22, 31))[0]

            self.assertTrue(store.mark_reminder_notified(due.id, datetime(2026, 9, 7, 22, 31), expected_due_at=due.due_at))
            self.assertTrue(store.complete_reminder(due.id, datetime(2026, 9, 7, 22, 32), expected_due_at=due.due_at))
            self.assertFalse(store.mark_reminder_notified(due.id, datetime(2026, 9, 7, 22, 33), expected_due_at=due.due_at))

            updated = store.get_reminder(reminder.id)
            self.assertIsNotNone(updated)
            self.assertEqual(updated.status, "pending")
            self.assertEqual(updated.due_at, datetime(2026, 9, 8, 22, 30))
            self.assertEqual(updated.completed_count, 1)
            self.assertEqual(store.due_reminders(datetime(2026, 9, 7, 22, 40)), [])

    def test_recurring_snooze_triggers_once_without_changing_next_cycle(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = AssistantStore(Path(temp_dir) / "assistant.sqlite3")
            parsed = parse_natural_reminder("每週二、四、六 18:30 喝水", datetime(2026, 9, 7, 12, 0))
            reminder = store.add_reminder(
                parsed.text,
                parsed.due_at,
                parsed.recurrence_rule,
                parsed.next_due_at,
            )

            store.mark_reminder_notified(reminder.id, datetime(2026, 9, 8, 18, 30))
            store.snooze_reminder(
                reminder.id,
                datetime(2026, 9, 8, 18, 40),
                datetime(2026, 9, 8, 18, 30),
            )
            snoozed = store.get_reminder(reminder.id)
            self.assertIsNotNone(snoozed)
            self.assertEqual(snoozed.due_at, datetime(2026, 9, 8, 18, 40))
            self.assertEqual(snoozed.next_due_at, datetime(2026, 9, 10, 18, 30))
            self.assertEqual(
                [item.id for item in store.due_reminders(datetime(2026, 9, 8, 18, 40))],
                [reminder.id],
            )

            store.mark_reminder_notified(reminder.id, datetime(2026, 9, 8, 18, 40))
            store.complete_reminder(reminder.id, datetime(2026, 9, 8, 18, 41))
            updated = store.get_reminder(reminder.id)
            self.assertIsNotNone(updated)
            self.assertEqual(updated.due_at, datetime(2026, 9, 10, 18, 30))
            self.assertEqual(updated.next_due_at, datetime(2026, 9, 12, 18, 30))

    def test_active_break_cycle_skips_break_days(self) -> None:
        parsed = parse_natural_reminder(
            "從 9/10 開始，每天 22:30 保養任務，活動 21 天休息 7 天循環",
            datetime(2026, 9, 7, 12, 0),
        )
        self.assertEqual(parsed.due_at, datetime(2026, 9, 10, 22, 30))
        rule = parsed.recurrence_rule or ""
        next_after_last_active_day = parse_natural_reminder(
            "從 9/10 開始，每天 22:30 保養任務，活動 21 天休息 7 天循環",
            datetime(2026, 9, 30, 22, 30),
        )
        self.assertEqual(next_after_last_active_day.due_at, datetime(2026, 10, 8, 22, 30))
        self.assertIn("休息期第 3/7 天", cycle_status(rule, datetime(2026, 10, 3, 12, 0)))

    def test_anchor_based_cycle_keeps_same_day_before_due_time(self) -> None:
        rule = self._pill_cycle_rule()
        occurrence = recurrence_occurrence(rule, datetime(2026, 9, 8, 22, 29))
        self.assertIsNotNone(occurrence)
        self.assertEqual(occurrence.due_at, datetime(2026, 9, 8, 22, 30))
        self.assertEqual(occurrence.cycle_index, 1)
        self.assertEqual(occurrence.occurrence_index, 20)
        self.assertIn("第 1 輪第 20/21 天", cycle_status(rule, datetime(2026, 9, 8, 22, 29)))

    def test_anchor_based_cycle_keeps_missed_same_day_after_due_time(self) -> None:
        rule = self._pill_cycle_rule()
        occurrence = recurrence_occurrence(rule, datetime(2026, 9, 8, 22, 31))
        self.assertIsNotNone(occurrence)
        self.assertEqual(occurrence.due_at, datetime(2026, 9, 8, 22, 30))
        self.assertTrue(occurrence.is_missed)
        self.assertEqual(occurrence.next_due_at, datetime(2026, 9, 9, 22, 30))
        self.assertEqual(next_occurrence(rule, datetime(2026, 9, 8, 22, 31)), datetime(2026, 9, 9, 22, 30))

    def test_anchor_based_cycle_reschedules_after_completing_day_20_and_21(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = AssistantStore(Path(temp_dir) / "assistant.sqlite3")
            rule = self._pill_cycle_rule()
            reminder = store.add_reminder("吃避孕藥", datetime(2026, 9, 8, 22, 30), rule, datetime(2026, 9, 9, 22, 30))

            self.assertTrue(store.mark_reminder_notified(reminder.id, datetime(2026, 9, 8, 22, 31), expected_due_at=reminder.due_at))
            self.assertTrue(store.complete_reminder(reminder.id, datetime(2026, 9, 8, 22, 32), expected_due_at=reminder.due_at))
            updated = store.get_reminder(reminder.id)
            self.assertIsNotNone(updated)
            self.assertEqual(updated.due_at, datetime(2026, 9, 9, 22, 30))
            self.assertIn("第 1 輪第 21/21 天", cycle_status(rule, updated.due_at))

            self.assertTrue(store.mark_reminder_notified(reminder.id, datetime(2026, 9, 9, 22, 31), expected_due_at=updated.due_at))
            self.assertTrue(store.complete_reminder(reminder.id, datetime(2026, 9, 9, 22, 32), expected_due_at=updated.due_at))
            updated = store.get_reminder(reminder.id)
            self.assertIsNotNone(updated)
            self.assertEqual(updated.due_at, datetime(2026, 9, 17, 22, 30))
            self.assertIn("第 2 輪第 1/21 天", cycle_status(rule, updated.due_at))

    def test_anchor_based_cycle_does_not_create_active_occurrence_during_break(self) -> None:
        rule = self._pill_cycle_rule()
        for day in range(10, 17):
            self.assertEqual(
                next_occurrence(rule, datetime(2026, 9, day, 12, 0)),
                datetime(2026, 9, 17, 22, 30),
            )
            self.assertIn("休息期", cycle_status(rule, datetime(2026, 9, day, 12, 0)))
        self.assertIn("第 2 輪第 3/21 天", cycle_status(rule, datetime(2026, 9, 19, 22, 30)))

    def test_cycle_counts_do_not_change_calendar_position(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = AssistantStore(Path(temp_dir) / "assistant.sqlite3")
            rule = self._pill_cycle_rule()
            reminder = store.add_reminder("吃避孕藥", datetime(2026, 9, 8, 22, 30), rule, datetime(2026, 9, 9, 22, 30))
            store.mark_reminder_notified(reminder.id, datetime(2026, 9, 8, 22, 31), expected_due_at=reminder.due_at)
            store.snooze_reminder(reminder.id, datetime(2026, 9, 8, 22, 41), datetime(2026, 9, 8, 22, 31), expected_due_at=reminder.due_at)
            with store.connect() as conn:
                conn.execute(
                    "UPDATE reminders SET notification_count = 9, snooze_count = 4, completed_count = 12 WHERE id = ?",
                    (reminder.id,),
                )
                store._repair_recurring_schedule_cache(conn, datetime(2026, 9, 8, 22, 35))
            updated = store.get_reminder(reminder.id)
            self.assertIsNotNone(updated)
            self.assertEqual(updated.due_at, datetime(2026, 9, 8, 22, 41))
            self.assertEqual(updated.next_due_at, datetime(2026, 9, 9, 22, 30))
            self.assertIn("第 1 輪第 20/21 天", cycle_status(rule, datetime(2026, 9, 8, 22, 35)))

    def test_cycle_restart_repair_rebuilds_bad_cached_future_schedule(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = AssistantStore(Path(temp_dir) / "assistant.sqlite3")
            rule = self._pill_cycle_rule()
            reminder = store.add_reminder("吃避孕藥", datetime(2026, 9, 19, 22, 30), rule, datetime(2026, 9, 20, 22, 30))
            with store.connect() as conn:
                store._repair_recurring_schedule_cache(conn, datetime(2026, 9, 8, 22, 31))
            repaired = store.get_reminder(reminder.id)
            self.assertIsNotNone(repaired)
            self.assertEqual(repaired.status, "pending")
            self.assertEqual(repaired.due_at, datetime(2026, 9, 8, 22, 30))
            self.assertEqual(repaired.next_due_at, datetime(2026, 9, 9, 22, 30))

    def test_cycle_uses_local_date_boundary_without_utc_day_shift(self) -> None:
        rule = self._pill_cycle_rule()
        before_due = recurrence_occurrence(rule, datetime(2026, 9, 8, 0, 1))
        self.assertIsNotNone(before_due)
        self.assertEqual(before_due.due_at, datetime(2026, 9, 8, 22, 30))
        self.assertEqual(before_due.occurrence_index, 20)

    def test_cycle_snooze_does_not_change_cycle_basis(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = AssistantStore(Path(temp_dir) / "assistant.sqlite3")
            parsed = parse_natural_reminder(
                "從 9/10 開始，每天 22:30 保養任務，活動 21 天休息 7 天循環",
                datetime(2026, 9, 7, 12, 0),
            )
            reminder = store.add_reminder(parsed.text, parsed.due_at, parsed.recurrence_rule, parsed.next_due_at)

            store.mark_reminder_notified(reminder.id, datetime(2026, 9, 10, 22, 30))
            store.snooze_reminder(reminder.id, datetime(2026, 9, 10, 22, 40), datetime(2026, 9, 10, 22, 30))
            snoozed = store.get_reminder(reminder.id)
            self.assertIsNotNone(snoozed)
            self.assertEqual(snoozed.due_at, datetime(2026, 9, 10, 22, 40))
            self.assertEqual(snoozed.next_due_at, datetime(2026, 9, 11, 22, 30))

            store.complete_reminder(reminder.id, datetime(2026, 9, 10, 22, 41))
            updated = store.get_reminder(reminder.id)
            self.assertIsNotNone(updated)
            self.assertEqual(updated.due_at, datetime(2026, 9, 11, 22, 30))

    def _pill_cycle_rule(self) -> str:
        return json.dumps(
            {
                "freq": "cycle",
                "start_date": "2026-08-20",
                "time": "22:30",
                "active_days": 21,
                "break_days": 7,
                "repeat": True,
                "weekdays": None,
                "interval_days": None,
            },
            ensure_ascii=False,
            sort_keys=True,
        )

    def test_interaction_logging_records_response_seconds(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = AssistantStore(Path(temp_dir) / "assistant.sqlite3")
            reminder = store.add_reminder("健康檢查", datetime(2026, 9, 7, 21, 0))

            store.mark_reminder_notified(reminder.id, datetime(2026, 9, 7, 21, 0))
            store.log_reminder_interaction(reminder.id, "notified", datetime(2026, 9, 7, 21, 0), "工作")
            interaction = store.log_reminder_interaction(
                reminder.id,
                "snoozed",
                datetime(2026, 9, 7, 21, 3),
                "工作",
            )

            self.assertEqual(interaction.response_seconds, 180)
            self.assertEqual(interaction.source, "assistant_reminder")
            self.assertEqual([item.event_type for item in store.reminder_interactions(reminder.id)], ["snoozed", "notified"])

    def test_habit_summary_groups_recurring_series(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = AssistantStore(Path(temp_dir) / "assistant.sqlite3")
            parsed = parse_natural_reminder("每天 21:00 伸展", datetime(2026, 9, 7, 12, 0))
            reminder = store.add_reminder(parsed.text, parsed.due_at, parsed.recurrence_rule, parsed.next_due_at)

            store.mark_reminder_notified(reminder.id, datetime(2026, 9, 7, 21, 0))
            store.log_reminder_interaction(reminder.id, "completed", datetime(2026, 9, 7, 21, 1), "一般")
            store.complete_reminder(reminder.id, datetime(2026, 9, 7, 21, 1))
            store.mark_reminder_notified(reminder.id, datetime(2026, 9, 8, 21, 0))
            store.log_reminder_interaction(reminder.id, "ignored", datetime(2026, 9, 8, 21, 5), "一般")
            store.ignore_reminder(reminder.id, datetime(2026, 9, 8, 21, 5))

            summary = store.habit_summaries()[0]
            self.assertEqual(summary.reminder_id, reminder.id)
            self.assertEqual(summary.notification_count, 2)
            self.assertEqual(summary.completed_count, 1)
            self.assertEqual(summary.ignored_count, 1)

    def test_store_migrates_legacy_fired_reminders_to_notified(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "assistant.sqlite3"
            due_at = datetime.now() - timedelta(minutes=5)
            conn = sqlite3.connect(db_path)
            try:
                conn.execute(
                    """
                    CREATE TABLE reminders (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        text TEXT NOT NULL,
                        due_at TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        fired_at TEXT
                    )
                    """
                )
                conn.execute(
                    "INSERT INTO reminders (text, due_at, created_at, fired_at) VALUES (?, ?, ?, ?)",
                    (
                        "舊事項",
                        due_at.isoformat(),
                        due_at.isoformat(),
                        datetime.now().isoformat(),
                    ),
                )
                conn.commit()
            finally:
                conn.close()

            store = AssistantStore(db_path)
            reminder = store.list_reminders()[0]
            self.assertEqual(reminder.status, "notified")
            self.assertEqual(store.due_reminders(), [])
            self.assertIsNone(reminder.recurrence_rule)
            self.assertEqual(reminder.notification_count, 0)

            store.log_reminder_interaction(reminder.id, "notified", datetime.now(), "一般")
            self.assertEqual(len(store.reminder_interactions(reminder.id)), 1)

            with store.connect() as conn:
                tables = {
                    row["name"]
                    for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
                }
            self.assertIn("notification_events", tables)
            self.assertIn("notification_interactions", tables)

    def test_top_status_bar_visibility_setting(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = AssistantStore(Path(temp_dir) / "assistant.sqlite3")
            self.assertEqual(store.get_setting("top_status_bar_visible", "1"), "1")
            store.set_setting("top_status_bar_visible", "0")
            reopened = AssistantStore(Path(temp_dir) / "assistant.sqlite3")
            self.assertEqual(reopened.get_setting("top_status_bar_visible", "1"), "0")

    def test_store_usage_events(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = AssistantStore(Path(temp_dir) / "assistant.sqlite3")
            snapshot = ForegroundSnapshot(
                timestamp=datetime(2026, 9, 7, 18, 30),
                app_name="Code.exe",
                window_title="main.py",
                mode="工作",
                idle_seconds=0,
            )
            store.add_usage_event(snapshot)
            events = store.recent_events()
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].mode, "工作")

    def test_event_pipeline_records_only_state_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = AssistantStore(Path(temp_dir) / "assistant.sqlite3")
            bus = EventBus(store)
            snapshot = ForegroundSnapshot(
                timestamp=datetime(2026, 9, 7, 18, 30),
                app_name="Code.exe",
                window_title="main.py",
                mode="工作",
                idle_seconds=0,
            )

            self.assertIsNotNone(bus.publish_foreground_snapshot(snapshot))
            self.assertIsNone(bus.publish_foreground_snapshot(snapshot))
            changed = ForegroundSnapshot(
                timestamp=datetime(2026, 9, 7, 18, 31),
                app_name="steam.exe",
                window_title="Helldivers 2",
                mode="遊戲",
                idle_seconds=0,
            )
            self.assertIsNotNone(bus.publish_foreground_snapshot(changed))

            events = store.recent_assistant_events()
            self.assertEqual(len(events), 2)
            self.assertEqual(events[0].event_type, "foreground_changed")

    def test_learning_updates_outcome_patterns_and_confidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = AssistantStore(Path(temp_dir) / "assistant.sqlite3")
            learning = LearningEngine(store)
            reminder = store.add_reminder("整理桌面", datetime(2026, 9, 7, 21, 0))

            for minute in range(5):
                store.mark_reminder_notified(reminder.id, datetime(2026, 9, 7, 21, minute))
                store.log_reminder_interaction(reminder.id, "snoozed", datetime(2026, 9, 7, 21, minute + 1), "遊戲")
                learning.learn_from_reminder_outcome(reminder.id, "snoozed", "遊戲")

            patterns = store.learned_patterns()
            reminder_pattern = next(item for item in patterns if item.pattern_type == "reminder_outcome_rate")
            context_pattern = next(item for item in patterns if item.pattern_type == "mode_reminder_outcome")
            self.assertEqual(reminder_pattern.sample_count, 5)
            self.assertGreater(reminder_pattern.confidence, 0.3)
            self.assertEqual(context_pattern.status, "provisional")
            self.assertGreater(store.behavior_hypotheses_count(), 0)
            self.assertEqual(
                store.get_adaptive_preference("presentation:遊戲:general:normal"),
                PRESENTATION_DEFER_GAME_END,
            )

    def test_external_notification_pattern_update_and_intervention_threshold(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = AssistantStore(Path(temp_dir) / "assistant.sqlite3")
            store.set_setting("notification_listener_beta_enabled", "1")
            learning = LearningEngine(store)
            base = datetime(2026, 9, 7, 18, 0)

            for index in range(5):
                event = learning.record_external_notification(
                    "Telegram.exe",
                    base + timedelta(minutes=index),
                    title="新訊息",
                    context_key="friend_chat",
                    current_mode="工作",
                    away_state="active",
                )
                self.assertIsNotNone(event)
                snapshot = ForegroundSnapshot(base + timedelta(minutes=index, seconds=45), "Telegram.exe", "chat", "聊天", 0)
                learning.observe_notification_app_visit(snapshot)

            patterns = store.learned_patterns()
            pattern = next(item for item in patterns if item.pattern_type == "notification_response_seconds")
            self.assertEqual(pattern.sample_count, 5)
            self.assertGreaterEqual(pattern.confidence, 0.35)

            late_event = learning.record_external_notification(
                "Telegram.exe",
                base + timedelta(hours=1),
                title="新訊息",
                context_key="friend_chat",
                current_mode="工作",
                away_state="active",
            )
            self.assertIsNotNone(late_event)
            candidate = learning.notification_intervention_candidate(base + timedelta(hours=1, minutes=4), away=False)

            self.assertIsNotNone(candidate)
            self.assertEqual(candidate[0].id, late_event.id)

    def test_external_notification_ignore_is_negative_feedback(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = AssistantStore(Path(temp_dir) / "assistant.sqlite3")
            store.set_setting("notification_listener_beta_enabled", "1")
            learning = LearningEngine(store)
            base = datetime(2026, 9, 7, 18, 0)

            event = learning.record_external_notification("Discord.exe", base, context_key="group", away_state="active")
            self.assertIsNotNone(event)
            learning.handle_notification_prompt_action(event.id, "ignore", base + timedelta(minutes=5))

            pattern = next(item for item in store.learned_patterns() if item.pattern_type == "notification_response_seconds")
            self.assertEqual(pattern.sample_count, 1)
            self.assertIn('"ignore_rate": 1.0', pattern.value)

    def test_external_notification_privacy_and_excluded_app_do_not_learn(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = AssistantStore(Path(temp_dir) / "assistant.sqlite3")
            store.set_setting("notification_listener_beta_enabled", "1")
            store.set_setting("excluded_apps", '["telegram.exe"]')
            learning = LearningEngine(store)

            self.assertIsNone(learning.record_external_notification("Telegram.exe", datetime(2026, 9, 7, 18, 0)))
            self.assertEqual(store.pending_notification_events(), [])

            store.set_setting("excluded_apps", "[]")
            store.set_setting("privacy_mode_enabled", "1")
            self.assertIsNone(learning.record_external_notification("Discord.exe", datetime(2026, 9, 7, 18, 0)))
            self.assertEqual(store.pending_notification_events(), [])

    def test_provisional_preference_does_not_overwrite_formal_reminder(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = AssistantStore(Path(temp_dir) / "assistant.sqlite3")
            reminder = store.add_reminder("整理桌面", datetime(2026, 9, 7, 21, 0))
            store.set_adaptive_preference(
                "presentation:遊戲:general:normal",
                PRESENTATION_DEFER_GAME_END,
                2,
                "遊戲中一般提醒常延後",
            )

            updated = store.get_reminder(reminder.id)
            self.assertIsNotNone(updated)
            self.assertEqual(updated.due_at, datetime(2026, 9, 7, 21, 0))
            self.assertEqual(updated.status, "pending")

    def test_level3_requires_confirmation_for_health_and_schedule_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = AssistantStore(Path(temp_dir) / "assistant.sqlite3")
            health = store.add_reminder("健康檢查", datetime(2026, 9, 7, 21, 0))
            policy = AdaptivePolicy()

            self.assertFalse(policy.classify_change("presentation_strategy", health).allowed)
            self.assertEqual(policy.classify_change("presentation_strategy", health).level, 3)

            normal = store.add_reminder("整理桌面", datetime(2026, 9, 7, 21, 0))
            schedule_change = policy.classify_change("schedule_time_change", normal)
            self.assertFalse(schedule_change.allowed)
            self.assertEqual(schedule_change.level, 3)

    def test_adaptive_strategy_game_and_idle_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = AssistantStore(Path(temp_dir) / "assistant.sqlite3")
            strategy = AdaptiveReminderStrategy(store)
            reminder = store.add_reminder("整理桌面", datetime(2026, 9, 7, 21, 0))
            game = ForegroundSnapshot(datetime(2026, 9, 7, 21, 0), "steam.exe", "Helldivers 2", "遊戲", 0)
            idle = ForegroundSnapshot(datetime(2026, 9, 7, 21, 0), "Code.exe", "main.py", "閒置", 600)

            store.set_adaptive_preference(
                "presentation:遊戲:general:normal",
                PRESENTATION_DEFER_GAME_END,
                2,
                "遊戲中一般提醒常延後",
            )
            self.assertEqual(strategy.decide(reminder, game).strategy, PRESENTATION_DEFER_GAME_END)
            self.assertEqual(strategy.decide(reminder, idle, auto_idle_paused=True).strategy, PRESENTATION_DEFER_ACTIVE)

    def test_health_reminder_is_not_low_level_deferred(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = AssistantStore(Path(temp_dir) / "assistant.sqlite3")
            strategy = AdaptiveReminderStrategy(store)
            reminder = store.add_reminder("健康檢查", datetime(2026, 9, 7, 21, 0))
            game = ForegroundSnapshot(datetime(2026, 9, 7, 21, 0), "steam.exe", "Helldivers 2", "遊戲", 0)

            decision = strategy.decide(reminder, game)

            self.assertEqual(decision.strategy, PRESENTATION_REPEAT)
            self.assertIn("正式時間", decision.reason)

    def test_privacy_blacklist_suppresses_foreground_events(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = AssistantStore(Path(temp_dir) / "assistant.sqlite3")
            bus = EventBus(store)
            snapshot = ForegroundSnapshot(
                timestamp=datetime(2026, 9, 7, 18, 30),
                app_name="Bitwarden.exe",
                window_title="Passwords",
                mode="一般",
                idle_seconds=0,
            )

            self.assertIsNone(bus.publish_foreground_snapshot(snapshot))
            self.assertEqual(store.recent_assistant_events(), [])

    def test_analyzer_is_deterministic_without_api_key(self) -> None:
        reminder = Reminder(
            id=1,
            text="喝水",
            due_at=datetime(2026, 9, 7, 18, 30),
            created_at=datetime(2026, 9, 7, 12, 0),
        )
        snapshot = ForegroundSnapshot(
            timestamp=datetime(2026, 9, 7, 18, 30),
            app_name="steam.exe",
            window_title="Steam Game",
            mode="遊戲",
            idle_seconds=0,
        )
        title, body = AssistantAnalyzer().build_reminder_message(reminder, snapshot)
        self.assertIn("喝水", title)
        self.assertIn("遊戲", body)

    def test_main_window_reminder_service_runs_without_monitoring(self) -> None:
        from PySide6.QtWidgets import QApplication

        from personal_ai_assistant.main_window import MainWindow

        app = QApplication.instance() or QApplication([])
        app.setQuitOnLastWindowClosed(False)
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "assistant.sqlite3"
            SettingsService(AssistantStore(db_path)).update(auto_start_monitoring_enabled=False)
            window = MainWindow(db_path)
            window.notifications.notify = lambda _title, _body: True
            shown_reminders: list[int] = []
            window.show_reminder_popup = lambda reminder, _title, _body: shown_reminders.append(reminder.id)

            reminder = window.store.add_reminder("健康檢查", datetime.now() - timedelta(minutes=1))
            self.assertFalse(window.monitoring)
            self.assertTrue(window.reminder_timer.isActive())

            window.reminder_tick()

            self.assertEqual(shown_reminders, [reminder.id])
            self.assertEqual(window.store.list_reminders()[0].status, "notified")
            self.assertFalse(window.monitor_timer.isActive())
            window.exit_application()
            app.processEvents()

    def test_main_window_auto_starts_monitoring_by_default(self) -> None:
        from PySide6.QtWidgets import QApplication

        from personal_ai_assistant.main_window import MainWindow

        app = QApplication.instance() or QApplication([])
        app.setQuitOnLastWindowClosed(False)
        with tempfile.TemporaryDirectory() as temp_dir:
            snapshot = ForegroundSnapshot(datetime(2026, 9, 7, 18, 30), "Code.exe", "main.py", "工作", 0)
            with patch("personal_ai_assistant.main_window.get_foreground_snapshot", return_value=snapshot), patch(
                "personal_ai_assistant.main_window.is_windows_locked_or_unavailable", return_value=False
            ):
                window = MainWindow(Path(temp_dir) / "assistant.sqlite3")

            self.assertTrue(window.monitoring)
            self.assertTrue(window.monitor_timer.isActive())
            self.assertIn("監督中", window.monitor_service_label.text())
            self.assertGreaterEqual(window.idle_threshold_combo.minimumWidth(), 120)
            self.assertGreaterEqual(window.top_status_bar_screen_combo.minimumWidth(), 230)
            self.assertIn("已排除", window.excluded_apps_label.text())
            window.exit_application()
            app.processEvents()

    def test_main_window_settings_and_assistant_pages_are_scrollable_when_narrow(self) -> None:
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QApplication, QScrollArea, QSizePolicy

        from personal_ai_assistant.main_window import MainWindow

        app = QApplication.instance() or QApplication([])
        app.setQuitOnLastWindowClosed(False)
        with tempfile.TemporaryDirectory() as temp_dir:
            SettingsService(AssistantStore(Path(temp_dir) / "assistant.sqlite3")).update(auto_start_monitoring_enabled=False)
            window = MainWindow(Path(temp_dir) / "assistant.sqlite3")
            window.resize(850, 900)
            app.processEvents()
            window.resize(900, 700)
            app.processEvents()

            scroll_names = {scroll.objectName() for scroll in window.findChildren(QScrollArea)}
            ai_scroll = window.findChild(QScrollArea, "AISettingsScroll")
            assistant_scroll = window.findChild(QScrollArea, "AssistantScroll")

            self.assertIn("AISettingsScroll", scroll_names)
            self.assertIn("AssistantScroll", scroll_names)
            self.assertTrue(ai_scroll.widgetResizable())
            self.assertTrue(assistant_scroll.widgetResizable())
            self.assertGreaterEqual(window.advisor_endpoint_input.minimumWidth(), 320)
            self.assertGreaterEqual(window.advisor_model_combo.minimumWidth(), 420)
            self.assertGreaterEqual(window.advisor_model_input.minimumWidth(), 320)
            advisor_labels = [
                window.understanding_label,
                window.intent_label,
                window.session_label,
                window.goal_label,
                window.next_action_label,
                window.advisor_source_label,
            ]
            for label in advisor_labels:
                self.assertTrue(label.wordWrap())
                self.assertEqual(label.minimumWidth(), 0)
                self.assertEqual(label.sizePolicy().horizontalPolicy(), QSizePolicy.Policy.Expanding)
                self.assertTrue(label.textInteractionFlags() & Qt.TextSelectableByMouse)

            long_unbroken_text = "https://example.test/" + ("very-long-unbroken-piece" * 12)
            broken = window._soft_break_long_tokens(f"目前理解：{long_unbroken_text}")
            self.assertIn("\u200b", broken)
            window._set_advisor_text(window.understanding_label, broken)
            window.resize(900, 700)
            app.processEvents()
            self.assertLessEqual(window.understanding_label.minimumSizeHint().width(), 900)
            window.exit_application()
            app.processEvents()

    def test_main_window_advisor_status_and_notice_dedupe(self) -> None:
        from PySide6.QtWidgets import QApplication

        from personal_ai_assistant.main_window import MainWindow

        app = QApplication.instance() or QApplication([])
        app.setQuitOnLastWindowClosed(False)
        with tempfile.TemporaryDirectory() as temp_dir:
            SettingsService(AssistantStore(Path(temp_dir) / "assistant.sqlite3")).update(auto_start_monitoring_enabled=False)
            window = MainWindow(Path(temp_dir) / "assistant.sqlite3")
            action = NextAction("回到目前目標：測試", "偏離", 0.66, "rule", intervention_level="status_only")

            window._set_advisor_status_notice(action)
            first_notice = window.status_bar_notice
            window._set_advisor_status_notice(action)

            self.assertEqual(window.status_bar_notice, first_notice)
            self.assertIn("規則", window._advisor_osd_prefix(action))
            self.assertEqual(window._advisor_osd_label(action), "規則：回目標")
            self.assertEqual(window._advisor_status_text(action), "Advisor：本機規則")
            unavailable = NextAction("正在休息，暫不打擾", "AI 暫時不可用", 0.4, "ai_unavailable", provider_label="OpenAI / gpt-test", health_state="degraded")
            self.assertEqual(window._advisor_osd_prefix(unavailable), "AI暫時不可用")
            self.assertEqual(window._advisor_osd_label(unavailable), "AI暫時不可用")
            self.assertIn("連線不穩", window._advisor_status_text(unavailable))
            long_ai = NextAction(
                "No immediate action required; continue to monitor the current work session and be ready to assist if the user initiates a request.",
                "The user appears to be engaged in the goal-directed task.",
                0.8,
                "ai",
            )
            self.assertEqual(window._advisor_osd_label(long_ai), "AI：專注中")
            self.assertEqual(window._display_app_name("ChatGPT.exe", "ChatGPT"), "ChatGPT")
            compact = window._compact_status_bar_text(["一般｜ChatGPT｜00:54", "AI：專注中", "9分鐘後：這是一個非常非常非常長的提醒事項"], max_chars=36)
            self.assertLessEqual(len(compact), 36)
            self.assertIn("…", compact)
            window.exit_application()
            app.processEvents()

    def test_main_window_provider_preset_key_url_and_empty_key_validation(self) -> None:
        from PySide6.QtWidgets import QApplication

        from personal_ai_assistant.main_window import MainWindow

        app = QApplication.instance() or QApplication([])
        app.setQuitOnLastWindowClosed(False)
        with tempfile.TemporaryDirectory() as temp_dir:
            SettingsService(AssistantStore(Path(temp_dir) / "assistant.sqlite3")).update(
                auto_start_monitoring_enabled=False,
                advisor_api_enabled=True,
            )
            window = MainWindow(Path(temp_dir) / "assistant.sqlite3")
            index = window.advisor_preset_combo.findData("openrouter")
            window.advisor_preset_combo.setCurrentIndex(index)

            self.assertEqual(window.advisor_endpoint_input.text(), "https://openrouter.ai/api/v1")
            self.assertIsNone(window.ai_form.labelForField(window.advisor_key_url_label))
            self.assertFalse(window.ai_form.isRowVisible(window.advisor_endpoint_input))
            window.advisor_api_key_input.clear()
            with patch("personal_ai_assistant.main_window.OpenAICompatibleAdvisorProvider.test_connection") as test_connection:
                window.test_advisor_connection()

            test_connection.assert_not_called()
            self.assertIn("請先填 API key", window.advisor_test_label.text())
            window.exit_application()
            app.processEvents()

    def test_main_window_advisor_disabled_grays_controls_and_preserves_settings(self) -> None:
        from PySide6.QtWidgets import QApplication

        from personal_ai_assistant.main_window import MainWindow

        app = QApplication.instance() or QApplication([])
        app.setQuitOnLastWindowClosed(False)
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "assistant.sqlite3"
            store = AssistantStore(db_path)
            SettingsService(store).update(
                auto_start_monitoring_enabled=False,
                advisor_api_enabled=True,
                advisor_provider_preset="groq",
                advisor_endpoint_url="https://api.groq.com/openai/v1",
                advisor_model_name="qwen/qwen3.6-27b",
            )
            store.set_setting("advisor_api_key", "dummy-test-key")
            window = MainWindow(db_path)

            window.set_advisor_api_enabled(False)

            self.assertFalse(window.advisor_preset_combo.isEnabled())
            self.assertFalse(window.advisor_model_combo.isEnabled())
            self.assertFalse(window.advisor_api_key_input.isEnabled())
            self.assertEqual(window.advisor_preset_combo.currentData(), ADVISOR_DISABLED_SELECTION)
            self.assertEqual(window.advisor_preset_combo.currentText(), "未啟用")
            self.assertEqual(window.advisor_model_combo.currentText(), "未啟用")
            self.assertEqual(SettingsService(window.store).load().advisor_provider_preset, "groq")
            self.assertEqual(SettingsService(window.store).load().advisor_model_name, "qwen/qwen3.6-27b")
            self.assertEqual(window.store.get_setting("advisor_api_key", ""), "dummy-test-key")
            self.assertEqual(window._advisor_status_text(), "Advisor：本機規則")
            window.exit_application()
            app.processEvents()

    def test_main_window_standard_endpoint_hidden_custom_visible_and_model_paths(self) -> None:
        from PySide6.QtWidgets import QApplication

        from personal_ai_assistant.main_window import MainWindow

        app = QApplication.instance() or QApplication([])
        app.setQuitOnLastWindowClosed(False)
        with tempfile.TemporaryDirectory() as temp_dir:
            SettingsService(AssistantStore(Path(temp_dir) / "assistant.sqlite3")).update(
                auto_start_monitoring_enabled=False,
                advisor_api_enabled=True,
                advisor_provider_preset="groq",
                advisor_endpoint_url="https://api.groq.com/openai/v1",
            )
            window = MainWindow(Path(temp_dir) / "assistant.sqlite3")

            self.assertFalse(window.ai_form.isRowVisible(window.advisor_endpoint_input))
            self.assertIn("GPT OSS 20B", window.advisor_model_combo.itemText(0))
            custom_index = window.advisor_model_combo.findData(ADVISOR_CUSTOM_MODEL)
            window.advisor_model_combo.setCurrentIndex(custom_index)
            self.assertTrue(window.ai_form.isRowVisible(window.advisor_model_input))
            window.advisor_model_input.setText("provider/custom-model")
            window.set_custom_advisor_model_name()
            self.assertEqual(SettingsService(window.store).load().advisor_model_name, "provider/custom-model")

            custom_provider_index = window.advisor_preset_combo.findData("custom")
            window.advisor_preset_combo.setCurrentIndex(custom_provider_index)
            self.assertTrue(window.ai_form.isRowVisible(window.advisor_endpoint_input))
            window.exit_application()
            app.processEvents()

    def test_main_window_models_refresh_failure_keeps_builtin_models(self) -> None:
        from PySide6.QtWidgets import QApplication

        from personal_ai_assistant.main_window import MainWindow

        app = QApplication.instance() or QApplication([])
        app.setQuitOnLastWindowClosed(False)
        with tempfile.TemporaryDirectory() as temp_dir:
            store = AssistantStore(Path(temp_dir) / "assistant.sqlite3")
            SettingsService(store).update(
                auto_start_monitoring_enabled=False,
                advisor_api_enabled=True,
                advisor_provider_preset="groq",
                advisor_endpoint_url="https://api.groq.com/openai/v1",
            )
            store.set_setting("advisor_api_key", "dummy-test-key")
            window = MainWindow(Path(temp_dir) / "assistant.sqlite3")

            with patch("personal_ai_assistant.main_window.OpenAICompatibleAdvisorProvider.list_models", side_effect=OSError("offline")):
                window.refresh_advisor_models()

            self.assertIn("GPT OSS 20B", window.advisor_model_combo.itemText(0))
            self.assertIn("模型更新失敗", window.advisor_test_label.text())
            self.assertNotIn("dummy-test-key", window.advisor_test_label.text())
            window.exit_application()
            app.processEvents()

    def test_main_window_models_refresh_marks_available_recommendations_only(self) -> None:
        from PySide6.QtWidgets import QApplication

        from personal_ai_assistant.main_window import MainWindow

        app = QApplication.instance() or QApplication([])
        app.setQuitOnLastWindowClosed(False)
        with tempfile.TemporaryDirectory() as temp_dir:
            store = AssistantStore(Path(temp_dir) / "assistant.sqlite3")
            SettingsService(store).update(
                auto_start_monitoring_enabled=False,
                advisor_api_enabled=True,
                advisor_provider_preset="groq",
                advisor_endpoint_url="https://api.groq.com/openai/v1",
                advisor_model_name="openai/gpt-oss-20b",
            )
            store.set_setting("advisor_api_key", "dummy-test-key")
            window = MainWindow(Path(temp_dir) / "assistant.sqlite3")
            remote_models = (
                AdvisorModelOption("openai/gpt-oss-20b", "openai/gpt-oss-20b"),
                AdvisorModelOption("provider/not-recommended", "provider/not-recommended"),
            )

            with patch("personal_ai_assistant.main_window.OpenAICompatibleAdvisorProvider.list_models", return_value=remote_models):
                window.refresh_advisor_models()

            labels = [window.advisor_model_combo.itemText(index) for index in range(window.advisor_model_combo.count())]
            self.assertIn("GPT OSS 20B｜文字 Advisor 推薦｜帳號可用", labels)
            self.assertIn("provider/not-recommended", labels)
            self.assertFalse(any("qwen/qwen3.6-27b" == str(window.advisor_model_combo.itemData(index)) for index in range(window.advisor_model_combo.count())))
            self.assertEqual(window.advisor_model_combo.currentData(), "openai/gpt-oss-20b")
            window.exit_application()
            app.processEvents()

    def test_main_window_test_connection_success_updates_osd_and_uses_ai(self) -> None:
        from PySide6.QtWidgets import QApplication

        from personal_ai_assistant.main_window import MainWindow

        class FakeProvider:
            def __init__(self, *_args: object) -> None:
                pass

            def advise(self, _context: dict[str, object]) -> NextAction:
                return NextAction("AI 建議：先整理下一步", "AI smoke", 0.9, "ai", intervention_level="status_only")

        app = QApplication.instance() or QApplication([])
        app.setQuitOnLastWindowClosed(False)
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "assistant.sqlite3"
            store = AssistantStore(db_path)
            SettingsService(store).update(
                auto_start_monitoring_enabled=False,
                advisor_api_enabled=True,
                advisor_provider_preset="groq",
                advisor_endpoint_url="https://api.groq.com/openai/v1",
                advisor_model_name="openai/gpt-oss-20b",
            )
            store.set_setting("advisor_api_key", "dummy-test-key")
            window = MainWindow(db_path)
            window.current_snapshot = ForegroundSnapshot(datetime(2026, 9, 8, 10, 0), "Code.exe", "main.py", "工作", 0)
            window.advisor_service = AdvisorService(
                window.store,
                SettingsService(window.store),
                NextActionEngine(window.store),
                ContextBuilder(window.store),
                provider_factory=lambda *_args: FakeProvider(),
            )

            with patch("personal_ai_assistant.main_window.OpenAICompatibleAdvisorProvider.test_connection", return_value=(True, "連線成功")):
                window.test_advisor_connection()

            self.assertEqual(window.store.get_setting("advisor_api_last_ok", ""), "1")
            self.assertEqual(window.last_advisor_action.source, "ai")
            self.assertEqual(window._advisor_osd_prefix(), "AI")
            self.assertIn("Advisor：Groq / openai/gpt-oss-20b｜可用", window.advisor_source_label.text())
            self.assertIn("AI 建議", window.next_action_label.text())
            window.exit_application()
            app.processEvents()

    def test_handoff_provider_does_not_change_advisor_source(self) -> None:
        from PySide6.QtWidgets import QApplication

        from personal_ai_assistant.main_window import MainWindow

        app = QApplication.instance() or QApplication([])
        app.setQuitOnLastWindowClosed(False)
        with tempfile.TemporaryDirectory() as temp_dir:
            SettingsService(AssistantStore(Path(temp_dir) / "assistant.sqlite3")).update(
                auto_start_monitoring_enabled=False,
                ai_handoff_enabled=True,
                ai_provider="grok",
                advisor_api_enabled=False,
            )
            window = MainWindow(Path(temp_dir) / "assistant.sqlite3")

            window.refresh_advisor(auto=False)

            self.assertEqual(window.last_advisor_action.source, "rule")
            self.assertEqual(window._advisor_osd_prefix(), "規則")
            self.assertIn("本機規則", window.advisor_source_label.text())
            window.exit_application()
            app.processEvents()

    def test_auto_idle_pause_pauses_monitoring_without_logging(self) -> None:
        from PySide6.QtWidgets import QApplication

        from personal_ai_assistant.main_window import MainWindow

        app = QApplication.instance() or QApplication([])
        app.setQuitOnLastWindowClosed(False)
        with tempfile.TemporaryDirectory() as temp_dir:
            idle_snapshot = ForegroundSnapshot(
                timestamp=datetime(2026, 9, 7, 18, 30),
                app_name="Code.exe",
                window_title="main.py",
                mode="閒置",
                idle_seconds=10 * 60,
            )

            with patch("personal_ai_assistant.main_window.get_foreground_snapshot", return_value=idle_snapshot), patch(
                "personal_ai_assistant.main_window.is_windows_locked_or_unavailable", return_value=False
            ):
                window = MainWindow(Path(temp_dir) / "assistant.sqlite3")

            self.assertTrue(window.monitoring)
            self.assertTrue(window.auto_idle_paused)
            self.assertTrue(window.monitor_timer.isActive())
            self.assertEqual(window.store.recent_events(), [])
            self.assertIn("離席暫停", window.monitor_service_label.text())
            window.exit_application()
            app.processEvents()

    def test_auto_idle_pause_resumes_after_input(self) -> None:
        from PySide6.QtWidgets import QApplication

        from personal_ai_assistant.main_window import MainWindow

        app = QApplication.instance() or QApplication([])
        app.setQuitOnLastWindowClosed(False)
        with tempfile.TemporaryDirectory() as temp_dir:
            idle_snapshot = ForegroundSnapshot(
                timestamp=datetime(2026, 9, 7, 18, 30),
                app_name="Code.exe",
                window_title="main.py",
                mode="閒置",
                idle_seconds=10 * 60,
            )
            active_snapshot = ForegroundSnapshot(
                timestamp=datetime(2026, 9, 7, 18, 31),
                app_name="Code.exe",
                window_title="main.py",
                mode="工作",
                idle_seconds=0,
            )

            with patch("personal_ai_assistant.main_window.get_foreground_snapshot", return_value=idle_snapshot), patch(
                "personal_ai_assistant.main_window.is_windows_locked_or_unavailable", return_value=False
            ):
                window = MainWindow(Path(temp_dir) / "assistant.sqlite3")
            with patch("personal_ai_assistant.main_window.get_foreground_snapshot", return_value=active_snapshot), patch(
                "personal_ai_assistant.main_window.is_windows_locked_or_unavailable", return_value=False
            ):
                window.monitor_tick()

            self.assertTrue(window.monitoring)
            self.assertFalse(window.auto_idle_paused)
            self.assertEqual(len(window.store.recent_events()), 1)
            self.assertIn("監督中", window.monitor_service_label.text())
            window.exit_application()
            app.processEvents()

    def test_manual_stop_is_not_auto_resumed_by_activity(self) -> None:
        from PySide6.QtWidgets import QApplication

        from personal_ai_assistant.main_window import MainWindow

        app = QApplication.instance() or QApplication([])
        app.setQuitOnLastWindowClosed(False)
        with tempfile.TemporaryDirectory() as temp_dir:
            active_snapshot = ForegroundSnapshot(
                timestamp=datetime(2026, 9, 7, 18, 30),
                app_name="Code.exe",
                window_title="main.py",
                mode="工作",
                idle_seconds=0,
            )

            with patch("personal_ai_assistant.main_window.get_foreground_snapshot", return_value=active_snapshot), patch(
                "personal_ai_assistant.main_window.is_windows_locked_or_unavailable", return_value=False
            ):
                window = MainWindow(Path(temp_dir) / "assistant.sqlite3")
                window.toggle_monitoring()
                window.start_monitoring(manual=False)
                window.monitor_tick()

            self.assertFalse(window.monitoring)
            self.assertFalse(window.auto_idle_paused)
            self.assertFalse(window.monitor_timer.isActive())
            self.assertEqual(len(window.store.recent_events()), 1)
            self.assertIn("手動停止", window.monitor_service_label.text())
            window.exit_application()
            app.processEvents()

    def test_reminder_service_runs_during_auto_idle_pause(self) -> None:
        from PySide6.QtWidgets import QApplication

        from personal_ai_assistant.main_window import MainWindow

        app = QApplication.instance() or QApplication([])
        app.setQuitOnLastWindowClosed(False)
        with tempfile.TemporaryDirectory() as temp_dir:
            idle_snapshot = ForegroundSnapshot(
                timestamp=datetime(2026, 9, 7, 18, 30),
                app_name="Code.exe",
                window_title="main.py",
                mode="閒置",
                idle_seconds=10 * 60,
            )

            with patch("personal_ai_assistant.main_window.get_foreground_snapshot", return_value=idle_snapshot), patch(
                "personal_ai_assistant.main_window.is_windows_locked_or_unavailable", return_value=False
            ):
                window = MainWindow(Path(temp_dir) / "assistant.sqlite3")
            sent_notifications: list[str] = []
            window.notifications.notify = lambda title, _body: sent_notifications.append(title) or True
            shown_reminders: list[int] = []
            window.show_reminder_popup = lambda reminder, _title, _body: shown_reminders.append(reminder.id)
            reminder = window.store.add_reminder("健康檢查", datetime.now() - timedelta(minutes=1))

            window.reminder_tick()

            self.assertTrue(window.auto_idle_paused)
            self.assertTrue(window.reminder_timer.isActive())
            self.assertEqual(shown_reminders, [])
            self.assertEqual(sent_notifications, ["提醒先幫你留著"])
            self.assertEqual(window.store.list_reminders()[0].status, "pending_due")
            window.exit_application()
            app.processEvents()

    def test_main_window_close_hides_instead_of_exiting(self) -> None:
        from PySide6.QtWidgets import QApplication

        from personal_ai_assistant.main_window import MainWindow

        app = QApplication.instance() or QApplication([])
        app.setQuitOnLastWindowClosed(False)
        with tempfile.TemporaryDirectory() as temp_dir:
            window = MainWindow(Path(temp_dir) / "assistant.sqlite3")
            window.show()
            app.processEvents()

            window.close()
            app.processEvents()

            self.assertFalse(window.isVisible())
            self.assertTrue(window.reminder_timer.isActive())
            self.assertFalse(window.exiting)
            self.assertFalse(QApplication.closingDown())
            window.exit_application()
            app.processEvents()

    def test_settings_export_excludes_private_records(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = AssistantStore(Path(temp_dir) / "assistant.sqlite3")
            settings = SettingsService(store)
            store.add_reminder("健康檢查", datetime(2026, 9, 7, 21, 0))
            settings.update(privacy_mode_enabled=True, excluded_apps=("bitwarden.exe", "bank"))

            exported = settings.export_public_settings()

            self.assertTrue(exported["settings"]["privacy_mode_enabled"])
            self.assertEqual(exported["settings"]["excluded_apps"], ["bitwarden.exe", "bank"])
            self.assertNotIn("reminders", exported)
            self.assertNotIn("usage_events", exported)

    def test_reset_learning_data_keeps_formal_reminders(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = AssistantStore(Path(temp_dir) / "assistant.sqlite3")
            reminder = store.add_reminder("檢查待辦", datetime(2026, 9, 7, 21, 0))
            store.add_usage_event(ForegroundSnapshot(datetime(2026, 9, 7, 18, 30), "Code.exe", "main.py", "工作", 0))
            store.upsert_pattern("app_seen", "app", "code.exe", "Code.exe", 1, 1, 0.1, "observing", "常用 App：Code.exe")

            store.reset_learning_data()

            self.assertEqual(store.get_reminder(reminder.id).text, "檢查待辦")
            self.assertEqual(store.recent_events(), [])
            self.assertEqual(store.learned_patterns(), [])

    def test_dead_loop_detector_builds_handoff_prompt(self) -> None:
        now = datetime(2026, 9, 7, 18, 30)
        events = [
            AssistantEvent(
                id=index,
                timestamp=now - timedelta(minutes=index),
                event_type="foreground_changed",
                source="foreground_monitor",
                context_mode="工作",
                app_name="Code.exe" if index % 2 else "chrome.exe",
                window_title_summary="error in tests" if index % 3 == 0 else "main.py",
            )
            for index in range(12)
        ]
        snapshot = ForegroundSnapshot(now, "Code.exe", "main.py", "工作", 0)

        assessment = DeadLoopDetector(threshold=0.4).assess(snapshot, events, now)

        self.assertTrue(assessment.should_alert)
        self.assertIn("不要沿用", assessment.prompt)
        self.assertIn("最近 10-30 分鐘重要事件", assessment.prompt)

    def test_ai_handoff_copies_prompt_without_api(self) -> None:
        copied: list[str] = []
        result = AIHandoffProvider("chatgpt").handoff(
            "請重新檢查替代方向",
            copy_text=copied.append,
            open_browser=False,
        )

        self.assertEqual(copied, ["請重新檢查替代方向"])
        self.assertEqual(result.provider, "chatgpt")

    def test_ai_handoff_can_be_disabled_in_settings(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = AssistantStore(Path(temp_dir) / "assistant.sqlite3")
            settings = SettingsService(store).load()

            self.assertFalse(settings.ai_handoff_enabled)
            self.assertIn("grok", PROVIDER_URLS)

            updated = SettingsService(store).update(ai_handoff_enabled=True, ai_provider="grok")
            self.assertTrue(updated.ai_handoff_enabled)
            self.assertEqual(updated.ai_provider, "grok")

    def test_global_hotkey_setting_parses_and_reports_conflict(self) -> None:
        parsed = parse_hotkey("Ctrl + Alt + A")

        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.label, "Ctrl+Alt+A")
        self.assertEqual(parsed.virtual_key, ord("A"))
        self.assertIsNone(parse_hotkey("Ctrl+Space"))
        self.assertIn("已被其他程式使用", hotkey_error_message(1409))

    def test_quick_add_escape_cancels_without_creating_reminder(self) -> None:
        from PySide6.QtCore import QTimer, Qt
        from PySide6.QtTest import QTest
        from PySide6.QtWidgets import QApplication, QLineEdit

        from personal_ai_assistant.main_window import MainWindow

        app = QApplication.instance() or QApplication([])
        app.setQuitOnLastWindowClosed(False)
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "assistant.sqlite3"
            SettingsService(AssistantStore(db_path)).update(auto_start_monitoring_enabled=False)
            window = MainWindow(db_path)

            def cancel_dialog() -> None:
                dialog = app.activeModalWidget()
                self.assertIsNotNone(dialog)
                input_box = dialog.findChild(QLineEdit) if dialog else None
                self.assertIsNotNone(input_box)
                input_box.setText("18:30 喝水")
                QTest.keyClick(input_box, Qt.Key.Key_Escape)

            QTimer.singleShot(50, cancel_dialog)
            window.show_quick_add_dialog()

            self.assertEqual(window.store.list_reminders(), [])
            window.exit_application()
            app.processEvents()

    def test_quick_add_enter_creates_reminder(self) -> None:
        from PySide6.QtCore import QTimer, Qt
        from PySide6.QtTest import QTest
        from PySide6.QtWidgets import QApplication, QLineEdit

        from personal_ai_assistant.main_window import MainWindow

        app = QApplication.instance() or QApplication([])
        app.setQuitOnLastWindowClosed(False)
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "assistant.sqlite3"
            SettingsService(AssistantStore(db_path)).update(auto_start_monitoring_enabled=False)
            window = MainWindow(db_path)

            def accept_dialog() -> None:
                dialog = app.activeModalWidget()
                self.assertIsNotNone(dialog)
                input_box = dialog.findChild(QLineEdit) if dialog else None
                self.assertIsNotNone(input_box)
                input_box.setText("十分鐘後提醒我吃飯")
                QTest.keyClick(input_box, Qt.Key.Key_Return)

            QTimer.singleShot(50, accept_dialog)
            window.show_quick_add_dialog()

            reminders = window.store.list_reminders()
            self.assertEqual(len(reminders), 1)
            self.assertEqual(reminders[0].text, "吃飯")
            self.assertGreater(reminders[0].due_at, datetime.now())
            self.assertLess(reminders[0].due_at, datetime.now() + timedelta(minutes=11))
            window.exit_application()
            app.processEvents()

    def test_reminder_popup_only_shows_complete_and_snooze_buttons(self) -> None:
        from PySide6.QtWidgets import QApplication, QPushButton

        from personal_ai_assistant.reminder_popup import ReminderPopup

        app = QApplication.instance() or QApplication([])
        reminder = Reminder(
            id=1,
            text="喝水",
            due_at=datetime(2026, 9, 8, 18, 30),
            created_at=datetime(2026, 9, 8, 12, 0),
        )
        popup = ReminderPopup(
            reminder=reminder,
            title="提醒：喝水",
            body="現在要處理這件事。",
            on_complete=lambda _reminder_id: None,
            on_snooze=lambda _reminder_id: None,
            on_ignore=lambda _reminder_id: None,
        )

        button_labels = [button.text() for button in popup.findChildren(QPushButton)]

        self.assertEqual(button_labels, ["完成", "10 分鐘後"])
        self.assertNotIn("忽略", button_labels)
        popup.close()
        app.processEvents()

    def test_assistant_notification_prompt_has_ignore_button(self) -> None:
        from PySide6.QtWidgets import QApplication, QPushButton

        from personal_ai_assistant.reminder_popup import AssistantNotificationPrompt

        app = QApplication.instance() or QApplication([])
        prompt = AssistantNotificationPrompt(
            notification_event_id=1,
            title="訊息提醒",
            body="要不要確認？",
            on_open=lambda _event_id: None,
            on_snooze=lambda _event_id: None,
            on_ignore=lambda _event_id: None,
        )

        button_labels = [button.text() for button in prompt.findChildren(QPushButton)]

        self.assertEqual(button_labels, ["去看看", "稍後提醒", "忽略"])
        prompt.close()
        app.processEvents()

    def test_legacy_ignored_reminders_remain_readable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = AssistantStore(Path(temp_dir) / "assistant.sqlite3")
            reminder = store.add_reminder("舊忽略事項", datetime(2026, 9, 8, 18, 30))
            store.mark_reminder_notified(reminder.id, datetime(2026, 9, 8, 18, 30))
            store.log_reminder_interaction(reminder.id, "ignored", datetime(2026, 9, 8, 18, 31), "一般")
            store.ignore_reminder(reminder.id, datetime(2026, 9, 8, 18, 31))

            reopened = AssistantStore(Path(temp_dir) / "assistant.sqlite3")
            ignored = reopened.get_reminder(reminder.id)

            self.assertIsNotNone(ignored)
            self.assertEqual(ignored.status, "ignored")
            self.assertEqual(ignored.ignored_count, 1)
            self.assertEqual(reopened.reminder_interactions(reminder.id)[0].event_type, "ignored")

    def test_list_formatters_wrap_and_hide_internal_event_type(self) -> None:
        reminder = Reminder(
            id=1,
            text="吃避孕藥",
            due_at=datetime(2026, 9, 8, 22, 30),
            created_at=datetime(2026, 9, 8, 9, 0),
            recurrence_rule='{"freq": "daily"}',
        )
        event = AssistantEvent(
            id=1,
            timestamp=datetime(2026, 9, 8, 22, 0),
            event_type="foreground_changed",
            source="foreground_monitor",
            context_mode="聊天",
            app_name="Telegram.exe",
            window_title_summary="股癌台股採購",
        )

        reminder_text = format_reminder_card_text(reminder)
        event_text = format_event_card_text(event)

        self.assertIn("吃避孕藥  [等待中]\n", reminder_text)
        self.assertIn("正式設定", reminder_text)
        self.assertIn("切換到 Telegram.exe：股癌台股採購", event_text)
        self.assertNotIn("foreground_changed", event_text)

    def test_multiscreen_auto_prefers_non_active_screen(self) -> None:
        primary = DisplayGeometry("Primary", 0, True, 0, 0, 1920, 1080, 0, 0, 1920, 1040, 1.25)
        secondary = DisplayGeometry("Second", 1, False, 1920, 0, 1920, 1080, 1920, 0, 1920, 1040, 1.0)

        selected = choose_status_screen([primary, secondary], active_screen_index=0, setting="auto")

        self.assertEqual(selected, secondary)
        self.assertEqual(choose_status_screen([primary], active_screen_index=0, setting="auto"), primary)
        self.assertEqual(choose_popup_screen([primary, secondary], 0, secondary, "status_bar"), secondary)

    def test_status_bar_position_and_screen_settings(self) -> None:
        primary = DisplayGeometry("Primary", 0, True, 0, 0, 1920, 1080, 0, 0, 1920, 1040, 1.25)
        secondary = DisplayGeometry("Second", 1, False, 1920, 0, 1920, 1080, 1920, 0, 1920, 1040, 1.0)

        self.assertEqual(choose_status_screen([primary, secondary], active_screen_index=0, setting="primary"), primary)
        self.assertEqual(choose_status_screen([primary, secondary], active_screen_index=0, setting="secondary"), secondary)
        self.assertEqual(choose_status_screen([primary, secondary], active_screen_index=0, setting="screen:1"), secondary)
        self.assertEqual(choose_status_screen([primary, secondary], active_screen_index=1, setting="active"), secondary)

    def test_status_bar_right_edge_geometry_uses_screen_available_bounds(self) -> None:
        cases = [
            DisplayGeometry("Primary", 0, True, 0, 0, 1920, 1080, 0, 0, 1920, 1040, 1.0),
            DisplayGeometry("Right", 1, False, 1920, 0, 2560, 1440, 1920, 40, 2560, 1340, 1.25),
            DisplayGeometry("Left", 1, False, -1920, 0, 1920, 1080, -1920, 0, 1920, 1040, 1.5),
            DisplayGeometry("Negative", 2, False, -2560, -120, 2560, 1440, -2560, -80, 2560, 1360, 1.5),
        ]

        for screen in cases:
            with self.subTest(screen=screen.name):
                x, y, width, height = status_bar_geometry(screen, 480, "top_right", margin=12, height=28)
                self.assertEqual(screen.available_x + screen.available_width - (x + width), 12)
                self.assertEqual(y, screen.available_y + 12)
                self.assertEqual(height, 28)

                left_x, left_y, left_width, _ = status_bar_geometry(screen, 480, "top_left", margin=12, height=28)
                self.assertEqual(left_x, screen.available_x + 12)
                self.assertEqual(left_y, screen.available_y + 12)
                self.assertEqual(left_width, width)

    def test_status_bar_geometry_clamps_to_small_or_scaled_screens(self) -> None:
        screen = DisplayGeometry("Scaled", 0, True, -1280, 0, 1280, 720, -1260, 30, 1000, 650, 1.5)

        x, _y, width, _height = status_bar_geometry(screen, 3000, "top_right", margin=16)

        self.assertEqual(width, 620)
        self.assertEqual(screen.available_x + screen.available_width - (x + width), 16)

    def test_reminder_popup_target_selection(self) -> None:
        primary = DisplayGeometry("Primary", 0, True, 0, 0, 1920, 1080, 0, 0, 1920, 1040, 1.25)
        secondary = DisplayGeometry("Second", 1, False, 1920, 0, 1920, 1080, 1920, 0, 1920, 1040, 1.0)

        self.assertEqual(choose_popup_screen([primary, secondary], 1, primary, "active"), secondary)
        self.assertEqual(choose_popup_screen([primary, secondary], 1, secondary, "status_bar"), secondary)
        self.assertEqual(choose_popup_screen([primary, secondary], 1, secondary, "primary"), primary)
        self.assertEqual(choose_popup_screen([primary, secondary], 1, primary, "secondary"), secondary)
        self.assertEqual(choose_popup_screen([primary, secondary], 0, primary, "screen:1"), secondary)

    def test_dpi_scaling_and_borderless_classification(self) -> None:
        screen = DisplayGeometry("Primary", 0, True, 0, 0, 2560, 1440, 0, 0, 2560, 1390, 1.5)

        self.assertEqual(scaled_size(100, 1.5), 150)
        self.assertEqual(classify_window_presentation((0, 0, 2560, 1440), screen, "steam game"), "borderless_fullscreen")
        self.assertEqual(classify_window_presentation((100, 100, 1200, 900), screen, ""), "windowed")
        self.assertEqual(classify_window_presentation((0, 0, 2560, 1440), screen, "exclusive"), "exclusive_fullscreen")

    def test_today_inbox_low_distraction_rules(self) -> None:
        now = datetime(2026, 9, 7, 10, 0)

        self.assertTrue(looks_like_today_inbox("今天記得繳費"))
        self.assertEqual(strip_inbox_prefix("有空提醒我整理企劃"), "整理企劃")
        self.assertEqual(today_inbox_due_at(now), datetime(2026, 9, 7, 23, 59))
        self.assertTrue(should_suggest_today_inbox(True, "一般", False, 60))
        self.assertFalse(should_suggest_today_inbox(True, "遊戲", False, 60))

    def test_context_reminder_triggers_once_on_transition(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = AssistantStore(Path(temp_dir) / "assistant.sqlite3")
            rule = store.add_context_reminder("檢查 build", "code.exe", "open")
            snapshot = ForegroundSnapshot(datetime(2026, 9, 7, 18, 30), "code.exe", "main.py", "工作", 0)

            first = store.evaluate_context_reminders(snapshot)
            second = store.evaluate_context_reminders(snapshot)

            self.assertEqual(len(first), 1)
            self.assertEqual(first[0].context_rule_id, rule.id)
            self.assertEqual(second, [])

    def test_pending_due_releases_when_user_returns(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = AssistantStore(Path(temp_dir) / "assistant.sqlite3")
            reminder = store.add_reminder("一般提醒", datetime(2026, 9, 7, 18, 30))

            store.mark_reminder_pending_due(reminder.id, "你離席")
            self.assertEqual(store.pending_due_reminders()[0].id, reminder.id)
            store.release_pending_due(reminder.id)

            self.assertEqual(store.due_reminders(datetime(2026, 9, 7, 18, 31))[0].id, reminder.id)

    def test_away_due_defers_popup_and_return_confirms_once(self) -> None:
        from PySide6.QtWidgets import QApplication

        from personal_ai_assistant.main_window import MainWindow

        app = QApplication.instance() or QApplication([])
        app.setQuitOnLastWindowClosed(False)
        with tempfile.TemporaryDirectory() as temp_dir:
            idle_snapshot = ForegroundSnapshot(datetime(2026, 9, 7, 18, 30), "Code.exe", "main.py", "閒置", 10 * 60)
            active_snapshot = ForegroundSnapshot(datetime(2026, 9, 7, 18, 40), "Code.exe", "main.py", "工作", 0)
            with patch("personal_ai_assistant.main_window.get_foreground_snapshot", return_value=idle_snapshot), patch(
                "personal_ai_assistant.main_window.is_windows_locked_or_unavailable", return_value=False
            ):
                window = MainWindow(Path(temp_dir) / "assistant.sqlite3")
            window.notifications.notify = lambda _title, _body: True
            shown: list[str] = []
            window.show_reminder_popup = lambda reminder, title, _body: shown.append(title)
            reminder = window.store.add_reminder("吃避孕藥", datetime.now() - timedelta(minutes=1))

            with patch("personal_ai_assistant.main_window.is_windows_locked_or_unavailable", return_value=False):
                window.reminder_tick()
            self.assertEqual(shown, [])
            self.assertEqual(window.store.get_reminder(reminder.id).status, "pending_due")

            window.auto_idle_paused = False
            with patch("personal_ai_assistant.main_window.is_windows_locked_or_unavailable", return_value=False):
                window._release_pending_due_reminders(active_snapshot)

            self.assertEqual(shown, ["回來確認：吃避孕藥"])
            self.assertEqual(window.store.get_reminder(reminder.id).status, "notified")
            window.complete_reminder(reminder.id)
            self.assertEqual(window.store.get_reminder(reminder.id).status, "completed")
            window.reminder_tick()
            self.assertEqual(shown, ["回來確認：吃避孕藥"])
            window.exit_application()
            app.processEvents()

    def test_multi_away_reminders_are_queued(self) -> None:
        from types import SimpleNamespace
        from PySide6.QtWidgets import QApplication

        from personal_ai_assistant.main_window import MainWindow

        app = QApplication.instance() or QApplication([])
        app.setQuitOnLastWindowClosed(False)
        with tempfile.TemporaryDirectory() as temp_dir:
            active_snapshot = ForegroundSnapshot(datetime(2026, 9, 7, 18, 40), "Code.exe", "main.py", "工作", 0)
            SettingsService(AssistantStore(Path(temp_dir) / "assistant.sqlite3")).update(auto_start_monitoring_enabled=False)
            window = MainWindow(Path(temp_dir) / "assistant.sqlite3")
            shown: list[int] = []

            def fake_popup(reminder, _title, _body):
                shown.append(reminder.id)
                window.active_popups[reminder.id] = SimpleNamespace(reminder=reminder)

            window.show_reminder_popup = fake_popup
            first = window.store.add_reminder("第一件事", datetime.now() - timedelta(minutes=2))
            second = window.store.add_reminder("第二件事", datetime.now() - timedelta(minutes=1))
            window.store.mark_reminder_pending_due(first.id, "離席")
            window.store.mark_reminder_pending_due(second.id, "離席")

            with patch("personal_ai_assistant.main_window.is_windows_locked_or_unavailable", return_value=False):
                window._release_pending_due_reminders(active_snapshot)
                window._release_pending_due_reminders(active_snapshot)
            self.assertEqual(shown, [first.id])
            self.assertEqual(window.store.get_reminder(second.id).status, "pending_due")

            window.active_popups.clear()
            with patch("personal_ai_assistant.main_window.is_windows_locked_or_unavailable", return_value=False):
                window._release_pending_due_reminders(active_snapshot)
            self.assertEqual(shown, [first.id, second.id])
            window.exit_application()
            app.processEvents()

    def test_unconfirmed_reminder_escalates_after_interval(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = AssistantStore(Path(temp_dir) / "assistant.sqlite3")
            reminder = store.add_reminder("一般提醒", datetime(2026, 9, 7, 18, 30))

            store.mark_reminder_notified(reminder.id, datetime(2026, 9, 7, 18, 30))
            escalated = store.escalate_unconfirmed_reminders(datetime(2026, 9, 7, 18, 40), datetime(2026, 9, 7, 18, 45))

            self.assertEqual([item.id for item in escalated], [reminder.id])
            self.assertEqual(store.get_reminder(reminder.id).status, "pending")

    def test_daily_summary_buckets_modes(self) -> None:
        events = [
            ForegroundSnapshot(datetime(2026, 9, 7, 9, 0), "Code.exe", "main.py", "工作", 0),
            ForegroundSnapshot(datetime(2026, 9, 7, 10, 0), "Discord.exe", "chat", "聊天", 0),
            ForegroundSnapshot(datetime(2026, 9, 7, 10, 30), "Steam.exe", "game", "遊戲", 0),
        ]
        usage = []
        with tempfile.TemporaryDirectory() as temp_dir:
            store = AssistantStore(Path(temp_dir) / "assistant.sqlite3")
            for snapshot in events:
                usage.append(store.add_usage_event(snapshot))

        summary = build_daily_summary(usage, datetime(2026, 9, 7).date(), datetime(2026, 9, 7, 11, 0))

        self.assertEqual({item.label: item.seconds for item in summary}, {"工作": 3600, "聊天": 1800, "遊戲": 1800})

    def test_settings_version_startup_and_update_checker_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = AssistantStore(Path(temp_dir) / "assistant.sqlite3")
            settings = SettingsService(store).load()

            self.assertEqual(settings.version, "v0.1.0-alpha")
            self.assertTrue(settings.auto_start_monitoring_enabled)
            self.assertFalse(settings.ai_handoff_enabled)
            self.assertFalse(settings.advisor_api_enabled)
            self.assertEqual(settings.top_status_bar_position, "top_left")
            self.assertEqual(settings.top_status_bar_screen, "auto")
            self.assertEqual(settings.reminder_popup_screen, "active")
            self.assertIn("run_silent.pyw", startup_command(Path(temp_dir), pythonw_path="pythonw.exe"))
            self.assertTrue(should_check_for_updates(True, None))
            self.assertFalse(check_latest_release().checked)

    def test_advisor_api_key_is_not_exported(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = AssistantStore(Path(temp_dir) / "assistant.sqlite3")
            store.set_setting("advisor_api_key", "dummy-test-key")
            exported = SettingsService(store).export_public_settings()

            self.assertNotIn("advisor_api_key", json.dumps(exported))

    def test_settings_import_migrates_new_ui_settings(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = AssistantStore(Path(temp_dir) / "assistant.sqlite3")
            settings = SettingsService(store).import_public_settings(
                {
                    "settings": {
                        "auto_start_monitoring_enabled": False,
                        "ai_handoff_enabled": True,
                        "ai_provider": "custom",
                        "ai_handoff_url": "https://example.test/ai",
                        "top_status_bar_screen": "secondary",
                        "top_status_bar_position": "top_right",
                        "reminder_popup_screen": "status_bar",
                    }
                }
            )

            self.assertFalse(settings.auto_start_monitoring_enabled)
            self.assertTrue(settings.ai_handoff_enabled)
            self.assertEqual(settings.ai_provider, "custom")
            self.assertEqual(settings.ai_handoff_url, "https://example.test/ai")
            self.assertEqual(settings.top_status_bar_screen, "secondary")
            self.assertEqual(settings.top_status_bar_position, "top_right")
            self.assertEqual(settings.reminder_popup_screen, "status_bar")

    def test_intent_router_routes_core_commands(self) -> None:
        now = datetime(2026, 9, 10, 17, 30)
        cases = {
            "10分鐘後提醒我吃飯": "reminder",
            "幫我開 Steam": "activate_or_launch_app",
            "打開 Discord": "activate_or_launch_app",
            "切到 Steam": "activate_or_launch_app",
            "幫我問 ChatGPT 為什麼 Helldivers 2 一直崩潰": "ask_ai",
            "我現在要休息": "set_mode",
            "今天目標是把提醒 bug 修完": "set_goal",
            "開下載資料夾": "open_folder",
            "打開 example.com": "open_url",
        }
        for text, intent in cases.items():
            with self.subTest(text=text):
                self.assertEqual(route_assistant_command(text, now).intent, intent)

    def test_intent_router_reuses_reminder_parser(self) -> None:
        routed = route_assistant_command("5點半提醒我收垃圾", datetime(2026, 9, 10, 17, 20))
        self.assertEqual(routed.intent, "reminder")
        self.assertIsNotNone(routed.parsed_reminder)
        self.assertEqual(routed.parsed_reminder.due_at, datetime(2026, 9, 10, 17, 30))

    def test_set_goal_and_set_mode_payloads(self) -> None:
        goal = route_assistant_command("今天目標是把提醒 bug 修完")
        self.assertEqual(goal.intent, "set_goal")
        self.assertEqual(goal.payload["goal"], "把提醒 bug 修完")

        rest = route_assistant_command("我休息20分鐘")
        self.assertEqual(rest.intent, "set_mode")
        self.assertEqual(rest.payload["mode"], "intentional_break")
        self.assertEqual(rest.payload["minutes"], 20)

        private = route_assistant_command("進入私人時間")
        self.assertEqual(private.payload["mode"], "private_time")

    def test_ask_ai_provider_selection_and_default(self) -> None:
        self.assertEqual(route_assistant_command("問 Grok Helldivers 2 crash").target, "grok")
        self.assertEqual(route_assistant_command("問 Claude Python 錯誤").target, "claude")
        self.assertEqual(route_assistant_command("問 Gemini 測試").target, "gemini")
        routed = route_assistant_command("幫我問 為什麼一直崩潰", default_ai_provider="claude")
        self.assertEqual(routed.intent, "ask_ai")
        self.assertEqual(routed.payload["provider"], "claude")

    def test_action_risk_policy_and_auto_send_default(self) -> None:
        self.assertFalse(classify_action_risk("launch_app").requires_confirmation)
        self.assertFalse(classify_action_risk("reminder").requires_confirmation)
        self.assertTrue(classify_action_risk("shell").requires_confirmation)
        self.assertFalse(route_assistant_command("問 GPT 幫我看錯誤").risk.requires_confirmation)
        self.assertTrue(route_assistant_command("問 GPT 幫我看錯誤", auto_send_ai=True).risk.requires_confirmation)

    def test_app_resolver_learned_alias_and_ambiguous_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            exe = Path(temp_dir) / "Steam.exe"
            exe.write_text("", encoding="utf-8")
            resolver = AppResolver({"steam": str(exe)})
            result = resolver.resolve_app("Steam")
            self.assertEqual(result.best.path, str(exe))
            self.assertFalse(result.needs_confirmation)

        resolver = AppResolver()
        resolver._from_app_paths = lambda _tokens: [  # type: ignore[method-assign]
            ResolveCandidate("Discord A", r"C:\A\Discord.exe", "app_paths"),
            ResolveCandidate("Discord B", r"C:\B\Discord.exe", "app_paths"),
        ]
        resolver._from_start_menu = lambda _tokens: []  # type: ignore[method-assign]
        resolver._from_uninstall_metadata = lambda _tokens: []  # type: ignore[method-assign]
        resolver._from_common_paths = lambda _tokens: []  # type: ignore[method-assign]
        ambiguous = resolver.resolve_app("Discord")
        self.assertTrue(ambiguous.needs_confirmation)
        self.assertEqual(len(ambiguous.candidates), 2)

    def test_folder_alias_resolution(self) -> None:
        result = AppResolver(project_root=Path("D:/private_assistant_ai_mvp")).resolve_folder("下載資料夾")
        self.assertIn(result.query, ("下載資料夾",))

    def test_voice_command_is_optional_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = SettingsService(AssistantStore(Path(temp_dir) / "assistant.sqlite3")).load()
            self.assertFalse(settings.voice_command_enabled)
            self.assertFalse(settings.ai_handoff_auto_send)

    def test_assistant_name_persistence_and_import_export(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = AssistantStore(Path(temp_dir) / "assistant.sqlite3")
            service = SettingsService(store)
            service.update(assistant_name="米米", wake_aliases=("Hey 米米",), voice_microphone_id="mic-1")

            exported = service.export_public_settings()
            self.assertEqual(exported["settings"]["assistant_name"], "米米")
            self.assertEqual(exported["settings"]["wake_aliases"], ["Hey 米米"])
            self.assertNotIn("recording", json.dumps(exported).lower())

            imported = SettingsService(AssistantStore(Path(temp_dir) / "imported.sqlite3")).import_public_settings(exported)
            self.assertEqual(imported.assistant_name, "米米")
            self.assertEqual(imported.wake_aliases, ("Hey 米米",))

    def test_wake_aliases_hot_reload_after_rename(self) -> None:
        class FakeWakeProvider:
            def __init__(self) -> None:
                self.configured: list[tuple[str, ...]] = []

            def configure(self, aliases, microphone_id="") -> None:
                self.configured.append(tuple(aliases))

            def poll(self) -> bool:
                return False

        provider = FakeWakeProvider()
        pipeline = VoiceAssistantPipeline(WakeSettings("米米"), wake_provider=provider)
        pipeline.reload(WakeSettings("小光", ("Hey 小光",)))

        self.assertIn("米米", provider.configured[0])
        self.assertIn("小光", provider.configured[-1])
        self.assertIn("Hey 小光", provider.configured[-1])

    def test_wake_phrase_stripped_before_intent_routing(self) -> None:
        settings = WakeSettings("米米", ("Hey 米米",))
        self.assertEqual(strip_wake_phrase("米米，幫我開 Steam", wake_aliases_for_settings(settings)), "幫我開 Steam")
        routed = route_assistant_command(strip_wake_phrase("Hey 米米 我休息20分鐘", wake_aliases_for_settings(settings)))
        self.assertEqual(routed.intent, "set_mode")
        self.assertEqual(routed.payload["minutes"], 20)

    def test_wake_detector_idle_does_not_invoke_full_stt(self) -> None:
        class ExplodingSTT:
            def listen_once(self, timeout_seconds=8):
                raise AssertionError("full STT should not run while idle")

        provider = PrefixWakeWordProvider(lambda: "普通房間聲音")
        pipeline = VoiceAssistantPipeline(WakeSettings("米米"), wake_provider=provider, stt_provider=ExplodingSTT())
        pipeline.start()

        self.assertFalse(pipeline.idle_tick())
        self.assertEqual(pipeline.full_stt_invocations, 0)

    def test_selected_device_id_reaches_wake_input_backend(self) -> None:
        microphone_id = build_sounddevice_identifier(7, "HyperX QuadCast S", "Windows WASAPI")
        provider = PrefixWakeWordProvider(lambda: "")
        pipeline = VoiceAssistantPipeline(WakeSettings("莉莉絲", microphone_id=microphone_id), wake_provider=provider)

        pipeline.start()

        self.assertEqual(provider.microphone_id, microphone_id)
        self.assertEqual(sounddevice_index_from_identifier(microphone_id), 7)

    def test_wake_pipeline_starts_when_voice_enabled_and_components_ready(self) -> None:
        provider = PrefixWakeWordProvider(lambda: "")
        pipeline = VoiceAssistantPipeline(WakeSettings("莉莉絲"), wake_provider=provider)

        pipeline.start()

        self.assertTrue(pipeline.active)
        self.assertEqual(pipeline.runtime_state.state, "standby")

    def test_fixed_keyword_backend_falls_back_for_arbitrary_chinese_name(self) -> None:
        primary = FixedKeywordWakeWordProvider(("hey assistant",))
        fallback = PrefixWakeWordProvider(lambda: "莉莉絲")
        provider = FallbackWakeWordProvider(primary, fallback)
        pipeline = VoiceAssistantPipeline(WakeSettings("莉莉絲"), wake_provider=provider)

        pipeline.start()

        self.assertIs(provider.active_provider, fallback)
        self.assertTrue(pipeline.idle_tick())

    def test_fuzzy_chinese_variants_for_lilith_match(self) -> None:
        settings = WakeSettings("莉莉絲")
        aliases = wake_aliases_for_settings(settings)

        for variant in ("莉莉絲", "莉莉斯", "麗麗絲", "莉莉思"):
            with self.subTest(variant=variant):
                self.assertTrue(wake_phrase_matches(f"{variant} 幫我開 Steam", aliases).matched)

    def test_unrelated_speech_does_not_match_lilith(self) -> None:
        aliases = wake_aliases_for_settings(WakeSettings("莉莉絲"))

        for phrase in ("今天幫我開 Steam", "嘿助理", "米米幫我記一下"):
            with self.subTest(phrase=phrase):
                self.assertFalse(wake_phrase_matches(phrase, aliases).matched)

    def test_no_audio_input_reports_mic_no_signal_instead_of_fake_ready(self) -> None:
        class NoAudioProvider(PrefixWakeWordProvider):
            def start(self):
                self.started = False
                from personal_ai_assistant.voice_command import WakeRuntimeState

                return WakeRuntimeState("mic-no-signal", "麥克風無訊號")

        pipeline = VoiceAssistantPipeline(WakeSettings("莉莉絲"), wake_provider=NoAudioProvider())

        pipeline.start()

        self.assertFalse(pipeline.active)
        self.assertEqual(pipeline.runtime_state.state, "mic-no-signal")

    def test_wake_event_launches_listening_stt_exactly_once(self) -> None:
        calls = {"stt": 0, "intent": 0}

        class OneShotWake(PrefixWakeWordProvider):
            def __init__(self) -> None:
                super().__init__(lambda: "莉莉絲")
                self.fired = False

            def poll(self) -> bool:
                if self.fired:
                    return False
                self.fired = True
                return True

        class FakeSTT:
            def configure(self, microphone_id="") -> None:
                self.microphone_id = microphone_id

            def listen_once(self, timeout_seconds=8):
                calls["stt"] += 1
                return "莉莉絲 幫我開 Steam"

        pipeline = VoiceAssistantPipeline(
            WakeSettings("莉莉絲"),
            wake_provider=OneShotWake(),
            stt_provider=FakeSTT(),
            intent_runner=lambda _command: calls.__setitem__("intent", calls["intent"] + 1) or True,
        )
        pipeline.start()

        self.assertTrue(pipeline.idle_tick())
        self.assertTrue(pipeline.handle_wake())
        self.assertFalse(pipeline.idle_tick())
        self.assertEqual(calls["stt"], 1)
        self.assertEqual(calls["intent"], 1)

    def test_wake_phrase_variants_removed_before_intent_router(self) -> None:
        command = strip_wake_phrase("莉莉斯，幫我開 Steam", wake_aliases_for_settings(WakeSettings("莉莉絲")))

        self.assertEqual(command, "幫我開 Steam")
        self.assertEqual(route_assistant_command(command).intent, "activate_or_launch_app")

    def test_mic_and_wake_tests_expose_stage_without_persisting_audio(self) -> None:
        class TestableProvider(PrefixWakeWordProvider):
            def test_microphone(self, duration_seconds=2.0):
                from personal_ai_assistant.voice_command import MicrophoneTestResult

                return MicrophoneTestResult("voice", "有收到聲音，音量 42%，偵測到人聲", True, True, 0.42, 0.2)

            def test_wake_word(self, duration_seconds=8.0):
                return WakeTestResult(
                    "matched",
                    "有聽到聲音 → 辨識為「莉莉斯」→ 喚醒匹配成功",
                    True,
                    True,
                    "莉莉斯",
                    True,
                    "莉莉絲",
                )

        pipeline = VoiceAssistantPipeline(WakeSettings("莉莉絲"), wake_provider=TestableProvider(lambda: ""))

        pipeline.start()
        mic = pipeline.test_microphone()
        wake = pipeline.test_wake_word()

        self.assertEqual(mic.stage, "voice")
        self.assertTrue(wake.matched)
        self.assertEqual(wake.transcript, "莉莉斯")

    def test_enrollment_collects_valid_samples_and_stores_only_features(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            matcher = BaselineAcousticKeywordMatcher(WakeTemplateStore(Path(temp_dir)))
            features = [acoustic_features_from_pcm(self._wake_phrase_pcm(speed=speed)) for speed in (1.0, 0.96, 1.04, 1.02, 0.98)]

            result = matcher.enroll_features("賈維斯", features, acoustic_features_from_pcm(self._wake_phrase_pcm(speed=1.01)))
            template = matcher.store.load(result.template_id)

            self.assertTrue(result.success)
            self.assertEqual(result.sample_count, 5)
            self.assertEqual(template["assistant_name"], "賈維斯")
            self.assertTrue(template["self_validation_passed"])
            self.assertIn("baseline_threshold", template)
            self.assertIn("samples", template)
            self.assertNotIn("pcm", json.dumps(template).lower())
            self.assertNotIn("wav", json.dumps(template).lower())

    def test_raw_temp_audio_deleted_after_enrollment(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            matcher = BaselineAcousticKeywordMatcher(WakeTemplateStore(Path(temp_dir)))
            features = [acoustic_features_from_pcm(self._wake_phrase_pcm(speed=speed)) for speed in (1.0, 0.98, 1.02)]
            result = matcher.enroll_features("賈維斯", features, acoustic_features_from_pcm(self._wake_phrase_pcm(speed=1.01)))

            files = [path.name for path in Path(temp_dir).rglob("*") if path.is_file()]

            self.assertTrue(result.success)
            self.assertEqual(files, [f"{result.template_id}.json"])

    def test_assistant_name_change_invalidates_old_template(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            matcher = BaselineAcousticKeywordMatcher(WakeTemplateStore(Path(temp_dir)))
            features = [acoustic_features_from_pcm(self._wake_phrase_pcm(speed=speed)) for speed in (1.0, 0.98, 1.02)]
            result = matcher.enroll_features("賈維斯", features, acoustic_features_from_pcm(self._wake_phrase_pcm(speed=1.01)))

            status = matcher.template_status(WakeSettings("莉莉絲", template_id=result.template_id))

            self.assertEqual(status.state, "needs-training")

    def test_acoustic_similarity_matches_when_stt_text_differs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            matcher = BaselineAcousticKeywordMatcher(WakeTemplateStore(Path(temp_dir)))
            samples = [acoustic_features_from_pcm(self._wake_phrase_pcm(speed=speed)) for speed in (1.0, 0.98, 1.02)]
            enrollment = matcher.enroll_features("賈維斯", samples, acoustic_features_from_pcm(self._wake_phrase_pcm(speed=1.01)))

            result = matcher.match(self._wake_phrase_pcm(speed=0.94), WakeSettings("賈維斯", sensitivity="standard", template_id=enrollment.template_id))

            self.assertTrue(result.matched)
            self.assertGreaterEqual(result.score, result.threshold)
            self.assertGreater(result.best_template_index, 0)
            self.assertTrue(wake_phrase_matches("配灰威士 幫我開 Steam", wake_aliases_for_settings(WakeSettings("賈維斯"))).matched is False)

    def test_unrelated_speech_remains_below_threshold(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            matcher = BaselineAcousticKeywordMatcher(WakeTemplateStore(Path(temp_dir)))
            samples = [acoustic_features_from_pcm(self._wake_phrase_pcm(speed=speed)) for speed in (1.0, 0.98, 1.02)]
            enrollment = matcher.enroll_features("賈維斯", samples, acoustic_features_from_pcm(self._wake_phrase_pcm(speed=1.01)))

            result = matcher.match(self._wake_phrase_pcm(frequencies=(900, 760, 1100)), WakeSettings("賈維斯", sensitivity="standard", template_id=enrollment.template_id))

            self.assertFalse(result.matched)

    def test_cooldown_prevents_duplicate_wake(self) -> None:
        class ReadyProvider(PrefixWakeWordProvider):
            def __init__(self, matcher):
                super().__init__(lambda: "")
                self.matcher = matcher
                self.settings = WakeSettings("賈維斯")
                self._wake_event = __import__("threading").Event()
                self.runtime_state = None
                self.last_wake_at = 0.0
                self.sample_rate = 16000

        with tempfile.TemporaryDirectory() as temp_dir:
            matcher = BaselineAcousticKeywordMatcher(WakeTemplateStore(Path(temp_dir)))
            samples = [acoustic_features_from_pcm(self._wake_phrase_pcm(speed=speed)) for speed in (1.0, 0.98, 1.02)]
            enrollment = matcher.enroll_features("賈維斯", samples, acoustic_features_from_pcm(self._wake_phrase_pcm(speed=1.01)))
            provider = ReadyProvider(matcher)
            provider.settings = WakeSettings("賈維斯", template_id=enrollment.template_id)
            from personal_ai_assistant.voice_command import SoundDevicePrefixWakeWordProvider

            SoundDevicePrefixWakeWordProvider._check_prefix(provider, self._wake_phrase_pcm())
            first = provider._wake_event.is_set()
            provider._wake_event.clear()
            SoundDevicePrefixWakeWordProvider._check_prefix(provider, self._wake_phrase_pcm())

            self.assertTrue(first)
            self.assertFalse(provider._wake_event.is_set())

    def test_sensitivity_levels_map_to_thresholds(self) -> None:
        self.assertGreater(sensitivity_threshold("low"), sensitivity_threshold("standard"))
        self.assertGreater(sensitivity_threshold("standard"), sensitivity_threshold("high"))

    def test_wake_test_reports_acoustic_similarity_and_debug_only_stt(self) -> None:
        wake = WakeTestResult(
            "matched",
            "有聽到聲音\n喚醒詞相似度：高\n結果：匹配「賈維斯」\n匹配強度：91/100\nbest template：3/5\nSTT 辨識：配灰威士（僅供參考）",
            True,
            True,
            "配灰威士",
            True,
            "賈維斯",
            "高",
            0.9,
            True,
        )

        self.assertIn("喚醒詞相似度：高", wake.message)
        self.assertIn("僅供參考", wake.message)
        self.assertTrue(wake.debug_only_stt)

    def test_no_template_status_requires_training(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            matcher = BaselineAcousticKeywordMatcher(WakeTemplateStore(Path(temp_dir)))

            status = matcher.template_status(WakeSettings("賈維斯"))

            self.assertEqual(status.state, "needs-training")
            self.assertFalse(status.available)

    def test_privacy_clear_removes_templates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = WakeTemplateStore(Path(temp_dir))
            matcher = BaselineAcousticKeywordMatcher(store)
            samples = [acoustic_features_from_pcm(self._wake_phrase_pcm(speed=speed)) for speed in (1.0, 0.98, 1.02)]
            result = matcher.enroll_features("賈維斯", samples, acoustic_features_from_pcm(self._wake_phrase_pcm(speed=1.01)))

            store.clear(result.template_id)

            self.assertFalse(store.template_path(result.template_id).exists())

    def test_text_command_bar_works_even_if_wake_matcher_unavailable(self) -> None:
        status = BaselineAcousticKeywordMatcher(WakeTemplateStore(Path(tempfile.mkdtemp()))).template_status(WakeSettings("賈維斯"))
        routed = route_assistant_command("幫我問 GPT 這個錯誤是什麼")

        self.assertEqual(status.state, "needs-training")
        self.assertEqual(routed.intent, "ask_ai")

    def test_enrollment_rejects_low_volume_and_long_samples(self) -> None:
        quiet = enrollment_sample_from_pcm(self._tone_pcm(330, amplitude=100))
        long_sample = enrollment_sample_from_pcm(self._tone_pcm(330, seconds=2.5))

        self.assertFalse(quiet.accepted)
        self.assertFalse(long_sample.accepted)

    def test_enrollment_and_runtime_use_identical_preprocessing(self) -> None:
        raw = self._silence_pcm(0.18) + self._wake_phrase_pcm() + self._silence_pcm(0.22)

        enrolled = enrollment_sample_from_pcm(raw)
        runtime = acoustic_features_from_pcm(first_voice_prefix(raw))

        self.assertTrue(enrolled.accepted)
        self.assertEqual(enrolled.features, runtime)

    def test_vad_keeps_padding_for_short_chinese_phrase(self) -> None:
        raw = self._silence_pcm(0.24) + self._wake_phrase_pcm() + self._silence_pcm(0.24)

        segment = first_voice_prefix(raw)

        self.assertGreater(len(segment), len(self._wake_phrase_pcm()))

    def test_speed_variation_still_matches_with_dtw(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            matcher = BaselineAcousticKeywordMatcher(WakeTemplateStore(Path(temp_dir)))
            samples = [acoustic_features_from_pcm(self._wake_phrase_pcm(speed=speed)) for speed in (1.0, 0.96, 1.04, 1.02, 0.98)]
            enrollment = matcher.enroll_features("賈維斯", samples, acoustic_features_from_pcm(self._wake_phrase_pcm(speed=1.01)))

            result = matcher.match(self._wake_phrase_pcm(speed=0.82), WakeSettings("賈維斯", template_id=enrollment.template_id))

            self.assertTrue(result.matched)

    def test_gain_changes_still_match(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            matcher = BaselineAcousticKeywordMatcher(WakeTemplateStore(Path(temp_dir)))
            samples = [acoustic_features_from_pcm(self._wake_phrase_pcm(amplitude=amp)) for amp in (7600, 8800, 9200, 8400, 9000)]
            enrollment = matcher.enroll_features("賈維斯", samples, acoustic_features_from_pcm(self._wake_phrase_pcm(amplitude=8600)))

            result = matcher.match(self._wake_phrase_pcm(amplitude=4200), WakeSettings("賈維斯", template_id=enrollment.template_id))

            self.assertTrue(result.matched)

    def test_outlier_enrollment_sample_is_detected(self) -> None:
        samples = [acoustic_features_from_pcm(self._wake_phrase_pcm(speed=speed)) for speed in (1.0, 0.98, 1.02, 1.01)]
        samples.append(acoustic_features_from_pcm(self._wake_phrase_pcm(frequencies=(900, 760, 1100))))

        quality = enrollment_quality(samples)

        self.assertFalse(quality["ready"])

    def test_low_consistency_enrollment_does_not_become_ready(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            matcher = BaselineAcousticKeywordMatcher(WakeTemplateStore(Path(temp_dir)))
            samples = [
                acoustic_features_from_pcm(self._wake_phrase_pcm(frequencies=freqs))
                for freqs in ((320, 520, 410), (700, 440, 900), (250, 880, 360))
            ]

            result = matcher.enroll_features("賈維斯", samples, acoustic_features_from_pcm(self._wake_phrase_pcm()))

            self.assertFalse(result.success)

    def test_post_enrollment_self_validation_must_pass_before_ready(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            matcher = BaselineAcousticKeywordMatcher(WakeTemplateStore(Path(temp_dir)))
            samples = [acoustic_features_from_pcm(self._wake_phrase_pcm(speed=speed)) for speed in (1.0, 0.98, 1.02)]

            result = matcher.enroll_features("賈維斯", samples)

            self.assertFalse(result.success)
            self.assertIn("自我驗證", result.message)

    def test_multi_template_topk_beats_naive_prototype_case(self) -> None:
        templates = [
            acoustic_features_from_pcm(self._wake_phrase_pcm(frequencies=freqs))
            for freqs in ((320, 520, 410), (335, 540, 420), (300, 500, 390))
        ]
        candidate = acoustic_features_from_pcm(self._wake_phrase_pcm(frequencies=(335, 540, 420)))
        prototype = tuple(
            tuple(sum(frame[index] for frame in aligned) / len(aligned) for index in range(len(aligned[0])))
            for aligned in zip(*templates)
        )

        aggregate_score, best_index, _scores = multi_template_similarity(candidate, templates)
        prototype_score = sequence_similarity(candidate, prototype)

        self.assertGreater(aggregate_score, prototype_score)
        self.assertEqual(best_index, 2)

    def test_sensitivity_adjusts_around_calibrated_baseline(self) -> None:
        samples = [acoustic_features_from_pcm(self._wake_phrase_pcm(speed=speed)) for speed in (1.0, 0.98, 1.02)]
        baseline = calibrated_baseline_threshold(samples)
        template = {"baseline_threshold": baseline}

        self.assertGreater(sensitivity_threshold("low", template), sensitivity_threshold("standard", template))
        self.assertGreater(sensitivity_threshold("standard", template), sensitivity_threshold("high", template))

    @staticmethod
    def _tone_pcm(frequency: float, seconds: float = 0.72, amplitude: int = 9000, sample_rate: int = 16000) -> bytes:
        values = []
        for index in range(int(sample_rate * seconds)):
            envelope = min(1.0, index / 800, (sample_rate * seconds - index) / 800)
            value = int(amplitude * envelope * math.sin(2 * math.pi * frequency * index / sample_rate))
            values.append(value.to_bytes(2, "little", signed=True))
        return b"".join(values)

    @staticmethod
    def _silence_pcm(seconds: float, sample_rate: int = 16000) -> bytes:
        return b"\x00\x00" * int(sample_rate * seconds)

    @staticmethod
    def _wake_phrase_pcm(
        speed: float = 1.0,
        amplitude: int = 9000,
        sample_rate: int = 16000,
        frequencies: tuple[int, int, int] = (320, 520, 410),
    ) -> bytes:
        values: list[bytes] = []
        for frequency in frequencies:
            sample_count = int(sample_rate * 0.20 * speed)
            for index in range(sample_count):
                envelope = max(0.0, min(1.0, index / 320, (sample_count - index) / 320))
                value = int(amplitude * envelope * math.sin(2 * math.pi * frequency * index / sample_rate))
                values.append(value.to_bytes(2, "little", signed=True))
            values.extend((0).to_bytes(2, "little", signed=True) for _ in range(int(sample_rate * 0.04 * speed)))
        return b"".join(values)

    def test_voice_dependency_install_success_failure_and_retry(self) -> None:
        calls: list[list[str]] = []

        def ok_runner(command, **_kwargs):
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, "", "")

        def fail_runner(command, **_kwargs):
            calls.append(command)
            return subprocess.CompletedProcess(command, 1, "", "network down")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self.assertEqual(install_voice_dependencies(root, runner=fail_runner).state, "error_dependency")
            retried = install_voice_dependencies(root, runner=ok_runner)

        self.assertEqual(retried.state, "verified")
        self.assertTrue(any("faster-whisper" in command for command in calls))

    def test_audio_device_enumeration_filters_output_loopback_devices(self) -> None:
        class FakeSoundDevice:
            @staticmethod
            def query_hostapis():
                return [{"name": "Windows WASAPI"}]

            @staticmethod
            def query_devices():
                return [
                    {"name": "USB Microphone", "hostapi": 0, "max_input_channels": 1},
                    {"name": "Speakers (Loopback)", "hostapi": 0, "max_input_channels": 2},
                    {"name": "Headphones Output", "hostapi": 0, "max_input_channels": 2},
                    {"name": "HDMI Output", "hostapi": 0, "max_input_channels": 0},
                ]

        with patch("personal_ai_assistant.audio_devices.importlib.import_module", return_value=FakeSoundDevice):
            devices = SoundDeviceAudioInputProvider().list_input_devices()

        self.assertEqual([device.display_name for device in devices], ["USB Microphone"])
        self.assertTrue(devices[0].identifier.startswith("sounddevice:"))

    def test_microphone_combo_populates_and_default_entry_first(self) -> None:
        from PySide6.QtWidgets import QApplication
        from personal_ai_assistant.main_window import MainWindow

        app = QApplication.instance() or QApplication([])
        window = MainWindow.__new__(MainWindow)
        window.settings = type("Settings", (), {"voice_microphone_id": "", "voice_command_enabled": False})()
        window.settings_service = type("Service", (), {"update": lambda _self, **changes: type("Settings", (), {**window.settings.__dict__, **changes})()})()
        window.voice_device_provider = type(
            "Provider",
            (),
            {"list_input_devices": lambda _self: (AudioInputDevice("mic-a", "USB Microphone"),)},
        )()
        window.voice_microphone_combo = __import__("PySide6.QtWidgets", fromlist=["QComboBox"]).QComboBox()
        window.voice_pipeline = type("Pipeline", (), {"runtime_state": type("State", (), {"state": "disabled"})(), "reload": lambda *_args: None, "start": lambda *_args: None, "stop": lambda *_args: None})()
        window.store = type("Store", (), {"privacy_mode_enabled": lambda _self: False})()
        window.voice_wake_timer = type("Timer", (), {"start": lambda *_args: None, "stop": lambda *_args: None})()
        window._update_voice_status_label = lambda: None
        window._command_notice = lambda _text: None

        window.refresh_voice_microphones()

        self.assertEqual(window.voice_microphone_combo.itemText(0), "系統預設麥克風")
        self.assertEqual(window.voice_microphone_combo.itemData(0), SYSTEM_DEFAULT_MICROPHONE_ID)
        self.assertEqual(window.voice_microphone_combo.itemText(1), "USB Microphone")
        window.voice_microphone_combo.deleteLater()
        app.processEvents()

    def test_selected_device_persistence_uses_identifier_not_display_text(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = SettingsService(AssistantStore(Path(temp_dir) / "assistant.sqlite3"))
            settings = service.update(voice_microphone_id="sounddevice:abc123:4")
            self.assertEqual(settings.voice_microphone_id, "sounddevice:abc123:4")

    def test_refresh_preserves_existing_microphone_selection(self) -> None:
        devices = (
            AudioInputDevice("mic-a", "USB Microphone"),
            AudioInputDevice("mic-b", "Webcam Microphone"),
        )
        self.assertEqual(recoverable_sounddevice_identifier("mic-b", devices), "mic-b")

    def test_selected_microphone_missing_falls_back_to_system_default(self) -> None:
        devices = (AudioInputDevice("mic-a", "USB Microphone"),)
        self.assertEqual(recoverable_sounddevice_identifier("missing", devices), SYSTEM_DEFAULT_MICROPHONE_ID)

    def test_no_microphone_disables_voice_gracefully_text_command_unaffected(self) -> None:
        from PySide6.QtWidgets import QApplication
        from personal_ai_assistant.main_window import MainWindow

        app = QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as temp_dir:
            window = MainWindow(Path(temp_dir) / "assistant.sqlite3")
            window.voice_device_provider = type("Provider", (), {"list_input_devices": lambda _self: ()})()
            window.settings = window.settings_service.update(voice_command_enabled=True)
            window.refresh_voice_microphones()

            self.assertFalse(window.settings.voice_command_enabled)
            self.assertFalse(window.voice_pipeline.active)
            self.assertEqual(window.voice_microphone_combo.itemText(0), "未偵測到可用麥克風")
            self.assertEqual(route_assistant_command("幫我問 GPT 這個錯誤是什麼").intent, "ask_ai")
            for timer_name in ("monitor_timer", "reminder_timer", "status_bar_timer", "voice_wake_timer"):
                getattr(window, timer_name).stop()
            window.exiting = True
            window.close()
            app.processEvents()

    def test_changing_microphone_hot_restarts_voice_input(self) -> None:
        provider = PrefixWakeWordProvider(lambda: "")
        pipeline = VoiceAssistantPipeline(WakeSettings("米米", microphone_id="mic-a"), wake_provider=provider)
        pipeline.start()

        pipeline.reload(WakeSettings("米米", microphone_id="mic-b"))

        self.assertTrue(pipeline.active)
        self.assertEqual(pipeline.hot_restart_count, 1)
        self.assertEqual(provider.microphone_id, "mic-b")

    def test_sounddevice_identifier_can_recover_when_index_changes(self) -> None:
        old_id = build_sounddevice_identifier(4, "USB Microphone", "Windows WASAPI")
        new_id = build_sounddevice_identifier(9, "USB Microphone", "Windows WASAPI")
        recovered = recoverable_sounddevice_identifier(old_id, (AudioInputDevice(new_id, "USB Microphone"),))
        self.assertEqual(recovered, new_id)

    def test_activate_existing_window_before_launch_and_restore_minimized(self) -> None:
        class FakeWindows:
            def verify(self, window):
                return True

            def __init__(self) -> None:
                self.activated: list[AppWindow] = []

            def list_windows(self):
                return (AppWindow(10, "Steam.exe", "Steam", minimized=True),)

            def activate(self, window):
                self.activated.append(window)
                return True

        launches: list[ResolveCandidate] = []
        service = ActivateOrLaunchService(AppResolver(), FakeWindows(), launches.append)
        result = service.activate_or_launch("Steam")

        self.assertTrue(result.ok)
        self.assertEqual(result.action, "activated")
        self.assertTrue(service.window_controller.activated[0].minimized)
        self.assertEqual(launches, [])

    def test_no_duplicate_launch_when_existing_window_found(self) -> None:
        class FakeWindows:
            def verify(self, window):
                return True

            def list_windows(self):
                return (AppWindow(11, "Steam.exe", "Library"),)

            def activate(self, window):
                return True

        launched: list[str] = []
        service = ActivateOrLaunchService(AppResolver(), FakeWindows(), lambda candidate: launched.append(candidate.path) or True)

        self.assertTrue(service.activate_or_launch("Steam").ok)
        self.assertEqual(launched, [])

    def test_multiple_window_ranking_uses_title_keyword(self) -> None:
        windows = (
            AppWindow(1, "chrome.exe", "Docs", False, 0),
            AppWindow(2, "chrome.exe", "YouTube - music", False, 1),
        )

        self.assertEqual(rank_matching_window(windows, "Chrome", "YouTube").hwnd, 2)

    def test_unknown_app_resolver_alias_cache_and_multiple_candidate_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            exe = Path(temp_dir) / "Tool.exe"
            exe.write_text("", encoding="utf-8")
            cached = AppResolver({"my tool": str(exe)}).resolve_app("my tool")
            self.assertEqual(cached.best.path, str(exe))

        resolver = AppResolver()
        resolver._from_app_paths = lambda _tokens: [
            ResolveCandidate("Tool A", r"C:\A\Tool.exe", "app_paths"),
            ResolveCandidate("Tool B", r"C:\B\Tool.exe", "app_paths"),
        ]
        resolver._from_start_menu = lambda _tokens: []
        resolver._from_uninstall_metadata = lambda _tokens: []
        resolver._from_common_paths = lambda _tokens: []
        self.assertTrue(resolver.resolve_app("tool").needs_confirmation)

    def test_privacy_mode_stops_microphone_pipeline(self) -> None:
        privacy = {"enabled": False}
        pipeline = VoiceAssistantPipeline(
            WakeSettings("米米"),
            wake_provider=PrefixWakeWordProvider(lambda: ""),
            privacy_enabled=lambda: privacy["enabled"],
        )
        pipeline.start()
        self.assertTrue(pipeline.active)

        privacy["enabled"] = True
        self.assertFalse(pipeline.idle_tick())
        self.assertFalse(pipeline.active)

    def test_text_command_bar_still_routes_without_voice_deps(self) -> None:
        status = validate_assistant_name("AI")
        self.assertTrue(status.risky)
        routed = route_assistant_command("幫我問 GPT 這個錯誤是什麼")
        self.assertEqual(routed.intent, "ask_ai")

    def test_advisor_prompt_requires_traditional_chinese_keys(self) -> None:
        import inspect

        source = inspect.getsource(OpenAICompatibleAdvisorProvider.advise)
        self.assertIn("understanding, next_action, reason", source)
        self.assertIn("Use Traditional Chinese", source)
        self.assertIn("questions", source)

    def test_osd_compacts_last_segment_and_app_name(self) -> None:
        from personal_ai_assistant.main_window import MainWindow

        window = MainWindow.__new__(MainWindow)
        text = MainWindow._compact_status_bar_text(
            window,
            ["一般｜ChatGPT｜12:42", "AI：專注中", "25分鐘後：這是一個非常非常長的提醒內容"],
            max_chars=38,
        )
        self.assertLessEqual(len(text), 38)
        self.assertIn("一般｜ChatGPT｜12:42", text)
        self.assertEqual(MainWindow._display_app_name(window, "ChatGPT.exe", "ChatGPT"), "ChatGPT")
        self.assertEqual(MainWindow._display_app_name(window, "Code.exe", "main.py"), "Code")

    def test_assistant_command_dialog_escape_rejects(self) -> None:
        from PySide6.QtCore import QEvent, Qt
        from PySide6.QtGui import QKeyEvent
        from PySide6.QtWidgets import QApplication, QDialog
        from personal_ai_assistant.main_window import AssistantCommandDialog

        app = QApplication.instance() or QApplication([])
        dialog = AssistantCommandDialog()
        event = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier)
        dialog.keyPressEvent(event)
        self.assertEqual(dialog.result(), QDialog.DialogCode.Rejected)
        dialog.deleteLater()


if __name__ == "__main__":
    unittest.main()

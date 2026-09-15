from __future__ import annotations

from .reminder_trace import trace_reminder

import json
import math
import os
import sqlite3
import subprocess
import threading
import webbrowser
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, QSize, QThread, QTimer, Qt, QUrl, Signal
from PySide6.QtGui import QAction, QCloseEvent, QDesktopServices, QKeyEvent
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QStyle,
    QSystemTrayIcon,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .display import POSITION_TOP_LEFT, POSITION_TOP_RIGHT, choose_popup_screen, choose_status_screen
from .hotkeys import GlobalHotkey, WM_HOTKEY
from .app_resolver import AppResolver, ResolveCandidate
from .command_router import AssistantIntent, route_assistant_command
from .adaptive import (
    PRESENTATION_DEFER_ACTIVE,
    PRESENTATION_DEFER_GAME_END,
    PRESENTATION_IMMEDIATE_POPUP,
    PRESENTATION_REPEAT,
    PRESENTATION_STATUS_ONLY,
    PRESENTATION_WINDOWS_ONLY,
    AdaptivePolicy,
    AdaptiveReminderStrategy,
)
from .activity_sessions import ActivitySessionManager, session_identity
from .advisor_core import (
    ADVISOR_CUSTOM_MODEL,
    ADVISOR_DISABLED_SELECTION,
    ADVISOR_PROVIDER_PRESETS,
    AdvisorService,
    ContextBuilder,
    CurrentStateBuilder,
    GoalEngine,
    NextActionEngine,
    OpenAICompatibleAdvisorProvider,
    advisor_health_label,
    advisor_http_error_message,
    advisor_key_url,
    advisor_load_health,
    advisor_preset_endpoint,
    advisor_provider_label,
    advisor_record_failure,
    advisor_record_success,
    advisor_reset_health,
    advisor_recommended_model_labels,
    advisor_recommended_models,
)
from .ai import AssistantAnalyzer
from .audio_devices import (
    AudioInputDeviceProvider,
    NO_MICROPHONE_LABEL,
    SYSTEM_DEFAULT_MICROPHONE_ID,
    SYSTEM_DEFAULT_MICROPHONE_LABEL,
    SoundDeviceAudioInputProvider,
    recoverable_sounddevice_identifier,
)
from .assistant.dead_loop import DeadLoopDetector
from .db import AssistantStore
from .events import EventBus
from .integrations.ai_handoff import AIHandoffProvider, PROVIDER_URLS
from .learning import LearningEngine, RuleBasedAdvisor
from .models import CurrentState, ForegroundSnapshot, NextAction, Reminder
from .monitor import get_foreground_snapshot, is_windows_locked_or_unavailable
from .rhythm_ui import RhythmDiagnosticWidget
from .inbox import looks_like_today_inbox, should_suggest_today_inbox, strip_inbox_prefix, today_inbox_due_at
from .notifications import NotificationService
from .reminder_popup import AssistantNotificationPrompt, ReminderPopup
from .reminders import (
    cycle_status,
    format_response_seconds,
    format_due_at,
    next_occurrence,
    parse_natural_reminder,
)
from .top_status_bar import TopStatusBar
from .settings import SettingsService
from .startup import is_startup_enabled, set_startup_enabled
from .summary import build_daily_summary, build_daily_summary_from_sessions, format_summary_bucket
from .ui_formatters import format_event_card_text, format_reminder_card_text, status_label
from .updates import CURRENT_VERSION, check_latest_release, should_check_for_updates
from .voice_command import (
    DEFAULT_ASSISTANT_NAME,
    VoiceAssistantPipeline,
    VoiceInstallState,
    install_voice_dependencies,
    WakeRuntimeState,
    settings_from_assistant_settings,
    validate_assistant_name,
    voice_command_status,
    wake_aliases_for_settings,
)
from .window_activation import ActivateOrLaunchService


TOP_STATUS_REMINDER_LOOKAHEAD = timedelta(minutes=30)
IDLE_THRESHOLD_OPTIONS = (5, 10, 15, 30)


class VoiceSignals(QObject):
    notice = Signal(str)
    completed = Signal(str, object, int)


class VoiceInstallWorker(QObject):
    progress = Signal(object)
    finished = Signal(object)

    def __init__(self, project_root: Path) -> None:
        super().__init__()
        self.project_root = project_root

    def run(self) -> None:
        self.finished.emit(install_voice_dependencies(self.project_root, progress=self.progress.emit))


def provider_label(provider: str) -> str:
    return {
        "chatgpt": "ChatGPT",
        "grok": "Grok",
        "claude": "Claude",
        "gemini": "Gemini",
        "custom": "AI",
    }.get(provider, provider or "AI")


class AssistantCommandDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("助理指令")
        self.setWindowFlags(self.windowFlags() | Qt.WindowStaysOnTopHint)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)

        self.input_box = QLineEdit()
        self.input_box.setPlaceholderText("例如：10分鐘後提醒我吃飯、開 Steam、問 ChatGPT 錯誤、我休息20分鐘")
        self.input_box.installEventFilter(self)
        input_row = QHBoxLayout()
        input_row.addWidget(self.input_box, stretch=1)
        self.voice_button = QPushButton("語音")
        self.voice_button.clicked.connect(self.show_voice_status)
        input_row.addWidget(self.voice_button)
        layout.addLayout(input_row)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("執行")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.input_box.returnPressed.connect(self.accept)
        self.input_box.setFocus()

    def text(self) -> str:
        return self.input_box.text().strip()

    def show_voice_status(self) -> None:
        parent = self.parent()
        pipeline = getattr(parent, "voice_pipeline", None)
        message = pipeline.readiness().message if pipeline else "語音助理未啟用"
        QMessageBox.information(self, "語音指令", message)

    def eventFilter(self, watched: object, event: QEvent) -> bool:
        if watched is self.input_box and event.type() == QEvent.Type.KeyPress:
            key_event = event
            if isinstance(key_event, QKeyEvent) and key_event.key() == Qt.Key.Key_Escape:
                self.reject()
                return True
        return super().eventFilter(watched, event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.reject()
            event.accept()
            return
        super().keyPressEvent(event)


class MainWindow(QMainWindow):
    def __init__(self, db_path: Path) -> None:
        super().__init__()
        self.setWindowTitle("Private Assistant AI Alpha")
        self.resize(860, 560)
        self.setMinimumSize(720, 520)

        self.store = AssistantStore(db_path)
        self.rhythm_storage = Path(db_path).parent / "rhythm_diagnostics"
        self.settings_service = SettingsService(self.store)
        self.settings = self.settings_service.load()
        self.analyzer = AssistantAnalyzer()
        self.policy = AdaptivePolicy()
        self.strategy = AdaptiveReminderStrategy(self.store, self.policy)
        self.learning = LearningEngine(self.store)
        self.advisor = RuleBasedAdvisor(self.store)
        self.dead_loop_detector = DeadLoopDetector()
        self.current_state_builder = CurrentStateBuilder(self.store)
        self.goal_engine = GoalEngine(self.store)
        self.context_builder = ContextBuilder(self.store)
        self.next_action_engine = NextActionEngine(self.store, self.dead_loop_detector)
        self.advisor_service = AdvisorService(self.store, self.settings_service, self.next_action_engine, self.context_builder)
        self.session_manager = ActivitySessionManager(self.store, self.settings.activity_session_grace_seconds)
        self.event_bus = EventBus(self.store)
        self.notifications = NotificationService()
        self.current_snapshot: ForegroundSnapshot | None = None
        self.previous_snapshot: ForegroundSnapshot | None = None
        self.activity_started_at = datetime.now()
        self.activity_key: tuple[str, str, str] | None = None
        self.current_activity_session = None
        self.monitoring = False
        self.manually_stopped_monitoring = False
        self.auto_idle_paused = False
        self.last_pause_reason = ""
        self.exiting = False
        self.active_popups: dict[int, ReminderPopup] = {}
        self.active_notification_prompts: dict[int, AssistantNotificationPrompt] = {}
        self.deferred_reminders: dict[int, tuple[Reminder, object, str, str, ForegroundSnapshot]] = {}
        self.suggestion_ids: list[int] = []
        self.status_bar_notice: tuple[str, datetime] | None = None
        self.top_status_bar = TopStatusBar()
        self.top_status_visible = self.settings.top_status_bar_visible
        self.auto_idle_pause_enabled = self.settings.auto_idle_pause_enabled
        self.idle_threshold_minutes = self.settings.idle_threshold_minutes
        self.last_today_inbox_suggestion_at: datetime | None = None
        self.last_advisor_action: NextAction | None = None
        self.last_advisor_analyzed_at: datetime | None = None
        self.last_status_notice_signature: tuple[str, str] | None = None
        self.last_status_notice_at: datetime | None = None
        self.global_hotkey = GlobalHotkey()
        self.quick_add_hotkey_registered = False
        self.voice_device_provider: AudioInputDeviceProvider = SoundDeviceAudioInputProvider()
        self.voice_microphones_available = False
        self.voice_missing_device_notice_shown = False
        self.voice_install_thread: QThread | None = None
        self.voice_install_worker: VoiceInstallWorker | None = None
        self.voice_signals = VoiceSignals(self)
        self.voice_signals.notice.connect(self._voice_notice)
        self.voice_signals.completed.connect(self._finish_voice_job)
        self._voice_job_busy = False
        self._voice_generation = 0
        self.voice_listening_overlay = QLabel(self, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)
        self.voice_listening_overlay.setStyleSheet("padding: 20px; background: #172435; color: white; font-size: 20px; border-radius: 12px;")
        self.voice_pipeline = VoiceAssistantPipeline(
            settings_from_assistant_settings(self.settings),
            intent_runner=self.execute_voice_command,
            privacy_enabled=lambda: self.settings.privacy_mode_enabled,
            status_callback=self.voice_signals.notice.emit,
        )

        self.voice_state_timer = QTimer(QApplication.instance())
        self.voice_state_timer.setInterval(250)
        self.voice_state_timer.timeout.connect(self._voice_service_tick)
        self.voice_state_timer.start()
        QTimer.singleShot(0, self._ensure_entity_navigator)
        self.monitor_timer = QTimer(self)
        self.monitor_timer.setInterval(5000)
        self.monitor_timer.timeout.connect(self.monitor_tick)

        self.reminder_timer = QTimer(self)
        self.reminder_timer.setInterval(10000)
        self.reminder_timer.timeout.connect(self.reminder_tick)
        self.voice_wake_timer = QTimer(self)
        self.voice_wake_timer.setInterval(100)
        self.voice_wake_timer.timeout.connect(self._voice_wake_tick)
        self._build_ui()
        self._build_tray()
        self.configure_quick_add_hotkey(show_errors=False)
        if self.settings.auto_start_monitoring_enabled:
            self.start_monitoring(manual=False)
        self._refresh_monitor_controls()
        self.status_bar_timer = QTimer(self)
        self.status_bar_timer.setInterval(1500)
        self.status_bar_timer.timeout.connect(self.update_top_status_bar)
        self.status_bar_timer.start()
        if self.top_status_visible:
            self.top_status_bar.show()
        self.reminder_timer.start()
        self._sync_voice_pipeline()
        self._repair_known_ambiguous_time_misparse()
        self.refresh_lists()
        self.reminder_tick()
        self.update_top_status_bar()
        if QApplication.instance() and QApplication.instance().property("interactive_setup"):
            self.maybe_show_first_run_setup()
        self.maybe_check_updates()

    def _build_ui(self) -> None:
        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(12)

        title = QLabel("Private Assistant AI")
        title.setObjectName("Title")
        subtitle = QLabel("本機優先的自然語言提醒、情境監督、透明學習與 AI handoff")
        subtitle.setObjectName("Subtitle")

        add_row = QHBoxLayout()
        self.reminder_input = QLineEdit()
        self.reminder_input.setPlaceholderText("例如：18:30 喝水、每天 21:00 伸展、每週一三五 09:00 檢查待辦")
        self.reminder_input.returnPressed.connect(self.add_reminder)
        self.add_button = QPushButton("新增提醒")
        self.add_button.clicked.connect(self.add_reminder)
        add_row.addWidget(self.reminder_input, stretch=1)
        add_row.addWidget(self.add_button)

        status_row = QHBoxLayout()
        self.toggle_button = QPushButton("開始監督")
        self.toggle_button.clicked.connect(self.toggle_monitoring)
        self.mode_label = QLabel("目前模式：尚未開始")
        self.mode_label.setObjectName("ModeLabel")
        status_row.addWidget(self.toggle_button)
        status_row.addWidget(self.mode_label, stretch=1)

        service_row = QHBoxLayout()
        self.reminder_service_label = QLabel("提醒服務：運作中")
        self.reminder_service_label.setObjectName("ServiceLabel")
        self.monitor_service_label = QLabel("監督：關")
        self.monitor_service_label.setObjectName("ServiceLabel")
        service_row.addWidget(self.reminder_service_label)
        service_row.addWidget(self.monitor_service_label)
        service_row.addStretch(1)

        self.settings_tabs = QTabWidget()
        self.settings_tabs.setObjectName("SettingsTabs")

        ai_settings_page, ai_settings_layout = self._scroll_page("AISettingsScroll")
        ai_group = QGroupBox("AI 整合")
        ai_form = QFormLayout(ai_group)
        self.ai_form = ai_form
        ai_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        ai_form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.ai_handoff_checkbox = QCheckBox("啟用 AI Handoff（開網站用）")
        self.ai_handoff_checkbox.setChecked(self.settings.ai_handoff_enabled)
        self.ai_handoff_checkbox.toggled.connect(self.set_ai_handoff_enabled)
        self.ai_provider_combo = QComboBox()
        for label, provider in (
            ("ChatGPT", "chatgpt"),
            ("Grok", "grok"),
            ("Claude", "claude"),
            ("Gemini", "gemini"),
            ("Custom URL", "custom"),
        ):
            self.ai_provider_combo.addItem(label, provider)
        provider_index = self.ai_provider_combo.findData(self.settings.ai_provider)
        self.ai_provider_combo.setCurrentIndex(max(0, provider_index))
        self.ai_provider_combo.currentIndexChanged.connect(self.set_ai_provider)
        self.ai_custom_url_input = QLineEdit()
        self.ai_custom_url_input.setPlaceholderText("Custom URL")
        self.ai_custom_url_input.setMinimumWidth(260)
        self.ai_custom_url_input.setText(self.settings.ai_handoff_url if self.settings.ai_provider == "custom" else "")
        self.ai_custom_url_input.editingFinished.connect(self.set_ai_custom_url)
        self.ai_auto_send_checkbox = QCheckBox("自動送出 AI prompt（需確認，預設關閉）")
        self.ai_auto_send_checkbox.setChecked(self.settings.ai_handoff_auto_send)
        self.ai_auto_send_checkbox.toggled.connect(self.set_ai_handoff_auto_send)
        self._fit_combo_to_contents(self.ai_provider_combo, 150)
        ai_form.addRow(self.ai_handoff_checkbox)
        handoff_hint = QLabel("這只影響 Handoff 開哪個網站，不會讓助理自動用 GPT 分析。")
        handoff_hint.setWordWrap(True)
        handoff_hint.setObjectName("HintLabel")
        ai_form.addRow(handoff_hint)
        ai_form.addRow("AI Handoff provider", self.ai_provider_combo)
        ai_form.addRow("Custom URL", self.ai_custom_url_input)
        ai_form.addRow(self.ai_auto_send_checkbox)
        self.advisor_api_checkbox = QCheckBox("啟用 AI Advisor（真正用 API 分析）")
        self.advisor_api_checkbox.setChecked(self.settings.advisor_api_enabled)
        self.advisor_api_checkbox.toggled.connect(self.set_advisor_api_enabled)
        self.advisor_preset_combo = QComboBox()
        self.advisor_preset_combo.addItem("未啟用", ADVISOR_DISABLED_SELECTION)
        for key in ("openai", "groq", "openrouter", "custom"):
            self.advisor_preset_combo.addItem(str(ADVISOR_PROVIDER_PRESETS[key]["label"]), key)
        preset_index = self.advisor_preset_combo.findData(self.settings.advisor_provider_preset)
        self.advisor_preset_combo.setCurrentIndex(max(0, preset_index if self.settings.advisor_api_enabled else 0))
        self.advisor_preset_combo.currentIndexChanged.connect(self.set_advisor_provider_preset)
        self.advisor_endpoint_input = QLineEdit()
        self.advisor_endpoint_input.setPlaceholderText("OpenAI-compatible endpoint，例如 http://localhost:1234/v1")
        self.advisor_endpoint_input.setMinimumWidth(320)
        self.advisor_endpoint_input.setText(self.settings.advisor_endpoint_url)
        self.advisor_endpoint_input.editingFinished.connect(self.set_advisor_endpoint_url)
        self.advisor_model_combo = QComboBox()
        self.advisor_model_combo.setMinimumWidth(420)
        self.advisor_model_combo.setMinimumContentsLength(28)
        self.advisor_model_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        self.advisor_model_combo.currentIndexChanged.connect(self.set_advisor_model_from_combo)
        self.advisor_model_input = QLineEdit()
        self.advisor_model_input.setPlaceholderText("自訂 model ID")
        self.advisor_model_input.setMinimumWidth(320)
        self.advisor_model_input.setText(self.settings.advisor_model_name)
        self.advisor_model_input.editingFinished.connect(self.set_custom_advisor_model_name)
        self.advisor_api_key_input = QLineEdit()
        self.advisor_api_key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.advisor_api_key_input.setPlaceholderText("API key（只存在本機，不匯出）")
        self.advisor_api_key_input.setMinimumWidth(220)
        self.advisor_api_key_input.setText(self.store.get_setting("advisor_api_key", "") or "")
        self.advisor_api_key_input.editingFinished.connect(self.set_advisor_api_key)
        self.advisor_api_key_input.addAction(
            self.style().standardIcon(QStyle.StandardPixmap.SP_FileDialogDetailedView),
            QLineEdit.ActionPosition.TrailingPosition,
        ).triggered.connect(self.toggle_advisor_api_key_visible)
        self.advisor_context_combo = QComboBox()
        for minutes in (10, 20, 30):
            self.advisor_context_combo.addItem(f"{minutes} 分鐘", minutes)
        context_index = self.advisor_context_combo.findData(self.settings.advisor_context_minutes)
        self.advisor_context_combo.setCurrentIndex(max(0, context_index))
        self.advisor_context_combo.currentIndexChanged.connect(self.set_advisor_context_minutes)
        self.advisor_event_triggered_checkbox = QCheckBox("事件觸發優先")
        self.advisor_event_triggered_checkbox.setChecked(self.settings.advisor_event_triggered)
        self.advisor_event_triggered_checkbox.toggled.connect(self.set_advisor_event_triggered)
        self.advisor_refresh_models_button = QPushButton("重新整理模型")
        self.advisor_refresh_models_button.clicked.connect(self.refresh_advisor_models)
        self.advisor_test_button = QPushButton("測試連線")
        self.advisor_test_button.clicked.connect(self.test_advisor_connection)
        self.advisor_key_button = QPushButton("取得 API Key")
        self.advisor_key_button.clicked.connect(self.open_advisor_key_url)
        self.advisor_test_label = QLabel("")
        self.advisor_test_label.setObjectName("HintLabel")
        self.advisor_test_label.setWordWrap(True)
        self.advisor_key_url_label = QLabel("")
        self.advisor_key_url_label.setObjectName("HintLabel")
        self.advisor_key_url_label.setWordWrap(True)
        self.advisor_key_url_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        advisor_model_row = QHBoxLayout()
        advisor_model_row.addWidget(self.advisor_model_combo, stretch=1)
        advisor_model_row.addWidget(self.advisor_refresh_models_button)
        advisor_test_row = QHBoxLayout()
        advisor_test_row.addWidget(self.advisor_key_button)
        advisor_test_row.addWidget(self.advisor_test_button)
        advisor_test_row.addWidget(self.advisor_test_label, stretch=1)
        self._fit_combo_to_contents(self.advisor_context_combo, 120)
        advisor_hint = QLabel("AI Advisor 才會呼叫 OpenAI-compatible API 產生理解與下一步建議。ChatGPT Plus 訂閱不等於 API 額度。")
        advisor_hint.setWordWrap(True)
        advisor_hint.setObjectName("HintLabel")
        ai_form.addRow(advisor_hint)
        ai_form.addRow(self.advisor_api_checkbox)
        ai_form.addRow("AI Advisor preset", self.advisor_preset_combo)
        ai_form.addRow("Endpoint URL", self.advisor_endpoint_input)
        ai_form.addRow("模型", advisor_model_row)
        ai_form.addRow("自訂模型", self.advisor_model_input)
        ai_form.addRow("API key", self.advisor_api_key_input)
        ai_form.addRow("分析窗口", self.advisor_context_combo)
        ai_form.addRow(self.advisor_event_triggered_checkbox)
        ai_form.addRow("連線", advisor_test_row)
        self._populate_advisor_model_combo()
        self._sync_advisor_controls_enabled()
        ai_settings_layout.addWidget(ai_group)
        ai_settings_layout.addStretch(1)
        self.settings_tabs.addTab(ai_settings_page, "AI 整合")

        monitor_settings_page, monitor_settings_layout = self._scroll_page("MonitorSettingsScroll")
        monitor_group = QGroupBox("監督")
        monitor_form = QFormLayout(monitor_group)
        self.auto_start_monitoring_checkbox = QCheckBox("啟動時自動開始監督")
        self.auto_start_monitoring_checkbox.setChecked(self.settings.auto_start_monitoring_enabled)
        self.auto_start_monitoring_checkbox.toggled.connect(self.set_auto_start_monitoring_enabled)
        self.auto_idle_pause_checkbox = QCheckBox("閒置時自動暫停監督")
        self.auto_idle_pause_checkbox.setChecked(self.auto_idle_pause_enabled)
        self.auto_idle_pause_checkbox.toggled.connect(self.set_auto_idle_pause_enabled)
        self.privacy_mode_checkbox = QCheckBox("隱私模式")
        self.privacy_mode_checkbox.setChecked(self.settings.privacy_mode_enabled)
        self.privacy_mode_checkbox.toggled.connect(self.set_privacy_mode_enabled)
        self.learning_checkbox = QCheckBox("啟用學習")
        self.learning_checkbox.setChecked(self.settings.learning_enabled)
        self.learning_checkbox.toggled.connect(self.set_learning_enabled)
        self.notification_learning_checkbox = QCheckBox("外部通知反應學習 Beta")
        self.notification_learning_checkbox.setChecked(self.settings.notification_listener_beta_enabled)
        self.notification_learning_checkbox.toggled.connect(self.set_notification_learning_enabled)
        self.dead_loop_checkbox = QCheckBox("死循環提醒")
        self.dead_loop_checkbox.setChecked(self.settings.dead_loop_detection_enabled)
        self.dead_loop_checkbox.toggled.connect(self.set_dead_loop_detection_enabled)
        self.idle_threshold_combo = QComboBox()
        for minutes in IDLE_THRESHOLD_OPTIONS:
            self.idle_threshold_combo.addItem(f"{minutes} 分鐘", minutes)
        threshold_index = self.idle_threshold_combo.findData(self.idle_threshold_minutes)
        self.idle_threshold_combo.setCurrentIndex(max(0, threshold_index))
        self.idle_threshold_combo.currentIndexChanged.connect(self.set_idle_threshold_minutes)
        self.session_grace_combo = QComboBox()
        for seconds in (30, 60, 90, 120):
            self.session_grace_combo.addItem(f"{seconds} 秒", seconds)
        grace_index = self.session_grace_combo.findData(self.settings.activity_session_grace_seconds)
        self.session_grace_combo.setCurrentIndex(max(0, grace_index))
        self.session_grace_combo.currentIndexChanged.connect(self.set_activity_session_grace_seconds)
        self.startup_checkbox = QCheckBox("開機自動啟動")
        self.startup_checkbox.setChecked(self.settings.startup_enabled or is_startup_enabled())
        self.startup_checkbox.toggled.connect(self.set_startup_enabled)
        self.quick_add_checkbox = QCheckBox("助理指令快捷鍵")
        self.quick_add_checkbox.setChecked(self.settings.quick_add_enabled)
        self.quick_add_checkbox.toggled.connect(self.set_quick_add_enabled)
        self.quick_add_hotkey_input = QLineEdit()
        self.quick_add_hotkey_input.setText(self.settings.quick_add_hotkey)
        self.quick_add_hotkey_input.setPlaceholderText("例如 Ctrl+Alt+A")
        self.quick_add_hotkey_input.editingFinished.connect(self.set_quick_add_hotkey)
        self.voice_command_checkbox = QCheckBox("語音助理")
        self.voice_command_checkbox.setChecked(self.settings.voice_command_enabled)
        self.voice_command_checkbox.toggled.connect(self.set_voice_command_enabled)
        self.assistant_name_input = QLineEdit()
        self.assistant_name_input.setText(self.settings.assistant_name)
        self.assistant_name_input.setPlaceholderText(f"例如 {DEFAULT_ASSISTANT_NAME}")
        self.assistant_name_input.editingFinished.connect(self.set_assistant_name)
        self.wake_aliases_label = QLabel(self._wake_aliases_summary())
        self.wake_aliases_label.setObjectName("HintLabel")
        self.manage_wake_aliases_button = QPushButton("管理")
        self.manage_wake_aliases_button.clicked.connect(self.manage_wake_aliases)
        wake_alias_row = QHBoxLayout()
        wake_alias_row.addWidget(self.wake_aliases_label, stretch=1)
        wake_alias_row.addWidget(self.manage_wake_aliases_button)
        wake_alias_widget = QWidget()
        wake_alias_widget.setLayout(wake_alias_row)
        self.voice_microphone_combo = QComboBox()
        self.voice_microphone_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        self.voice_microphone_combo.currentIndexChanged.connect(self.set_voice_microphone_id)
        self.refresh_microphones_button = QPushButton("重新整理")
        self.refresh_microphones_button.clicked.connect(lambda: self.refresh_voice_microphones(show_notice=True))
        microphone_row = QHBoxLayout()
        microphone_row.addWidget(self.voice_microphone_combo, stretch=1)
        microphone_row.addWidget(self.refresh_microphones_button)
        microphone_widget = QWidget()
        microphone_widget.setLayout(microphone_row)
        self.refresh_voice_microphones(show_notice=False)
        self.voice_mic_test_button = QPushButton("測試麥克風")
        self.voice_mic_test_button.clicked.connect(self.test_voice_microphone_now)
        self.voice_wake_test_button = QPushButton("測試喚醒詞")
        self.voice_wake_test_button.clicked.connect(self.test_voice_wake_word_now)
        voice_test_row = QHBoxLayout()
        voice_test_row.addWidget(self.voice_mic_test_button)
        voice_test_row.addWidget(self.voice_wake_test_button)
        voice_test_row.addStretch(1)
        voice_test_widget = QWidget()
        voice_test_widget.setLayout(voice_test_row)
        self.voice_wake_sensitivity_combo = QComboBox()
        self.voice_wake_sensitivity_combo.addItem("自動", "auto")
        self.voice_wake_sensitivity_combo.addItem("低", "low")
        self.voice_wake_sensitivity_combo.addItem("標準", "standard")
        self.voice_wake_sensitivity_combo.addItem("高", "high")
        if hasattr(self, "voice_microphone_gain_combo"):
            self.voice_microphone_gain_combo.setCurrentIndex(max(0, self.voice_microphone_gain_combo.findData(self.settings.voice_microphone_gain)))
        sensitivity_index = self.voice_wake_sensitivity_combo.findData(self.settings.voice_wake_sensitivity)
        self.voice_wake_sensitivity_combo.setCurrentIndex(max(0, sensitivity_index))
        self.voice_wake_sensitivity_combo.currentIndexChanged.connect(self.set_voice_wake_sensitivity)
        self.wake_sound_checkbox = QCheckBox("喚醒提示音")
        self.wake_sound_checkbox.setChecked(self.settings.wake_sound_enabled)
        self.wake_sound_checkbox.toggled.connect(self.set_wake_sound_enabled)
        self.voice_wake_display_combo = QComboBox()
        self.voice_wake_display_combo.addItem("小 OSD", "osd")
        self.voice_wake_display_combo.addItem("Command Bar", "command_bar")
        display_index = self.voice_wake_display_combo.findData(self.settings.voice_wake_display)
        self.voice_wake_display_combo.setCurrentIndex(max(0, display_index))
        self.voice_wake_display_combo.currentIndexChanged.connect(self.set_voice_wake_display)
        self.voice_components_button = QPushButton("安裝 / 重試")
        self.voice_components_button.clicked.connect(self.install_voice_components_now)
        self.voice_command_label = QLabel(self.voice_pipeline.readiness().message)
        self.voice_command_label.setObjectName("HintLabel")
        self.voice_command_label.setWordWrap(True)
        self.quick_add_status_label = QLabel("")
        self.quick_add_status_label.setObjectName("HintLabel")
        self._fit_combo_to_contents(self.idle_threshold_combo, 120)
        monitor_form.addRow(self.auto_start_monitoring_checkbox)
        monitor_form.addRow(self.auto_idle_pause_checkbox)
        monitor_form.addRow("閒置門檻", self.idle_threshold_combo)
        monitor_form.addRow("Session 合併時間", self.session_grace_combo)
        monitor_form.addRow(self.startup_checkbox)
        monitor_form.addRow(self.quick_add_checkbox)
        monitor_form.addRow("快捷鍵", self.quick_add_hotkey_input)
        monitor_form.addRow("", self.quick_add_status_label)
        monitor_form.addRow(self.voice_command_checkbox)
        monitor_form.addRow("助理名稱", self.assistant_name_input)
        monitor_form.addRow("備用喚醒詞", wake_alias_widget)
        monitor_form.addRow("麥克風", microphone_widget)
        monitor_form.addRow("測試", voice_test_widget)
        self.voice_microphone_gain_combo = QComboBox()
        self.voice_microphone_gain_combo.addItem("自動", "auto")
        self.voice_microphone_gain_combo.addItem("關閉", "off")
        self.voice_microphone_gain_combo.setCurrentIndex(max(0, self.voice_microphone_gain_combo.findData(self.settings.voice_microphone_gain)))
        self.voice_microphone_gain_combo.currentIndexChanged.connect(self.set_voice_microphone_gain)
        monitor_form.addRow("麥克風增益", self.voice_microphone_gain_combo)
        monitor_form.addRow("喚醒靈敏度", self.voice_wake_sensitivity_combo)
        self.voice_calibrate_button = QPushButton("測試正常說話音量 / 校準喚醒靈敏度")
        self.voice_calibrate_button.clicked.connect(self.calibrate_voice_sensitivity)
        monitor_form.addRow("", self.voice_calibrate_button)
        monitor_form.addRow(self.wake_sound_checkbox)
        monitor_form.addRow("喚醒後顯示", self.voice_wake_display_combo)
        monitor_form.addRow("語音元件", self.voice_components_button)
        monitor_form.addRow("", self.voice_command_label)
        voice_diagnostics_button = QPushButton("語音診斷／最近一次指令")
        voice_diagnostics_button.clicked.connect(self.show_voice_diagnostics)
        monitor_form.addRow("", voice_diagnostics_button)
        monitor_settings_layout.addWidget(monitor_group)
        monitor_settings_layout.addStretch(1)
        self.settings_tabs.addTab(monitor_settings_page, "監督")

        display_settings_page, display_settings_layout = self._scroll_page("DisplaySettingsScroll")
        display_group = QGroupBox("顯示")
        display_form = QFormLayout(display_group)
        self.top_status_bar_visible_checkbox = QCheckBox("顯示狀態列")
        self.top_status_bar_visible_checkbox.setChecked(self.top_status_visible)
        self.top_status_bar_visible_checkbox.toggled.connect(self.set_top_status_bar_visible)
        self.top_status_bar_screen_combo = QComboBox()
        self.reminder_popup_screen_combo = QComboBox()
        self.top_status_bar_position_combo = QComboBox()
        self._populate_display_controls()
        self.top_status_bar_screen_combo.currentIndexChanged.connect(self.set_top_status_bar_screen)
        self.reminder_popup_screen_combo.currentIndexChanged.connect(self.set_reminder_popup_screen)
        self.top_status_bar_position_combo.currentIndexChanged.connect(self.set_top_status_bar_position)
        display_form.addRow(self.top_status_bar_visible_checkbox)
        display_form.addRow("狀態列螢幕", self.top_status_bar_screen_combo)
        display_form.addRow("狀態列位置", self.top_status_bar_position_combo)
        display_form.addRow("提醒浮窗螢幕", self.reminder_popup_screen_combo)
        display_settings_layout.addWidget(display_group)
        display_settings_layout.addStretch(1)
        self.settings_tabs.addTab(display_settings_page, "顯示")

        privacy_settings_page, privacy_settings_layout = self._scroll_page("PrivacySettingsScroll")
        privacy_group = QGroupBox("隱私 / 資料")
        privacy_form = QFormLayout(privacy_group)
        self.excluded_apps_label = QLabel(self._excluded_apps_summary())
        self.excluded_apps_label.setObjectName("HintLabel")
        self.manage_excluded_apps_button = QPushButton("管理")
        self.manage_excluded_apps_button.clicked.connect(self.manage_excluded_apps)
        excluded_row = QHBoxLayout()
        excluded_row.addWidget(self.excluded_apps_label, stretch=1)
        excluded_row.addWidget(self.manage_excluded_apps_button)
        excluded_widget = QWidget()
        excluded_widget.setLayout(excluded_row)
        self.app_aliases_label = QLabel(self._app_aliases_summary())
        self.app_aliases_label.setObjectName("HintLabel")
        self.manage_app_aliases_button = QPushButton("管理")
        self.manage_app_aliases_button.clicked.connect(self.manage_app_aliases)
        app_alias_row = QHBoxLayout()
        app_alias_row.addWidget(self.app_aliases_label, stretch=1)
        app_alias_row.addWidget(self.manage_app_aliases_button)
        app_alias_widget = QWidget()
        app_alias_widget.setLayout(app_alias_row)
        self.export_settings_button = QPushButton("匯出設定")
        self.export_settings_button.clicked.connect(self.export_settings)
        self.import_settings_button = QPushButton("匯入設定")
        self.import_settings_button.clicked.connect(self.import_settings)
        self.reset_learning_button = QPushButton("清除學習")
        self.reset_learning_button.clicked.connect(self.reset_learning_data)
        self.reset_all_button = QPushButton("清全部個人資料")
        self.reset_all_button.clicked.connect(self.reset_all_personal_data)
        data_row = QHBoxLayout()
        data_row.addWidget(self.export_settings_button)
        data_row.addWidget(self.import_settings_button)
        data_row.addWidget(self.reset_learning_button)
        data_row.addWidget(self.reset_all_button)
        data_row.addStretch(1)
        data_widget = QWidget()
        data_widget.setLayout(data_row)
        privacy_form.addRow(self.privacy_mode_checkbox)
        privacy_form.addRow(self.learning_checkbox)
        privacy_form.addRow(self.notification_learning_checkbox)
        privacy_form.addRow(self.dead_loop_checkbox)
        privacy_form.addRow("排除 App", excluded_widget)
        privacy_form.addRow("名稱 / 別名", app_alias_widget)
        privacy_form.addRow("資料", data_widget)
        privacy_settings_layout.addWidget(privacy_group)
        privacy_settings_layout.addStretch(1)
        self.settings_tabs.addTab(privacy_settings_page, "隱私 / 資料")

        tabs = QTabWidget()

        overview = QWidget()
        overview_layout = QVBoxLayout(overview)
        overview_layout.setContentsMargins(0, 0, 0, 0)

        splitter = QSplitter(Qt.Horizontal)
        reminders_panel = QWidget()
        reminders_layout = QVBoxLayout(reminders_panel)
        reminders_layout.setContentsMargins(0, 0, 0, 0)
        reminders_layout.addWidget(QLabel("提醒"))
        self.reminders_list = QListWidget()
        self._prepare_wrapping_list(self.reminders_list)
        reminders_layout.addWidget(self.reminders_list)
        edit_row = QHBoxLayout()
        for label, action in (("修改", "update"), ("刪除", "delete"), ("復原上次操作", "undo")):
            button = QPushButton(label)
            button.clicked.connect(lambda checked=False, action=action: self._selected_reminder_operation(action))
            edit_row.addWidget(button)
        reminders_layout.addLayout(edit_row)

        events_panel = QWidget()
        events_layout = QVBoxLayout(events_panel)
        events_layout.setContentsMargins(0, 0, 0, 0)
        events_layout.addWidget(QLabel("最近事件"))
        self.events_list = QListWidget()
        self._prepare_wrapping_list(self.events_list)
        events_layout.addWidget(self.events_list)

        splitter.addWidget(reminders_panel)
        splitter.addWidget(events_panel)
        splitter.setSizes([430, 430])
        overview_layout.addWidget(splitter)

        learning_page = QWidget()
        learning_layout = QVBoxLayout(learning_page)
        learning_layout.setContentsMargins(0, 0, 0, 0)
        learning_layout.setSpacing(8)
        learning_layout.addWidget(QLabel("AI 推測 / 已觀察到的習慣"))
        self.habits_list = QListWidget()
        learning_layout.addWidget(self.habits_list, stretch=1)

        pattern_buttons = QHBoxLayout()
        self.reject_pattern_button = QPushButton("不要再用這個推測")
        self.reject_pattern_button.clicked.connect(self.reject_selected_pattern)
        self.reset_pattern_button = QPushButton("重置這個推測")
        self.reset_pattern_button.clicked.connect(self.reset_selected_pattern)
        pattern_buttons.addWidget(self.reject_pattern_button)
        pattern_buttons.addWidget(self.reset_pattern_button)
        pattern_buttons.addStretch(1)
        learning_layout.addLayout(pattern_buttons)

        learning_layout.addWidget(QLabel("AI 建議（Level 3，需要你確認）"))
        self.suggestions_list = QListWidget()
        learning_layout.addWidget(self.suggestions_list, stretch=1)
        suggestion_buttons = QHBoxLayout()
        self.apply_suggestion_button = QPushButton("套用")
        self.apply_suggestion_button.clicked.connect(self.apply_selected_suggestion)
        self.reject_suggestion_button = QPushButton("不要套用")
        self.reject_suggestion_button.clicked.connect(self.reject_selected_suggestion)
        suggestion_buttons.addWidget(self.apply_suggestion_button)
        suggestion_buttons.addWidget(self.reject_suggestion_button)
        suggestion_buttons.addStretch(1)
        learning_layout.addLayout(suggestion_buttons)

        tabs.addTab(overview, "總覽")
        assistant_page, assistant_layout = self._scroll_page("AssistantScroll")
        assistant_layout.setContentsMargins(0, 0, 0, 0)
        assistant_layout.setSpacing(8)
        self.understanding_label = QLabel("目前理解：尚未分析")
        self._prepare_advisor_text_label(self.understanding_label)
        self.intent_label = QLabel("目前意圖：未知")
        self._prepare_advisor_text_label(self.intent_label)
        self.session_label = QLabel("目前 session：尚未開始")
        self._prepare_advisor_text_label(self.session_label)
        self.goal_label = QLabel("目前目標：尚未設定")
        self._prepare_advisor_text_label(self.goal_label)
        self.next_action_label = QLabel("下一步建議：尚未分析")
        self._prepare_advisor_text_label(self.next_action_label)
        self.advisor_source_label = QLabel("Advisor：本機規則")
        self.advisor_source_label.setObjectName("HintLabel")
        self._prepare_advisor_text_label(self.advisor_source_label)
        self.goal_input = QLineEdit()
        self.goal_input.setPlaceholderText("輸入目前目標，例如：今天測試提醒系統")
        self.goal_input.returnPressed.connect(self.set_manual_goal)
        assistant_buttons = QHBoxLayout()
        self.save_goal_button = QPushButton("儲存目標")
        self.save_goal_button.clicked.connect(self.set_manual_goal)
        self.clear_goal_button = QPushButton("清除目標")
        self.clear_goal_button.clicked.connect(self.clear_manual_goal)
        self.analyze_button = QPushButton("重新分析")
        self.analyze_button.clicked.connect(self.refresh_advisor)
        self.break_button = QPushButton("現在休息")
        self.break_button.clicked.connect(self.set_intent_break)
        self.work_button = QPushButton("回到工作")
        self.work_button.clicked.connect(self.set_intent_work)
        self.private_time_button = QPushButton("進入私人時間")
        self.private_time_button.clicked.connect(self.set_intent_private_time)
        assistant_buttons.addWidget(self.save_goal_button)
        assistant_buttons.addWidget(self.clear_goal_button)
        assistant_buttons.addWidget(self.analyze_button)
        assistant_buttons.addStretch(1)
        intent_buttons = QHBoxLayout()
        intent_buttons.addWidget(self.break_button)
        intent_buttons.addWidget(self.work_button)
        intent_buttons.addWidget(self.private_time_button)
        intent_buttons.addStretch(1)
        assistant_layout.addWidget(QLabel("目前理解"))
        assistant_layout.addWidget(self.understanding_label)
        assistant_layout.addWidget(QLabel("目前意圖"))
        assistant_layout.addWidget(self.intent_label)
        assistant_layout.addWidget(QLabel("目前 session"))
        assistant_layout.addWidget(self.session_label)
        assistant_layout.addLayout(intent_buttons)
        assistant_layout.addWidget(QLabel("目前目標"))
        assistant_layout.addWidget(self.goal_label)
        assistant_layout.addWidget(self.goal_input)
        assistant_layout.addLayout(assistant_buttons)
        assistant_layout.addWidget(QLabel("下一步建議"))
        assistant_layout.addWidget(self.next_action_label)
        assistant_layout.addWidget(self.advisor_source_label)
        assistant_layout.addStretch(1)
        tabs.addTab(assistant_page, "助理")
        today_page = QWidget()
        today_layout = QVBoxLayout(today_page)
        today_layout.setContentsMargins(0, 0, 0, 0)
        today_layout.addWidget(QLabel("今日摘要 / Inbox"))
        self.today_list = QListWidget()
        today_layout.addWidget(self.today_list)
        tabs.addTab(today_page, "今日")
        tabs.addTab(learning_page, "學習 / 習慣")
        self.rhythm_diagnostic = RhythmDiagnosticWidget(
            self.rhythm_storage, lambda: self.current_snapshot, self)
        self.rhythm_status_label = QLabel("")
        self.statusBar().addPermanentWidget(self.rhythm_status_label)
        self.rhythm_diagnostic.status_changed.connect(self.rhythm_status_label.setText)
        tabs.addTab(self.rhythm_diagnostic, "音遊診斷")

        layout.addWidget(title)
        layout.addWidget(subtitle)
        layout.addLayout(add_row)
        layout.addLayout(service_row)
        layout.addWidget(self.settings_tabs)
        layout.addLayout(status_row)
        layout.addWidget(tabs, stretch=1)
        self.setCentralWidget(root)

        self.setStyleSheet(
            """
            QWidget {
                font-family: "Microsoft JhengHei UI", "Segoe UI", sans-serif;
                font-size: 14px;
            }
            #Title {
                font-size: 24px;
                font-weight: 700;
            }
            #Subtitle {
                color: #555;
            }
            #ModeLabel {
                font-weight: 600;
            }
            #ServiceLabel {
                color: #285a36;
                font-weight: 600;
            }
            #HintLabel {
                color: #5c6670;
            }
            QCheckBox, QComboBox {
                padding: 4px;
            }
            QPushButton {
                padding: 8px 12px;
            }
            QLineEdit {
                padding: 8px;
            }
            QListWidget {
                border: 1px solid #d0d0d0;
                border-radius: 6px;
                padding: 6px;
            }
            #ListCardLabel {
                background: #ffffff;
                border: 1px solid #e1e5ea;
                border-radius: 6px;
                color: #20242a;
                line-height: 1.35;
            }
            """
        )
        self._sync_settings_controls()

    def _page_layout(self, page: QWidget) -> QVBoxLayout:
        layout = QVBoxLayout(page)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)
        return layout

    def _scroll_page(self, object_name: str) -> tuple[QScrollArea, QVBoxLayout]:
        content = QWidget()
        content.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        layout = self._page_layout(content)
        scroll = QScrollArea()
        scroll.setObjectName(object_name)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scroll.setWidget(content)
        return scroll, layout

    def _prepare_advisor_text_label(self, label: QLabel) -> QLabel:
        label.setWordWrap(True)
        label.setMinimumWidth(0)
        label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        return label

    def _set_advisor_text(self, label: QLabel, text: str) -> None:
        label.setText(self._soft_break_long_tokens(text))
        label.updateGeometry()

    @staticmethod
    def _soft_break_long_tokens(text: str, chunk_size: int = 48) -> str:
        if chunk_size < 8:
            chunk_size = 8
        pieces: list[str] = []
        token: list[str] = []

        def flush_token() -> None:
            if not token:
                return
            raw = "".join(token)
            if len(raw) <= chunk_size:
                pieces.append(raw)
            else:
                pieces.append("\u200b".join(raw[index : index + chunk_size] for index in range(0, len(raw), chunk_size)))
            token.clear()

        for char in text:
            if char.isspace():
                flush_token()
                pieces.append(char)
            else:
                token.append(char)
        flush_token()
        return "".join(pieces)

    def _prepare_wrapping_list(self, widget: QListWidget) -> None:
        widget.setWordWrap(True)
        widget.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        widget.setUniformItemSizes(False)
        widget.setSpacing(6)

    def _fit_combo_to_contents(self, combo: QComboBox, minimum_width: int = 150) -> None:
        combo.setMinimumContentsLength(10)
        combo.setMinimumWidth(minimum_width)
        combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)

    def _add_wrapped_item(self, widget: QListWidget, text: str) -> QListWidgetItem:
        label = QLabel(text)
        label.setWordWrap(True)
        label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        label.setObjectName("ListCardLabel")
        label.setContentsMargins(10, 8, 10, 8)
        item = QListWidgetItem()
        width = max(260, widget.viewport().width() - 18)
        label.setFixedWidth(width)
        label.adjustSize()
        item.setSizeHint(QSize(width, max(54, label.sizeHint().height() + 12)))
        widget.addItem(item)
        widget.setItemWidget(item, label)
        return item

    def _excluded_apps_summary(self) -> str:
        count = len(self.settings.excluded_apps)
        return f"已排除 {count} 個 App" if count else "尚未排除 App"

    def _build_tray(self) -> None:
        icon = self.windowIcon()
        if icon.isNull():
            icon = self.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon)
            self.setWindowIcon(icon)

        self.tray_icon = QSystemTrayIcon(icon, self)
        self.tray_icon.setToolTip("Private Assistant AI Alpha")
        self.tray_icon.activated.connect(self._on_tray_activated)

        self.open_action = QAction("開啟 Private Assistant AI", self)
        self.open_action.triggered.connect(self.show_main_window)
        self.toggle_monitor_action = QAction("開始監督", self)
        self.toggle_monitor_action.triggered.connect(self.toggle_monitoring)
        self.toggle_top_bar_action = QAction("隱藏頂部狀態列" if self.top_status_visible else "顯示頂部狀態列", self)
        self.toggle_top_bar_action.triggered.connect(self.toggle_top_status_bar)
        self.exit_action = QAction("離開", self)
        self.exit_action.triggered.connect(self.exit_application)

        menu = QMenu(self)
        menu.addAction(self.open_action)
        menu.addAction(self.toggle_monitor_action)
        menu.addAction(self.toggle_top_bar_action)
        menu.addSeparator()
        menu.addAction(self.exit_action)
        self.tray_icon.setContextMenu(menu)
        self.tray_icon.show()

    def add_reminder(self) -> None:
        raw_text = self.reminder_input.text()
        from .reminder_operations import parse_operation
        operation = parse_operation(raw_text)
        if operation:
            if self._execute_reminder_operation(operation):
                self.reminder_input.clear()
            return
        try:
            parsed = parse_natural_reminder(
                raw_text,
                active_hour_scores=self.store.active_hour_scores(),
                user_is_active=not self._is_user_away(self.current_snapshot) if self.current_snapshot else True,
            )
        except ValueError as exc:
            QMessageBox.warning(self, "無法新增提醒", str(exc))
            return
        self._add_parsed_reminder(parsed, clear_input=True)

    def _add_parsed_reminder(self, parsed, clear_input: bool = False) -> bool:

        parsed = self._confirm_ambiguous_time_if_needed(parsed)
        if parsed is None:
            return False

        if parsed.needs_confirmation:
            if parsed.missing_fields:
                QMessageBox.warning(
                    self,
                    "需要補充週期資訊",
                    (parsed.confirmation_summary or "")
                    + "\n\n請補充未指定欄位後再新增；例如：休息 7 天再開始下一輪。",
                )
                return False
            answer = QMessageBox.question(
                self,
                "確認週期提醒",
                parsed.confirmation_summary or "確認新增這個週期提醒？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return False

        trace_reminder("creator", due_at=parsed.due_at, needs_confirmation=parsed.needs_confirmation)
        if parsed.kind == "context":
            self.store.add_context_reminder(parsed.text, parsed.context_name or "", parsed.context_trigger or "open")
        elif parsed.kind == "today_inbox":
            self.store.add_reminder(parsed.text, parsed.due_at, status="today_inbox")
        else:
            self.store.add_reminder(
                parsed.text,
                parsed.due_at,
                parsed.recurrence_rule,
                parsed.next_due_at,
                time_inferred=parsed.time_inferred,
                inference_reason=parsed.inference_reason,
            )
        if clear_input:
            self.reminder_input.clear()
        self.refresh_lists()
        self.status_bar_notice = (parsed.understood_message, datetime.now() + timedelta(minutes=5))
        self.update_top_status_bar()
        return True

    def add_context_reminder_from_text(self, raw_text: str) -> bool:
        try:
            parsed = parse_natural_reminder(raw_text, active_hour_scores=self.store.active_hour_scores())
        except ValueError:
            return False
        if parsed.kind != "context" or not parsed.context_name:
            return False
        self.store.add_context_reminder(parsed.text, parsed.context_name, parsed.context_trigger or "open")
        self.status_bar_notice = ("已加入情境提醒", datetime.now() + timedelta(minutes=5))
        return True

    def _confirm_ambiguous_time_if_needed(self, parsed):
        if not parsed.ambiguous_time_options:
            return parsed
        options = parsed.ambiguous_time_options
        if len(options) < 2:
            return parsed

        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("確認提醒時間")
        box.setText(parsed.confirmation_summary or "請確認你說的是哪個時間。")
        first_button = box.addButton(format_due_at(options[0]), QMessageBox.ButtonRole.AcceptRole)
        second_button = box.addButton(format_due_at(options[1]), QMessageBox.ButtonRole.AcceptRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        clicked = box.clickedButton()
        if clicked == first_button:
            selected = options[0]
        elif clicked == second_button:
            selected = options[1]
        else:
            return None

        inference_reason = f"使用者確認模糊時間為 {selected.strftime('%Y-%m-%d %H:%M')}"
        if parsed.recurrence_rule:
            try:
                rule = json.loads(parsed.recurrence_rule)
                rule["time"] = selected.strftime("%H:%M")
                rule_text = json.dumps(rule, ensure_ascii=False, sort_keys=True)
                due_at = next_occurrence(rule_text, datetime.now()) or selected
                return replace(
                    parsed,
                    due_at=due_at,
                    recurrence_rule=rule_text,
                    next_due_at=next_occurrence(rule_text, due_at),
                    needs_confirmation=rule.get("freq") in {"cycle", "interval"},
                    confirmation_summary=(parsed.confirmation_summary or "").split("\n\n")[0] + f"\n確認時間：{format_due_at(due_at)}",
                    ambiguous_time_options=(),
                    time_inferred=True,
                    inference_reason=inference_reason,
                )
            except (ValueError, json.JSONDecodeError, KeyError):
                pass

        return replace(
            parsed,
            due_at=selected,
            needs_confirmation=False,
            confirmation_summary=None,
            ambiguous_time_options=(),
            time_inferred=True,
            inference_reason=inference_reason,
        )

    def _repair_known_ambiguous_time_misparse(self) -> None:
        now = datetime.now()
        wrong_due_at = datetime(2026, 9, 11, 5, 30)
        intended_due_at = datetime(2026, 9, 10, 17, 30)
        repaired = self.store.repair_misparsed_garbage_reminder(wrong_due_at, intended_due_at, now)
        if not repaired:
            return
        self.status_bar_notice = (
            "已修正：收拾垃圾改為今天 17:30，這筆現在已到期",
            now + timedelta(minutes=10),
        )

    def _populate_display_controls(self) -> None:
        screens = self.top_status_bar.screen_geometries()
        status_options = [
            ("自動（多螢幕優先副螢幕）", "auto"),
            ("主螢幕", "primary"),
            ("副螢幕", "secondary"),
            ("跟隨目前活動視窗", "active"),
        ]
        popup_options = [
            ("目前活動螢幕", "active"),
            ("與狀態列相同", "status_bar"),
            ("主螢幕", "primary"),
            ("副螢幕", "secondary"),
        ]
        for screen in screens:
            label = f"指定螢幕 {screen.index + 1}"
            if screen.is_primary:
                label += "（主）"
            status_options.append((label, f"screen:{screen.index}"))
            popup_options.append((label, f"screen:{screen.index}"))

        self._replace_combo_items(self.top_status_bar_screen_combo, status_options, self.settings.top_status_bar_screen)
        self._replace_combo_items(self.reminder_popup_screen_combo, popup_options, self.settings.reminder_popup_screen)
        self._replace_combo_items(
            self.top_status_bar_position_combo,
            [("左上角", POSITION_TOP_LEFT), ("右上角", POSITION_TOP_RIGHT), ("自訂（暫用左上）", "custom")],
            self.settings.top_status_bar_position,
        )
        self._fit_combo_to_contents(self.top_status_bar_screen_combo, 230)
        self._fit_combo_to_contents(self.reminder_popup_screen_combo, 200)
        self._fit_combo_to_contents(self.top_status_bar_position_combo, 150)

    def _replace_combo_items(self, combo: QComboBox, options: list[tuple[str, str]], current_value: str) -> None:
        combo.blockSignals(True)
        combo.clear()
        for label, value in options:
            combo.addItem(label, value)
        index = combo.findData(current_value)
        combo.setCurrentIndex(max(0, index))
        combo.blockSignals(False)

    def toggle_top_status_bar(self) -> None:
        self.top_status_visible = not self.top_status_visible
        self.settings = self.settings_service.update(top_status_bar_visible=self.top_status_visible)
        self.toggle_top_bar_action.setText("隱藏頂部狀態列" if self.top_status_visible else "顯示頂部狀態列")
        if hasattr(self, "top_status_bar_visible_checkbox"):
            self.top_status_bar_visible_checkbox.blockSignals(True)
            self.top_status_bar_visible_checkbox.setChecked(self.top_status_visible)
            self.top_status_bar_visible_checkbox.blockSignals(False)
        if self.top_status_visible:
            self.top_status_bar.show()
            self.update_top_status_bar()
        else:
            self.top_status_bar.hide()

    def toggle_monitoring(self) -> None:
        if self.monitoring:
            self.stop_monitoring(manual=True)
        else:
            self.start_monitoring(manual=True)

    def start_monitoring(self, manual: bool = False) -> None:
        if self.monitoring:
            return
        if not manual and self.manually_stopped_monitoring:
            return
        self.monitoring = True
        if manual:
            self.manually_stopped_monitoring = False
        self.auto_idle_paused = False
        self.last_pause_reason = ""
        self.settings = self.settings_service.update(monitoring_enabled=True)
        self.monitor_timer.start()
        self.monitor_tick()
        self._refresh_monitor_controls()
        self.update_top_status_bar()

    def stop_monitoring(self, manual: bool = False) -> None:
        self.monitoring = False
        if manual:
            self.manually_stopped_monitoring = True
        self.auto_idle_paused = False
        self.last_pause_reason = ""
        self.settings = self.settings_service.update(monitoring_enabled=False)
        self.monitor_timer.stop()
        self.mode_label.setText("目前模式：已停止")
        self._refresh_monitor_controls()
        self.update_top_status_bar()

    def monitor_tick(self) -> None:
        if not self.monitoring:
            return
        snapshot = get_foreground_snapshot()
        self.previous_snapshot = self.current_snapshot
        self.current_snapshot = snapshot
        if self._should_auto_pause(snapshot):
            if not self.auto_idle_paused:
                self.auto_idle_paused = True
                self.last_pause_reason = "Windows 已鎖定" if is_windows_locked_or_unavailable() else "閒置"
                self.event_bus.publish_idle_state(snapshot, True, self.last_pause_reason)
            self.mode_label.setText(f"目前模式：監督暫停（{self.last_pause_reason}）")
            self._refresh_monitor_controls()
            self.update_top_status_bar()
            return
        if self.auto_idle_paused:
            self.auto_idle_paused = False
            self.last_pause_reason = ""
            self.event_bus.publish_idle_state(snapshot, False, "使用者回到電腦")
            self._release_pending_due_reminders(snapshot)
            self._maybe_show_notification_learning_prompt(snapshot)

        self._update_activity_duration(snapshot)
        if self.store.should_record_app(snapshot.app_name, snapshot.window_title) and not self.store.privacy_mode_enabled():
            self.event_bus.publish_foreground_snapshot(snapshot)
            self.learning.observe_foreground_change(snapshot)
            self.learning.observe_notification_app_visit(snapshot)
            self.store.evaluate_context_reminders(snapshot)
            if self.previous_snapshot is None or session_identity(snapshot) != session_identity(self.previous_snapshot):
                self.store.add_usage_event(snapshot)
            self._maybe_alert_dead_loop(snapshot)
        title = snapshot.window_title[:70] or "(無標題)"
        self.mode_label.setText(f"目前模式：{snapshot.mode} ｜ {snapshot.app_name} ｜ {title}")
        self._refresh_monitor_controls()
        self.refresh_events()
        self.refresh_advisor(auto=True)
        self.update_top_status_bar()

    def reminder_tick(self) -> None:
        snapshot = self.current_snapshot or get_foreground_snapshot()
        if self.settings.escalation_enabled:
            self.store.escalate_unconfirmed_reminders(datetime.now() - timedelta(minutes=self.settings.escalation_minutes))
        self._release_deferred_reminders(snapshot)
        self._release_pending_due_reminders(snapshot)
        self._maybe_show_notification_learning_prompt(snapshot)
        self._mark_timed_out_reminders(snapshot)
        for reminder in self.store.due_reminders(datetime.now()):
            if reminder.id in self.deferred_reminders:
                continue
            if self._is_user_away(snapshot):
                self.store.mark_reminder_pending_due(reminder.id, "離席期間到期", datetime.now())
                if reminder.category == "health" or reminder.importance == "critical":
                    self.notifications.notify("提醒先幫你留著", f"你離席時有一個重要提醒到期：{reminder.text}")
                continue
            title, body = self.analyzer.build_reminder_message(reminder, snapshot)
            decision = self.strategy.decide(reminder, snapshot, self.auto_idle_paused)
            self.event_bus.publish_reminder_event(
                "reminder_strategy_decided",
                reminder,
                metadata={"strategy": decision.strategy, "reason": decision.reason},
            )
            if decision.strategy in (PRESENTATION_DEFER_ACTIVE, PRESENTATION_DEFER_GAME_END):
                if self.auto_idle_paused:
                    self.store.mark_reminder_pending_due(reminder.id, decision.reason)
                self.deferred_reminders[reminder.id] = (reminder, decision, title, body, snapshot)
                self.events_list.insertItem(0, QListWidgetItem(f"延後提醒：{reminder.text}｜{decision.reason}"))
                continue
            self._present_reminder(reminder, title, body, snapshot, decision.strategy, decision.reason)
        self.refresh_reminders()
        self.refresh_habits()
        self.maybe_suggest_today_inbox(snapshot)
        self.update_top_status_bar()

    def _present_reminder(
        self,
        reminder: Reminder,
        title: str,
        body: str,
        snapshot: ForegroundSnapshot,
        strategy_name: str,
        reason: str,
    ) -> None:
        if not self.store.mark_reminder_notified(reminder.id, expected_due_at=reminder.due_at):
            return
        sent = False
        if self.settings.windows_notifications_enabled and strategy_name in (PRESENTATION_IMMEDIATE_POPUP, PRESENTATION_WINDOWS_ONLY, PRESENTATION_REPEAT):
            sent = self.notifications.notify(title, f"{body}\n\n原因：{reason}")
        self.store.log_reminder_interaction(reminder.id, "notified", current_mode=snapshot.mode)
        self.event_bus.publish_reminder_event(
            "reminder_presented",
            reminder,
            metadata={"strategy": strategy_name, "reason": reason},
        )
        if self.settings.reminder_popups_enabled and strategy_name in (PRESENTATION_IMMEDIATE_POPUP, PRESENTATION_REPEAT):
            self.show_reminder_popup(reminder, title, body)
        elif strategy_name == PRESENTATION_STATUS_ONLY:
            self.events_list.insertItem(0, QListWidgetItem(f"狀態列提醒：{title}｜{reason}"))
            self.status_bar_notice = (f"提醒：{self._shorten(reminder.text, 18)}", datetime.now() + timedelta(minutes=10))
        if not sent and strategy_name != PRESENTATION_STATUS_ONLY:
            self.events_list.insertItem(0, QListWidgetItem(f"通知備援：{title} - {body}"))

    def _release_deferred_reminders(self, snapshot: ForegroundSnapshot) -> None:
        if self._is_user_away(snapshot):
            return
        for reminder_id, (reminder, decision, title, body, old_snapshot) in list(self.deferred_reminders.items()):
            if self.strategy.should_release_deferred(decision, old_snapshot, snapshot):
                self.deferred_reminders.pop(reminder_id, None)
                self._present_reminder(reminder, title, body, snapshot, PRESENTATION_IMMEDIATE_POPUP, "你回到電腦或切換離遊戲，補送先前延後的提醒")

    def _release_pending_due_reminders(self, snapshot: ForegroundSnapshot) -> None:
        if self._is_user_away(snapshot):
            return
        if self.active_popups:
            return
        pending = self.store.pending_due_reminders()
        if not pending:
            return
        reminder = pending[0]
        if len(pending) == 1:
            body = f"你離席時有一個事項提醒：{reminder.text}。請問你已經處理完畢了嗎？"
        else:
            body = f"你離席時有 {len(pending)} 個事項到期。先確認：{reminder.text}。請問你已經處理完畢了嗎？"
        self._present_reminder(
            reminder,
            f"回來確認：{reminder.text}",
            body,
            snapshot,
            PRESENTATION_IMMEDIATE_POPUP,
            "離席期間到期，回來後再確認",
        )

    def _is_user_away(self, snapshot: ForegroundSnapshot) -> bool:
        if not self.monitoring and not self.auto_idle_paused:
            return False
        threshold_seconds = max(60, self.idle_threshold_minutes * 60)
        return self.auto_idle_paused or snapshot.mode == "閒置" or snapshot.idle_seconds >= threshold_seconds or is_windows_locked_or_unavailable()

    def _mark_timed_out_reminders(self, snapshot: ForegroundSnapshot) -> None:
        cutoff = datetime.now() - timedelta(minutes=30)
        for reminder in self.store.notified_reminders_older_than(cutoff):
            if reminder.id in self.active_popups:
                continue
            if any(item.event_type == "timeout" for item in self.store.reminder_interactions(reminder.id)):
                continue
            self.store.log_reminder_interaction(reminder.id, "timeout", current_mode=snapshot.mode)
            self.learning.learn_from_reminder_outcome(reminder.id, "timeout", snapshot.mode)
            self.event_bus.publish_reminder_event("reminder_timeout", reminder, metadata={"minutes": 30})

    def show_reminder_popup(self, reminder: Reminder, title: str, body: str) -> None:
        old_popup = self.active_popups.pop(reminder.id, None)
        if old_popup:
            old_popup.close()

        popup = ReminderPopup(
            reminder=reminder,
            title=title,
            body=body,
            on_complete=self.complete_reminder,
            on_snooze=self.snooze_reminder,
            on_ignore=self.ignore_reminder,
            parent=None,
        )
        popup.destroyed.connect(lambda _=None, reminder_id=reminder.id: self.active_popups.pop(reminder_id, None))
        self.active_popups[reminder.id] = popup
        QApplication.beep()
        popup.show_near_top_center(
            stack_index=len(self.active_popups) - 1,
            top_offset=44 if self.top_status_visible else 18,
            screen_geometry=self._popup_screen(),
        )

    def _maybe_show_notification_learning_prompt(self, snapshot: ForegroundSnapshot) -> None:
        if self.active_notification_prompts or self._is_user_away(snapshot):
            return
        candidate = self.learning.notification_intervention_candidate(datetime.now(), away=False)
        if not candidate:
            return
        event, pattern = candidate
        if not self.store.mark_notification_intervened(event.id):
            return
        title = f"訊息提醒：{event.source_app}"
        body = "你平常這類訊息會很快查看，現在已經放了一段時間，要不要確認一下？"
        popup = AssistantNotificationPrompt(
            notification_event_id=event.id,
            title=title,
            body=body,
            on_open=self.open_notification_source,
            on_snooze=self.snooze_notification_prompt,
            on_ignore=self.ignore_notification_prompt,
            parent=None,
        )
        popup.destroyed.connect(lambda _=None, event_id=event.id: self.active_notification_prompts.pop(event_id, None))
        self.active_notification_prompts[event.id] = popup
        popup.show_near_top_center(
            stack_index=len(self.active_popups),
            top_offset=44 if self.top_status_visible else 18,
            screen_geometry=self._popup_screen(),
        )
        self.store.record_event(
            event_type="notification_learning_prompt",
            source="notification_learning",
            context_mode=snapshot.mode,
            app_name=event.source_app,
            metadata={"reason": pattern.reason or event.source_app},
        )

    def open_notification_source(self, notification_event_id: int) -> None:
        self.learning.handle_notification_prompt_action(notification_event_id, "open")
        self.status_bar_notice = ("已記錄：去看看", datetime.now() + timedelta(minutes=3))
        self.refresh_habits()

    def snooze_notification_prompt(self, notification_event_id: int) -> None:
        self.learning.handle_notification_prompt_action(notification_event_id, "snooze")
        self.status_bar_notice = ("稍後再提醒", datetime.now() + timedelta(minutes=3))

    def ignore_notification_prompt(self, notification_event_id: int) -> None:
        self.learning.handle_notification_prompt_action(notification_event_id, "ignore")
        self.status_bar_notice = ("已忽略這次助理提示", datetime.now() + timedelta(minutes=3))
        self.refresh_habits()

    def record_reminder_feedback(self, reminder_id: int, helpful: bool) -> None:
        self.store.add_reminder_feedback(reminder_id, helpful, self._current_mode())
        self.status_bar_notice = ("已記錄回饋", datetime.now() + timedelta(minutes=3))
        self.refresh_habits()

    def complete_reminder(self, reminder_id: int) -> None:
        expected_due_at = self._active_popup_due_at(reminder_id)
        self.store.log_reminder_interaction(reminder_id, "completed", current_mode=self._current_mode())
        changed = self.store.complete_reminder(reminder_id, expected_due_at=expected_due_at)
        if not changed:
            return
        self.deferred_reminders.pop(reminder_id, None)
        self.learning.learn_from_reminder_outcome(reminder_id, "completed", self._current_mode())
        self.refresh_lists()
        self.update_top_status_bar()

    def snooze_reminder(self, reminder_id: int) -> None:
        expected_due_at = self._active_popup_due_at(reminder_id)
        self.store.log_reminder_interaction(reminder_id, "snoozed", current_mode=self._current_mode())
        changed = self.store.snooze_reminder(
            reminder_id,
            datetime.now() + timedelta(minutes=10),
            expected_due_at=expected_due_at,
        )
        if not changed:
            return
        self.learning.learn_from_reminder_outcome(reminder_id, "snoozed", self._current_mode())
        self.refresh_lists()
        self.update_top_status_bar()

    def ignore_reminder(self, reminder_id: int) -> None:
        expected_due_at = self._active_popup_due_at(reminder_id)
        self.store.log_reminder_interaction(reminder_id, "ignored", current_mode=self._current_mode())
        changed = self.store.ignore_reminder(reminder_id, expected_due_at=expected_due_at)
        if not changed:
            return
        self.learning.learn_from_reminder_outcome(reminder_id, "ignored", self._current_mode())
        self.refresh_lists()
        self.update_top_status_bar()

    def _active_popup_due_at(self, reminder_id: int) -> datetime | None:
        popup = self.active_popups.get(reminder_id)
        return popup.reminder.due_at if popup else None

    def refresh_lists(self) -> None:
        self.refresh_reminders()
        self.refresh_events()
        self.refresh_habits()
        self.refresh_today()
        self.refresh_advisor(auto=True)

    def refresh_reminders(self) -> None:
        self.reminders_list.clear()
        for reminder in self.store.list_reminders():
            summary = self._learning_summary(reminder)
            item = self._add_wrapped_item(self.reminders_list, format_reminder_card_text(reminder, summary))
            item.setData(Qt.ItemDataRole.UserRole, reminder.id)

    def _status_label(self, status: str) -> str:
        return status_label(status)

    def refresh_events(self) -> None:
        self.events_list.clear()
        for event in self.store.recent_assistant_events(40):
            self._add_wrapped_item(self.events_list, format_event_card_text(event))

    def refresh_habits(self) -> None:
        self.habits_list.clear()
        for pattern in self.store.learned_patterns():
            confidence = self._confidence_label(pattern.confidence)
            status = self._pattern_status_label(pattern.status)
            item = QListWidgetItem(
                f"{pattern.reason or pattern.key}｜樣本 {pattern.sample_count}｜信心 {confidence}｜{status}｜{pattern.last_updated.strftime('%m/%d %H:%M')}"
            )
            item.setData(Qt.UserRole, pattern.id)
            self.habits_list.addItem(item)
        self.refresh_suggestions()

    def refresh_today(self) -> None:
        if not hasattr(self, "today_list"):
            return
        self.today_list.clear()
        now = datetime.now()
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        sessions = self.store.activity_sessions_between(start, now)
        summary = build_daily_summary_from_sessions(sessions, now.date(), now)
        if not summary:
            events = self.store.usage_events_between(start, now)
            summary = build_daily_summary(events, now.date(), now)
        if summary:
            for bucket in summary:
                self.today_list.addItem(format_summary_bucket(bucket))
        else:
            self.today_list.addItem("今天還沒有可統計的活動")
        inbox_items = self.store.today_inbox_items(now)
        if inbox_items:
            self.today_list.addItem("Today Inbox")
            for reminder in inbox_items:
                self.today_list.addItem(f"- {reminder.text}")

    def refresh_suggestions(self) -> None:
        self.suggestions_list.clear()
        self.suggestion_ids = []
        for row in self.store.pending_suggestions():
            self.suggestion_ids.append(int(row["id"]))
            confidence = self._confidence_label(float(row["confidence"] or 0))
            self.suggestions_list.addItem(f"{row['title']}｜信心 {confidence}｜{row['body']}")

    def _current_state(self) -> CurrentState:
        snapshot = self.current_snapshot or get_foreground_snapshot()
        explicit_intent, explicit_until = self._explicit_intent()
        state = self.current_state_builder.build(
            snapshot,
            self.activity_started_at,
            away=self._is_user_away(snapshot),
            manual_stop=self.manually_stopped_monitoring,
            privacy_mode=self.store.privacy_mode_enabled(),
            current_session=self.current_activity_session,
            explicit_intent=explicit_intent,
            explicit_intent_until=explicit_until,
        )
        self.current_state_builder.save_if_changed(state)
        return state

    def _explicit_intent(self) -> tuple[str | None, datetime | None]:
        intent = self.store.get_setting("explicit_intent", "") or ""
        until_raw = self.store.get_setting("explicit_intent_until", "") or ""
        until = None
        if until_raw:
            try:
                until = datetime.fromisoformat(until_raw)
            except ValueError:
                until = None
        if until and until <= datetime.now():
            self.store.delete_setting("explicit_intent")
            self.store.delete_setting("explicit_intent_until")
            return None, None
        return intent or None, until

    def refresh_advisor(self, auto: bool = False) -> None:
        if getattr(self, '_executing_voice', False):
            return  # Local voice goals/modes must not invoke Advisor/API.
        if not hasattr(self, "understanding_label"):
            return
        now = datetime.now()
        if auto and self.last_advisor_analyzed_at and now - self.last_advisor_analyzed_at < timedelta(seconds=20):
            return
        state = self._current_state()
        self.goal_engine.infer_goal(state)
        goal = self.store.current_goal()
        action = self.advisor_service.analyze(state, goal)
        self.last_advisor_action = action
        self.last_advisor_analyzed_at = now
        self._set_advisor_text(self.understanding_label, f"目前理解：{action.understanding or state.activity_summary}")
        intent_text = {
            "working": "工作",
            "intentional_break": "休息",
            "private_time": "私人時間",
            "away": "離席",
            "returning_to_work": "回到工作",
            "unknown": "未知",
        }.get(state.intent, state.intent)
        source_text = "手動指定" if state.intent_source == "explicit" else "本機推測"
        self._set_advisor_text(self.intent_label, f"目前意圖：{intent_text}（{source_text}）")
        session_duration = self._format_duration(timedelta(seconds=state.session_duration_seconds))
        self._set_advisor_text(self.session_label, f"目前 session：{state.session_category}｜已持續 {session_duration}")
        if goal:
            source = "手動" if goal.source == "manual" else "推測"
            self._set_advisor_text(
                self.goal_label,
                f"目前目標：{goal.text}（{source}，信心 {self._confidence_label(goal.confidence)}）",
            )
            if goal.source == "manual" and not self.goal_input.text().strip():
                self.goal_input.setText(goal.text)
        else:
            self._set_advisor_text(self.goal_label, "目前目標：尚未設定")
        self._set_advisor_text(self.next_action_label, f"下一步建議：{action.action_text}\n原因：{action.reason}")
        level = {"silent": "安靜", "status_only": "狀態列", "popup": "浮窗"}.get(action.intervention_level, action.intervention_level)
        self._set_advisor_text(
            self.advisor_source_label,
            f"{self._advisor_status_text(action)}｜信心：{self._confidence_label(action.confidence)}｜介入：{level}",
        )
        if action.intervention_level == "status_only":
            self._set_advisor_status_notice(action)

    def _advisor_status_text(self, action: NextAction | None = None) -> str:
        action = action or self.last_advisor_action
        settings = self.settings_service.load()
        provider = action.provider_label if action and action.provider_label else advisor_provider_label(
            settings.advisor_provider_preset,
            settings.advisor_endpoint_url,
            settings.advisor_model_name,
        )
        health = advisor_load_health(self.store, settings.advisor_api_enabled)
        health_state = action.health_state if action and action.health_state != "unknown" else health.state
        last_success = health.last_success_at.strftime("%H:%M:%S") if health.last_success_at else ""
        if action and action.source == "ai":
            suffix = f"｜可用｜最近成功 {last_success}" if last_success else "｜可用"
            return f"Advisor：{provider}{suffix}"
        if action and action.source == "ai_unavailable":
            return f"Advisor：{provider}｜{advisor_health_label(health_state)}"
        if not settings.advisor_api_enabled:
            return "Advisor：本機規則"
        return f"Advisor：{provider}｜{advisor_health_label(health_state)}"

    def _advisor_osd_label(self, action: NextAction | None = None) -> str:
        action = action or self.last_advisor_action
        if action and action.source == "ai_unavailable":
            return "AI暫時不可用"
        if action and action.source == "ai":
            return f"AI：{self._short_action_label(action)}"
        return f"規則：{self._short_action_label(action)}"

    def _short_action_label(self, action: NextAction | None = None) -> str:
        if not action:
            return "觀察中"
        text = f"{action.action_text} {action.reason} {action.understanding}".lower()
        if any(token in text for token in ("確認", "question", "需確認", "是否", "要不要")) or action.questions:
            return "需確認"
        if any(token in text for token in ("回到目前目標", "回目標", "return to", "back to")):
            return "回目標"
        if any(token in text for token in ("休息中", "休息", "break")):
            return "休息中"
        if any(token in text for token in ("專注", "focus", "goal-directed", "工作", "繼續目前")):
            return "專注中"
        if any(token in text for token in ("不打擾", "no interruption", "no immediate action", "沉默", "silent")):
            return "暫不打擾"
        return "觀察中"

    def _display_app_name(self, app_name: str, window_title: str = "") -> str:
        raw = " ".join((app_name or "", window_title or "")).lower()
        if "chatgpt" in raw:
            return "ChatGPT"
        if "youtube" in raw:
            return "YouTube"
        if "helldivers" in raw:
            return "Helldivers 2"
        if "telegram" in raw:
            return "Telegram"
        app = (app_name or "未知 App").strip()
        if app.lower().endswith(".exe"):
            app = app[:-4]
        return self._shorten(app, 18)

    def _compact_status_bar_text(self, parts: list[str], max_chars: int = 64) -> str:
        text = "｜".join(part for part in parts if part)
        if len(text) <= max_chars:
            return text
        if len(parts) <= 1:
            return self._shorten(text, max_chars)
        head = "｜".join(parts[:-1])
        budget = max(6, max_chars - len(head) - 1)
        return f"{head}｜{self._shorten(parts[-1], budget)}"

    def _format_osd_duration(self, value: timedelta) -> str:
        total = max(0, int(value.total_seconds()))
        hours = total // 3600
        minutes = (total % 3600) // 60
        seconds = total % 60
        if hours:
            return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        return f"{minutes:02d}:{seconds:02d}"

    def _advisor_osd_prefix(self, action: NextAction | None = None) -> str:
        action = action or self.last_advisor_action
        if action and action.source == "ai":
            return "AI"
        if action and action.source == "ai_unavailable":
            return "AI暫時不可用"
        return "規則"

    def _set_advisor_status_notice(self, action: NextAction) -> None:
        notice = self._advisor_osd_label(action)
        signature = (notice, action.action_text)
        now = datetime.now()
        if (
            signature == self.last_status_notice_signature
            and self.last_status_notice_at
            and now - self.last_status_notice_at < timedelta(minutes=5)
        ):
            return
        self.last_status_notice_signature = signature
        self.last_status_notice_at = now
        self.status_bar_notice = (notice, now + timedelta(minutes=8))

    def set_manual_goal(self) -> None:
        text = self.goal_input.text().strip()
        if not text:
            return
        self.goal_engine.set_manual_goal(text, "current_session")
        self.refresh_advisor(auto=False)

    def clear_manual_goal(self) -> None:
        self.goal_engine.clear_manual_goal("current_session")
        self.goal_input.clear()
        self.refresh_advisor(auto=False)

    def set_intent_break(self) -> None:
        self._set_explicit_intent("intentional_break", timedelta(minutes=45), "已標記：現在休息")

    def set_intent_work(self) -> None:
        self.store.delete_setting("explicit_intent")
        self.store.delete_setting("explicit_intent_until")
        self.status_bar_notice = ("已標記：回到工作", datetime.now() + timedelta(minutes=5))
        if not getattr(self, '_executing_voice', False):
            self.refresh_advisor(auto=False)
        self.update_top_status_bar()

    def set_intent_private_time(self) -> None:
        self._set_explicit_intent("private_time", timedelta(hours=6), "已標記：私人時間")

    def _set_explicit_intent(self, intent: str, duration: timedelta, notice: str) -> None:
        self.store.set_setting("explicit_intent", intent)
        self.store.set_setting("explicit_intent_until", (datetime.now() + duration).isoformat())
        self.status_bar_notice = (notice, datetime.now() + timedelta(minutes=5))
        if not getattr(self, '_executing_voice', False):
            self.refresh_advisor(auto=False)
        self.update_top_status_bar()

    def reject_selected_pattern(self) -> None:
        item = self.habits_list.currentItem()
        if not item:
            return
        pattern_id = item.data(Qt.UserRole)
        if pattern_id is not None:
            self.store.reject_pattern(int(pattern_id))
            self.refresh_habits()

    def reset_selected_pattern(self) -> None:
        item = self.habits_list.currentItem()
        if not item:
            return
        pattern_id = item.data(Qt.UserRole)
        if pattern_id is not None:
            self.store.reset_pattern(int(pattern_id))
            self.refresh_habits()

    def apply_selected_suggestion(self) -> None:
        row = self.suggestions_list.currentRow()
        if row < 0 or row >= len(self.suggestion_ids):
            return
        self.store.resolve_suggestion(self.suggestion_ids[row], "applied")
        QMessageBox.information(self, "已套用建議", "這版先把 Level 3 建議標記為已套用；正式時間/週期仍需要你在提醒設定中確認調整。")
        self.refresh_habits()

    def reject_selected_suggestion(self) -> None:
        row = self.suggestions_list.currentRow()
        if row < 0 or row >= len(self.suggestion_ids):
            return
        self.store.resolve_suggestion(self.suggestion_ids[row], "rejected")
        self.refresh_habits()

    def _confidence_label(self, confidence: float) -> str:
        if confidence >= 0.7:
            return "高"
        if confidence >= 0.35:
            return "中"
        return "低"

    def _pattern_status_label(self, status: str) -> str:
        return {
            "observing": "觀察中",
            "provisional": "AI 推測",
            "confirmed": "正式習慣",
            "rejected": "已停用",
        }.get(status, status)

    def _learning_summary(self, reminder: Reminder) -> str:
        parts: list[str] = []
        if reminder.notification_count:
            parts.append(f"通知 {reminder.notification_count}")
        if reminder.completed_count:
            parts.append(f"完成 {reminder.completed_count}")
        if reminder.snooze_count:
            parts.append("常延後" if reminder.snooze_count >= 2 else f"延後 {reminder.snooze_count}")
        if reminder.ignored_count:
            parts.append(f"忽略 {reminder.ignored_count}")
        return "｜".join(parts) if parts else "尚未學習"

    def _current_mode(self) -> str | None:
        if self.current_snapshot:
            return self.current_snapshot.mode
        return None

    def update_top_status_bar(self) -> None:
        if not self.top_status_visible:
            return
        if self.auto_idle_paused:
            reason = self.last_pause_reason or "離席"
            activity = f"離席｜監督暫停｜{reason}"
        elif self.monitoring:
            snapshot = get_foreground_snapshot()
            self.current_snapshot = snapshot
            if self._should_auto_pause(snapshot):
                self.auto_idle_paused = True
                self.last_pause_reason = "Windows 已鎖定" if is_windows_locked_or_unavailable() else "閒置"
                activity = f"離席｜監督暫停｜{self.last_pause_reason}"
                self._refresh_monitor_controls()
            else:
                if not self.current_activity_session or session_identity(snapshot) != (
                    self.current_activity_session.app_name.lower(),
                    self.current_activity_session.mode,
                    self.current_activity_session.category,
                ):
                    self._update_activity_duration(snapshot)
                mode = snapshot.mode
                app_name = self._display_app_name(snapshot.app_name, snapshot.window_title)
                duration = self._format_osd_duration(datetime.now() - self.activity_started_at)
                activity = f"{mode}｜{app_name}｜{duration}"
        else:
            activity = "提醒服務中｜監督關閉"

        now = datetime.now()
        try:
            next_reminder = self.store.next_pending_reminder_within(TOP_STATUS_REMINDER_LOOKAHEAD, now)
        except sqlite3.OperationalError:
            return
        parts = [activity]
        if self.status_bar_notice:
            notice_text, expires_at = self.status_bar_notice
            if expires_at > now:
                parts.append(notice_text)
            else:
                self.status_bar_notice = None
        if next_reminder:
            minutes_left = max(0, math.ceil((next_reminder.due_at - now).total_seconds() / 60))
            next_text = f"{minutes_left}分鐘後：{self._shorten(next_reminder.text, 10)}"
            cycle = cycle_status(next_reminder.recurrence_rule, next_reminder.due_at)
            if cycle:
                next_text += f"｜{self._shorten(cycle, 8)}"
            parts.append(next_text)

        diagnostic = getattr(self, "rhythm_diagnostic", None)
        if diagnostic and (diagnostic.active is not None or diagnostic.pending):
            parts.insert(0, "音遊診斷：正在記錄" if diagnostic.timer.isActive() else "音遊診斷：等待中")
        self.top_status_bar.set_status_text(
            self._compact_status_bar_text(parts),
            self._status_screen(),
            self.settings.top_status_bar_position,
        )

    def set_auto_idle_pause_enabled(self, enabled: bool) -> None:
        self.auto_idle_pause_enabled = enabled
        self.settings = self.settings_service.update(auto_idle_pause_enabled=enabled)
        if not enabled and self.auto_idle_paused:
            self.auto_idle_paused = False
            self.last_pause_reason = ""
        self._refresh_monitor_controls()
        self.update_top_status_bar()

    def set_auto_start_monitoring_enabled(self, enabled: bool) -> None:
        self.settings = self.settings_service.update(auto_start_monitoring_enabled=enabled)

    def set_idle_threshold_minutes(self, *_args: object) -> None:
        value = self.idle_threshold_combo.currentData()
        if value not in IDLE_THRESHOLD_OPTIONS:
            value = 10
        self.idle_threshold_minutes = int(value)
        self.settings = self.settings_service.update(idle_threshold_minutes=self.idle_threshold_minutes)
        self.update_top_status_bar()

    def set_top_status_bar_visible(self, enabled: bool) -> None:
        self.top_status_visible = enabled
        self.settings = self.settings_service.update(top_status_bar_visible=enabled)
        self.toggle_top_bar_action.setText("隱藏頂部狀態列" if enabled else "顯示頂部狀態列")
        if enabled:
            self.top_status_bar.show()
            self.update_top_status_bar()
        else:
            self.top_status_bar.hide()

    def set_top_status_bar_screen(self, *_args: object) -> None:
        self.settings = self.settings_service.update(
            top_status_bar_screen=str(self.top_status_bar_screen_combo.currentData() or "auto")
        )
        self.update_top_status_bar()

    def set_top_status_bar_position(self, *_args: object) -> None:
        self.settings = self.settings_service.update(
            top_status_bar_position=str(self.top_status_bar_position_combo.currentData() or POSITION_TOP_LEFT)
        )
        self.update_top_status_bar()

    def set_reminder_popup_screen(self, *_args: object) -> None:
        self.settings = self.settings_service.update(
            reminder_popup_screen=str(self.reminder_popup_screen_combo.currentData() or "active")
        )

    def set_privacy_mode_enabled(self, enabled: bool) -> None:
        self.settings = self.settings_service.update(privacy_mode_enabled=enabled)
        if enabled:
            self._voice_generation += 1
            self._voice_job_busy = False
            self.voice_listening_overlay.hide()
            self.status_bar_notice = ("隱私模式：活動記錄與學習暫停", datetime.now() + timedelta(minutes=10))
            self.voice_pipeline.stop()
            if hasattr(self, "voice_wake_timer"):
                self.voice_wake_timer.stop()
        self.refresh_lists()
        self.update_top_status_bar()

    def set_learning_enabled(self, enabled: bool) -> None:
        self.settings = self.settings_service.update(learning_enabled=enabled)
        self.update_top_status_bar()

    def set_notification_learning_enabled(self, enabled: bool) -> None:
        self.settings = self.settings_service.update(notification_listener_beta_enabled=enabled)
        if enabled:
            self.status_bar_notice = ("外部通知學習 Beta 已開啟", datetime.now() + timedelta(minutes=5))
        else:
            self.status_bar_notice = ("外部通知學習已關閉", datetime.now() + timedelta(minutes=5))
        self.update_top_status_bar()

    def set_dead_loop_detection_enabled(self, enabled: bool) -> None:
        self.settings = self.settings_service.update(dead_loop_detection_enabled=enabled)

    def set_startup_enabled(self, enabled: bool) -> None:
        project_root = Path(__file__).resolve().parent.parent
        ok = set_startup_enabled(enabled, project_root)
        self.settings = self.settings_service.update(startup_enabled=enabled and ok)
        if not ok:
            QMessageBox.warning(self, "無法設定開機啟動", "目前環境無法寫入 Windows 啟動設定；請用 start_assistant.vbs 或打包版捷徑。")
            self.startup_checkbox.setChecked(False)

    def set_quick_add_enabled(self, enabled: bool) -> None:
        self.settings = self.settings_service.update(quick_add_enabled=enabled)
        self.configure_quick_add_hotkey(show_errors=True)

    def set_voice_command_enabled(self, enabled: bool) -> None:
        if not enabled:
            self._voice_generation += 1
            self._voice_job_busy = False
            self.voice_listening_overlay.hide()
            self.voice_pipeline.stop()
        if enabled and not self.voice_microphones_available:
            self.refresh_voice_microphones(show_notice=True)
        if enabled and not self.voice_microphones_available:
            self.settings = self.settings_service.update(voice_command_enabled=False)
            self.voice_command_checkbox.setChecked(False)
            self._command_notice("未偵測到可用麥克風；文字 Command Bar 可照常使用")
            self._sync_voice_pipeline()
            return
        if enabled and not self.settings.assistant_name.strip():
            name, ok = QInputDialog.getText(self, "設定助理名稱", "第一次啟用語音助理，請設定喚醒名稱：", text=DEFAULT_ASSISTANT_NAME)
            if not ok or not name.strip():
                self.voice_command_checkbox.setChecked(False)
                return
            if not self._confirm_risky_assistant_name(name):
                self.voice_command_checkbox.setChecked(False)
                return
            old_template_id = self.settings.voice_wake_template_id
            if old_template_id:
                self.voice_pipeline.clear_wake_template()
            self.settings = self.settings_service.update(assistant_name=name.strip(), voice_wake_template_id="")
            if hasattr(self, "assistant_name_input"):
                self.assistant_name_input.setText(name.strip())
        if enabled and self.voice_pipeline.readiness().state in {"missing_components", "error_dependency"}:
            answer = QMessageBox.question(
                self,
                "安裝語音元件",
                "首次啟用需下載語音元件，是否現在安裝？\n\n文字 Command Bar 不受影響。",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if answer == QMessageBox.StandardButton.Yes:
                self.install_voice_components_now()
        self.settings = self.settings_service.update(voice_command_enabled=enabled)
        self._sync_voice_pipeline()

    def set_assistant_name(self) -> None:
        name = self.assistant_name_input.text().strip()
        if name and not self._confirm_risky_assistant_name(name):
            self.assistant_name_input.setText(self.settings.assistant_name)
            return
        changes = {"assistant_name": name}
        if name != self.settings.assistant_name:
            if self.settings.voice_wake_template_id:
                self.voice_pipeline.clear_wake_template()
            changes["voice_wake_template_id"] = ""
        self.settings = self.settings_service.update(**changes)
        self._sync_voice_pipeline()

    def _confirm_risky_assistant_name(self, name: str) -> bool:
        validation = validate_assistant_name(name)
        if not validation.risky:
            return True
        answer = QMessageBox.question(
            self,
            "名稱容易誤觸",
            validation.message,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def set_voice_microphone_id(self, *_args: object) -> None:
        if not hasattr(self, "voice_microphone_combo"):
            return
        microphone_id = str(self.voice_microphone_combo.currentData() or SYSTEM_DEFAULT_MICROPHONE_ID)
        if microphone_id == self.settings.voice_microphone_id:
            return
        self.settings = self.settings_service.update(voice_microphone_id=microphone_id)
        self._sync_voice_pipeline()

    def refresh_voice_microphones(self, show_notice: bool = False) -> None:
        if not hasattr(self, "voice_microphone_combo"):
            return
        devices = self.voice_device_provider.list_input_devices()
        current_id = recoverable_sounddevice_identifier(self.settings.voice_microphone_id, devices)
        self.voice_microphones_available = bool(devices)
        if self.settings.voice_microphone_id and current_id == SYSTEM_DEFAULT_MICROPHONE_ID:
            self.settings = self.settings_service.update(voice_microphone_id=SYSTEM_DEFAULT_MICROPHONE_ID)
            if show_notice or not self.voice_missing_device_notice_shown:
                self._command_notice("原本選擇的麥克風已消失，已改用系統預設麥克風")
                self.voice_missing_device_notice_shown = True
        elif current_id != self.settings.voice_microphone_id:
            self.settings = self.settings_service.update(voice_microphone_id=current_id)

        self.voice_microphone_combo.blockSignals(True)
        self.voice_microphone_combo.clear()
        if devices:
            self.voice_microphone_combo.addItem(SYSTEM_DEFAULT_MICROPHONE_LABEL, SYSTEM_DEFAULT_MICROPHONE_ID)
            for device in devices:
                self.voice_microphone_combo.addItem(device.display_name, device.identifier)
            index = self.voice_microphone_combo.findData(self.settings.voice_microphone_id)
            self.voice_microphone_combo.setCurrentIndex(max(0, index))
            self.voice_microphone_combo.setEnabled(True)
        else:
            self.voice_microphone_combo.addItem(NO_MICROPHONE_LABEL, SYSTEM_DEFAULT_MICROPHONE_ID)
            self.voice_microphone_combo.setEnabled(False)
            if self.settings.voice_command_enabled:
                self.settings = self.settings_service.update(voice_command_enabled=False, voice_microphone_id=SYSTEM_DEFAULT_MICROPHONE_ID)
                if hasattr(self, "voice_command_checkbox"):
                    self.voice_command_checkbox.setChecked(False)
            if show_notice:
                self._command_notice("未偵測到可用麥克風；文字 Command Bar 可照常使用")
        self.voice_microphone_combo.blockSignals(False)
        if hasattr(self, "voice_command_checkbox"):
            self.voice_command_checkbox.setEnabled(self.voice_microphones_available)
        self._sync_voice_pipeline()

    def set_wake_sound_enabled(self, enabled: bool) -> None:
        self.settings = self.settings_service.update(wake_sound_enabled=enabled)
        self._sync_voice_pipeline()

    def set_voice_wake_display(self, *_args: object) -> None:
        self.settings = self.settings_service.update(voice_wake_display=str(self.voice_wake_display_combo.currentData() or "osd"))
        self._sync_voice_pipeline()

    def set_voice_microphone_gain(self, *_args: object) -> None:
        self.settings = self.settings_service.update(voice_microphone_gain=str(self.voice_microphone_gain_combo.currentData() or "auto"))
        self._sync_voice_pipeline()

    def set_voice_wake_sensitivity(self, *_args: object) -> None:
        self.settings = self.settings_service.update(
            voice_wake_sensitivity=str(self.voice_wake_sensitivity_combo.currentData() or "standard")
        )
        self._sync_voice_pipeline()

    def test_voice_microphone_now(self) -> None:
        self._sync_voice_pipeline()
        if hasattr(self, "voice_mic_test_button"):
            self.voice_mic_test_button.setEnabled(False)
        try:
            previous_active = self.voice_pipeline.active
            if previous_active:
                self.voice_pipeline.stop()
            self.voice_pipeline.reload(settings_from_assistant_settings(self.settings))
            self._voice_notice("測試麥克風中")
            result = self.voice_pipeline.test_microphone(2.0)
            message = "麥克風：" + self.voice_microphone_combo.currentText() + "\n" + result.message
            self._voice_notice(message)
            QMessageBox.information(self, "測試麥克風", message)
        except Exception:
            import logging
            logging.getLogger(__name__).exception("Microphone diagnostic failed")
            self._voice_notice("選定麥克風無法開啟或讀取，請檢查裝置與權限")
        finally:
            if hasattr(self, "voice_mic_test_button"):
                self.voice_mic_test_button.setEnabled(True)
            self._sync_voice_pipeline()

    def calibrate_voice_sensitivity(self):
        if self._voice_job_busy or self.store.privacy_mode_enabled():
            return
        provider = self.voice_pipeline.wake_provider
        if self.voice_pipeline.readiness().state != 'ready' or not hasattr(provider, 'calibrate'):
            self._voice_notice('請先啟用語音助理並等候待機。')
            return
        self._voice_notice('請在平常距離，用正常音量說一次助理名稱（6 秒）。')
        self._start_voice_job('calibration', provider.calibrate)

    def test_voice_wake_word_now(self) -> None:
        if self._voice_job_busy or self.settings.privacy_mode_enabled:
            return
        state = self.voice_pipeline.readiness()
        if state.state != "ready":
            self._voice_notice(state.message)
            return
        self.voice_wake_timer.stop()
        self.voice_wake_test_button.setEnabled(False)
        self._voice_notice("請在 8 秒內喊名字")
        self._start_voice_job("test", lambda: self.voice_pipeline.test_wake_word(8.0))

    def _start_voice_job(self, kind, action):
        worker = getattr(self, '_voice_worker', None)
        if worker is not None and worker.is_alive():
            return
        self._voice_generation += 1
        self._voice_job_busy = True
        if kind == 'command':
            self._ensure_entity_navigator()
        generation = self._voice_generation
        def run():
            try:
                result = action()
            except Exception as exc:
                result = exc
            try:
                self.voice_signals.completed.emit(kind, result, generation)
            except RuntimeError:
                pass  # The window was closed while optional STT was finishing.
        self._voice_worker = threading.Thread(target=run, name="voice-" + kind, daemon=True)
        self._voice_worker.start()

    def _finish_voice_job(self, kind, result, generation):
        if generation != self._voice_generation:
            return  # A watchdog/new session owns the mic and UI now.
        try:
            self.voice_listening_overlay.hide()
            if self.settings.privacy_mode_enabled:
                return
            if isinstance(result, Exception):
                self._voice_notice(f"語音元件異常：{result}")
            elif kind == "calibration":
                QMessageBox.information(self, "正常說話音量", result)
            elif kind == "test":
                QMessageBox.information(self, "測試喚醒詞", result.message)
            elif self.settings.voice_command_enabled:
                if not result and self.voice_pipeline.diagnostics.snapshot().get('discard_reason'):
                    self._voice_notice('沒有聽到指令')
                else:
                    self.voice_pipeline.handle_wake(spoken_text=result)
        except Exception as exc:
            self._voice_notice(f'指令執行失敗：{exc}')
        finally:
            self._voice_job_busy = False
            self.voice_pipeline.cleanup()
            if self.voice_pipeline.runtime_state.state != 'error_rearm':
                self._sync_voice_pipeline()
            self._update_voice_status_label()

    def install_voice_components_now(self) -> None:
        if self.voice_pipeline.runtime_state.state == 'error_rearm':
            self.voice_pipeline.stop()
            self._sync_voice_pipeline()
            return
        if self.voice_pipeline.readiness().state == "restart_required":
            import sys
            subprocess.Popen([sys.executable] if getattr(sys, "frozen", False) else [sys.executable, str(Path(__file__).resolve().parent.parent / "run.py")],
                             cwd=str(Path(__file__).resolve().parent.parent),
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self.exiting = True
            self.close()
            QApplication.quit()
            return
        if self.voice_install_thread and self.voice_install_thread.isRunning():
            return
        self.voice_wake_timer.stop()
        self.voice_pipeline.stop()
        self.voice_pipeline.runtime_state = WakeRuntimeState("installing", "正在安裝語音元件…")
        self._update_voice_status_label()
        self.voice_components_button.setEnabled(False)
        self.voice_install_thread = QThread(self)
        self.voice_install_worker = VoiceInstallWorker(Path(__file__).resolve().parent.parent)
        self.voice_install_worker.moveToThread(self.voice_install_thread)
        self.voice_install_thread.started.connect(self.voice_install_worker.run)
        self.voice_install_worker.progress.connect(self._voice_install_progress)
        self.voice_install_worker.finished.connect(self._finish_voice_components_install)
        self.voice_install_worker.finished.connect(self.voice_install_thread.quit)
        self.voice_install_worker.finished.connect(self.voice_install_worker.deleteLater)
        self.voice_install_thread.finished.connect(self.voice_install_thread.deleteLater)
        self.voice_install_thread.start()

    def _voice_install_progress(self, state):
        self.voice_pipeline.runtime_state = state
        self._update_voice_status_label()

    def _finish_voice_components_install(self, result: VoiceInstallState) -> None:
        self.voice_install_thread = None
        self.voice_install_worker = None
        self.voice_pipeline.runtime_state = WakeRuntimeState(result.state, result.message)
        if result.state == "verified":
            self.voice_pipeline.stop()
            from .kws import StreamingKwsWakeWordProvider
            from .voice_command import FasterWhisperSTTProvider
            self.voice_pipeline.stt_provider = FasterWhisperSTTProvider(device=self.settings.voice_stt_device, status_callback=self.voice_pipeline.status_callback)
            self.voice_pipeline.wake_provider = StreamingKwsWakeWordProvider(status_callback=self.voice_pipeline.status_callback)
            self.voice_pipeline.reload(settings_from_assistant_settings(self.settings))
            self._sync_voice_pipeline()
        else:
            self._command_notice(self.voice_pipeline.readiness().message)
        self._update_voice_status_label()

    def _sync_voice_pipeline(self) -> None:
        if self.voice_pipeline.runtime_state.state in {"installing", "restart_required"} or (self.voice_pipeline.runtime_state.state == "verifying" and not self.voice_pipeline.active):
            self._update_voice_status_label()
            return
        if getattr(self, "_voice_job_busy", False):
            if settings_from_assistant_settings(self.settings) != self.voice_pipeline.settings:
                self._voice_generation += 1
                self._voice_job_busy = False
                self.voice_pipeline.stop()
            return
        self.voice_pipeline.reload(settings_from_assistant_settings(self.settings))
        microphones_available = True  # Service probes the selected mic independently of settings UI discovery.
        if self.settings.voice_command_enabled and microphones_available and not self.store.privacy_mode_enabled():
            self._ensure_entity_navigator()
            self.voice_pipeline.start()
            if self.voice_pipeline.active:
                self.voice_wake_timer.start()
            elif hasattr(self, "voice_wake_timer"):
                self.voice_wake_timer.stop()
        else:
            self.voice_pipeline.stop()
            if hasattr(self, "voice_wake_timer"):
                self.voice_wake_timer.stop()
        if hasattr(self, "wake_aliases_label"):
            self.wake_aliases_label.setText(self._wake_aliases_summary())
        self._update_voice_status_label()

    def _voice_service_tick(self) -> None:
        if self.voice_pipeline.watchdog():
            self._voice_generation += 1
            self._voice_job_busy = False
            self.voice_listening_overlay.hide()
            if self.voice_pipeline.active:
                self.voice_wake_timer.start()
        self._update_voice_status_label()

    def _update_voice_status_label(self) -> None:
        self.voice_pipeline.diagnostics.snapshot()  # Expire short-lived text during regular UI ticks.
        if self.settings.privacy_mode_enabled:
            self.voice_pipeline.diagnostics.clear()
        state = self.voice_pipeline.readiness()
        if hasattr(self, 'tray_icon'):
            self.tray_icon.setToolTip('Private Assistant AI｜' + state.message)
        if hasattr(self, "voice_command_label"):
            self.voice_command_label.setText(state.message)
            checks = getattr(self.voice_pipeline.wake_provider, "checks", {})
            self.voice_command_label.setToolTip("語音診斷\n" + "\n".join(
                name + ": " + ("✅" if checks.get(name) else "❌")
                for name in ("Dependencies", "KWS", "Mic", "Pipeline")) +
                "\nSTT 模型：喚醒後按需載入；不保存原始音訊")
        if hasattr(self, "voice_mic_test_button"):
            self.voice_mic_test_button.setEnabled(bool(getattr(self, "voice_microphones_available", False)) and not self._voice_job_busy and not self.settings.privacy_mode_enabled)
        if hasattr(self, "voice_calibrate_button"):
            self.voice_calibrate_button.setEnabled(state.state == "ready" and not self._voice_job_busy and not self.settings.privacy_mode_enabled)
        if hasattr(self, "voice_wake_test_button"):
            self.voice_wake_test_button.setEnabled(state.state == "ready" and not self._voice_job_busy and not self.settings.privacy_mode_enabled)
        if hasattr(self, "voice_components_button"):
            busy = state.state in {"installing", "verifying", "loading_models", "opening_microphone"}
            self.voice_components_button.setEnabled(not busy)
            self.voice_components_button.setText("重新啟動助理" if state.state == "restart_required" else "處理中…" if busy else "重試語音元件" if state.state.startswith("error") else "安裝／驗證語音元件")

    def _voice_wake_tick(self) -> None:
        if self._voice_job_busy:
            self._update_voice_status_label()
            return
        detected = self.voice_pipeline.idle_tick()
        self._update_voice_status_label()
        if detected:
            dialog = getattr(self, '_voice_retry_dialog', None)
            if dialog is not None:
                self._voice_retry_dialog = None
                try:
                    dialog.close()
                    dialog.deleteLater()
                except RuntimeError:
                    pass
            self.voice_listening_overlay.setText(f"{self.settings.assistant_name}正在聽…")
            self.voice_listening_overlay.adjustSize()
            self.voice_listening_overlay.show()
            if self.settings.wake_sound_enabled:
                QApplication.beep()
            self._start_voice_job("command", self.voice_pipeline.transcribe_after_wake)

    def _voice_notice(self, text: str) -> None:
        self.status_bar_notice = (text, datetime.now() + timedelta(minutes=2))
        self.update_top_status_bar()

    def set_ai_handoff_auto_send(self, enabled: bool) -> None:
        self.settings = self.settings_service.update(ai_handoff_auto_send=enabled)

    def set_quick_add_hotkey(self) -> None:
        hotkey = self.quick_add_hotkey_input.text().strip() or "Ctrl+Alt+A"
        self.settings = self.settings_service.update(quick_add_hotkey=hotkey)
        self.configure_quick_add_hotkey(show_errors=True)

    def configure_quick_add_hotkey(self, show_errors: bool = False) -> None:
        if not self.settings.quick_add_enabled:
            self.global_hotkey.unregister()
            self.quick_add_hotkey_registered = False
            if hasattr(self, "quick_add_status_label"):
                self.quick_add_status_label.setText("助理指令快捷鍵已停用")
            return
        result = self.global_hotkey.register(int(self.winId()), self.settings.quick_add_hotkey)
        self.quick_add_hotkey_registered = result.ok
        if hasattr(self, "quick_add_status_label"):
            self.quick_add_status_label.setText(result.message)
        if show_errors and not result.ok:
            QMessageBox.warning(self, "快捷鍵無法使用", result.message)

    def nativeEvent(self, event_type: bytes | bytearray | str, message: int) -> tuple[bool, int]:
        try:
            import ctypes
            from ctypes import wintypes

            msg = wintypes.MSG.from_address(int(message))
            if msg.message == WM_HOTKEY and int(msg.wParam) == self.global_hotkey.hotkey_id:
                self.show_quick_add_dialog()
                return True, 0
        except Exception:
            pass
        return super().nativeEvent(event_type, message)

    def show_quick_add_dialog(self) -> None:
        dialog = AssistantCommandDialog(self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            text = dialog.text()
            if text:
                self.execute_assistant_command(text)
        dialog.deleteLater()

    def execute_voice_command(self, text):
        self._executing_voice = True
        try:
            ok = self.execute_assistant_command(text)
            self.voice_pipeline.diagnostics.update(result=getattr(self, '_voice_result', '') or ('已完成' if ok else '未執行'))
            self.voice_listening_overlay.setText(self.voice_pipeline.diagnostics.snapshot().get('result', ''))
            self.voice_listening_overlay.adjustSize()
            self.voice_listening_overlay.show()
            QTimer.singleShot(1500, self.voice_listening_overlay.hide)
            return ok
        finally:
            self._executing_voice = False
            self._voice_result = ''

    def show_voice_diagnostics(self):
        import json
        data = self.voice_pipeline.diagnostics.snapshot()
        dialog = QMessageBox(self)
        dialog.setWindowTitle('語音診斷：最近一次指令')
        dialog.setText(('聽到：' + data.get('raw', '')[:120] + '\n理解：' + {'activate_or_launch_app': '開啟程式／遊戲／檔案', 'open_folder': '開啟資料夾', 'unknown': '尚未聽清楚'}.get(data.get('intent'), data.get('intent', '')) +
                        '\n人聲：' + str(data.get('speech_detected', '未知')) + '\nSTT 信心：' + str(data.get('stt_confidence')) + '\n忽略原因：' + data.get('discard_reason', '') + '\n最後動作：' + data.get('final_action', '') + '\n解析目標：' + data.get('target', '') + ('\n辨識名稱已修正為：' + data['corrected_target'] if data.get('corrected_target') else '') + '\n結果：' + data.get('result', '') + '\n耗時：' + str(round(data.get('latency_ms', {}).get('total', 0) / 1000, 2)) + ' 秒') if data else '尚無指令（文字診斷於 10 分鐘後清除）')
        dialog.setDetailedText(json.dumps(data, ensure_ascii=False, indent=2))
        clear = dialog.addButton('清除', QMessageBox.ButtonRole.DestructiveRole)
        dialog.addButton('關閉', QMessageBox.ButtonRole.RejectRole)
        dialog.exec()
        if dialog.clickedButton() == clear:
            self.voice_pipeline.diagnostics.clear()

    def _voice_retry(self, message):
        self._voice_result = message
        dialog = QMessageBox(self)
        dialog.setWindowTitle('語音指令')
        heard = self.voice_pipeline.diagnostics.snapshot().get('raw', '')[:120]
        dialog.setText(message + ('\n聽到：「' + heard + '」' if heard else ''))
        retry = dialog.addButton('重說', QMessageBox.ButtonRole.AcceptRole)
        dialog.addButton('取消', QMessageBox.ButtonRole.RejectRole)
        def finished(_result):
            if self._voice_retry_dialog is not dialog:
                return
            self._voice_retry_dialog = None
            self._voice_generation += 1
            self.voice_pipeline.cancel()
            self._voice_job_busy = False
            if dialog.clickedButton() == retry:
                self._start_voice_job('command', self.voice_pipeline.transcribe_retry)
            dialog.deleteLater()
        dialog.finished.connect(finished)
        self._voice_retry_dialog = dialog
        dialog.setModal(False)
        dialog.show()
        return False

    def execute_assistant_command(self, text: str) -> bool:
        from .reminder_operations import parse_operation
        operation = parse_operation(text)
        if operation:
            return self._execute_reminder_operation(operation)
        from .voice_text import route_voice_command
        router = route_voice_command if getattr(self, '_executing_voice', False) else route_assistant_command
        routed = router(
            text,
            default_ai_provider=self.settings.ai_provider,
            active_hour_scores=self.store.active_hour_scores(),
            user_is_active=not self._is_user_away(self.current_snapshot) if self.current_snapshot else True,
            auto_send_ai=self.settings.ai_handoff_auto_send,
        )
        voice = getattr(self, '_executing_voice', False)
        if voice:
            self.voice_listening_overlay.setText('正在處理：' + text[:80])
            self.voice_listening_overlay.adjustSize()
            self.voice_listening_overlay.show()
            self.voice_listening_overlay.repaint()
            self.voice_pipeline.diagnostics.update(intent=routed.intent, confidence=routed.confidence, target=routed.target)
            self.voice_pipeline.diagnostics.mark('intent_done')
        if routed.intent in {"reminder_delete", "reminder_update", "reminder_undo"}:
            return self._execute_reminder_operation(routed.payload)
        if routed.intent == "unknown":
            if voice:
                return self._voice_retry('剛剛沒有聽清楚，請再說一次。' if not text else '你可以再說一次要我做什麼。')
            QMessageBox.information(self, "需要確認", routed.confirmation_summary or "我還不確定這句要做什麼。")
            return False
        if voice and routed.intent == 'activate_or_launch_app' and not routed.target:
            return self._voice_retry('你想打開什麼？')
        if routed.risk.requires_confirmation:
            answer = QMessageBox.question(
                self,
                "確認助理動作",
                routed.risk.reason,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return False
        if routed.intent == "reminder" and routed.parsed_reminder:
            return self._add_parsed_reminder(routed.parsed_reminder)
        if routed.intent in {"activate_or_launch_app", "launch_app"}:
            return self._execute_launch_app(routed)
        if routed.intent == "open_folder":
            return self._execute_open_folder(routed)
        if routed.intent == "open_url":
            url = str((routed.payload or {}).get("url") or routed.target)
            opened = QDesktopServices.openUrl(QUrl(url))
            self._command_notice("已開啟網址" if opened else f"無法開啟：{url}")
            return opened
        if routed.intent == "ask_ai":
            return self._execute_ask_ai(routed)
        if routed.intent == "set_goal":
            goal = str((routed.payload or {}).get("goal") or routed.target).strip()
            if not goal:
                return False
            self.goal_engine.set_manual_goal(goal, "current_session")
            if hasattr(self, "goal_input"):
                self.goal_input.setText(goal)
            if not voice:
                self.refresh_advisor(auto=False)
            self._command_notice(f"已設定目標：{self._shorten(goal, 18)}")
            return True
        if routed.intent == "set_mode":
            return self._execute_set_mode(routed)
        return False

    def _selected_reminder_operation(self, action):
        if action == 'undo':
            return self._execute_reminder_operation(dict(action=action))
        item = self.reminders_list.currentItem()
        if item is None:
            self._command_notice('請先選擇一筆提醒。')
            return False
        reminder = self.store.get_reminder(item.data(Qt.ItemDataRole.UserRole))
        return self._execute_reminder_operation(dict(action=action), selected=reminder)

    def _execute_reminder_operation(self, operation, selected=None):
        import json
        from .reminder_operations import operation_candidates, parse_replacement
        action = operation['action']
        try:
            if action == 'undo':
                event = self.store.last_reminder_operation()
                if not event:
                    self._command_notice('沒有可復原的最近操作。')
                    return False
                before = json.loads(event['before_json'])
                summary = f"復原上次操作：{before['text']}｜{before['due_at']}？"
                if QMessageBox.question(self, '確認復原', summary,
                        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                        QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
                    return False
                changed = self.store.undo_reminder_operation(event['id'])
            else:
                candidates = [selected] if selected is not None else operation_candidates(self.store, operation)
                if not candidates:
                    self._command_notice('找不到符合的提醒；「剛剛」限建立後 60 秒內，超過請指定提醒標題或從清單選取。')
                    return False
                reminder = candidates[0]
                if len(candidates) > 1:
                    labels = [f"{r.text}｜{r.due_at:%Y-%m-%d %H:%M}｜#{r.id}" for r in candidates]
                    choice, accepted = QInputDialog.getItem(self, '選擇提醒', '找到多筆，請選擇要操作的一筆：', labels, 0, False)
                    if not accepted:
                        return False
                    reminder = candidates[labels.index(choice)]
                due = None
                if action == 'update':
                    new_time = operation.get('new_time', '')
                    if not new_time:
                        new_time, accepted = QInputDialog.getText(self, '修改提醒時間',
                            f'{reminder.text}｜{reminder.due_at:%Y-%m-%d %H:%M}\n新的時間（例如凌晨一點）：')
                        if not accepted or not new_time.strip():
                            return False
                    parsed = parse_replacement(reminder, new_time)
                    parsed = self._confirm_ambiguous_time_if_needed(parsed)
                    if parsed is None:
                        return False
                    due = parsed.due_at
                    if reminder.recurrence_rule:
                        from .reminders import next_occurrence
                        rule = json.loads(reminder.recurrence_rule)
                        rule['time'] = due.strftime('%H:%M')
                        due = next_occurrence(json.dumps(rule), due - timedelta(microseconds=1))
                        if due is None:
                            raise ValueError('這個週期已結束，沒有可修改的下一次提醒。')
                summary = f"{reminder.text}｜{reminder.due_at:%Y-%m-%d %H:%M}"
                summary = ('修改：' + summary + f' → {due:%Y-%m-%d %H:%M}' + ('（保留週期）' if reminder.recurrence_rule else '')) if due else '刪除：' + summary
                # Explicit cancellation of the sole, freshly created item is the quick Undo gesture.
                quick_cancel = action == 'delete' and operation.get('recent') and selected is None
                if not quick_cancel and QMessageBox.question(self, '確認提醒操作', summary,
                        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                        QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
                    return False
                changed = (self.store.update_reminder(reminder.id, due, expected=reminder) if due
                           else self.store.delete_reminder(reminder.id, expected=reminder))
            self.deferred_reminders.pop(changed.id, None)
            popup = self.active_popups.pop(changed.id, None)
            if popup:
                popup.close()
            self.refresh_lists()
            self._command_notice(('已復原' if action == 'undo' else '已修改' if action == 'update' else '已取消') +
                                 f'：{changed.text}｜{changed.due_at:%Y-%m-%d %H:%M}' +
                                 ('。可說「復原剛剛的操作」。' if action != 'undo' else ''))
            return True
        except ValueError as exc:
            self._command_notice(str(exc))
            return False

    def _ensure_entity_navigator(self):
        from .entity_navigation import EntityNavigator
        config = (self.settings.app_aliases, self.settings.file_index_enabled, self.settings.file_index_roots)
        if getattr(self, '_entity_config', None) != config:
            roots = [Path.home() / name for name in ('Desktop', 'Documents', 'Downloads')]
            roots.extend(Path(p) for p in self.settings.file_index_roots)
            if hasattr(self, 'entity_navigator'):
                self.entity_navigator.games.close()
            self.entity_navigator = EntityNavigator(self._app_alias_map(), roots, self.settings.file_index_enabled)
            self._entity_config = config

    def _execute_launch_app(self, routed: AssistantIntent) -> bool:
        query = str((routed.payload or {}).get("query") or routed.target)
        self._ensure_entity_navigator()
        navigator = self.entity_navigator
        context = self.goal_input.text() if hasattr(self, 'goal_input') else ''
        result = navigator.resolve(query, context=context)
        if getattr(self, '_executing_voice', False):
            self.voice_pipeline.diagnostics.mark('resolver_done')
        if getattr(self, '_executing_voice', False):
            self.voice_pipeline.diagnostics.update(candidates=[dict(name=r.entity.name, kind=r.entity.kind, confidence=r.confidence, reason=r.reason) for r in result.candidates], resolver='NameResolver/GameResolver/FileResolver')
        entity = result.best
        confirmed = False
        if result.needs_confirmation and result.candidates:
            labels = [f"{r.entity.name} ({('Steam' if r.entity.provider == 'steam' else r.entity.provider) or r.entity.kind})｜{r.entity.target}" for r in result.candidates]
            choice, accepted = QInputDialog.getItem(self, '選擇要開啟的項目',
                ('聽到：「' + self.voice_pipeline.diagnostics.snapshot().get('raw', '')[:120] + '」\n' if getattr(self, '_executing_voice', False) else '') + '你是要開哪一個？選擇後會記住這個稱呼。', labels, 0, False)
            if not accepted:
                return False
            entity = result.candidates[labels.index(choice)].entity
            confirmed = True
        if entity is None and getattr(self, '_executing_voice', False):
            return self._voice_retry('我猜你是想打開程式，但沒聽清楚名稱；找不到相近的程式、遊戲或檔案。')
        if entity is None:
            QMessageBox.information(self, '找不到項目', '找不到相近的 App、遊戲或檔案。可在「名稱 / 別名」管理稱呼與檔案索引。')
            return False
        if confirmed:
            navigator.confirm(query, entity)
        if getattr(self, '_executing_voice', False):
            self.voice_pipeline.diagnostics.mark('action_start')
        # Poll the action worker through Qt so verification never blocks UI/mic timers.
        from PySide6.QtCore import QEventLoop
        done = threading.Event()
        outcome = []
        def execute():
            try:
                outcome.append(navigator.execute(entity))
            except Exception as exc:
                outcome.append(exc)
            finally:
                done.set()
        threading.Thread(target=execute, daemon=True, name='local-app-action').start()
        loop = QEventLoop(self)
        timer = QTimer(self)
        timer.timeout.connect(lambda: loop.quit() if done.is_set() else None)
        timer.start(15)
        if not done.is_set():
            loop.exec()
        timer.stop()
        timer.deleteLater()
        if isinstance(outcome[0], Exception):
            raise outcome[0]
        ok = outcome[0]
        if getattr(self, '_executing_voice', False):
            self.voice_pipeline.diagnostics.mark('action_done')
            self.voice_pipeline.diagnostics.update(final_action=str(navigator.last_action))
        if getattr(self, '_executing_voice', False):
            self._voice_result = (('已切換到 ' if navigator.last_action == 'activated' else '已開啟 ') if ok else '無法開啟 ') + entity.name
            if isinstance(navigator.last_message, str) and navigator.last_message:
                self._voice_result = navigator.last_message
            self.voice_pipeline.diagnostics.update(target=entity.name + (f' ({entity.provider})' if entity.provider else ''), entity_type=entity.kind, corrected_target=entity.name if query.casefold() != entity.name.casefold() else '')
        message = navigator.last_message if isinstance(navigator.last_message, str) and navigator.last_message else (f'已開啟或切換到 {entity.name}' if ok else f'無法開啟 {entity.name}；請確認檔案或啟動器已就緒')
        self._command_notice(message)
        return ok

    def _execute_open_folder(self, routed: AssistantIntent) -> bool:
        resolver = AppResolver(self._app_alias_map(), Path(__file__).resolve().parent.parent)
        result = resolver.resolve_folder(str((routed.payload or {}).get("query") or routed.target))
        if getattr(self, '_executing_voice', False):
            self.voice_pipeline.diagnostics.mark('resolver_done')
            self.voice_pipeline.diagnostics.update(resolver='folder_alias')
        candidate = self._select_resolve_candidate(result.candidates, result.message) if result.needs_confirmation else result.best
        if not candidate:
            QMessageBox.information(self, "找不到資料夾", result.message)
            return False
        if getattr(self, '_executing_voice', False):
            self.voice_pipeline.diagnostics.mark('action_start')
        ok = self._open_resolved_candidate(candidate, f"已開啟 {routed.target}")
        if getattr(self, '_executing_voice', False):
            self.voice_pipeline.diagnostics.mark('action_done')
            self.voice_pipeline.diagnostics.update(final_action='open_folder' if ok else 'failed')
        return ok

    def _execute_ask_ai(self, routed: AssistantIntent) -> bool:
        payload = routed.payload or {}
        provider = str(payload.get("provider") or self.settings.ai_provider or "chatgpt")
        url = str(payload.get("url") or PROVIDER_URLS.get(provider, PROVIDER_URLS["chatgpt"]))
        prompt = str(payload.get("prompt") or routed.target).strip()
        handoff = AIHandoffProvider(provider, url)
        handoff.handoff(prompt, copy_text=QApplication.clipboard().setText, open_browser=True)
        if self.settings.ai_handoff_auto_send:
            self._command_notice("已複製問題並開啟 AI；自動送出仍需人工確認")
        else:
            self._command_notice(f"已複製問題並開啟 {provider_label(provider)}")
        return True

    def _execute_set_mode(self, routed: AssistantIntent) -> bool:
        payload = routed.payload or {}
        mode = str(payload.get("mode") or routed.target)
        minutes = payload.get("minutes")
        if mode == "work":
            self.set_intent_work()
            return True
        if mode == "private_time":
            self.set_intent_private_time()
            return True
        if mode == "intentional_break":
            duration = timedelta(minutes=int(minutes)) if isinstance(minutes, int) and minutes > 0 else timedelta(minutes=45)
            self._set_explicit_intent("intentional_break", duration, f"已標記：休息 {int(duration.total_seconds() // 60)} 分鐘")
            return True
        return False

    def _open_resolved_candidate(self, candidate: ResolveCandidate, notice: str) -> bool:
        path = candidate.path
        try:
            if path.lower().endswith(".lnk") or Path(path).is_dir():
                os.startfile(path)  # type: ignore[attr-defined]
            else:
                subprocess.Popen([path], close_fds=True)
        except OSError as exc:
            QMessageBox.warning(self, "無法開啟", str(exc))
            return False
        self._remember_app_alias(candidate.name, path)
        self._command_notice(notice)
        return True

    def _select_resolve_candidate(self, candidates: tuple[ResolveCandidate, ...], message: str) -> ResolveCandidate | None:
        labels = [f"{candidate.name}｜{candidate.path}" for candidate in candidates]
        if not labels:
            return None
        choice, ok = QInputDialog.getItem(self, "選擇要開啟的項目", message or "找到多個候選，請選擇", labels, 0, False)
        if not ok:
            return None
        index = labels.index(choice)
        return candidates[index]

    def _command_notice(self, text: str) -> None:
        self.status_bar_notice = (text, datetime.now() + timedelta(minutes=5))
        self.tray_icon.showMessage("私人助理", text, QSystemTrayIcon.MessageIcon.Information, 2500)
        self.update_top_status_bar()

    def _app_alias_map(self) -> dict[str, str]:
        aliases: dict[str, str] = {}
        for item in self.settings.app_aliases:
            if "=" not in item:
                continue
            alias, path = item.split("=", 1)
            if alias.strip() and path.strip():
                aliases[alias.strip()] = path.strip()
        return aliases

    def _remember_app_alias(self, alias: str, path: str) -> None:
        if not alias or not path or Path(path).suffix.lower() == ".lnk" or Path(path).is_dir():
            return
        aliases = self._app_alias_map()
        normalized_alias = alias.strip()
        if aliases.get(normalized_alias) == path:
            return
        aliases[normalized_alias] = path
        self.settings = self.settings_service.update(app_aliases=tuple(f"{key}={value}" for key, value in aliases.items()))
        if hasattr(self, "app_aliases_label"):
            self.app_aliases_label.setText(self._app_aliases_summary())

    def set_excluded_apps(self, apps: tuple[str, ...]) -> None:
        self.settings = self.settings_service.update(excluded_apps=apps)
        if hasattr(self, "excluded_apps_label"):
            self.excluded_apps_label.setText(self._excluded_apps_summary())

    def _wake_aliases_summary(self) -> str:
        aliases = wake_aliases_for_settings(settings_from_assistant_settings(self.settings))
        return "、".join(aliases[:4]) if aliases else "尚未設定，第一次啟用會提示"

    def manage_wake_aliases(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("管理喚醒詞")
        dialog.resize(460, 340)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(14, 14, 14, 14)
        aliases_list = QListWidget()
        self._prepare_wrapping_list(aliases_list)
        for item in self.settings.wake_aliases:
            aliases_list.addItem(item)
        alias_input = QLineEdit()
        alias_input.setPlaceholderText(f"例如：Hey {self.settings.assistant_name or DEFAULT_ASSISTANT_NAME}")
        add_button = QPushButton("新增")
        remove_button = QPushButton("刪除選取")
        row = QHBoxLayout()
        row.addWidget(alias_input, stretch=1)
        row.addWidget(add_button)
        row.addWidget(remove_button)
        layout.addWidget(QLabel("助理名稱本身會自動成為喚醒詞；這裡只放備用說法。"))
        layout.addWidget(aliases_list, stretch=1)
        layout.addLayout(row)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("儲存")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        layout.addWidget(buttons)

        def add_value() -> None:
            value = alias_input.text().strip()
            existing = {aliases_list.item(index).text().casefold() for index in range(aliases_list.count())}
            if value and value.casefold() not in existing:
                aliases_list.addItem(value)
            alias_input.clear()

        def remove_selected() -> None:
            for item in aliases_list.selectedItems():
                aliases_list.takeItem(aliases_list.row(item))

        add_button.clicked.connect(add_value)
        remove_button.clicked.connect(remove_selected)
        alias_input.returnPressed.connect(add_value)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            aliases = tuple(aliases_list.item(index).text().strip() for index in range(aliases_list.count()) if aliases_list.item(index).text().strip())
            self.settings = self.settings_service.update(wake_aliases=aliases)
            self._sync_voice_pipeline()

    def _app_aliases_summary(self) -> str:
        count = len(self._app_alias_map())
        return f"已記住 {count} 個 alias" if count else "尚未新增自訂 alias"

    def manage_app_aliases(self) -> None:
        from .name_resolver import AliasStore, Entity
        memory = AliasStore()
        dialog = QDialog(self)
        dialog.setWindowTitle('名稱 / 別名：Apps · Games · Files')
        dialog.resize(620, 460)
        layout = QVBoxLayout(dialog)
        listing = QListWidget()
        self._prepare_wrapping_list(listing)
        keys = []
        def refresh():
            listing.clear()
            keys.clear()
            for key, item in reversed(list(memory.data['aliases'].items())):
                keys.append(key)
                listing.addItem(f"{item['query']} → {item['name']} ({item.get('provider') or item['kind']})")
        refresh()
        layout.addWidget(QLabel('明確選擇後才記住；最近學到的稱呼列在最上方。'))
        layout.addWidget(listing)
        row = QHBoxLayout()
        edit = QPushButton('修改稱呼')
        remove = QPushButton('移除別名')
        manual = QPushButton('新增 / 管理 App 路徑')
        row.addWidget(edit); row.addWidget(remove); row.addWidget(manual)
        layout.addLayout(row)
        def remove_alias():
            index = listing.currentRow()
            if index >= 0:
                memory.remove(keys[index]); refresh()
        def edit_alias():
            index = listing.currentRow()
            if index < 0:
                return
            key = keys[index]
            item = memory.data['aliases'][key]
            value, ok = QInputDialog.getText(dialog, '修改稱呼', '新的稱呼', text=item['query'])
            if ok and value.strip():
                memory.remove(key)
                memory.confirm(value.strip(), Entity(item['id'], item['name'], item['kind'], item['target'], item.get('provider', '')))
                refresh()
        remove.clicked.connect(remove_alias)
        edit.clicked.connect(edit_alias)
        manual.clicked.connect(self.manage_legacy_app_aliases)
        enabled = QCheckBox('啟用本機檔案索引：桌面、文件、下載（僅索引名稱與時間）')
        enabled.setChecked(self.settings.file_index_enabled)
        layout.addWidget(enabled)
        roots = QLineEdit(';'.join(self.settings.file_index_roots))
        roots.setPlaceholderText('額外專案資料夾；多個路徑以分號分隔')
        layout.addWidget(roots)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dialog.accept)
        layout.addWidget(buttons)
        dialog.exec()
        self.settings = self.settings_service.update(file_index_enabled=enabled.isChecked(),
            file_index_roots=tuple(p.strip() for p in roots.text().split(';') if p.strip()))
        self._entity_config = None

    def manage_legacy_app_aliases(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("管理 App aliases")
        dialog.resize(560, 380)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(14, 14, 14, 14)
        aliases_list = QListWidget()
        self._prepare_wrapping_list(aliases_list)
        for item in self.settings.app_aliases:
            aliases_list.addItem(item)
        input_row = QHBoxLayout()
        alias_input = QLineEdit()
        alias_input.setPlaceholderText("例如：steam=D:\\Steam\\steam.exe")
        add_button = QPushButton("新增/更新")
        remove_button = QPushButton("刪除選取")
        input_row.addWidget(alias_input, stretch=1)
        input_row.addWidget(add_button)
        input_row.addWidget(remove_button)
        layout.addWidget(QLabel("格式：alias=完整 exe 路徑。已學到的 alias 也會出現在這裡。"))
        layout.addWidget(aliases_list, stretch=1)
        layout.addLayout(input_row)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("儲存")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        layout.addWidget(buttons)

        def add_value() -> None:
            value = alias_input.text().strip()
            if "=" not in value:
                return
            alias = value.split("=", 1)[0].strip().lower()
            for index in range(aliases_list.count()):
                if aliases_list.item(index).text().split("=", 1)[0].strip().lower() == alias:
                    aliases_list.takeItem(index)
                    break
            aliases_list.addItem(value)
            alias_input.clear()

        def remove_selected() -> None:
            for item in aliases_list.selectedItems():
                aliases_list.takeItem(aliases_list.row(item))

        add_button.clicked.connect(add_value)
        remove_button.clicked.connect(remove_selected)
        alias_input.returnPressed.connect(add_value)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            aliases = tuple(aliases_list.item(index).text().strip() for index in range(aliases_list.count()) if aliases_list.item(index).text().strip())
            self.settings = self.settings_service.update(app_aliases=aliases)
            self.app_aliases_label.setText(self._app_aliases_summary())

    def manage_excluded_apps(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("管理排除 App")
        dialog.resize(420, 360)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(14, 14, 14, 14)
        apps_list = QListWidget()
        self._prepare_wrapping_list(apps_list)
        for app_name in self.settings.excluded_apps:
            apps_list.addItem(app_name)
        input_row = QHBoxLayout()
        app_input = QLineEdit()
        app_input.setPlaceholderText("例如：game.exe 或 window title 關鍵字")
        add_button = QPushButton("新增")
        remove_button = QPushButton("刪除選取")
        input_row.addWidget(app_input, stretch=1)
        input_row.addWidget(add_button)
        input_row.addWidget(remove_button)
        layout.addWidget(QLabel("可輸入 executable 或視窗標題關鍵字。"))
        layout.addWidget(apps_list, stretch=1)
        layout.addLayout(input_row)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("儲存")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        layout.addWidget(buttons)

        def add_value() -> None:
            value = app_input.text().strip().lower()
            existing = {apps_list.item(index).text().lower() for index in range(apps_list.count())}
            if value and value not in existing:
                apps_list.addItem(value)
                app_input.clear()

        def remove_selected() -> None:
            for item in apps_list.selectedItems():
                apps_list.takeItem(apps_list.row(item))

        add_button.clicked.connect(add_value)
        remove_button.clicked.connect(remove_selected)
        app_input.returnPressed.connect(add_value)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            apps = tuple(apps_list.item(index).text().strip().lower() for index in range(apps_list.count()) if apps_list.item(index).text().strip())
            self.set_excluded_apps(apps)

    def set_ai_handoff_enabled(self, enabled: bool) -> None:
        self.settings = self.settings_service.update(ai_handoff_enabled=enabled)
        self.ai_provider_combo.setEnabled(enabled)
        self.ai_custom_url_input.setEnabled(enabled and self.settings.ai_provider == "custom")
        self.ai_auto_send_checkbox.setEnabled(enabled)

    def set_ai_provider(self, *_args: object) -> None:
        provider = str(self.ai_provider_combo.currentData() or "chatgpt")
        url = self.ai_custom_url_input.text().strip() if provider == "custom" else PROVIDER_URLS.get(provider, PROVIDER_URLS["chatgpt"])
        self.settings = self.settings_service.update(
            ai_provider=provider,
            ai_handoff_url=url or PROVIDER_URLS["chatgpt"],
        )
        self.ai_custom_url_input.setEnabled(self.settings.ai_handoff_enabled and provider == "custom")

    def set_ai_custom_url(self) -> None:
        if self.settings.ai_provider != "custom":
            return
        url = self.ai_custom_url_input.text().strip() or PROVIDER_URLS["chatgpt"]
        self.settings = self.settings_service.update(ai_handoff_url=url)

    def _active_advisor_preset(self) -> str:
        current = str(self.advisor_preset_combo.currentData() or "")
        if current and current != ADVISOR_DISABLED_SELECTION:
            return current
        return self.settings.advisor_provider_preset or "custom"

    def _current_advisor_endpoint(self) -> str:
        preset = self._active_advisor_preset()
        return advisor_preset_endpoint(preset) or self.advisor_endpoint_input.text().strip()

    def _selected_advisor_model_id(self) -> str:
        data = str(self.advisor_model_combo.currentData() or "")
        if data == ADVISOR_DISABLED_SELECTION:
            return self.settings.advisor_model_name
        if data == ADVISOR_CUSTOM_MODEL:
            return self.advisor_model_input.text().strip()
        return data.strip()

    def _populate_advisor_model_combo(self, remote_models: tuple[object, ...] | None = None) -> None:
        if not hasattr(self, "advisor_model_combo"):
            return
        current_model = self.settings.advisor_model_name
        preset = self._active_advisor_preset()
        self.advisor_model_combo.blockSignals(True)
        self.advisor_model_combo.clear()
        if not self.settings.advisor_api_enabled:
            self.advisor_model_combo.addItem("未啟用", ADVISOR_DISABLED_SELECTION)
            self.advisor_model_combo.setCurrentIndex(0)
            self.advisor_model_combo.setToolTip("AI Advisor 未啟用")
            self.advisor_model_combo.blockSignals(False)
            return
        options = remote_models or advisor_recommended_models(preset)
        recommended_labels = advisor_recommended_model_labels(preset) if remote_models else {}
        selected_index = -1
        for option in options:
            label = str(getattr(option, "label", ""))
            model_id = str(getattr(option, "model_id", ""))
            if not label:
                continue
            if remote_models and model_id in recommended_labels:
                label = f"{recommended_labels[model_id]}｜帳號可用"
            self.advisor_model_combo.addItem(label, model_id)
            index = self.advisor_model_combo.count() - 1
            self.advisor_model_combo.setItemData(index, model_id or "請在自訂模型輸入 model ID", Qt.ItemDataRole.ToolTipRole)
            if model_id and model_id == current_model:
                selected_index = index
        self.advisor_model_combo.addItem("自訂模型...", ADVISOR_CUSTOM_MODEL)
        custom_index = self.advisor_model_combo.count() - 1
        self.advisor_model_combo.setItemData(custom_index, "進階：手動輸入 model ID", Qt.ItemDataRole.ToolTipRole)
        if selected_index >= 0:
            self.advisor_model_combo.setCurrentIndex(selected_index)
        elif current_model:
            self.advisor_model_combo.setCurrentIndex(custom_index)
        else:
            self.advisor_model_combo.setCurrentIndex(0)
        self.advisor_model_combo.setToolTip(self.advisor_model_combo.currentText())
        self.advisor_model_combo.blockSignals(False)

    def _sync_advisor_controls_enabled(self) -> None:
        if not hasattr(self, "advisor_preset_combo"):
            return
        enabled = bool(self.settings.advisor_api_enabled)
        if enabled:
            preset_index = self.advisor_preset_combo.findData(self.settings.advisor_provider_preset)
            if preset_index < 0:
                preset_index = self.advisor_preset_combo.findData("custom")
            self.advisor_preset_combo.blockSignals(True)
            self.advisor_preset_combo.setCurrentIndex(max(0, preset_index))
            self.advisor_preset_combo.blockSignals(False)
        else:
            self.advisor_preset_combo.blockSignals(True)
            self.advisor_preset_combo.setCurrentIndex(0)
            self.advisor_preset_combo.blockSignals(False)
        preset = self._active_advisor_preset()
        is_custom_provider = preset == "custom"
        is_custom_model = enabled and str(self.advisor_model_combo.currentData() or "") == ADVISOR_CUSTOM_MODEL
        self.advisor_preset_combo.setEnabled(enabled)
        self.advisor_endpoint_input.setEnabled(enabled and is_custom_provider)
        self.advisor_model_combo.setEnabled(enabled)
        self.advisor_model_input.setEnabled(enabled and is_custom_model)
        self.advisor_api_key_input.setEnabled(enabled)
        self.advisor_refresh_models_button.setEnabled(enabled)
        self.advisor_test_button.setEnabled(enabled)
        self.advisor_context_combo.setEnabled(enabled)
        self.advisor_event_triggered_checkbox.setEnabled(enabled)
        self.advisor_key_button.setEnabled(enabled)
        if hasattr(self, "ai_form"):
            self.ai_form.setRowVisible(self.advisor_endpoint_input, enabled and is_custom_provider)
            self.ai_form.setRowVisible(self.advisor_model_input, is_custom_model)
        if not enabled:
            self._populate_advisor_model_combo()
            self.advisor_test_label.setText("")
        self._update_advisor_key_url_label()

    def set_advisor_api_enabled(self, enabled: bool) -> None:
        self.settings = self.settings_service.update(advisor_api_enabled=enabled)
        if not enabled:
            self.last_advisor_action = None
            advisor_reset_health(self.store)
            self.status_bar_notice = ("Advisor：本機規則", datetime.now() + timedelta(minutes=5))
        else:
            self._populate_advisor_model_combo()
        self._sync_advisor_controls_enabled()
        self.refresh_advisor(auto=False)

    def set_advisor_provider_preset(self, *_args: object) -> None:
        preset = str(self.advisor_preset_combo.currentData() or "custom")
        if preset == ADVISOR_DISABLED_SELECTION:
            return
        changes: dict[str, object] = {"advisor_provider_preset": preset}
        endpoint = advisor_preset_endpoint(preset)
        if endpoint:
            changes["advisor_endpoint_url"] = endpoint
            self.advisor_endpoint_input.setText(str(changes["advisor_endpoint_url"]))
        self.settings = self.settings_service.update(**changes)
        advisor_reset_health(self.store)
        self._update_advisor_key_url_label()
        self._populate_advisor_model_combo()
        self._sync_advisor_controls_enabled()
        self.refresh_advisor(auto=False)

    def set_advisor_endpoint_url(self) -> None:
        preset = str(self.advisor_preset_combo.currentData() or "custom")
        if preset == ADVISOR_DISABLED_SELECTION:
            return
        preset_endpoint = advisor_preset_endpoint(preset)
        if preset_endpoint:
            self.advisor_endpoint_input.setText(preset_endpoint)
            self.settings = self.settings_service.update(advisor_endpoint_url=preset_endpoint, advisor_provider_preset=preset)
            advisor_reset_health(self.store)
            return
        next_preset = "custom"
        self.settings = self.settings_service.update(
            advisor_endpoint_url=self.advisor_endpoint_input.text().strip(),
            advisor_provider_preset=next_preset,
        )
        if next_preset == "custom" and self.advisor_preset_combo.currentData() != "custom":
            index = self.advisor_preset_combo.findData("custom")
            self.advisor_preset_combo.blockSignals(True)
            self.advisor_preset_combo.setCurrentIndex(max(0, index))
            self.advisor_preset_combo.blockSignals(False)
            self._update_advisor_key_url_label()
        advisor_reset_health(self.store)

    def set_advisor_model_name(self) -> None:
        self.settings = self.settings_service.update(advisor_model_name=self._selected_advisor_model_id())
        advisor_reset_health(self.store)

    def set_advisor_model_from_combo(self, *_args: object) -> None:
        if str(self.advisor_model_combo.currentData() or "") == ADVISOR_DISABLED_SELECTION:
            return
        model = self._selected_advisor_model_id()
        if model != self.settings.advisor_model_name:
            self.settings = self.settings_service.update(advisor_model_name=model)
            advisor_reset_health(self.store)
        self._sync_advisor_controls_enabled()

    def set_custom_advisor_model_name(self) -> None:
        if str(self.advisor_model_combo.currentData() or "") != ADVISOR_CUSTOM_MODEL:
            return
        self.settings = self.settings_service.update(advisor_model_name=self.advisor_model_input.text().strip())
        advisor_reset_health(self.store)

    def set_advisor_api_key(self) -> None:
        key = self.advisor_api_key_input.text().strip()
        if key:
            self.store.set_setting("advisor_api_key", key)
        else:
            self.store.delete_setting("advisor_api_key")
        advisor_reset_health(self.store)

    def toggle_advisor_api_key_visible(self) -> None:
        if self.advisor_api_key_input.echoMode() == QLineEdit.EchoMode.Password:
            self.advisor_api_key_input.setEchoMode(QLineEdit.EchoMode.Normal)
        else:
            self.advisor_api_key_input.setEchoMode(QLineEdit.EchoMode.Password)

    def _update_advisor_key_url_label(self) -> None:
        if not hasattr(self, "advisor_key_url_label"):
            return
        preset = self._active_advisor_preset()
        url = advisor_key_url(preset)
        if url:
            self.advisor_key_url_label.setText(url)
            self.advisor_key_button.setEnabled(self.settings.advisor_api_enabled)
        else:
            self.advisor_key_url_label.setText("請向服務提供者取得 API key")
            self.advisor_key_button.setEnabled(self.settings.advisor_api_enabled)

    def open_advisor_key_url(self) -> None:
        preset = self._active_advisor_preset()
        url = advisor_key_url(preset)
        if not url:
            self.advisor_test_label.setText("請向服務提供者取得 API key")
            return
        opened = QDesktopServices.openUrl(QUrl(url))
        if not opened:
            try:
                opened = bool(webbrowser.open(url))
            except Exception:
                opened = False
        self.advisor_key_url_label.setText(url)
        self.advisor_test_label.setText("已開啟 API key 頁面" if opened else f"無法自動開啟，請複製網址：{url}")

    def refresh_advisor_models(self) -> None:
        preset = self._active_advisor_preset()
        endpoint = self._current_advisor_endpoint()
        key = self.advisor_api_key_input.text().strip()
        self.set_advisor_api_key()
        if not key:
            self.advisor_test_label.setText("請先填 API key")
            self._populate_advisor_model_combo()
            return
        if not endpoint:
            self.advisor_test_label.setText("請先填 endpoint")
            self._populate_advisor_model_combo()
            return
        try:
            models = OpenAICompatibleAdvisorProvider(endpoint, self._selected_advisor_model_id() or "ping", key, timeout_seconds=10).list_models()
        except Exception as exc:
            self._populate_advisor_model_combo()
            self.advisor_test_label.setText(f"模型更新失敗，保留內建推薦：{advisor_http_error_message(exc, key)}")
            return
        if not models:
            self._populate_advisor_model_combo()
            self.advisor_test_label.setText("沒有讀到可用模型，保留內建推薦")
            return
        self._populate_advisor_model_combo(models)
        self.advisor_test_label.setText(f"{ADVISOR_PROVIDER_PRESETS.get(preset, ADVISOR_PROVIDER_PRESETS['custom'])['label']}：已更新模型")

    def set_advisor_context_minutes(self, *_args: object) -> None:
        value = int(self.advisor_context_combo.currentData() or 20)
        self.settings = self.settings_service.update(advisor_context_minutes=value)

    def set_advisor_event_triggered(self, enabled: bool) -> None:
        self.settings = self.settings_service.update(advisor_event_triggered=enabled)

    def set_activity_session_grace_seconds(self, *_args: object) -> None:
        value = int(self.session_grace_combo.currentData() or 90)
        self.settings = self.settings_service.update(activity_session_grace_seconds=value)
        self.session_manager.grace_period_seconds = value

    def test_advisor_connection(self) -> None:
        endpoint = self._current_advisor_endpoint()
        model = self._selected_advisor_model_id()
        key = self.advisor_api_key_input.text().strip()
        self.set_advisor_endpoint_url()
        self.set_advisor_model_name()
        self.set_advisor_api_key()
        if not key:
            self.advisor_test_label.setText("請先填 API key")
            advisor_reset_health(self.store)
            self.refresh_advisor(auto=False)
            return
        if not endpoint or not model:
            self.advisor_test_label.setText("請先選擇模型" if endpoint else "請先填 endpoint")
            advisor_reset_health(self.store)
            self.refresh_advisor(auto=False)
            return
        provider_client = OpenAICompatibleAdvisorProvider(endpoint, model, key, timeout_seconds=10)
        ok, message = provider_client.test_connection()
        provider = advisor_provider_label(self.settings.advisor_provider_preset, endpoint, model)
        self.advisor_test_label.setText(f"{provider}：{message}")
        if ok:
            advisor_record_success(self.store, message="測試連線成功")
            self.status_bar_notice = (f"Advisor API 連線成功：{provider}", datetime.now() + timedelta(minutes=5))
        else:
            advisor_reset_health(self.store)
            self.store.set_setting("advisor_health_state", "unknown")
            self.store.set_setting("advisor_health_message", message)
        self.refresh_advisor(auto=False)

    def export_settings(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "匯出設定", "private-assistant-settings.json", "JSON (*.json)")
        if not path:
            return
        Path(path).write_text(
            json.dumps(self.settings_service.export_public_settings(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        QMessageBox.information(self, "已匯出設定", "已匯出偏好與規則；未包含提醒歷史、活動紀錄或學習資料。")

    def import_settings(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "匯入設定", "", "JSON (*.json)")
        if not path:
            return
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
            self.settings = self.settings_service.import_public_settings(payload)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            QMessageBox.warning(self, "無法匯入設定", str(exc))
            return
        self._sync_settings_controls()
        self._sync_voice_pipeline()
        QMessageBox.information(self, "已匯入設定", "設定已更新。私人活動與提醒歷史不會從設定檔匯入。")

    def reset_learning_data(self) -> None:
        answer = QMessageBox.question(
            self,
            "清除學習資料",
            "這會清除活動事件、學習模式與 AI 推測，但保留正式提醒與設定。確定嗎？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.store.reset_learning_data()
        self.refresh_lists()

    def reset_all_personal_data(self) -> None:
        answer = QMessageBox.question(
            self,
            "清全部個人資料",
            "這會清除提醒、活動紀錄、學習資料與通知紀錄，但保留 App 設定。確定嗎？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.store.reset_all_personal_data()
        self.refresh_lists()
        self.update_top_status_bar()

    def _sync_settings_controls(self) -> None:
        self.top_status_visible = self.settings.top_status_bar_visible
        self.auto_idle_pause_enabled = self.settings.auto_idle_pause_enabled
        self.idle_threshold_minutes = self.settings.idle_threshold_minutes
        self.auto_start_monitoring_checkbox.setChecked(self.settings.auto_start_monitoring_enabled)
        self.auto_idle_pause_checkbox.setChecked(self.auto_idle_pause_enabled)
        self.privacy_mode_checkbox.setChecked(self.settings.privacy_mode_enabled)
        self.learning_checkbox.setChecked(self.settings.learning_enabled)
        self.notification_learning_checkbox.setChecked(self.settings.notification_listener_beta_enabled)
        self.dead_loop_checkbox.setChecked(self.settings.dead_loop_detection_enabled)
        self.startup_checkbox.setChecked(self.settings.startup_enabled or is_startup_enabled())
        self.quick_add_checkbox.setChecked(self.settings.quick_add_enabled)
        self.quick_add_hotkey_input.setText(self.settings.quick_add_hotkey)
        self.voice_command_checkbox.setChecked(self.settings.voice_command_enabled)
        self.assistant_name_input.setText(self.settings.assistant_name)
        self.wake_aliases_label.setText(self._wake_aliases_summary())
        self.refresh_voice_microphones(show_notice=False)
        self.wake_sound_checkbox.setChecked(self.settings.wake_sound_enabled)
        display_index = self.voice_wake_display_combo.findData(self.settings.voice_wake_display)
        self.voice_wake_display_combo.setCurrentIndex(max(0, display_index))
        if hasattr(self, "voice_microphone_gain_combo"):
            self.voice_microphone_gain_combo.setCurrentIndex(max(0, self.voice_microphone_gain_combo.findData(self.settings.voice_microphone_gain)))
        sensitivity_index = self.voice_wake_sensitivity_combo.findData(self.settings.voice_wake_sensitivity)
        self.voice_wake_sensitivity_combo.setCurrentIndex(max(0, sensitivity_index))
        self._update_voice_status_label()
        self.top_status_bar_visible_checkbox.setChecked(self.top_status_visible)
        self._populate_display_controls()
        index = self.idle_threshold_combo.findData(self.idle_threshold_minutes)
        self.idle_threshold_combo.setCurrentIndex(max(0, index))
        self.excluded_apps_label.setText(self._excluded_apps_summary())
        self.ai_handoff_checkbox.setChecked(self.settings.ai_handoff_enabled)
        provider_index = self.ai_provider_combo.findData(self.settings.ai_provider)
        self.ai_provider_combo.setCurrentIndex(max(0, provider_index))
        self.ai_provider_combo.setEnabled(self.settings.ai_handoff_enabled)
        self.ai_custom_url_input.setText(self.settings.ai_handoff_url if self.settings.ai_provider == "custom" else "")
        self.ai_custom_url_input.setEnabled(self.settings.ai_handoff_enabled and self.settings.ai_provider == "custom")
        self.ai_auto_send_checkbox.setChecked(self.settings.ai_handoff_auto_send)
        self.ai_auto_send_checkbox.setEnabled(self.settings.ai_handoff_enabled)
        self.app_aliases_label.setText(self._app_aliases_summary())
        self.advisor_api_checkbox.setChecked(self.settings.advisor_api_enabled)
        preset_index = self.advisor_preset_combo.findData(self.settings.advisor_provider_preset)
        self.advisor_preset_combo.setCurrentIndex(max(0, preset_index if self.settings.advisor_api_enabled else 0))
        self.advisor_endpoint_input.setText(self.settings.advisor_endpoint_url)
        self.advisor_model_input.setText(self.settings.advisor_model_name)
        self.advisor_api_key_input.setText(self.store.get_setting("advisor_api_key", "") or "")
        self._populate_advisor_model_combo()
        self._sync_advisor_controls_enabled()
        self._update_advisor_key_url_label()
        context_index = self.advisor_context_combo.findData(self.settings.advisor_context_minutes)
        self.advisor_context_combo.setCurrentIndex(max(0, context_index))
        self.advisor_event_triggered_checkbox.setChecked(self.settings.advisor_event_triggered)
        grace_index = self.session_grace_combo.findData(self.settings.activity_session_grace_seconds)
        self.session_grace_combo.setCurrentIndex(max(0, grace_index))
        if self.top_status_visible:
            self.top_status_bar.show()
        else:
            self.top_status_bar.hide()

    def _maybe_alert_dead_loop(self, snapshot: ForegroundSnapshot) -> None:
        if not self.settings.dead_loop_detection_enabled or self.store.privacy_mode_enabled():
            return
        assessment = self.dead_loop_detector.assess(snapshot, self.store.recent_assistant_events(80))
        if not assessment.should_alert:
            return
        self.dead_loop_detector.mark_alerted()
        reason_text = "\n".join(f"- {reason}" for reason in assessment.reasons)
        box = QMessageBox(self)
        box.setWindowTitle("可能陷入重複處理")
        box.setText(f"可能陷入重複處理/死循環。\n\n{reason_text}")
        ignore_button = box.addButton("忽略", QMessageBox.ButtonRole.RejectRole)
        not_issue_button = box.addButton("不是問題", QMessageBox.ButtonRole.NoRole)
        handoff_text = "交給 AI 重新檢查" if self.settings.ai_handoff_enabled else "AI Handoff 未啟用"
        handoff_button = box.addButton(handoff_text, QMessageBox.ButtonRole.AcceptRole)
        handoff_button.setEnabled(self.settings.ai_handoff_enabled)
        box.exec()
        clicked = box.clickedButton()
        if clicked == handoff_button:
            self._handoff_dead_loop_prompt(assessment.prompt)
        elif clicked == not_issue_button:
            self.store.upsert_pattern(
                "dead_loop_feedback",
                "global",
                "not_issue",
                "false_positive",
                -0.2,
                1,
                0.1,
                "observing",
                "使用者標記死循環提醒不是問題",
            )
            self.status_bar_notice = ("已標記：不是問題", datetime.now() + timedelta(minutes=5))
        elif clicked == ignore_button:
            self.status_bar_notice = ("已忽略死循環提醒", datetime.now() + timedelta(minutes=5))

    def _handoff_dead_loop_prompt(self, prompt: str) -> None:
        if not self.settings.ai_handoff_enabled:
            QMessageBox.information(self, "AI Handoff 未啟用", "可到設定 > AI 整合開啟後再交給 AI 重新檢查。")
            return
        provider = AIHandoffProvider(self.settings.ai_provider, self.settings.ai_handoff_url)
        provider.handoff(prompt, copy_text=QApplication.clipboard().setText)
        QMessageBox.information(
            self,
            "已準備 AI Handoff",
            "已把乾淨摘要複製到剪貼簿，並開啟你選擇的 AI 網站。貼上後即可請它重新檢查。",
        )

    def _load_idle_threshold_minutes(self) -> int:
        raw_value = self.store.get_setting("idle_threshold_minutes", "10")
        try:
            minutes = int(raw_value or "10")
        except ValueError:
            minutes = 10
        return minutes if minutes in IDLE_THRESHOLD_OPTIONS else 10

    def _should_auto_pause(self, snapshot: ForegroundSnapshot) -> bool:
        if not self.monitoring or not self.auto_idle_pause_enabled:
            return False
        threshold_seconds = self.idle_threshold_minutes * 60
        return snapshot.idle_seconds >= threshold_seconds or is_windows_locked_or_unavailable()

    def maybe_suggest_today_inbox(self, snapshot: ForegroundSnapshot) -> None:
        inbox_items = self.store.today_inbox_items(datetime.now(), limit=1)
        if not inbox_items:
            return
        minutes_since = None
        if self.last_today_inbox_suggestion_at:
            minutes_since = int((datetime.now() - self.last_today_inbox_suggestion_at).total_seconds() / 60)
        has_higher = bool([item for item in self.store.due_reminders(datetime.now()) if item.importance != "low"])
        if should_suggest_today_inbox(
            active=snapshot.idle_seconds < 60,
            mode=snapshot.mode,
            has_higher_priority_due=has_higher,
            minutes_since_last_suggestion=minutes_since,
        ):
            self.last_today_inbox_suggestion_at = datetime.now()
            self.status_bar_notice = (f"Inbox：{self._shorten(inbox_items[0].text, 18)}", datetime.now() + timedelta(minutes=8))

    def maybe_show_first_run_setup(self) -> None:
        if self.settings.first_run_setup_completed:
            return
        QMessageBox.information(
            self,
            "Alpha 初始設定",
            "核心功能都在本機運作。你可以在主畫面調整監督、學習、離席時間、狀態列、隱私模式、排除 App 與開機自啟。",
        )
        self.settings = self.settings_service.update(first_run_setup_completed=True)

    def maybe_check_updates(self) -> None:
        last_checked = None
        if self.settings.last_update_check_at:
            try:
                last_checked = datetime.fromisoformat(self.settings.last_update_check_at)
            except ValueError:
                last_checked = None
        if not should_check_for_updates(self.settings.update_checks_enabled, last_checked):
            return
        result = check_latest_release(self.settings.update_check_url, CURRENT_VERSION)
        self.settings = self.settings_service.update(last_update_check_at=datetime.now().isoformat())
        if result.update_available and result.latest_version:
            self.status_bar_notice = (f"有新版：{result.latest_version}", datetime.now() + timedelta(minutes=20))

    def _status_screen(self):
        screens = self.top_status_bar.screen_geometries()
        active_index = self._active_screen_index()
        return choose_status_screen(screens, active_index, self.settings.top_status_bar_screen)

    def _popup_screen(self):
        screens = self.top_status_bar.screen_geometries()
        status = choose_status_screen(screens, self._active_screen_index(), self.settings.top_status_bar_screen)
        return choose_popup_screen(screens, self._active_screen_index(), status, self.settings.reminder_popup_screen)

    def _active_screen_index(self) -> int | None:
        screen = QApplication.screenAt(self.cursor().pos())
        if not screen:
            return None
        for index, candidate in enumerate(QApplication.screens()):
            if candidate == screen:
                return index
        return None

    def _refresh_monitor_controls(self) -> None:
        if not self.monitoring:
            button_text = "開始監督"
            service_text = "監督：手動停止"
            tray_text = "Private Assistant AI Alpha｜手動停止"
        elif self.auto_idle_paused:
            button_text = "停止監督"
            service_text = "監督：離席暫停"
            tray_text = "Private Assistant AI Alpha｜離席暫停"
        else:
            button_text = "停止監督"
            service_text = "監督：監督中"
            tray_text = "Private Assistant AI Alpha｜監督中"

        self.toggle_button.setText(button_text)
        self.toggle_monitor_action.setText(button_text)
        self.monitor_service_label.setText(service_text)
        if hasattr(self, "tray_icon"):
            self.tray_icon.setToolTip(tray_text)

    def _update_activity_duration(self, snapshot: ForegroundSnapshot) -> None:
        privacy_mode = self.store.privacy_mode_enabled()
        update = self.session_manager.observe(snapshot, privacy_mode=privacy_mode)
        self.current_activity_session = update.session
        self.activity_key = (snapshot.app_name.lower(), snapshot.mode, update.session.category)
        self.activity_started_at = update.session.started_at

    def _format_duration(self, delta: timedelta) -> str:
        seconds = max(0, int(delta.total_seconds()))
        hours, remainder = divmod(seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

    def _shorten(self, text: str, max_chars: int) -> str:
        text = " ".join(text.split())
        if len(text) <= max_chars:
            return text
        return text[: max_chars - 1] + "…"

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.show_main_window()

    def show_main_window(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()

    def exit_application(self) -> None:
        if hasattr(self, "rhythm_diagnostic"):
            self.rhythm_diagnostic.cancel()
        self.exiting = True
        self._voice_generation += 1
        self.voice_listening_overlay.hide()
        self.voice_state_timer.stop()
        for timer_name in ("monitor_timer", "reminder_timer", "status_bar_timer", "voice_wake_timer"):
            timer = getattr(self, timer_name, None)
            if timer:
                timer.stop()
        self.voice_pipeline.stop()
        if hasattr(self, 'entity_navigator'):
            self.entity_navigator.games.close()
        if self.voice_install_thread and self.voice_install_thread.isRunning():
            self.voice_install_thread.quit()
            self.voice_install_thread.wait(1500)
        self.global_hotkey.unregister()
        self.top_status_bar.close()
        self.tray_icon.hide()
        self.close()
        QApplication.quit()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.exiting:
            event.accept()
            return

        event.ignore()
        self.hide()
        if self.tray_icon.isVisible():
            self.tray_icon.showMessage(
                "Private Assistant AI 仍在背景運作",
                "提醒與已啟用的語音喚醒會繼續執行。可從系統匣重新開啟或完全離開。",
                QSystemTrayIcon.MessageIcon.Information,
                2500,
            )


def create_app(db_path: Path) -> QApplication:
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    window = MainWindow(db_path)
    window.show()
    app.voice_service = window.voice_pipeline
    app.aboutToQuit.connect(window.voice_pipeline.stop)
    app.window = window
    return app

from __future__ import annotations

import json
from dataclasses import asdict, dataclass


DEFAULT_EXCLUDED_APPS = (
    "1password.exe",
    "bitwarden.exe",
    "keepass.exe",
    "lastpass.exe",
    "dashlane.exe",
    "bank",
)


@dataclass(frozen=True)
class AssistantSettings:
    version: str = "v0.1.0-alpha"
    monitoring_enabled: bool = False
    auto_start_monitoring_enabled: bool = True
    first_run_setup_completed: bool = False
    start_minimized_to_tray: bool = False
    startup_enabled: bool = False
    auto_idle_pause_enabled: bool = True
    idle_threshold_minutes: int = 10
    top_status_bar_visible: bool = True
    top_status_bar_screen: str = "auto"
    top_status_bar_position: str = "top_left"
    reminder_popup_screen: str = "active"
    reminder_popups_enabled: bool = True
    windows_notifications_enabled: bool = True
    notification_listener_beta_enabled: bool = False
    privacy_mode_enabled: bool = False
    learning_enabled: bool = True
    dead_loop_detection_enabled: bool = True
    quick_add_enabled: bool = True
    quick_add_hotkey: str = "Ctrl+Alt+A"
    escalation_enabled: bool = True
    escalation_minutes: int = 15
    update_checks_enabled: bool = True
    update_check_url: str = "https://api.github.com/repos/OWNER/private-assistant-ai-mvp/releases/latest"
    last_update_check_at: str = ""
    excluded_apps: tuple[str, ...] = DEFAULT_EXCLUDED_APPS
    ai_handoff_enabled: bool = False
    ai_provider: str = "chatgpt"
    ai_handoff_url: str = "https://chatgpt.com/"
    ai_handoff_auto_send: bool = False
    app_aliases: tuple[str, ...] = ()
    file_index_enabled: bool = False
    file_index_roots: tuple[str, ...] = ()
    voice_command_enabled: bool = False
    assistant_name: str = ""
    wake_aliases: tuple[str, ...] = ()
    voice_microphone_id: str = ""
    wake_sound_enabled: bool = True
    voice_wake_display: str = "osd"
    voice_components_state: str = "not_installed"
    voice_components_error: str = ""
    voice_allow_during_games: bool = False
    voice_stt_device: str = "auto"
    voice_wake_sensitivity: str = "auto"
    voice_microphone_gain: str = "auto"
    voice_wake_template_id: str = ""
    advisor_api_enabled: bool = False
    advisor_endpoint_url: str = ""
    advisor_model_name: str = ""
    advisor_provider_preset: str = "custom"
    advisor_context_minutes: int = 20
    advisor_event_triggered: bool = True
    activity_session_grace_seconds: int = 90


BOOLEAN_KEYS = {
    "file_index_enabled",
    "monitoring_enabled",
    "auto_start_monitoring_enabled",
    "auto_idle_pause_enabled",
    "first_run_setup_completed",
    "start_minimized_to_tray",
    "startup_enabled",
    "top_status_bar_visible",
    "reminder_popups_enabled",
    "windows_notifications_enabled",
    "notification_listener_beta_enabled",
    "privacy_mode_enabled",
    "learning_enabled",
    "dead_loop_detection_enabled",
    "quick_add_enabled",
    "escalation_enabled",
    "update_checks_enabled",
    "ai_handoff_enabled",
    "ai_handoff_auto_send",
    "voice_command_enabled",
    "wake_sound_enabled",
    "voice_allow_during_games",
    "advisor_api_enabled",
    "advisor_event_triggered",
}

PUBLIC_SETTING_KEYS = set(AssistantSettings.__dataclass_fields__)


class SettingsService:
    """Typed settings facade over the local SQLite app_settings table."""

    def __init__(self, store: object) -> None:
        self.store = store

    def load(self) -> AssistantSettings:
        defaults = AssistantSettings()
        values: dict[str, object] = {}
        for key in PUBLIC_SETTING_KEYS:
            default_value = getattr(defaults, key)
            raw = self.store.get_setting(key, self._serialize(default_value))
            values[key] = self._deserialize(key, raw, default_value)
        return AssistantSettings(**values)

    def save(self, settings: AssistantSettings) -> None:
        for key, value in asdict(settings).items():
            self.store.set_setting(key, self._serialize(value))

    def update(self, **changes: object) -> AssistantSettings:
        current = asdict(self.load())
        current.update({key: value for key, value in changes.items() if key in PUBLIC_SETTING_KEYS})
        settings = AssistantSettings(**current)
        self.save(settings)
        return settings

    def export_public_settings(self) -> dict[str, object]:
        data = asdict(self.load())
        for key in ("excluded_apps", "app_aliases", "wake_aliases", "file_index_roots"):
            data[key] = list(data[key])
        return {"version": 1, "settings": data}

    def import_public_settings(self, payload: dict[str, object]) -> AssistantSettings:
        raw_settings = payload.get("settings", payload)
        if not isinstance(raw_settings, dict):
            raise ValueError("設定檔格式不正確")
        current = asdict(self.load())
        for key, value in raw_settings.items():
            if key in PUBLIC_SETTING_KEYS:
                current[key] = self._coerce_import_value(key, value, current[key])
        settings = AssistantSettings(**current)
        self.save(settings)
        return settings

    def _serialize(self, value: object) -> str:
        if isinstance(value, bool):
            return "1" if value else "0"
        if isinstance(value, (tuple, list)):
            return json.dumps(list(value), ensure_ascii=False)
        return str(value)

    def _deserialize(self, key: str, raw: str | None, default: object) -> object:
        if raw is None:
            return default
        if key in BOOLEAN_KEYS:
            return raw == "1" or raw.lower() == "true"
        if key in {"idle_threshold_minutes", "escalation_minutes", "advisor_context_minutes", "activity_session_grace_seconds"}:
            try:
                value = max(1, int(raw))
                if key == "activity_session_grace_seconds":
                    return max(30, min(120, value))
                return value
            except ValueError:
                return default
        if key in {"excluded_apps", "app_aliases", "wake_aliases", "file_index_roots"}:
            try:
                value = json.loads(raw)
                if isinstance(value, list):
                    if key == "excluded_apps":
                        return tuple(str(item).strip().lower() for item in value if str(item).strip())
                    return tuple(str(item).strip() for item in value if str(item).strip())
            except json.JSONDecodeError:
                pass
            if key == "excluded_apps":
                return tuple(item.strip().lower() for item in raw.split(",") if item.strip())
            return tuple(item.strip() for item in raw.splitlines() if item.strip())
        if key == "voice_microphone_gain":
            return raw if raw in {"auto", "off"} else default
        if key == "voice_wake_sensitivity":
            return raw if raw in {"auto", "low", "standard", "high"} else default
        return raw

    def _coerce_import_value(self, key: str, value: object, default: object) -> object:
        if key in BOOLEAN_KEYS:
            return bool(value)
        if key in {"idle_threshold_minutes", "escalation_minutes", "advisor_context_minutes", "activity_session_grace_seconds"}:
            try:
                coerced = max(1, int(value))
                if key == "activity_session_grace_seconds":
                    return max(30, min(120, coerced))
                return coerced
            except (TypeError, ValueError):
                return default
        if key in {"excluded_apps", "app_aliases", "wake_aliases", "file_index_roots"}:
            if isinstance(value, str):
                separator = "," if key == "excluded_apps" else "\n"
                items = value.split(separator)
                if key == "excluded_apps":
                    return tuple(item.strip().lower() for item in items if item.strip())
                return tuple(item.strip() for item in items if item.strip())
            if isinstance(value, list):
                if key == "excluded_apps":
                    return tuple(str(item).strip().lower() for item in value if str(item).strip())
                return tuple(str(item).strip() for item in value if str(item).strip())
            return default
        if key == "voice_microphone_gain":
            return str(value) if str(value) in {"auto", "off"} else default
        if key == "voice_wake_sensitivity":
            coerced = str(value)
            return coerced if coerced in {"auto", "low", "standard", "high"} else default
        return str(value)

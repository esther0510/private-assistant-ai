from __future__ import annotations

from dataclasses import replace

from .models import DisplayGeometry, ForegroundSnapshot


STATUS_AUTO = "auto"
STATUS_PRIMARY = "primary"
STATUS_SECONDARY = "secondary"
STATUS_ACTIVE = "active"
POPUP_ACTIVE = "active"
POPUP_STATUS = "status_bar"
POSITION_TOP_LEFT = "top_left"
POSITION_TOP_RIGHT = "top_right"
STATUS_BAR_MARGIN = 12
STATUS_BAR_MIN_WIDTH = 220
STATUS_BAR_MAX_WIDTH = 620


def normalize_screen_choice(value: str | None) -> str:
    value = (value or STATUS_AUTO).strip().lower()
    if value in {STATUS_AUTO, STATUS_PRIMARY, STATUS_SECONDARY, STATUS_ACTIVE, POPUP_ACTIVE, POPUP_STATUS}:
        return value
    if value.startswith("screen:"):
        return value
    return STATUS_AUTO


def normalize_status_position(value: str | None) -> str:
    value = (value or POSITION_TOP_LEFT).strip().lower()
    if value in {POSITION_TOP_LEFT, POSITION_TOP_RIGHT, "custom"}:
        return value
    return POSITION_TOP_LEFT


def scaled_size(logical_pixels: int, device_pixel_ratio: float) -> int:
    return max(1, round(logical_pixels * max(0.75, device_pixel_ratio or 1.0)))


def status_bar_geometry(
    screen: DisplayGeometry,
    content_width: int,
    position: str | None = POSITION_TOP_LEFT,
    margin: int = STATUS_BAR_MARGIN,
    min_width: int = STATUS_BAR_MIN_WIDTH,
    max_width: int = STATUS_BAR_MAX_WIDTH,
    height: int = 28,
) -> tuple[int, int, int, int]:
    position = normalize_status_position(position)
    margin = max(0, int(margin))
    available_width = max(1, screen.available_width - (margin * 2))
    width = min(max(int(content_width), min_width), max_width, available_width)
    if position == POSITION_TOP_RIGHT:
        x = screen.available_x + screen.available_width - width - margin
    else:
        x = screen.available_x + margin
    y = screen.available_y + margin
    return x, y, width, height


def classify_window_presentation(
    window_rect: tuple[int, int, int, int] | None,
    screen: DisplayGeometry | None,
    style_hint: str | None = None,
) -> str:
    """Best-effort windowed/borderless/exclusive classification.

    Exclusive fullscreen cannot be reliably overlaid from normal desktop UI, so the app treats
    explicit exclusive hints as a non-intrusive fallback case.
    """

    hint = (style_hint or "").lower()
    if "exclusive" in hint:
        return "exclusive_fullscreen"
    if not window_rect or not screen:
        return "unknown"
    left, top, right, bottom = window_rect
    width = max(0, right - left)
    height = max(0, bottom - top)
    covers_screen = (
        abs(left - screen.x) <= 2
        and abs(top - screen.y) <= 2
        and abs(width - screen.width) <= 4
        and abs(height - screen.height) <= 4
    )
    if covers_screen and ("borderless" in hint or "game" in hint or "steam" in hint):
        return "borderless_fullscreen"
    if covers_screen:
        return "maximized_or_borderless"
    return "windowed"


def choose_status_screen(
    screens: list[DisplayGeometry],
    active_screen_index: int | None = None,
    setting: str | None = STATUS_AUTO,
) -> DisplayGeometry | None:
    if not screens:
        return None
    setting = normalize_screen_choice(setting)
    primary = next((screen for screen in screens if screen.is_primary), screens[0])
    active = _screen_by_index(screens, active_screen_index) or primary

    if setting == STATUS_PRIMARY:
        return primary
    if setting == STATUS_SECONDARY:
        return next((screen for screen in screens if not screen.is_primary), primary)
    if setting == STATUS_ACTIVE:
        return active
    if setting.startswith("screen:"):
        try:
            return _screen_by_index(screens, int(setting.split(":", 1)[1])) or primary
        except ValueError:
            return primary
    if len(screens) == 1:
        return active
    return next((screen for screen in screens if screen.index != active.index), primary)


def choose_popup_screen(
    screens: list[DisplayGeometry],
    active_screen_index: int | None,
    status_screen: DisplayGeometry | None,
    setting: str | None = POPUP_ACTIVE,
) -> DisplayGeometry | None:
    if not screens:
        return None
    setting = normalize_screen_choice(setting)
    primary = next((screen for screen in screens if screen.is_primary), screens[0])
    if setting == POPUP_STATUS and status_screen:
        return status_screen
    if setting == STATUS_PRIMARY:
        return primary
    if setting == STATUS_SECONDARY:
        return next((screen for screen in screens if not screen.is_primary), primary)
    if setting.startswith("screen:"):
        try:
            return _screen_by_index(screens, int(setting.split(":", 1)[1])) or status_screen or primary
        except ValueError:
            return status_screen or primary
    return _screen_by_index(screens, active_screen_index) or status_screen or next(
        (screen for screen in screens if screen.is_primary),
        screens[0],
    )


def summarize_activity(snapshot: ForegroundSnapshot, duration_text: str) -> str:
    app = snapshot.app_name or "unknown"
    title = " ".join((snapshot.window_title or "(無標題)").split())
    return f"{snapshot.mode}｜{app}｜{title}｜{duration_text}"


def sanitized_screen_geometry(screen: object, index: int, primary_name: str | None = None) -> DisplayGeometry:
    geometry = screen.geometry()
    available = screen.availableGeometry()
    name = screen.name() or f"Screen {index + 1}"
    return DisplayGeometry(
        name=name,
        index=index,
        is_primary=bool(primary_name and name == primary_name),
        x=geometry.x(),
        y=geometry.y(),
        width=geometry.width(),
        height=geometry.height(),
        available_x=available.x(),
        available_y=available.y(),
        available_width=available.width(),
        available_height=available.height(),
        device_pixel_ratio=float(screen.devicePixelRatio()),
    )


def mark_primary(screens: list[DisplayGeometry]) -> list[DisplayGeometry]:
    if not screens or any(screen.is_primary for screen in screens):
        return screens
    return [replace(screen, is_primary=(screen.index == 0)) for screen in screens]


def _screen_by_index(screens: list[DisplayGeometry], index: int | None) -> DisplayGeometry | None:
    if index is None:
        return None
    return next((screen for screen in screens if screen.index == index), None)

from __future__ import annotations

from datetime import datetime

from .classifier import classify_mode
from .display import classify_window_presentation
from .models import DisplayGeometry
from .models import ForegroundSnapshot


def _windows_idle_seconds() -> int:
    try:
        import ctypes
        from ctypes import wintypes

        class LastInputInfo(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

        info = LastInputInfo()
        info.cbSize = ctypes.sizeof(info)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return 0
        millis = ctypes.windll.kernel32.GetTickCount() - info.dwTime
        return max(0, int(millis / 1000))
    except Exception:
        return 0


def is_windows_locked_or_unavailable() -> bool:
    try:
        import ctypes
        from ctypes import wintypes

        wtsapi32 = ctypes.windll.wtsapi32
        kernel32 = ctypes.windll.kernel32
        active_session_id = kernel32.WTSGetActiveConsoleSessionId()
        if active_session_id == 0xFFFFFFFF:
            return True

        buffer = ctypes.c_void_p()
        bytes_returned = wintypes.DWORD()
        wts_connect_state = 8
        ok = wtsapi32.WTSQuerySessionInformationW(
            None,
            active_session_id,
            wts_connect_state,
            ctypes.byref(buffer),
            ctypes.byref(bytes_returned),
        )
        if not ok:
            return False
        try:
            state = ctypes.cast(buffer, ctypes.POINTER(wintypes.DWORD)).contents.value
        finally:
            wtsapi32.WTSFreeMemory(buffer)
        return state != 0
    except Exception:
        return False


def get_foreground_snapshot(now: datetime | None = None) -> ForegroundSnapshot:
    now = now or datetime.now()
    idle_seconds = _windows_idle_seconds()
    app_name = "unknown"
    title = ""

    try:
        import psutil
        import win32gui
        import win32process

        hwnd = win32gui.GetForegroundWindow()
        title = win32gui.GetWindowText(hwnd) or ""
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        app_name = psutil.Process(pid).name()
    except Exception:
        app_name = "unsupported"
        title = "Foreground detection unavailable"

    return ForegroundSnapshot(
        timestamp=now,
        app_name=app_name,
        window_title=title,
        mode=classify_mode(app_name, title, idle_seconds),
        idle_seconds=idle_seconds,
    )


def get_foreground_window_presentation(screen: DisplayGeometry | None = None) -> str:
    try:
        import win32gui

        hwnd = win32gui.GetForegroundWindow()
        rect = win32gui.GetWindowRect(hwnd)
        title = win32gui.GetWindowText(hwnd) or ""
        return classify_window_presentation(rect, screen, title)
    except Exception:
        return "unknown"

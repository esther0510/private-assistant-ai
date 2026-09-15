"""Read-only Windows telemetry, scoped to a selected existing game window."""
from __future__ import annotations

import ctypes
from ctypes import wintypes

from .monitor import get_foreground_snapshot
from .rhythm_diagnostic import timestamp


def keyboard_devices():
    # Enumerate Raw Input devices only. Do not register or intercept input.
    class Device(ctypes.Structure):
        _fields_ = [("handle", wintypes.HANDLE), ("kind", wintypes.DWORD)]
    user = ctypes.WinDLL("user32", use_last_error=True)
    count = wintypes.UINT()
    size = ctypes.sizeof(Device)
    if user.GetRawInputDeviceList(None, ctypes.byref(count), size) == -1:
        return []
    devices = (Device * count.value)()
    if user.GetRawInputDeviceList(devices, ctypes.byref(count), size) == -1:
        return []
    names = []
    user.GetRawInputDeviceInfoW.argtypes = [wintypes.HANDLE, wintypes.UINT, ctypes.c_void_p, ctypes.POINTER(wintypes.UINT)]
    for device in devices:
        if device.kind != 1:
            continue
        length = wintypes.UINT()
        user.GetRawInputDeviceInfoW(device.handle, 0x20000007, None, ctypes.byref(length))
        name = ctypes.create_unicode_buffer(length.value + 1)
        if user.GetRawInputDeviceInfoW(device.handle, 0x20000007, name, ctypes.byref(length)) != -1:
            names.append(name.value)
    return names


def foreground_target():
    import win32gui
    import win32process
    hwnd = win32gui.GetForegroundWindow()
    snapshot = get_foreground_snapshot()  # Reuse the existing activity classifier.
    _, pid = win32process.GetWindowThreadProcessId(hwnd)
    return {"hwnd": hwnd, "pid": pid, "app": snapshot.app_name,
            "title": snapshot.window_title, "activity_mode": snapshot.mode}


def capture_environment(target):
    result = {"captured_at": timestamp(), **{k: target[k] for k in ("hwnd", "pid", "app", "title", "activity_mode")}, "unavailable": [],
              "fps": None, "frametime_ms": None, "gpu_load": None, "dropped_frames": None,
              "keydown_timestamp": None}
    old_context = None
    user = None
    try:
        import win32api
        import win32con
        import win32gui
        import win32process
        user = ctypes.WinDLL("user32", use_last_error=True)
        user.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        user.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
        old_context = user.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
        hwnd = target["hwnd"]
        if not win32gui.IsWindow(hwnd) or win32process.GetWindowThreadProcessId(hwnd)[1] != target["pid"]:
            raise ValueError("遊戲視窗已關閉或替換")
        monitor = win32api.MonitorFromWindow(hwnd, win32con.MONITOR_DEFAULTTONEAREST)
        info = win32api.GetMonitorInfo(monitor)
        mode = win32api.EnumDisplaySettings(info["Device"], win32con.ENUM_CURRENT_SETTINGS)
        rect = win32gui.GetWindowRect(hwnd)
        client = win32gui.GetClientRect(hwnd)
        style = win32gui.GetWindowLong(hwnd, win32con.GWL_STYLE)
        covers = all(abs(a-b) <= 2 for a, b in zip(rect, info["Monitor"]))
        result.update(monitor=info["Device"], display_px=[mode.PelsWidth, mode.PelsHeight],
                      is_foreground=win32gui.GetForegroundWindow() == hwnd,
                      minimized=bool(win32gui.IsIconic(hwnd)),
                      client_px=[client[2]-client[0], client[3]-client[1]],
                      refresh_hz=mode.DisplayFrequency if mode.DisplayFrequency > 1 else None,
                      presentation="fullscreen_or_borderless" if covers and not style & win32con.WS_CAPTION else "windowed",
                      window_rect=list(rect))
        result["presentation_note"] = "覆蓋螢幕時無法只靠視窗幾何區分獨佔全螢幕與無邊框；請填遊戲設定"
        dpi_x, dpi_y = wintypes.UINT(), wintypes.UINT()
        shcore = ctypes.WinDLL("shcore")
        shcore.GetDpiForMonitor.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.POINTER(wintypes.UINT), ctypes.POINTER(wintypes.UINT)]
        if shcore.GetDpiForMonitor(int(monitor), 0, ctypes.byref(dpi_x), ctypes.byref(dpi_y)) == 0:
            result["dpi_percent"] = round(dpi_x.value / 96 * 100, 2)
        else:
            result["unavailable"].append("Windows DPI scaling")
    except Exception as exc:
        result["unavailable"].append(str(exc))
    finally:
        if old_context and user:
            user.SetThreadDpiAwarenessContext(old_context)
    return result


class CpuSampler:
    def __init__(self, pid):
        import psutil
        self.psutil = psutil
        self.process = psutil.Process(pid)
        self.process.cpu_percent(None)
        psutil.cpu_percent(None)

    def sample(self):
        try:
            return {"at": timestamp(), "system_cpu_percent": self.psutil.cpu_percent(None),
                    "game_cpu_percent": self.process.cpu_percent(None) / (self.psutil.cpu_count() or 1)}
        except self.psutil.Error:
            return {"at": timestamp(), "system_cpu_percent": None, "game_cpu_percent": None}

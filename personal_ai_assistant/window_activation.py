from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .app_profiles import app_profile, process_profile
from .app_resolver import AppResolver, ResolveCandidate, normalize_alias


@dataclass(frozen=True)
class AppWindow:
    hwnd: int
    process_name: str
    title: str
    minimized: bool = False
    last_active_rank: int = 0
    hidden: bool = False
    main: bool = True
    client_name: str = ""


@dataclass(frozen=True)
class ActivationResult:
    ok: bool
    action: str
    message: str
    candidate: ResolveCandidate | None = None
    needs_confirmation: bool = False
    candidates: tuple[ResolveCandidate, ...] = ()


class WindowsWindowController:
    def list_windows(self) -> tuple[AppWindow, ...]:
        try:
            import psutil
            import win32gui
            import win32process
        except Exception:
            return ()

        windows: list[AppWindow] = []

        def callback(hwnd: int, _extra: object) -> bool:
            visible = bool(win32gui.IsWindowVisible(hwnd))
            title = win32gui.GetWindowText(hwnd).strip()
            if not title:
                return True
            try:
                _thread_id, pid = win32process.GetWindowThreadProcessId(hwnd)
                process_name = psutil.Process(pid).name()
            except Exception:
                process_name = ""
            class_name = win32gui.GetClassName(hwnd).casefold()
            if any(token in class_name for token in ('trayicon', 'tooltip', 'shadow', 'observer', 'hook', 'message')):
                return True
            if win32gui.GetWindow(hwnd, 4) or win32gui.GetWindowLong(hwnd, -20) & 0x80:
                return True
            left, top, right, bottom = win32gui.GetWindowRect(hwnd)
            if not win32gui.IsIconic(hwnd) and (right - left < 100 or bottom - top < 80):
                return True
            client_name = ''
            if process_name.casefold() == 'steamwebhelper.exe':
                # Modern Steam hosts its actual main UI in the helper. A helper
                # process alone is never evidence of a ready client/window.
                if class_name != 'sdl_app' or title.casefold() != 'steam':
                    return True
                try:
                    if not any(parent.name().casefold() == 'steam.exe' for parent in psutil.Process(pid).parents()):
                        return True
                except Exception:
                    return True
                client_name = 'Steam'
            profile = app_profile(client_name) if client_name else process_profile(process_name)
            if not visible and (not profile or win32gui.GetWindow(hwnd, 4)):
                return True
            if profile and not visible:
                # Hidden chat titles need not contain the brand. Exclude tool
                # windows instead of rejecting the real localized main window.
                try:
                    if win32gui.GetWindowLong(hwnd, -20) & 0x80:
                        return True
                except Exception:
                    pass
            windows.append(AppWindow(hwnd, process_name, title, bool(win32gui.IsIconic(hwnd)), len(windows), not visible, True, client_name))
            return True

        try:
            win32gui.EnumWindows(callback, None)
        except Exception:
            return ()
        return tuple(windows)

    def activate(self, window: AppWindow) -> bool:
        try:
            import win32con
            import win32gui
        except Exception:
            return False
        try:
            if window.minimized or win32gui.IsIconic(window.hwnd):
                win32gui.ShowWindow(window.hwnd, win32con.SW_RESTORE)
            else:
                win32gui.ShowWindow(window.hwnd, win32con.SW_SHOW)
            win32gui.SetForegroundWindow(window.hwnd)
            return self.verify(window)
        except Exception:
            try:
                win32gui.BringWindowToTop(window.hwnd)
                return self.verify(window)
            except Exception:
                return False

    def verify(self, window: AppWindow) -> bool:
        try:
            import win32gui
            import win32process
            import psutil
            hwnd = window.hwnd
            if not win32gui.IsWindow(hwnd) or not win32gui.IsWindowVisible(hwnd) or win32gui.IsIconic(hwnd):
                return False
            if win32gui.GetWindow(hwnd, 4) or win32gui.GetWindowLong(hwnd, -20) & 0x80:
                return False
            left, top, right, bottom = win32gui.GetWindowRect(hwnd)
            if right-left < 100 or bottom-top < 80 or not win32gui.IsWindowEnabled(hwnd):
                return False
            tid, pid = win32process.GetWindowThreadProcessId(hwnd)
            if psutil.Process(pid).name().casefold() != window.process_name.casefold():
                return False
            if win32gui.GetForegroundWindow() == hwnd:
                return True
            # The target's GUI thread can own an active visible window even when
            # Windows refuses cross-process foreground stealing.
            import ctypes
            from ctypes import wintypes
            class GUIINFO(ctypes.Structure):
                _fields_ = [('cbSize', wintypes.DWORD), ('flags', wintypes.DWORD),
                            *[(n, wintypes.HWND) for n in ('hwndActive','hwndFocus','hwndCapture','hwndMenuOwner','hwndMoveSize','hwndCaret')],
                            ('rcCaret', wintypes.RECT)]
            info = GUIINFO(); info.cbSize = ctypes.sizeof(info)
            return bool(ctypes.windll.user32.GetGUIThreadInfo(tid, ctypes.byref(info)) and info.hwndActive == hwnd)
        except Exception:
            return False


class ActivateOrLaunchService:
    def __init__(
        self,
        resolver: AppResolver,
        window_controller: WindowsWindowController | None = None,
        launcher: Callable[[ResolveCandidate], bool] | None = None,
    ) -> None:
        self.resolver = resolver
        self.window_controller = window_controller or WindowsWindowController()
        self.launcher = launcher or self._default_launch

    def _verified(self, window):
        verifier = getattr(self.window_controller, 'verify', None)
        return callable(verifier) and verifier(window) is True

    def _wait_window(self, queries, title_keyword='', timeout=1.5):
        deadline = time.monotonic() + timeout
        while True:
            windows = self.window_controller.list_windows()
            for query in queries:
                window = rank_matching_window(windows, query, title_keyword)
                if window:
                    self.window_controller.activate(window)
                    if self._verified(window):
                        return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(.05)  # Caller runs verification on the action worker.

    def activate_or_launch(self, query: str, title_keyword: str = "") -> ActivationResult:
        profile = app_profile(query)
        name = profile.name if profile else query
        window = rank_matching_window(self.window_controller.list_windows(), name, title_keyword)
        if window:
            self.window_controller.activate(window)
            if self._verified(window):
                return ActivationResult(True, 'activated', f'已切換到 {name}')
        running = bool(window) or bool(profile and self._client_running(profile))
        if running and profile and profile.restore_uri:
            try:
                import os
                os.startfile(profile.restore_uri)
                if self._wait_window((name,), title_keyword):
                    return ActivationResult(True, 'activated', f'已切換到 {name}')
            except OSError:
                pass
        result = self.resolver.resolve_app(name)
        if result.needs_confirmation:
            return ActivationResult(False, 'confirm', result.message, needs_confirmation=True, candidates=result.candidates)
        candidate = result.best
        if candidate:
            queries = (name, candidate.name, Path(candidate.path).stem)
            if not window:
                existing = self.window_controller.list_windows()
                found = next((w for q in queries if (w := rank_matching_window(existing, q, title_keyword))), None)
                if found:
                    running = True
                    self.window_controller.activate(found)
                    if self._verified(found):
                        return ActivationResult(True, 'activated', f'已切換到 {name}', candidate)
            # Reinvoke the indexed executable/shortcut to restore a tray instance.
            if self.launcher(candidate) and self._wait_window(queries, title_keyword):
                return ActivationResult(True, 'activated' if running else 'launched',
                                        f'已切換到 {name}' if running or name == 'LINE' else f'已開啟 {name}', candidate)
        message = f'找到 {name}，但主視窗沒有成功喚回' if running or candidate else result.message
        return ActivationResult(False, 'activate_failed' if running else 'launch_failed', message, candidate)

    @staticmethod
    def _client_running(profile):
        try:
            import psutil
            return any((p.info.get('name') or '').casefold() in profile.processes
                       for p in psutil.process_iter(['name']))
        except Exception:
            return False

    @staticmethod
    def _default_launch(candidate: ResolveCandidate) -> bool:
        path = candidate.path
        try:
            if path.lower().endswith(".lnk") or Path(path).is_dir():
                import os

                os.startfile(path)  # type: ignore[attr-defined]
            else:
                subprocess.Popen([path], close_fds=True)
        except OSError:
            return False
        return True


def rank_matching_window(windows: tuple[AppWindow, ...], query: str, title_keyword: str = "") -> AppWindow | None:
    profile = app_profile(query)
    alias = normalize_alias(profile.name if profile else query).replace(".exe", "")
    title_key = normalize_alias(title_keyword)
    scored: list[tuple[int, int, AppWindow]] = []
    for window in windows:
        if not window.main or not window.title or any(t in window.title.casefold() for t in ("updater", "trayicon", "notification sink")):
            continue
        process = normalize_alias(window.process_name).replace(".exe", "")
        title = normalize_alias(window.title)
        if profile and window.process_name.casefold() not in profile.processes and window.client_name != profile.name:
            continue
        score = 80 if profile and window.client_name == profile.name else 0
        if alias and process and (alias == process):
            score += 80
        if alias and alias in title:
            score += 45
        if title_key and title_key in title:
            score += 100
        if window.hidden:
            score -= 10
        if window.minimized:
            score -= 5
        if score > 0:
            scored.append((score, -window.last_active_rank, window))
    if not scored:
        return None
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return scored[0][2]

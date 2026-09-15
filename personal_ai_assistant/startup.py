from __future__ import annotations

import sys
from pathlib import Path


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APP_NAME = "PrivateAssistantAI"


def launcher_path(project_root: Path) -> Path:
    return project_root / "run_silent.pyw"


def startup_command(project_root: Path, pythonw_path: str | None = None) -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --background'
    executable = pythonw_path or str(Path(sys.executable).with_name("pythonw.exe"))
    if not Path(executable).exists():
        executable = sys.executable
    return f'"{executable}" "{launcher_path(project_root)}" --background'


def is_startup_enabled(app_name: str = APP_NAME) -> bool:
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as key:
            winreg.QueryValueEx(key, app_name)
        return True
    except Exception:
        return False


def set_startup_enabled(enabled: bool, project_root: Path, app_name: str = APP_NAME) -> bool:
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            if enabled:
                winreg.SetValueEx(key, app_name, 0, winreg.REG_SZ, startup_command(project_root))
            else:
                try:
                    winreg.DeleteValue(key, app_name)
                except FileNotFoundError:
                    pass
        return True
    except Exception:
        return False

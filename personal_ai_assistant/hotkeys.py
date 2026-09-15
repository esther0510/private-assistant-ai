from __future__ import annotations

import ctypes
from dataclasses import dataclass


MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
WM_HOTKEY = 0x0312
ERROR_HOTKEY_ALREADY_REGISTERED = 1409


@dataclass(frozen=True)
class ParsedHotkey:
    modifiers: int
    virtual_key: int
    label: str


@dataclass(frozen=True)
class HotkeyRegistrationResult:
    ok: bool
    message: str
    error_code: int = 0


def parse_hotkey(value: str | None) -> ParsedHotkey | None:
    text = " ".join((value or "").replace("+", " + ").split())
    if not text:
        return None
    parts = [part.strip().lower() for part in text.split("+") if part.strip()]
    if not parts:
        return None

    modifiers = 0
    key: str | None = None
    for part in parts:
        if part in {"ctrl", "control"}:
            modifiers |= MOD_CONTROL
        elif part == "alt":
            modifiers |= MOD_ALT
        elif len(part) == 1 and part.isalnum():
            key = part.upper()
        else:
            return None

    if modifiers == 0 or not key:
        return None
    label_parts: list[str] = []
    if modifiers & MOD_CONTROL:
        label_parts.append("Ctrl")
    if modifiers & MOD_ALT:
        label_parts.append("Alt")
    label_parts.append(key)
    return ParsedHotkey(modifiers=modifiers, virtual_key=ord(key), label="+".join(label_parts))


def hotkey_error_message(error_code: int) -> str:
    if error_code == ERROR_HOTKEY_ALREADY_REGISTERED:
        return "這組快捷鍵已被其他程式使用"
    if error_code:
        return f"Windows 無法註冊快捷鍵（錯誤碼 {error_code}）"
    return "Windows 無法註冊快捷鍵"


class GlobalHotkey:
    def __init__(self, hotkey_id: int = 0xA11A) -> None:
        self.hotkey_id = hotkey_id
        self._hwnd: int | None = None
        self._registered = False

    @property
    def registered(self) -> bool:
        return self._registered

    def register(self, hwnd: int, hotkey: str | None) -> HotkeyRegistrationResult:
        self.unregister()
        parsed = parse_hotkey(hotkey)
        if not parsed:
            return HotkeyRegistrationResult(False, "快捷鍵格式不正確，請使用例如 Ctrl+Alt+A")
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        if user32.RegisterHotKey(int(hwnd), self.hotkey_id, parsed.modifiers, parsed.virtual_key):
            self._hwnd = int(hwnd)
            self._registered = True
            return HotkeyRegistrationResult(True, f"已註冊 {parsed.label}")
        error_code = ctypes.get_last_error()
        return HotkeyRegistrationResult(False, hotkey_error_message(error_code), error_code)

    def unregister(self) -> None:
        if self._registered and self._hwnd is not None:
            ctypes.WinDLL("user32", use_last_error=True).UnregisterHotKey(self._hwnd, self.hotkey_id)
        self._registered = False
        self._hwnd = None

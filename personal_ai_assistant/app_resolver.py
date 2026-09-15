from __future__ import annotations

import os
import re
import time
import threading
import winreg

from .name_resolver import Entity, NameResolver, normalize_name
from dataclasses import dataclass
from pathlib import Path


APP_ALIASES = {
    "steam": ("steam", "steam.exe"),
    "discord": ("discord", "discord.exe", "update.exe"),
    "telegram": ("telegram", "telegram.exe"),
    "unity": ("unity", "unity.exe", "unity hub", "unity hub.exe"),
    "unity hub": ("unity hub", "unity hub.exe"),
    "chrome": ("chrome", "google chrome", "chrome.exe"),
    "edge": ("edge", "microsoft edge", "msedge.exe"),
    "browser": ("chrome", "edge", "msedge.exe", "chrome.exe"),
    "瀏覽器": ("chrome", "edge", "msedge.exe", "chrome.exe"),
}

FOLDER_ALIASES = {
    "下載": "Downloads",
    "下載資料夾": "Downloads",
    "downloads": "Downloads",
    "桌面": "Desktop",
    "desktop": "Desktop",
    "文件": "Documents",
    "文件資料夾": "Documents",
    "documents": "Documents",
    "專案": "Projects",
    "專案資料夾": "Projects",
}


@dataclass(frozen=True)
class ResolveCandidate:
    name: str
    path: str
    source: str


@dataclass(frozen=True)
class ResolveResult:
    query: str
    candidates: tuple[ResolveCandidate, ...]
    needs_confirmation: bool = False
    message: str = ""

    @property
    def best(self) -> ResolveCandidate | None:
        return self.candidates[0] if len(self.candidates) == 1 and not self.needs_confirmation else None


def normalize_alias(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip().lower())


class AppResolver:
    def __init__(self, learned_aliases: dict[str, str] | None = None, project_root: Path | None = None) -> None:
        self.learned_aliases = {normalize_alias(key): value for key, value in (learned_aliases or {}).items() if key and value}
        self.project_root = project_root
        self._index = []
        self._indexed_at = 0.0
        self._deadline = float("inf")
        self._index_lock = threading.Lock()
        self._index_worker = None

    def refresh_background(self):
        with self._index_lock:
            if self._index_worker and self._index_worker.is_alive():
                return self._index_worker
            if self._indexed_at and time.monotonic() - self._indexed_at <= 300:
                return None
            self._index_worker = threading.Thread(target=self._refresh_index, name='app-index', daemon=True)
            self._index_worker.start()
            return self._index_worker

    def _refresh_index(self):
        try:
            import ctypes
            ctypes.windll.kernel32.SetThreadPriority(ctypes.windll.kernel32.GetCurrentThread(), -1)
        except (AttributeError, OSError):
            pass
        self._deadline = time.monotonic() + 3.0
        try:
            items = self._dedupe([
                *self._from_app_paths(('',)), *self._from_start_menu(('',)),
                *self._from_uninstall_metadata(('',)), *self._from_common_paths(('',))])
            self._index = items
        finally:
            self._indexed_at = time.monotonic()


    def resolve_app(self, query: str) -> ResolveResult:
        alias = normalize_alias(query)
        if not alias:
            return ResolveResult(query, (), False, "請輸入要開啟的程式")
        learned = self.learned_aliases.get(alias)
        if learned and Path(learned).exists():
            return ResolveResult(query, (ResolveCandidate(query, learned, "learned_alias"),))

        names = NameResolver()
        result = names.resolve(query, self.entities())
        candidates = tuple(ResolveCandidate(r.entity.name, r.entity.target, 'name_resolver') for r in result.candidates)
        if result.best:
            candidates = candidates[:1]
        return ResolveResult(query, candidates, result.needs_confirmation,
                             '請選擇要開啟的程式' if candidates else f'找不到 {query}')

    def entities(self):
        worker = self.refresh_background()
        if not self._indexed_at and worker:
            worker.join(3.2)  # Only a cold exact miss waits; running/alias paths bypass this.
        items = list(self._index)
        for alias, path in self.learned_aliases.items():
            if Path(path).is_file():
                items.append(ResolveCandidate(Path(path).stem, path, 'learned_alias'))
        result = []
        for item in self._dedupe(items):
            aliases = tuple(key for key, path in self.learned_aliases.items() if path.casefold() == item.path.casefold())
            result.append(Entity('app:' + item.path.casefold(), item.name, 'app', item.path, aliases=aliases))
        return result

    def resolve_folder(self, query: str) -> ResolveResult:
        alias = normalize_alias(query)
        home = Path.home()
        targets: list[Path] = []
        mapped = FOLDER_ALIASES.get(alias)
        if mapped:
            if mapped == "Projects" and self.project_root:
                targets.append(self.project_root)
            targets.append(home / mapped)
        elif alias:
            expanded = Path(os.path.expandvars(os.path.expanduser(query)))
            if expanded.exists():
                targets.append(expanded)
        candidates = [
            ResolveCandidate(path.name or str(path), str(path), "folder_alias")
            for path in targets
            if path.exists()
        ]
        candidates = self._dedupe(candidates)
        if len(candidates) > 1:
            return ResolveResult(query, tuple(candidates), True, f"找到多個 {query}，請選擇資料夾")
        if candidates:
            return ResolveResult(query, tuple(candidates), False, f"已找到 {query}")
        return ResolveResult(query, (), False, f"找不到資料夾：{query}")

    def _from_app_paths(self, tokens: tuple[str, ...]) -> list[ResolveCandidate]:
        results: list[ResolveCandidate] = []
        roots = (
            (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\App Paths"),
            (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\App Paths"),
        )
        for hive, root in roots:
            try:
                with winreg.OpenKey(hive, root) as key:
                    count = winreg.QueryInfoKey(key)[0]
                    for index in range(count):
                        if time.monotonic() > self._deadline:
                            break
                        name = winreg.EnumKey(key, index)
                        if not self._matches(name, tokens):
                            continue
                        with winreg.OpenKey(key, name) as app_key:
                            value, _ = winreg.QueryValueEx(app_key, "")
                            path = str(value).strip('"')
                            if Path(path).exists():
                                results.append(ResolveCandidate(name, path, "app_paths"))
            except OSError:
                continue
        return results

    def _from_start_menu(self, tokens: tuple[str, ...]) -> list[ResolveCandidate]:
        roots = [
            Path(os.environ.get("APPDATA", "")) / r"Microsoft\Windows\Start Menu\Programs",
            Path(os.environ.get("PROGRAMDATA", "")) / r"Microsoft\Windows\Start Menu\Programs",
        ]
        results: list[ResolveCandidate] = []
        for root in roots:
            if not root.exists():
                continue
            for shortcut in root.rglob("*.lnk"):
                if time.monotonic() > self._deadline:
                    break
                if self._matches(shortcut.stem, tokens):
                    results.append(ResolveCandidate(shortcut.stem, str(shortcut), "start_menu"))
        return results

    def _from_uninstall_metadata(self, tokens: tuple[str, ...]) -> list[ResolveCandidate]:
        results: list[ResolveCandidate] = []
        roots = (
            (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
            (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
            (winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        )
        for hive, root in roots:
            try:
                with winreg.OpenKey(hive, root) as key:
                    count = winreg.QueryInfoKey(key)[0]
                    for index in range(count):
                        if time.monotonic() > self._deadline:
                            break
                        with winreg.OpenKey(key, winreg.EnumKey(key, index)) as app_key:
                            name = self._query_registry_value(app_key, "DisplayName")
                            if not name or not self._matches(name, tokens):
                                continue
                            install_location = self._query_registry_value(app_key, "InstallLocation")
                            display_icon = self._query_registry_value(app_key, "DisplayIcon")
                            for raw_path in (display_icon, install_location):
                                candidate = self._candidate_from_install_metadata(name, raw_path)
                                if candidate:
                                    results.append(candidate)
            except OSError:
                continue
        return results

    def _query_registry_value(self, key: object, name: str) -> str:
        try:
            value, _ = winreg.QueryValueEx(key, name)
        except OSError:
            return ""
        return str(value).strip()

    def _candidate_from_install_metadata(self, name: str, raw_path: str) -> ResolveCandidate | None:
        if not raw_path:
            return None
        cleaned = raw_path.strip('"')
        if "," in cleaned and cleaned.lower().endswith((".exe,0", ".exe,1")):
            cleaned = cleaned.rsplit(",", 1)[0]
        path = Path(cleaned)
        if path.is_file() and path.suffix.lower() in {".exe", ".lnk"}:
            return ResolveCandidate(name, str(path), "install_metadata")
        if path.is_dir():
            direct = path / f"{normalize_alias(name).replace(' ', '')}.exe"
            if direct.exists():
                return ResolveCandidate(name, str(direct), "install_metadata")
            exe_files = [p for p in path.glob("*.exe") if self._matches(p.stem, (name,)) and not any(t in p.stem.lower() for t in ("unins", "setup", "update", "crash"))]
            if len(exe_files) == 1:
                return ResolveCandidate(name, str(exe_files[0]), "install_metadata")
        return None

    def _from_common_paths(self, tokens: tuple[str, ...]) -> list[ResolveCandidate]:
        results = []
        roots = [Path(os.environ[key]) for key in ('ProgramFiles', 'ProgramFiles(x86)', 'LOCALAPPDATA') if os.environ.get(key)]
        deadline = min(self._deadline, time.monotonic() + 1.)
        for root in roots:
            for directory, dirs, files in os.walk(root, followlinks=False):
                if time.monotonic() > deadline:
                    return results
                depth = len(Path(directory).relative_to(root).parts)
                dirs[:] = [d for d in dirs if depth < 2 and not d.startswith('.') and not (Path(directory) / d).is_symlink()]
                for filename in files:
                    if filename.lower().endswith('.exe') and self._matches(filename, tokens) and not any(t in filename.lower() for t in ('unins', 'setup', 'crash', 'update')):
                        results.append(ResolveCandidate(Path(filename).stem, str(Path(directory) / filename), 'common_path'))
        return results

    def _matches(self, value: str, tokens: tuple[str, ...]) -> bool:
        normalized = normalize_alias(value)
        return any(normalize_alias(token).replace(".exe", "") in normalized.replace(".exe", "") for token in tokens)

    def _dedupe(self, candidates: list[ResolveCandidate]) -> list[ResolveCandidate]:
        seen: set[str] = set()
        unique: list[ResolveCandidate] = []
        for candidate in candidates:
            key = str(Path(candidate.path)).lower()
            if key in seen:
                continue
            seen.add(key)
            unique.append(candidate)
        return unique[:2000]

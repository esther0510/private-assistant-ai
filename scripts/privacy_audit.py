from __future__ import annotations

import re
import sys
import subprocess
from pathlib import Path


GIT_INDEX = "--git-index" in sys.argv
ARGS = [arg for arg in sys.argv[1:] if arg != "--git-index"]
ROOT = Path(ARGS[0]).resolve() if ARGS else Path(__file__).resolve().parent.parent
SKIP_DIRS = {"__pycache__", ".git", ".venv", "venv", "env", "dist", "build", "release", "work"}
SENSITIVE_PATTERNS = {
    "secret-like key": re.compile(r"(?i)(api[_-]?key|secret|token|password)\s*=\s*['\"][^'\"]{8,}['\"]"),
    "sqlite database": re.compile(r"\.(sqlite3|sqlite|db)$", re.I),
    "local absolute path": re.compile(r"[A-Z]:\\Users\\[^\\\s]+\\"),
    "logs/screenshots": re.compile(r"\.(log|png|jpg|jpeg|webp)$", re.I),
    "audio recording": re.compile(r"\.(wav|mp3|m4a|flac|ogg|opus)$", re.I),
    "wake template": re.compile(r"wake[_-]?templates?|voice[_-]?wake[_-]?template", re.I),
}


def iter_files(root: Path):
    for path in root.rglob("*"):
        if any(part in SKIP_DIRS for part in path.relative_to(root).parts):
            continue
        if path.is_file():
            yield path


def main() -> int:
    findings: list[str] = []
    if GIT_INDEX:
        names = subprocess.check_output(["git", "-C", str(ROOT), "ls-files", "-z"]).decode("utf-8").split("\0")
        paths = [ROOT / name for name in names if name]
    else:
        paths = iter_files(ROOT)
    for path in paths:
        relative = path.relative_to(ROOT)
        if path.name.lower().startswith('wake_calibration_'):
            findings.append(f'{relative}: personal microphone calibration must not be packaged')
            continue
        if path.name.lower() in {'entity_memory.json', 'entity_memory.tmp', 'file_index.json', 'game_preferences.json', 'settings.json', '.env'}:
            findings.append(f"{relative}: personal resolver/settings data must not be packaged")
            continue
        if SENSITIVE_PATTERNS["sqlite database"].search(path.name):
            findings.append(f"{relative}: database file should not be committed")
            continue
        if SENSITIVE_PATTERNS["logs/screenshots"].search(path.name):
            findings.append(f"{relative}: generated log/image artifact should be reviewed")
            continue
        if SENSITIVE_PATTERNS["audio recording"].search(path.name):
            findings.append(f"{relative}: microphone audio should not be packaged")
            continue
        if any(SENSITIVE_PATTERNS["wake template"].search(part) for part in path.parts):
            findings.append(f"{relative}: wake enrollment template should not be packaged")
            continue
        try:
            if GIT_INDEX:
                text = subprocess.check_output(["git", "-C", str(ROOT), "show", ":" + relative.as_posix()]).decode("utf-8")
            else:
                text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for name, pattern in SENSITIVE_PATTERNS.items():
            if name in {"sqlite database", "logs/screenshots", "audio recording", "wake template"}:
                continue
            if pattern.search(text):
                findings.append(f"{relative}: matched {name}")
    if findings:
        print("Privacy audit findings:")
        for finding in findings:
            print(f"- {finding}")
        return 1
    print("Privacy audit passed: no obvious user data, tokens, local paths, DBs, logs, or screenshots found.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

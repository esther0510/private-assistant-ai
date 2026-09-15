from __future__ import annotations

import os
import shutil
from pathlib import Path


APP_DIR_NAME = "PrivateAssistantAI"
DB_FILENAME = "assistant.sqlite3"
LEGACY_DB_FILENAMES = ("assistant.sqlite3", "assistant.db", "private_assistant.sqlite3")


def default_data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / APP_DIR_NAME
    return Path.home() / ".private_assistant_ai"


def default_db_path() -> Path:
    return default_data_dir() / DB_FILENAME


def migrate_legacy_repo_data(project_root: Path, target_dir: Path | None = None) -> list[Path]:
    """Copy legacy repo-local SQLite files into the user data directory if needed."""

    target_dir = target_dir or default_data_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    migrated: list[Path] = []
    canonical = target_dir / DB_FILENAME
    if canonical.exists():
        return migrated

    for filename in LEGACY_DB_FILENAMES:
        source = project_root / filename
        if source.exists() and source.is_file():
            shutil.copy2(source, canonical)
            migrated.append(source)
            break
    return migrated

from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta


CURRENT_VERSION = "v0.1.0-alpha"
RELEASES_API_URL = "https://api.github.com/repos/OWNER/private-assistant-ai-mvp/releases/latest"


@dataclass(frozen=True)
class UpdateCheckResult:
    checked: bool
    update_available: bool
    current_version: str
    latest_version: str | None = None
    error: str | None = None


def should_check_for_updates(
    enabled: bool,
    last_checked_at: datetime | None,
    now: datetime | None = None,
    interval: timedelta = timedelta(days=1),
) -> bool:
    if not enabled:
        return False
    if last_checked_at is None:
        return True
    return (now or datetime.now()) - last_checked_at >= interval


def check_latest_release(url: str = RELEASES_API_URL, current_version: str = CURRENT_VERSION, timeout: float = 3.0) -> UpdateCheckResult:
    if "OWNER/" in url:
        return UpdateCheckResult(False, False, current_version, error="GitHub repo URL 尚未設定")
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        latest = str(payload.get("tag_name") or payload.get("name") or "")
        return UpdateCheckResult(True, bool(latest and latest != current_version), current_version, latest or None)
    except Exception as exc:
        return UpdateCheckResult(False, False, current_version, error=str(exc))

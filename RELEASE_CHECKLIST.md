# v0.1.0-alpha Release Checklist

## Automated Checks

- [ ] `python -m compileall personal_ai_assistant tests`
- [ ] `python -m unittest discover -s tests -v`
- [ ] `python scripts/privacy_audit.py`
- [ ] Clean-profile smoke test with a new `%LOCALAPPDATA%\PrivateAssistantAI` directory
- [ ] Existing-profile migration smoke test from a repo-local legacy SQLite file
- [ ] PyInstaller build: `.\scripts\build_windows.ps1 -Clean`

## Manual Windows Checks

- [ ] `python run.py` opens the debug app.
- [ ] `run_silent.pyw` launches without a console window.
- [ ] `start_assistant.vbs` launches without a console window.
- [ ] Closing the main window keeps reminders running in the system tray.
- [ ] Tray menu can reopen, start/stop monitoring, show/hide the status bar, and exit fully.
- [ ] Startup toggle creates/removes the current-user startup entry.
- [ ] Lock, sleep, wake, and idle transitions pause/resume monitoring without duplicate timers.
- [ ] Critical reminders still appear while idle/game policies are active.
- [ ] Normal and low reminders choose less disruptive presentation during games/focus.
- [ ] Today Inbox suggestions remain quiet during games, video, idle, and focused work.
- [ ] App-context reminders trigger once on open/close transitions.
- [ ] Status bar shows private reminder text only within 30 minutes of due time.
- [ ] Multi-monitor status bar and popup placement work on real mixed-DPI monitors.
- [ ] Borderless fullscreen is not treated as ordinary desktop work.
- [ ] Exclusive fullscreen fallback does not steal focus or block the game.

## Privacy Checks

- [ ] Runtime data is under `%LOCALAPPDATA%\PrivateAssistantAI`.
- [ ] Settings export excludes reminders, history, notification body, learning records, keys, and activity events.
- [ ] Privacy Mode pauses monitoring/learning while reminders continue.
- [ ] Excluded apps suppress detailed event logging.
- [ ] Repo contains no real SQLite DB, logs, screenshots, `.env`, tokens, API keys, private paths, or real reminder data.

## GitHub Release Steps

- [ ] Restore or initialize the Git repository.
- [ ] Fill the real GitHub release URL in `personal_ai_assistant/updates.py`.
- [ ] Commit alpha changes.
- [ ] Create tag `v0.1.0-alpha`.
- [ ] Build the Windows distribution from a clean checkout.
- [ ] Draft GitHub Release as pre-release / experimental.
- [ ] Attach the clean portable Windows artifact.
- [ ] Include privacy statement, data location, no-API AI Handoff explanation, and `KNOWN_ISSUES.md`.

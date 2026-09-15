# Contributing

Thanks for helping improve Private Assistant AI.

This project is local-first and privacy-sensitive. Please keep contributions small, explain user-visible behavior changes, and avoid adding network calls to core reminder, monitoring, learning, or storage flows.

## Guidelines

- Do not commit personal SQLite files, logs, screenshots, `.env` files, API keys, or copied user activity.
- Keep AI features optional. The app must remain usable without paid APIs or hosted model accounts.
- Prefer transparent rules and settings over hard-coded personal habits.
- Add or update tests for parser, storage migration, privacy, learning, and reminder behavior changes.
- Keep Windows support working: foreground detection, system tray, silent startup, top status bar, reminder popups, and Windows notifications should not regress.

## Local checks

```powershell
python -m compileall personal_ai_assistant tests
python -m unittest discover -s tests
```

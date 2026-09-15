# Private Assistant AI

Version: `v0.1.0-alpha`

**Windows local-first proactive desktop assistant — Alpha / Experimental.**

Private Assistant AI observes local computer activity only when monitoring is enabled, manages reminders, learns lightweight interaction patterns on-device, and can warn when your workflow may be stuck in a repeated loop. Core features do not require an API key, a paid AI plan, or any external service.

## Privacy Principles

- Local-first by default.
- No screen, behavior, reminder, learning, or SQLite data is sent outside your machine by core features.
- AI is optional. The default AI Handoff flow copies a user-reviewable prompt to the clipboard and opens your chosen AI website. Advisor API is separate, opt-in, and off by default.
- Update checks are low-frequency and metadata-only. They can be disabled, and the release URL is a placeholder until the public repo is created.
- API keys are not required and are never hard-coded.
- Privacy mode pauses behavior monitoring, activity logging, learning, and dead-loop detection while keeping necessary reminder scheduling active.
- App exclusions prevent activity logging for sensitive executables such as password managers and banking windows.
- Exported settings include preferences and rules only. Activity history, reminders, notification contents, SQLite records, and learning data are excluded.

## Features

- Natural-language one-time reminders, such as `18:30 drink water`.
- Daily, weekly, interval, and activity/rest cycle reminders.
- Background reminder service that starts with the app.
- Optional Windows startup entry that launches quietly into the system tray.
- Monitoring is separate from reminders: stopping monitoring does not stop reminders.
- Windows system tray support. Closing the main window hides it instead of exiting.
- Silent startup via `run_silent.pyw` or `start_assistant.vbs`, without a CMD/PowerShell window.
- Foreground app/window detection on Windows.
- Top dynamic status bar with current mode, app, title summary, session duration, and only reminders due within 30 minutes.
- Multi-monitor status-bar and popup placement strategies with DPI-aware geometry helpers.
- Idle auto-pause using Windows last keyboard/mouse input, with a configurable threshold.
- Reminder popup, Windows notification, status-only reminder, deferred reminder strategies, and away-time return confirmation.
- Reminder priority: `critical`, `normal`, and `low` shown as necessary/general/casual in UI surfaces.
- Today Inbox for reminders without a precise time.
- App-context reminders such as "next time I open Code, remind me to check the build".
- Daily local activity summary from activity sessions, so content/title changes such as YouTube Shorts do not fragment the day.
- Recurring reminders reschedule after completion or ignore. Snooze delays only the current occurrence.
- Transparent learning framework for observed app usage, reminder outcomes, response time, and low-risk presentation preferences.
- Opt-in Beta external notification response learning based on source app, context metadata, away state, and time-to-visit signals. Full Windows notification ingestion remains limited in this build.
- Level 3 suggestions for risky changes such as schedule changes, disabling reminders, health-related reminders, or sleep-related reminders.
- Privacy mode, learning toggle, dead-loop toggle, and executable-based App exclusions.
- Clear learning reset and full personal-data reset.
- Settings export/import that excludes personal records.
- Optional no-API AI Handoff to ChatGPT, Grok, Claude, Gemini, or a custom website.
- Rule-based dead-loop detector using repeated app/window switches, repeated title/error signals, long active loops, and similar local events.
- First Advisor core: current state, high-level intent, activity sessions, manual/inferred goals, break/private-time aware next-action suggestions, conservative intervention levels, and an optional OpenAI-compatible Advisor API provider.

## Voice, Commands and Diagnostics

- **Voice wake / KWS:** local sherpa-onnx keyword spotting with a configurable assistant name; Whisper transcribes commands after wake. Enable voice in settings, select a microphone, wait for standby, then say the name and command.
- **Microphone gain:** shared automatic/manual gain, microphone test, wake-word test and device calibration. Noise, distance and pronunciation affect results.
- **Reminder operations:** add, edit, delete and undo recent operations through text/voice or the reminder list. Examples: `10分鐘後提醒我喝水`, `把喝水提醒刪掉`, `復原剛剛的操作`. Ambiguous matches require selection/confirmation; recent-reminder shortcuts expire and undo can be refused after subsequent changes.
- **Activity/context detection:** foreground app/window, idle/away state, activity sessions, local learning and rule-based suggestions. This is not general visual understanding of the screen.
- **App/Game launch:** resolve installed apps and games from local metadata and aliases; ambiguous targets require review. Availability depends on installation paths, launcher metadata and permissions.
- **Rhythm game diagnostics:** local A/B sessions, Windows display/CPU observations, manual results and optional CSV samples. FPS/frametime and timing results require supplied data; no automatic OCR or game-memory reading. See [RHYTHM_DIAGNOSTIC.md](RHYTHM_DIAGNOSTIC.md).

For optional voice support after the base install:

```powershell
python -m pip install -r requirements-voice.txt
```

First use downloads KWS and Whisper models (GitHub/Hugging Face); allow several GB of free disk space. KWS models live under `%LOCALAPPDATA%\PrivateAssistantAI\models`; Whisper uses its model cache. Downloaded models have separate upstream license terms and are not part of this source repository. After download, voice inference runs locally.

`START_HERE.txt` remains the guide for the friend testing package. A source checkout uses the installation steps below; `START.bat` expects a separately supplied `tools/uv/uv.exe`, which is excluded from Git. No executable package is included in the source repository.

Settings are edited in the app and stored locally. `.env.example` is documentation only: the app does not automatically load `.env` or these placeholder variables. Optional Advisor credentials are entered in the settings UI and saved in the local database; protect that database and never share it.

Voice recognition and natural-language time interpretation can be wrong: verify the displayed date, time and target. Sleep, shutdown or fully exiting the app prevents timely reminders. Closing the window normally leaves the tray process running. Rhythm diagnosis gives heuristic evidence, not causal proof or a direct latency measurement.

## Current Structure

The project keeps the existing working modules and adds compatibility-friendly open-source boundaries:

```text
personal_ai_assistant/
├─ app.py
├─ activity_sessions.py
├─ advisor_core.py
├─ display.py
├─ inbox.py
├─ main_window.py
├─ monitor.py
├─ reminders.py
├─ startup.py
├─ summary.py
├─ updates.py
├─ db.py
├─ settings.py
├─ storage.py
├─ assistant/
│  └─ dead_loop.py
├─ integrations/
│  └─ ai_handoff.py
├─ core/
└─ ui/
```

The older flat modules remain in place so the Alpha does not lose behavior through a large rewrite. Future contributors can gradually move code behind these boundaries.

## Data Location

Runtime data is stored outside the source tree:

```text
%LOCALAPPDATA%\PrivateAssistantAI\assistant.sqlite3
```

On non-Windows systems, the fallback is:

```text
~/.private_assistant_ai/assistant.sqlite3
```

If an older repo-local SQLite file exists, startup copies it to the user data directory when the target database does not already exist. The original file is left untouched so users can inspect or back it up.

The repository `.gitignore` excludes SQLite databases, logs, screenshots, local learning data, `.env` files, keys, virtual environments, and build outputs.

## Install

Use Windows 64-bit and Python 3.12 for the current development setup.

```powershell
cd path\to\private-assistant-ai-mvp
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

## Run

For normal use, double-click:

```text
run_silent.pyw
```

or:

```text
start_assistant.vbs
```

Both start the app quietly without leaving a command window open.

For debugging:

```powershell
python run.py
```

The reminder service starts automatically. The current default also starts monitoring; review the first-run notice and pause monitoring or enable privacy mode when needed.

## First-Time Setup

On first launch, review the settings row in the main window:

- `閒置時自動暫停監督`: pauses monitoring when you are away.
- `隱私模式`: pauses activity logging, learning, and dead-loop detection.
- `啟用學習`: controls local behavior learning.
- `外部通知反應學習 Beta`: lets the app learn notification response patterns from metadata and app-switch timing. It is off by default.
- `死循環提醒`: controls local stuck-loop alerts.
- `閒置門檻`: sets away detection time.
- `排除`: comma-separated executable names or keywords that should not be logged.
- `AI Handoff`: chooses which AI website opens when you ask for outside review. It does not change Advisor or OSD analysis source.
- `AI Advisor`: optional OpenAI-compatible API analysis with provider preset, provider-aware model dropdown, test connection, and local-only API key storage. It is off by default.

## Update Check

The alpha includes a disabled-safe update checker. Before publishing, replace the placeholder in `personal_ai_assistant/updates.py` and the `update_check_url` setting with the real GitHub Releases API URL:

```text
https://api.github.com/repos/<owner>/<repo>/releases/latest
```

The app never auto-installs updates.

## AI Handoff

AI Handoff does not call an API. When the dead-loop detector thinks you may be stuck, it can generate a clean prompt with:

- current activity if inferable,
- important local events from the last 10 to 30 minutes,
- repeated patterns,
- why the app suspects a loop,
- a request for the AI to avoid inheriting the current assumptions and examine alternatives.

When you choose `交給 AI 重新檢查`, the app copies that prompt to your clipboard and opens the selected AI website. You decide whether to paste and send it.

## Advisor Core

The `助理` tab shows:

- current understanding,
- current intent, such as working, intentional break, private time, away, or returning to work,
- current activity session duration,
- current goal,
- next-action suggestion,
- source (`本機規則` or `AI`) and simplified confidence.

Without an API key, this remains fully local and rule-based. It uses foreground app/title summaries, activity sessions, reminders, recent important state changes, learned break patterns, and the dead-loop score. Manual goals are never overwritten by inferred goals.

Activity sessions separate content changes from context switches. For example, multiple YouTube Shorts title changes remain one video session; YouTube to ChatGPT, video to chat, game to work, and similar context switches start a new session. A configurable grace period can merge brief returns to the same activity.

Break handling is intentionally non-moralizing. By default, short breaks stay silent/status-only for a 15 to 25 minute range. After enough local samples, the app learns median and average break length and only uses the personal threshold when sample count and confidence are high enough.

Advisor API is optional and separate from Handoff. It expects an OpenAI-compatible `/chat/completions` service such as OpenAI, Groq, OpenRouter, local Ollama-compatible gateways, LM Studio, or another compatible endpoint. Standard presets fill the endpoint automatically and hide the endpoint field; Custom OpenAI-compatible exposes it for advanced users. Models are selected from a provider-aware dropdown with built-in recommendations, optional `/models` refresh after an API key is entered, and a final `自訂模型...` escape hatch. ChatGPT Plus is not treated as an API key or API credit. If Advisor is off, incomplete, or its test connection fails, Advisor falls back to local rules and the UI/OSD says so explicitly.

## Development Checks

```powershell
python -m compileall personal_ai_assistant tests
python -m unittest discover -s tests
python scripts/privacy_audit.py --git-index
.\scripts\build_windows.ps1 -Clean
```

The privacy audit command scans staged Git content; stage intended source files before running it. Without `--git-index`, it reviews a package directory using the original package audit rules. Pattern scans cannot guarantee that all secrets or personal information have been detected.

## Alpha Limitations

- Windows is the primary supported platform.
- Natural-language parsing is intentionally limited and rule-based.
- Dead-loop detection is explainable but heuristic; expect false positives and false negatives.
- The current UI is practical, not polished. First-run setup is intentionally small.
- Global hotkeys can conflict with other apps and need validation on each Windows setup.
- Exclusive fullscreen cannot always be overlaid safely; the app should fall back to non-intrusive Windows notification/status strategies.
- Advisor API support is opt-in and currently generic OpenAI-compatible only. Handoff remains no-API website handoff.
- External notification learning is opt-in Beta. This build can learn from metadata events and app-switch timing, while broad Windows UserNotificationListener ingestion remains limited/unavailable.
- Formal reminders do not have an ignore button; they use `完成` and `10 分鐘後`. Assistant-generated message prompts can show `去看看`, `稍後提醒`, and `忽略`.
- The module layout is partly transitional to preserve MVP behavior.

## Roadmap

- Add a small first-run dialog for users who prefer guided setup.
- Improve App exclusion matching and add per-window rules.
- Add a local-only activity review screen with deletion controls.
- Support more recurrence language and locale variants.
- Add richer local model/provider adapters behind the Advisor provider interface.
- Add packaged Windows releases.
- Expand privacy tests and migration tests.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Please do not submit issues, logs, screenshots, databases, or reproduction files containing private reminders, window titles, credentials, API keys, or personal activity history.

## Security

See [SECURITY.md](SECURITY.md).

## License

MIT. See [LICENSE](LICENSE).

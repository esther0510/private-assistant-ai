# Known Issues for v0.1.0-alpha

This is an experimental Windows-only alpha. Core reminders work local-first without an API key, but several desktop-integration behaviors need real-machine validation before a broad public announcement.

## Needs Manual Verification

- Multi-monitor placement should be tested on real 1, 2, and 3 monitor layouts, including mixed 100/125/150/175/200% DPI.
- Exclusive fullscreen games may block overlays. The intended behavior is to avoid forcing an overlay and use a lower-interruption notification/status path.
- Borderless fullscreen classification is best-effort and depends on foreground window geometry/title hints.
- Windows startup uses the current user's Run key. Managed/company devices may block registry writes.
- Global quick-add uses Windows OS-level `RegisterHotKey` by default. It should still be manually tested against browsers, chat apps, games, and any machine where Ctrl+Alt+A may already be reserved.
- Windows notification learning v1 is opt-in Beta. It can learn from recorded notification metadata and the time it takes the user to switch to the source app, but broad UserNotificationListener ingestion remains limited/unavailable in this Python build.
- Formal reminders are deferred while the user is away/idle/locked and are confirmed when the user returns. Critical health reminders may still use a low-interruption Windows notification, but they are not treated as handled until the user confirms.

## Product Limitations

- Natural-language reminder parsing supports common Traditional Chinese and simple time patterns, not a full calendar grammar.
- Today Inbox suggestions are intentionally quiet and may wait too long in heavy work/game sessions.
- Formal reminder popups intentionally offer `完成` and `10 分鐘後` only. `忽略` is reserved for assistant-generated prompts such as external message suggestions or dead-loop alerts.
- Dead-loop detection is rule-based and can produce false positives or miss subtle loops.
- AI Handoff opens a website and copies a prompt; it does not call ChatGPT, Claude, Gemini, or any API directly.
- First-run setup is deliberately lightweight rather than a step-by-step wizard.

## Public Alpha Packaging / Remaining Verification

- Download the Windows x64 EXE ZIP from [v0.1.0-alpha](https://github.com/esther0510/private-assistant-ai/releases/tag/v0.1.0-alpha). The older uv/bootstrap ZIP is not the public release artifact.
- In-app update discovery remains unconfigured. Download updates manually from GitHub Releases; this packaging release does not change update logic.
- Clean-profile smoke validation on the maintainer's Windows machine is not equivalent to testing another physical computer or a fresh Windows VM. Microphone, antivirus and hardware compatibility still need external testing.
- The executable is unsigned, and first voice use requires model downloads. See the release notes for the scope of lightweight verification.

## 本機 KWS 語音喚醒

- 正式路徑為 sherpa-onnx 1.13.8、zh-en Zipformer 3M 2025-12-20（CPU int8），不需聲音模板。模型首次下載約數十 MB，選用模型檔約 8 MB；完整 STT 模型另於首次喚醒後下載。
- 普通話中文使用拼音，英文使用隨模型提供的詞典；罕見英文、非中英文符號會明確顯示不支援。多音字採詞典讀音，沒有個人聲紋訓練，不能保證方言或所有名字的辨識率。
- 同音語音、電視或遊戲音訊仍可能喚醒。請以自己的麥克風做 8 秒測試；靈敏度可調整。模型 API 不提供單次 confidence。
- 待機只用 CPU；完整 Whisper 沿用原本裝置設定，喚醒後可能使用 GPU。未完成遊戲併行或長時誤觸率測試。


## 2026-09-12 voice/app verification update

App activation now requires an enabled, visible, restored main window plus foreground or target-thread active-window evidence. Qt tray-message/tooltip/shadow windows are excluded. Modern Steam's SDL main window may belong to steamwebhelper.exe, but is accepted only with a steam.exe ancestor; the helper process alone is never client readiness. Executable/shortcut fallback is verified by polling and can report failure on machines where startup takes longer than the verification window.

Whisper remains a warm multilingual small model with automatic language selection and bounded local context. Context is a hint, not a guarantee of precise English spelling. Local alias/phonetic ranking handles known spoken variants, and ambiguous targets require a selection. Game URI dispatch is reported as a request, not verified gameplay. The 1–2 second end-of-speech goal depends on hardware and model readiness; app activation timings alone do not measure speech latency.

The KWS detector source, thresholds and wake endpoint core were not changed. Post-wake capture has bounded waiting and cancellation; a native STT inference already in progress cannot be forcibly interrupted, but stale results are ignored and the microphone can be re-armed. Voice diagnostics expire after 10 minutes; audio is not persisted.

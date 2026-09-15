# v0.1.0-alpha publication

This release packages the existing Windows application and improves public documentation. Core application logic is unchanged.

## Package

- Windows x64 executable plus its complete `_internal` directory.
- English and Traditional Chinese start guides, README files, license and known limitations.
- No Python installation required. Voice libraries are included; KWS and Whisper models download on first use.
- Files are collected only from the clean PyInstaller output and an explicit public-document allowlist. Development folders and personal profiles are excluded.

## Lightweight validation scope

- Compare packaged application bytecode with the corresponding source.
- Inspect build input provenance, package filenames and textual data for credentials, personal paths and runtime data.
- Check ZIP CRC, extract to a separate folder containing spaces and Chinese characters, then run the packaged smoke entry point with empty local data.
- Smoke covers Qt startup, empty defaults, bundled voice imports/native VAD, add/edit/delete/undo/due reminder behavior, main window event loop and complete exit.
- Review the actual clean-start screenshot separately: the strict image audit flag is expected and remains enabled.
- Check bilingual local README links and public release/download URLs after publishing.

Full regression tests, new model downloads, live microphone accuracy tests and cross-machine compatibility tests are outside this packaging validation. No claims of those tests are made for this publication.

## Remaining limits

Unsigned Windows-only Alpha; primarily Traditional Chinese UI and command grammar. Voice and activity suggestions may misjudge input. Sleep/exit can delay or prevent reminders. In-app update discovery remains unconfigured; use GitHub Releases manually.

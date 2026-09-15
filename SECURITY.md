# Security

Private Assistant AI can store reminders, app names, window title summaries, learning patterns, and local activity events. Treat logs and databases as private.

## Please do not share

- SQLite databases or exported full user data
- Raw logs with window titles or reminder text
- Screenshots or captures from private apps
- `.env` files, API keys, tokens, cookies, or credentials
- Reproduction steps that include private messages, banking details, passwords, or health details

## Reporting

For public issues, remove personal content and replace it with placeholders. If a bug requires sensitive context, describe the behavior at a high level first and wait for maintainer guidance.

Core features should remain local-first. Any feature that sends screen, behavior, reminder, or learning data outside the machine must be opt-in, documented, and reviewable by the user.

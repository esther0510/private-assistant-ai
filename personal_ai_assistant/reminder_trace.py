"""Opt-in DEBUG diagnostics; no persistent user transcript logging by default."""
import json
import logging


def trace_reminder(stage, **values):
    logger = logging.getLogger("personal_ai_assistant.reminder_trace")
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug("reminder %s", json.dumps(dict(stage=stage, **values),
                                              default=str, ensure_ascii=False))

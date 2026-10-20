"""Structured JSON logs: one object per line on stdout."""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime

LOGGER_NAME = "anm"


class JsonFormatter(logging.Formatter):
    def __init__(self, run_id: str) -> None:
        super().__init__()
        self.run_id = run_id

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "run_id": self.run_id,
            "event": record.getMessage(),
        }
        payload.update(getattr(record, "fields", {}))
        return json.dumps(payload, ensure_ascii=False, default=str)


def setup_logging(run_id: str, level: int = logging.INFO) -> logging.Logger:
    logger = logging.getLogger(LOGGER_NAME)
    logger.handlers.clear()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(run_id))
    logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False
    return logger


def log(event: str, level: int = logging.INFO, **fields: object) -> None:
    """Log an event; conventional fields are phase, url, result."""
    logging.getLogger(LOGGER_NAME).log(level, event, extra={"fields": fields})

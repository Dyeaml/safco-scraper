"""
core/logging_config.py
=====================
Structured logging setup. JSON logs in production (machine-parseable, one
object per line, easy to ship to a log aggregator); human-readable console
logs in dev. Every log record can carry a `run_id` and other context so a
crawl is traceable end-to-end.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


class JsonFormatter(logging.Formatter):
    """One JSON object per log line."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        # Attach any extra structured fields (run_id, url, agent, ...).
        for key, value in record.__dict__.items():
            if key not in _STD_ATTRS and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


_STD_ATTRS = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__.keys()) | {
    "message", "asctime"
}


def setup_logging(level: str = "INFO", fmt: str = "json",
                  file: Optional[str] = None) -> logging.Logger:
    logger = logging.getLogger("scraper")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.handlers.clear()

    if fmt == "json":
        formatter: logging.Formatter = JsonFormatter()
    else:
        formatter = logging.Formatter(
            "%(asctime)s %(levelname)-7s %(name)s | %(message)s", "%H:%M:%S"
        )

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(formatter)
    logger.addHandler(stream)

    if file:
        Path(file).parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(file, encoding="utf-8")
        fh.setFormatter(JsonFormatter())  # always JSON to file
        logger.addHandler(fh)

    logger.propagate = False
    return logger


__all__ = ["setup_logging", "JsonFormatter"]

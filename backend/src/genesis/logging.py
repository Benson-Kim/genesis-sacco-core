"""Structured JSON logging with correlation IDs and PII scrubbing (the house gates)."""

import json
import logging
import re
from contextvars import ContextVar

correlation_id_var: ContextVar[str] = ContextVar("correlation_id", default="-")

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
_PHONE = re.compile(r"\+?\d[\d\s-]{8,}\d")


def scrub(text: str) -> str:
    """Redact obvious PII (emails, phone-like digit runs) from log text."""
    return _PHONE.sub("[redacted]", _EMAIL.sub("[redacted]", text))


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, str] = {
            "ts": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "message": scrub(record.getMessage()),
            "correlation_id": correlation_id_var.get(),
        }
        if record.exc_info:
            payload["exc"] = scrub(self.formatException(record.exc_info))
        return json.dumps(payload)


def configure_logging(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)

"""Structured JSON logging with correlation IDs and PII scrubbing (the house gates)."""

import json
import logging
import re
import uuid
from contextvars import ContextVar

correlation_id_var: ContextVar[str] = ContextVar("correlation_id", default="-")

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
_PHONE = re.compile(r"\+?\d[\d\s-]{8,}\d")
# National-ID-like digit runs (Kenyan IDs are 7-8 digits; 9 catches
# padded forms). Deliberately floored at 7 so code-owned diagnostics
# with shorter numbers (e.g. the 6-digit cron_lock namespace) survive;
# phone-length runs (10+) are already covered by _PHONE above.
_ID_NUMBER = re.compile(r"\b\d{7,9}\b")


def scrub(text: str) -> str:
    """Redact obvious PII (emails, phone-like and national-ID-like digit runs)."""
    return _ID_NUMBER.sub("[redacted]", _PHONE.sub("[redacted]", _EMAIL.sub("[redacted]", text)))


def new_run_id() -> str:
    """Correlation id for one cron one-shot cycle (issue #4).

    The four ``backend/scripts/cron_*.py`` entrypoints set this into
    ``correlation_id_var`` at cycle start, so every log record a cycle
    emits — across application and infrastructure modules — carries
    the same greppable run id, the worker analogue of the request id.
    """
    return f"run-{uuid.uuid4().hex}"


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

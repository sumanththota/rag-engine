"""Structured JSON logging foundation (Ticket 1 of 4).

Provides a JSON line formatter and a `configure_logging()` entrypoint that
attaches a rotating file handler to the root logger. This is the base layer
for a future Loki/Grafana observability stack — that stack itself, along
with trace_id population (a ContextVar/Filter, Ticket 3) and CLI wiring
(Ticket 2), are explicitly out of scope here.

stdlib `logging` only — no new dependencies.
"""

import json
import logging
import logging.handlers
from datetime import datetime, timezone
from pathlib import Path

# Single deployment target today; not worth config plumbing yet.
ENV = "local"

# Attribute names present on a bare LogRecord — used to detect caller-supplied
# `extra={...}` fields without re-emitting standard LogRecord noise.
_STANDARD_RECORD_ATTRS = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__.keys())


class JsonFormatter(logging.Formatter):
    """Formats each LogRecord as a single JSON line."""

    def __init__(self, service: str) -> None:
        super().__init__()
        self._default_service = service

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "service": getattr(record, "service", self._default_service),
            "env": ENV,
            "stage": getattr(record, "stage", None),
            "trace_id": getattr(record, "trace_id", None),
            "logger": record.name,
            "message": record.getMessage(),
        }

        for key, value in record.__dict__.items():
            if key in _STANDARD_RECORD_ATTRS or key in payload:
                continue
            payload[key] = value

        return json.dumps(payload)


def configure_logging(
    service: str, log_path: str = "logs/rag-engine.log", level: int = logging.INFO
) -> None:
    """Attaches a JSON-formatted RotatingFileHandler to the root logger.

    Idempotent: safe to call more than once without duplicating handlers.
    Must only be invoked explicitly from a production entrypoint (e.g.
    app/main.py's bootstrap()) — never at import time, and never from
    create_app(), which the test suite calls directly and must stay
    hermetic (no logs/ writes as a side effect of running pytest).
    """
    Path(log_path).parent.mkdir(parents=True, exist_ok=True)

    root_logger = logging.getLogger()
    root_logger.handlers.clear()

    handler = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=10 * 1024 * 1024, backupCount=5
    )
    handler.setFormatter(JsonFormatter(service))
    root_logger.addHandler(handler)
    root_logger.setLevel(level)

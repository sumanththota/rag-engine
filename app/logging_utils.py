"""Structured JSON logging foundation (Ticket 1 of 4), now with per-chat-turn
trace_id population (Ticket 3 of 4).

Provides a JSON line formatter, a `configure_logging()` entrypoint that
attaches a rotating file handler to the root logger, and a
contextvars.ContextVar + logging.Filter pair that stamps every LogRecord
with whatever trace_id is currently in scope — with zero signature changes
to anything downstream (app/rag.py, app/pdfextract.py, ...). Callers just
set `trace_id_var` for the duration of a logical unit of work (e.g. one
HTTP handler) and every log line emitted anywhere underneath picks it up
automatically.

stdlib `logging`/`contextvars` only — no new dependencies.
"""

import contextvars
import json
import logging
import logging.handlers
import random
from datetime import datetime, timezone
from pathlib import Path

# One trace_id per logical unit of work (e.g. one chat turn, which itself
# spans two separate HTTP requests: POST /chat/start then GET /chat/stream).
# Callers `.set()` this for the duration of their handler and `.reset()` it
# when done; TraceIdFilter below reads it back out for every LogRecord.
trace_id_var: "contextvars.ContextVar[str | None]" = contextvars.ContextVar(
    "trace_id", default=None
)


def new_trace_id() -> str:
    """8-hex-char id, matching the format app/main.py's new_req_id() already
    uses elsewhere — same shape, kept as a separate helper so this module
    stays decoupled from app/main.py."""
    return f"{random.getrandbits(32):08x}"


class TraceIdFilter(logging.Filter):
    """Stamps `record.trace_id` from `trace_id_var`. Always returns True —
    this Filter exists purely to annotate records, never to suppress them.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.trace_id = trace_id_var.get()
        return True


_trace_id_filter = TraceIdFilter()


def install_trace_id_filter() -> None:
    """Attaches the shared TraceIdFilter to the root logger AND to every
    logger already registered in the hierarchy. Idempotent (same Filter
    instance reused; Filterer.addFilter no-ops on a duplicate).

    Why every logger and not just root: `logging.Logger.filter()` only ever
    consults that logger's OWN `.filters` list — it is *not* consulted for
    ancestor loggers while a record propagates upward (only ancestors'
    *handlers* run during that walk, each with their own separate filter
    list). This app logs through several distinct named loggers ("server",
    "rag.pdfextract", "rag.ingest", "rag.retrieve", "rag.*.cli"), so a
    filter attached only to the root Logger object would never see records
    from any of them — trace_id would come out empty everywhere. Attaching
    to every logger means the annotation happens at the record's own
    originating `Logger.handle()` call, before ANY handler downstream ever
    sees it — which is what actually makes this filter-based approach work
    uniformly for our own RotatingFileHandler *and* for pytest's caplog
    fixture, which attaches its own separate handler straight to root.

    Only covers loggers that exist at call time — fine here since
    configure_logging() runs from bootstrap(), well after all app modules
    (and therefore all their module-level `logging.getLogger(...)` calls)
    have already been imported.
    """
    root_logger = logging.getLogger()
    root_logger.addFilter(_trace_id_filter)
    for existing_logger in root_logger.manager.loggerDict.values():
        if isinstance(existing_logger, logging.Logger):
            existing_logger.addFilter(_trace_id_filter)


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
    install_trace_id_filter()

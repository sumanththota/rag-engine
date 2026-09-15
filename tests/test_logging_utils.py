"""Unit tests for app/logging_utils.py's JsonFormatter — isolated, no HTTP,
no app wiring. Constructs a LogRecord directly and checks the emitted JSON
schema."""

import json
import logging

from app.logging_utils import ENV, JsonFormatter

_EXPECTED_KEYS = {
    "timestamp", "level", "service", "env", "stage", "trace_id", "logger", "message",
}


def _make_record(**extra) -> logging.LogRecord:
    record = logging.LogRecord(
        name="rag.ingest",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="hello %s",
        args=("world",),
        exc_info=None,
    )
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_json_formatter_emits_expected_key_set():
    formatter = JsonFormatter(service="rag-server")
    record = _make_record()

    line = formatter.format(record)
    parsed = json.loads(line)

    assert set(parsed.keys()) == _EXPECTED_KEYS
    assert parsed["level"] == "info"
    assert parsed["service"] == "rag-server"
    assert parsed["env"] == ENV
    assert parsed["stage"] is None
    assert parsed["trace_id"] is None
    assert parsed["logger"] == "rag.ingest"
    assert parsed["message"] == "hello world"


def test_json_formatter_includes_extra_fields():
    formatter = JsonFormatter(service="rag-server")
    record = _make_record(stage="ingest", chunks=6, err="timeout")

    parsed = json.loads(formatter.format(record))

    assert parsed["stage"] == "ingest"
    assert parsed["chunks"] == 6
    assert parsed["err"] == "timeout"
    # Extra fields are additional keys, not a replacement of the base schema.
    assert _EXPECTED_KEYS.issubset(parsed.keys())


def test_json_formatter_falls_back_to_default_service_when_absent():
    formatter = JsonFormatter(service="rag-server")
    record = _make_record()

    parsed = json.loads(formatter.format(record))

    assert parsed["service"] == "rag-server"

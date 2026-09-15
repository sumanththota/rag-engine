"""Guards the Ticket 2 wiring in app/cli/ingest.py and app/cli/eval.py:
importing either CLI module must not itself configure logging (no
basicConfig side effect, no root handler attached) — configure_logging()
must only fire once _run() actually executes. Mirrors the same hermeticity
concern Ticket 1 handled for app/main.py's create_app()."""

import importlib
import logging


def _reset_and_import(module_name: str):
    root_logger = logging.getLogger()
    root_logger.handlers.clear()

    if module_name in importlib.sys.modules:
        del importlib.sys.modules[module_name]
    importlib.import_module(module_name)

    return root_logger


def test_importing_ingest_cli_does_not_configure_root_logger():
    root_logger = _reset_and_import("app.cli.ingest")
    assert root_logger.handlers == []


def test_importing_eval_cli_does_not_configure_root_logger():
    root_logger = _reset_and_import("app.cli.eval")
    assert root_logger.handlers == []

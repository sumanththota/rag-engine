"""Unit tests for app/config.py, focused on _load_dotenv_into_environ() —
the piece that makes .env-only vars (LLAMA_CLOUD_API_KEY,
LLAMA_CLOUD_BASE_URL, LLAMAPARSE_TIER) actually visible to app/pdfextract.py
and app/llamaparse.py, which read them via os.environ.get(...) directly
rather than through Settings. Without this, those vars are silently
invisible regardless of what .env says — see app/config.py's docstring.
"""

import os

from app.config import _load_dotenv_into_environ, load_config


def test_load_dotenv_sets_missing_keys_into_os_environ(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("LLAMA_CLOUD_API_KEY=llx-test-key\nPORT=9999\n")
    monkeypatch.delenv("LLAMA_CLOUD_API_KEY", raising=False)
    monkeypatch.delenv("PORT", raising=False)

    _load_dotenv_into_environ(str(env_file))

    assert os.environ["LLAMA_CLOUD_API_KEY"] == "llx-test-key"
    assert os.environ["PORT"] == "9999"


def test_load_dotenv_does_not_override_existing_env_var(tmp_path, monkeypatch):
    # Matches Go's loadDotEnv: `if _, exists := os.LookupEnv(key); exists {
    # continue }` — an already-set env var always wins over the .env file.
    env_file = tmp_path / ".env"
    env_file.write_text("LLAMA_CLOUD_API_KEY=from-dotenv\n")
    monkeypatch.setenv("LLAMA_CLOUD_API_KEY", "from-real-shell-env")

    _load_dotenv_into_environ(str(env_file))

    assert os.environ["LLAMA_CLOUD_API_KEY"] == "from-real-shell-env"


def test_load_dotenv_skips_blank_lines_comments_and_strips_quotes(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n# a comment\nQUOTED_VAL=\"has quotes\"\nNO_EQUALS_LINE\nSPACED = value \n"
    )
    for key in ("QUOTED_VAL", "NO_EQUALS_LINE", "SPACED"):
        monkeypatch.delenv(key, raising=False)

    _load_dotenv_into_environ(str(env_file))

    assert os.environ["QUOTED_VAL"] == "has quotes"
    assert os.environ["SPACED"] == "value"
    assert "NO_EQUALS_LINE" not in os.environ


def test_load_dotenv_missing_file_is_a_noop(tmp_path):
    _load_dotenv_into_environ(str(tmp_path / "does-not-exist.env"))  # must not raise


def test_load_config_propagates_llamaparse_vars_into_os_environ(tmp_path, monkeypatch):
    # The actual bug this guards against: load_config() must make
    # LLAMA_CLOUD_API_KEY visible to os.environ.get(...) callers (pdfextract.py,
    # llamaparse.py), not just to the returned Settings object.
    env_file = tmp_path / ".env"
    env_file.write_text(
        "HANDBOOK_PATH=/tmp/h.pdf\n"
        "DATABASE_URL=postgresql://u:p@localhost/db\n"
        "SECRET_KEY=test-9-boot-secret\n"
        "LLAMA_CLOUD_API_KEY=llx-real\n"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("LLAMA_CLOUD_API_KEY", raising=False)
    monkeypatch.delenv("HANDBOOK_PATH", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("SECRET_KEY", raising=False)

    try:
        load_config()
        assert os.environ["LLAMA_CLOUD_API_KEY"] == "llx-real"
    finally:
        # load_config() -> _load_dotenv_into_environ() sets SECRET_KEY into the
        # real os.environ via setdefault(); monkeypatch.delenv can't undo that
        # (see tests/test_auth.py::test_load_config_succeeds_when_secret_key_present
        # for why), so pop it directly to keep it from leaking into later tests.
        os.environ.pop("SECRET_KEY", None)

import os
from pathlib import Path

from pydantic import Field, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ConfigError(Exception):
    """Raised when required configuration is missing or invalid."""


def _load_dotenv_into_environ(path: str = ".env") -> None:
    """Mirrors Go's loadDotEnv (internal/config/config.go): parses .env and
    calls os.environ.setdefault for every key, so an existing env var always
    wins over the file (matches Go's `if _, exists := os.LookupEnv(key);
    exists { continue }`).

    Needed because pydantic_settings' BaseSettings(env_file=...) only loads
    .env into its own model fields — it never touches os.environ. app/
    pdfextract.py and app/llamaparse.py read LLAMA_CLOUD_API_KEY,
    LLAMA_CLOUD_BASE_URL, and LLAMAPARSE_TIER via os.environ.get(...)
    directly (mirroring Go's os.Getenv calls in those same packages, per
    docs/PYTHON_INTERFACES.md — deliberately not plumbed through Settings),
    so without this those three vars are silently invisible no matter what
    .env says, and pdfextract silently falls back to pypdf forever.
    """
    p = Path(path)
    if not p.is_file():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"')
        if key:
            os.environ.setdefault(key, val)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    port: str = Field(default="8080", alias="PORT")
    openrouter_api_key: str = Field(default="", alias="OPENROUTER_API_KEY")
    groq_api_key: str = Field(default="", alias="GROQ_API_KEY")
    handbook_path: str = Field(alias="HANDBOOK_PATH")
    ollama_host: str = Field(default="http://localhost:11434", alias="OLLAMA_HOST")
    ollama_api_key: str = Field(default="", alias="OLLAMA_API_KEY")
    collection_name: str = Field(default="handbook_chunks", alias="QDRANT_COLLECTION")
    top_k: int = 10

    database_url: str = Field(alias="DATABASE_URL")

    app_env: str = Field(default="production", alias="APP_ENV")
    secret_key: str = Field(alias="SECRET_KEY")

    google_client_id: str = Field(default="", alias="GOOGLE_CLIENT_ID")
    google_client_secret: str = Field(default="", alias="GOOGLE_CLIENT_SECRET")
    google_redirect_uri: str = Field(default="", alias="GOOGLE_REDIRECT_URI")

    @field_validator("secret_key")
    @classmethod
    def _require_secret_key(cls, v: str) -> str:
        if not v:
            raise ValueError("SECRET_KEY is required")
        return v

    @field_validator("ollama_api_key")
    @classmethod
    def _trim_ollama_api_key(cls, v: str) -> str:
        return v.strip()

    @field_validator("handbook_path")
    @classmethod
    def _require_handbook_path(cls, v: str) -> str:
        if not v:
            raise ValueError("HANDBOOK_PATH is required")
        return v


def load_config() -> Settings:
    """Equivalent of Go's config.Load(). Raises ConfigError (wrapping pydantic's
    ValidationError) if HANDBOOK_PATH or DATABASE_URL is missing."""
    _load_dotenv_into_environ()
    try:
        return Settings()
    except ValidationError as e:
        raise ConfigError(str(e)) from e

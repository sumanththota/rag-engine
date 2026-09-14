from pydantic import Field, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ConfigError(Exception):
    """Raised when required configuration is missing or invalid."""


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
    try:
        return Settings()
    except ValidationError as e:
        raise ConfigError(str(e)) from e

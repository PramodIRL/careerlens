from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Shared repo-root .env (see .env.example), resolved relative to this file
# so it's found regardless of the process's working directory.
_ENV_FILE = Path(__file__).resolve().parents[3] / ".env"


class Settings(BaseSettings):
    """Environment-based configuration for the API.

    Values come from real process environment variables, or from the
    repo-root .env file if present (copy .env.example to .env — see
    README.md). The defaults below match .env.example and only work
    against a local dev Postgres container, never a real deployment.
    """

    model_config = SettingsConfigDict(
        env_file=_ENV_FILE,
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    postgres_user: str = "careerlens"
    postgres_password: str = "changeme"
    postgres_db: str = "careerlens"
    postgres_host: str = "localhost"
    postgres_port: int = 5432

    @property
    def database_url(self) -> str:
        """Async SQLAlchemy DSN built from the settings above."""
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()

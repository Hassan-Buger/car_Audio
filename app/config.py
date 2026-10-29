"""Application configuration using Pydantic Settings."""

from functools import lru_cache

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Central configuration for Digisound Car Audio Scraper."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Database URLs
    DATABASE_URL: str = Field(
        default="postgresql+asyncpg://postgres:postgres@localhost:5432/digisound",
        description="Async SQLAlchemy database connection string (asyncpg)",
    )
    DATABASE_URL_TEST: str | None = Field(
        default="postgresql+asyncpg://postgres:postgres@localhost:5432/digisound_test",
        description="Dedicated test database URL",
    )

    # Authentication Keys
    API_KEY: str | None = Field(
        default=None,
        description="Secret key required for job trigger endpoints (POST /api/jobs/*)",
    )
    READ_API_KEY: str | None = Field(
        default=None,
        description="Optional secret key to restrict read access (GET /api/*)",
    )

    # Source Scraper Configuration
    SOURCE_BASE_URL: str = Field(
        default="https://www.digisound.ee",
        description="Base URL for Digisound website",
    )
    HTTP_CONCURRENCY: int = Field(
        default=3,
        ge=1,
        le=20,
        description="Max concurrent requests to the source",
    )
    HTTP_TIMEOUT_SECONDS: float = Field(
        default=20.0,
        ge=1.0,
        le=120.0,
        description="HTTP request timeout in seconds",
    )
    REQUEST_DELAY_MS: int = Field(
        default=500,
        ge=0,
        le=10000,
        description="Delay between requests in milliseconds for pacing",
    )
    USER_AGENT: str = Field(
        default="DigisoundScraper/1.0 (+https://github.com/soundimports/digisound-car-audio-scraper)",
        description="User-Agent header sent to the source",
    )

    # Worker Schedules
    DISCOVERY_CRON: str = Field(
        default="0 2 * * *",
        description="Cron expression for full discovery crawl (empty string disables it)",
    )
    STOCK_CRON: str = Field(
        default="0 */4 * * *",
        description="Cron expression for stock and price refresh (empty string disables it)",
    )
    SCHEDULE_TIMEZONE: str = Field(
        default="UTC",
        description="IANA time zone in which the cron expressions are evaluated (e.g. UTC)",
    )
    BOOTSTRAP_DISCOVERY: bool = Field(
        default=False,
        description=(
            "When true the worker enqueues exactly one discovery job on start-up if no "
            "discovery job or run has ever been recorded"
        ),
    )
    MAX_CATEGORY_DEPTH: int = Field(
        default=4,
        ge=1,
        le=8,
        description="Maximum category nesting depth followed below the root category",
    )

    # Process role (used by app.entrypoint)
    SERVICE_ROLE: str = Field(
        default="all",
        description="web = API only, worker = background worker only, all = both in one service",
    )

    # Build identity (Railway injects RAILWAY_GIT_COMMIT_SHA at build and run time)
    GIT_COMMIT_SHA: str | None = Field(
        default=None,
        validation_alias=AliasChoices("RAILWAY_GIT_COMMIT_SHA", "GIT_COMMIT_SHA"),
        description="Commit SHA of the running build",
    )

    # Deployment Environment
    ENVIRONMENT: str = Field(
        default="development",
        description="Environment name: development, testing, or production",
    )

    @field_validator("SERVICE_ROLE")
    @classmethod
    def validate_service_role(cls, v: str) -> str:
        role = (v or "all").strip().lower()
        if role not in {"web", "worker", "all"}:
            raise ValueError("SERVICE_ROLE must be one of: web, worker, all")
        return role

    @property
    def database_configured(self) -> bool:
        """True only when DATABASE_URL was explicitly provided (not the localhost default)."""
        return "DATABASE_URL" in self.model_fields_set

    @property
    def git_commit_short(self) -> str:
        """Short commit SHA of the running build, or 'unknown'."""
        return (self.GIT_COMMIT_SHA or "unknown")[:7]

    @field_validator("DATABASE_URL", "DATABASE_URL_TEST", mode="before")
    @classmethod
    def normalize_database_url(cls, v: str | None) -> str | None:
        if not v:
            return v
        # Standardize postgres:// and postgresql:// to postgresql+asyncpg://
        if v.startswith("postgres://"):
            return "postgresql+asyncpg://" + v[len("postgres://") :]
        if v.startswith("postgresql://") and not v.startswith("postgresql+asyncpg://"):
            return "postgresql+asyncpg://" + v[len("postgresql://") :]
        return v


@lru_cache
def get_settings() -> Settings:
    """Return cached application settings instance."""
    return Settings()

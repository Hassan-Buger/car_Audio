"""Application configuration using Pydantic Settings."""

from functools import lru_cache

from pydantic import Field, field_validator
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
        description="Cron expression for full discovery crawl",
    )
    STOCK_CRON: str = Field(
        default="0 */4 * * *",
        description="Cron expression for stock and price refresh",
    )

    # Deployment Environment
    ENVIRONMENT: str = Field(
        default="development",
        description="Environment name: development, testing, or production",
    )

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

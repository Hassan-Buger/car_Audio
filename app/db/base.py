"""SQLAlchemy declarative base and common mixins."""

from datetime import UTC, datetime

from sqlalchemy.orm import DeclarativeBase


def utc_now() -> datetime:
    """Return timezone-aware current UTC datetime."""
    return datetime.now(UTC)


class Base(DeclarativeBase):
    """Base class for all SQLAlchemy database models."""

    pass

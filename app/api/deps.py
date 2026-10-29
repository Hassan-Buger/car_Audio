"""FastAPI dependencies for database sessions and authentication."""

import secrets
from collections.abc import AsyncGenerator
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from fastapi.security import APIKeyHeader
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db.session import get_db_session

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Dependency yielding async SQLAlchemy database session."""
    async for session in get_db_session():
        yield session


def verify_read_access(
    settings: Annotated[Settings, Depends(get_settings)],
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
) -> None:
    """Verify read access if READ_API_KEY is configured."""
    if not settings.READ_API_KEY:
        return

    if not x_api_key or not secrets.compare_digest(x_api_key, settings.READ_API_KEY):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": {
                    "code": "unauthorized",
                    "message": "Invalid or missing read API key",
                    "details": {},
                }
            },
        )


def verify_job_api_key(
    settings: Annotated[Settings, Depends(get_settings)],
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
) -> None:
    """Verify secret key for job trigger endpoints using constant-time comparison."""
    if not settings.API_KEY:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": {
                    "code": "service_unavailable",
                    "message": "Job trigger functionality is disabled: API_KEY is not configured",
                    "details": {},
                }
            },
        )

    if not x_api_key or not secrets.compare_digest(x_api_key, settings.API_KEY):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": {
                    "code": "unauthorized",
                    "message": "Invalid or missing X-API-Key header",
                    "details": {},
                }
            },
        )

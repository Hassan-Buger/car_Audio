"""FastAPI application entry point for Digisound Car Audio Scraper."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse

from app.api.routes import router as api_router
from app.config import get_settings

openapi_tags = [
    {
        "name": "System",
        "description": "System health, connectivity checks, and catalog statistics.",
    },
    {
        "name": "Catalog",
        "description": "Category tree, product browsing, and exact supplier ID / SKU lookups.",
    },
    {
        "name": "Sync",
        "description": "Stable-window delta synchronization cursor queries.",
    },
    {
        "name": "Jobs",
        "description": "Background crawler job enqueueing and execution inspection.",
    },
]


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan lifecycle context."""
    _ = get_settings()
    yield


app = FastAPI(
    title="Digisound Car Audio Scraper API",
    description="""API for accessing the Digisound CAR STEREO catalog, tracking product changes, and managing crawler sync jobs.

Conventions:
- Every product carries `source = "digisound"`.
- Product SKUs are preserved exactly (spaces, slashes, punctuation, case).
- Unknown stock quantity is `NULL` (never zero).
- Backorder is a distinct status from out of stock.
- Consistent error envelope: `{"error": {"code": "...", "message": "...", "details": {...}}}`.""",
    version=get_settings().git_commit_short,
    openapi_tags=openapi_tags,
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)


# Global root redirect to docs
@app.get("/", include_in_schema=False)
async def root_redirect() -> RedirectResponse:
    """Redirect root path to interactive OpenAPI documentation."""
    return RedirectResponse(url="/docs")


# Include API routes
app.include_router(api_router)


# Global HTTPException handler ensuring consistent error envelope
@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    if isinstance(exc.detail, dict) and "error" in exc.detail:
        return JSONResponse(status_code=exc.status_code, content=exc.detail)
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {
                "code": "http_error",
                "message": str(exc.detail),
                "details": {},
            }
        },
    )


# Global validation error handler ensuring consistent error shape
@app.exception_handler(RequestValidationError)
async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "error": {
                "code": "validation_error",
                "message": "Invalid request parameters or payload",
                "details": {"errors": exc.errors()},
            }
        },
    )

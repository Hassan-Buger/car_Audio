"""Helpers for reasoning about Alembic migration state."""

import logging
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@lru_cache
def get_expected_alembic_head() -> str | None:
    """Return the head revision shipped with this build, or None if it cannot be determined."""
    try:
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
        return ScriptDirectory.from_config(cfg).get_current_head()
    except Exception:  # pragma: no cover - defensive; health must not crash on this
        logger.exception("Could not determine expected Alembic head")
        return None

"""Authentication behaviour of the API key dependencies."""

import pytest
from fastapi import HTTPException

from app.api.deps import _keys_match, verify_job_api_key, verify_read_access
from app.config import Settings

DB = "postgresql+asyncpg://postgres:postgres@127.0.0.1:5432/digisound_test"


def _settings(api_key: str = "", read_key: str = "") -> Settings:
    return Settings(DATABASE_URL=DB, API_KEY=api_key, READ_API_KEY=read_key)


def test_keys_match_handles_non_ascii_without_raising() -> None:
    assert _keys_match("abc", "abc")
    assert not _keys_match("abc", "abd")
    assert _keys_match("kéy-ü", "kéy-ü")
    assert not _keys_match("wrong", "kéy-ü")
    assert not _keys_match("kéy-ü", "wrong")


@pytest.mark.parametrize("configured", ["plain-ascii-key", "key-with-ünicode"])
def test_read_access_wrong_key_is_401_not_500(configured: str) -> None:
    settings = _settings(read_key=configured)
    for bad in ("wrong", "", None, "ünicode-wrong"):
        with pytest.raises(HTTPException) as exc:
            verify_read_access(settings, bad)
        assert exc.value.status_code == 401
    verify_read_access(settings, configured)  # correct key accepted


def test_job_key_wrong_is_401_and_unset_is_503() -> None:
    with pytest.raises(HTTPException) as exc:
        verify_job_api_key(_settings(api_key="job-key"), "wrong")
    assert exc.value.status_code == 401
    with pytest.raises(HTTPException) as exc2:
        verify_job_api_key(_settings(api_key=""), "anything")
    assert exc2.value.status_code == 503
    verify_job_api_key(_settings(api_key="job-key"), "job-key")

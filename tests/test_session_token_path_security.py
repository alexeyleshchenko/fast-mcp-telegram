"""Tests for bearer token path traversal and session file containment."""

from pathlib import Path

import pytest

from src.client.connection import generate_bearer_token
from src.server_components.session_token_validation import (
    InvalidSessionTokenError,
    session_file_path,
    validate_session_token,
    validated_session_file_path,
)
from tests.conftest import VALID_TEST_BEARER_TOKEN


class TestValidateSessionToken:
    def test_accepts_generated_token(self):
        token = generate_bearer_token()
        assert validate_session_token(token) == token

    def test_accepts_fixture_token(self):
        assert (
            validate_session_token(VALID_TEST_BEARER_TOKEN) == VALID_TEST_BEARER_TOKEN
        )

    @pytest.mark.parametrize(
        "token",
        [
            "",
            "telegram",
            "../victim",
            "a/../b",
            "foo/bar",
            "short",
            "a" * 44,
            "invalid+chars",
        ],
    )
    def test_rejects_unsafe_tokens(self, token: str):
        with pytest.raises(InvalidSessionTokenError):
            validate_session_token(token)


class TestSessionFilePath:
    def test_stays_under_session_dir(self, tmp_path: Path):
        token = generate_bearer_token()
        path = session_file_path(tmp_path, token)
        assert path.is_relative_to(tmp_path.resolve())
        assert path.name == f"{token}.session"

    def test_traversal_token_raises_on_validated_path_build(self, tmp_path: Path):
        with pytest.raises(InvalidSessionTokenError):
            validated_session_file_path(tmp_path, "../outside")

    def test_resolved_path_cannot_escape(self, tmp_path: Path):
        """Even if a file exists outside session_dir, path builder must reject token."""
        outside = tmp_path.parent / "other.session"
        outside.write_text("x")
        with pytest.raises(InvalidSessionTokenError):
            validated_session_file_path(tmp_path, f"../{outside.stem}")

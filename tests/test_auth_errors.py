"""Tests for shared auth telemetry error categorization (ADR 0008)."""

import pytest
from telethon.errors import (
    FloodWaitError,
    PasswordHashInvalidError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberBannedError,
    PhoneNumberInvalidError,
    PhoneNumberUnoccupiedError,
)

from src.auth_errors import categorize_auth_error


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (PasswordHashInvalidError(request=None), "2fa_wrong_password"),
        (PhoneCodeInvalidError(request=None), "invalid_code"),
        (PhoneCodeExpiredError(request=None), "code_expired"),
        (FloodWaitError(request=None, capture=30), "flood_wait"),
        (PhoneNumberBannedError(request=None), "phone_banned"),
        (PhoneNumberInvalidError(request=None), "phone_invalid"),
        (PhoneNumberUnoccupiedError(request=None), "phone_unoccupied"),
        (ConnectionError("refused"), "connect_failed"),
        (TimeoutError("timed out"), "timeout"),
        (OSError("network down"), "timeout"),
        (RuntimeError("unexpected"), "unknown"),
    ],
)
def test_categorize_auth_error_maps_exceptions(exc, expected):
    assert categorize_auth_error(exc) == expected


def test_categorize_auth_error_matches_collector_allowlist():
    """Every category must be accepted by collector AuthPayload validation."""
    import sys
    from pathlib import Path

    collector_root = Path(__file__).resolve().parents[1] / "collector"
    if str(collector_root) not in sys.path:
        sys.path.insert(0, str(collector_root))
    from app.auth_models import _VALID_ERRORS

    sample_categories = {
        "2fa_wrong_password",
        "invalid_code",
        "code_expired",
        "flood_wait",
        "phone_banned",
        "phone_invalid",
        "phone_unoccupied",
        "connect_failed",
        "timeout",
        "unknown",
    }
    assert sample_categories <= _VALID_ERRORS

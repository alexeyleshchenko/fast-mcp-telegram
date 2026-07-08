"""Map Telethon/runtime exceptions to ADR 0008 auth telemetry error categories."""

from __future__ import annotations

from telethon.errors import (
    FloodWaitError,
    PasswordHashInvalidError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberBannedError,
    PhoneNumberInvalidError,
    PhoneNumberUnoccupiedError,
)


def categorize_auth_error(exc: BaseException) -> str:
    """Map a Telethon/OS exception to an ADR 0008 error category string."""
    if isinstance(exc, PasswordHashInvalidError):
        return "2fa_wrong_password"
    if isinstance(exc, PhoneCodeInvalidError):
        return "invalid_code"
    if isinstance(exc, PhoneCodeExpiredError):
        return "code_expired"
    if isinstance(exc, FloodWaitError):
        return "flood_wait"
    if isinstance(exc, PhoneNumberBannedError):
        return "phone_banned"
    if isinstance(exc, PhoneNumberInvalidError):
        return "phone_invalid"
    if isinstance(exc, PhoneNumberUnoccupiedError):
        return "phone_unoccupied"
    if isinstance(exc, ConnectionError):
        return "connect_failed"
    if isinstance(exc, (TimeoutError, OSError)):
        return "timeout"
    return "unknown"

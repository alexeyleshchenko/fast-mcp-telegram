"""Auth event payload model for the telemetry collector.

Validates auth telemetry events sent by fast-mcp-telegram library clients
(see ADR 0008). Uses stdlib dataclasses — same pattern as models.py.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from typing import Any, Literal

from app.models import ValidationError

# Valid event types
_VALID_EVENTS = frozenset(
    {"auth_started", "auth_completed", "auth_failed", "auth_abandoned"}
)

# Valid auth methods
_VALID_METHODS = frozenset({"phone", "qr", "reauth", "bearer_check"})

# Valid branch tags
_VALID_BRANCHES = frozenset(
    {
        "phone_code",
        "phone_2fa",
        "qr_scan",
        "qr_2fa",
        "reauth_phone",
        "bearer_valid",
        "bearer_no_session",
        "bearer_invalid",
    }
)

# Valid error categories
_VALID_ERRORS = frozenset(
    {
        "flood_wait",
        "invalid_code",
        "2fa_wrong_password",
        "timeout",
        "connect_failed",
        "session_expired",
        "unknown",
    }
)

# Allowed fields in the payload (reject unknown keys)
_KNOWN_FIELDS = frozenset(
    {"type", "iid", "ts", "ver", "event", "method", "branch", "duration_ms", "error"}
)

# Maximum allowed timestamp drift in seconds
_FUTURE_DRIFT_SECONDS = 300  # 5 min
_OLD_WINDOW_SECONDS = 7 * 24 * 3600  # 7 days


@dataclass
class AuthEvent:
    """Schema for auth telemetry events.

    Matches the payload sent by fast-mcp-telegram's ``send_auth_event()``
    function. No extra fields allowed.
    """

    type: Literal["auth"]
    iid: str
    ts: int
    ver: str
    event: str
    method: str
    branch: str
    duration_ms: float
    error: str | None = None

    def __post_init__(self) -> None:
        """Validate all field constraints."""
        errors: list[str] = []

        # --- type (Literal["auth"]) ---
        if self.type != "auth":
            errors.append(f"type must be 'auth', got {self.type!r}")

        # --- iid ---
        if not isinstance(self.iid, str) or not self.iid:
            errors.append("iid must be a non-empty string")
        elif len(self.iid) > 128:
            errors.append(f"iid exceeds 128 chars ({len(self.iid)})")

        # --- ts ---
        if not isinstance(self.ts, int) or isinstance(self.ts, bool):
            errors.append("ts must be an integer")
        else:
            now = int(time.time())
            if self.ts > now + _FUTURE_DRIFT_SECONDS:
                errors.append(
                    f"ts {self.ts} is {self.ts - now}s in the future "
                    f"(max {_FUTURE_DRIFT_SECONDS}s)"
                )
            if self.ts < now - _OLD_WINDOW_SECONDS:
                errors.append(
                    f"ts {self.ts} is {now - self.ts}s old (max {_OLD_WINDOW_SECONDS}s)"
                )

        # --- ver ---
        if not isinstance(self.ver, str) or not self.ver:
            errors.append("ver must be a non-empty string")
        elif len(self.ver) > 64:
            errors.append(f"ver exceeds 64 chars ({len(self.ver)})")

        # --- event ---
        if not isinstance(self.event, str) or self.event not in _VALID_EVENTS:
            errors.append(f"event must be one of {_VALID_EVENTS!r}, got {self.event!r}")

        # --- method ---
        if not isinstance(self.method, str) or self.method not in _VALID_METHODS:
            errors.append(
                f"method must be one of {_VALID_METHODS!r}, got {self.method!r}"
            )

        # --- branch ---
        if not isinstance(self.branch, str) or self.branch not in _VALID_BRANCHES:
            errors.append(
                f"branch must be one of {_VALID_BRANCHES!r}, got {self.branch!r}"
            )

        # --- duration_ms ---
        if not isinstance(self.duration_ms, (int, float)) or isinstance(
            self.duration_ms, bool
        ):
            errors.append(
                f"duration_ms must be a number, got {type(self.duration_ms).__name__}"
            )
        elif self.duration_ms < 0:
            errors.append(f"duration_ms must be >= 0, got {self.duration_ms}")

        # --- error (nullable) ---
        if self.error is not None and (
            not isinstance(self.error, str) or self.error not in _VALID_ERRORS
        ):
            errors.append(
                f"error must be one of {_VALID_ERRORS!r} or null, got {self.error!r}"
            )

        if errors:
            raise ValidationError("; ".join(errors))

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible dict."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AuthEvent:
        """Construct and validate from a raw dict.

        Rejects extra keys not in the schema.

        Raises ``ValidationError`` on any issue.
        """
        if extra := set(data) - _KNOWN_FIELDS:
            raise ValidationError(f"Unexpected fields: {', '.join(sorted(extra))}")
        try:
            return cls(**data)
        except TypeError as exc:
            raise ValidationError(str(exc)) from exc

"""Auth telemetry payload model (ADR 0008).

Validates auth event payloads with type discrimination, atomic events,
flow_id, method/branch/event/error allowlists.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from app.payload_validation import (
    ValidationError,
    coerce_ts,
    construct_from_dict,
    validate_iid,
    validate_ts,
    validate_ver,
)

# --- Allowlists ---

_VALID_METHODS = {"phone", "qr", "bot", "reauth"}

_VALID_BRANCHES = {
    "phone_code",
    "phone_2fa",
    "qr_scan",
    "qr_2fa",
    "bot_token",
    "reauth_code",
    "reauth_2fa",
}

# Branch → method mapping: which branches are valid for which methods
_BRANCH_METHODS = {
    "phone_code": "phone",
    "phone_2fa": "phone",
    "qr_scan": "qr",
    "qr_2fa": "qr",
    "bot_token": "bot",
    "reauth_code": "reauth",
    "reauth_2fa": "reauth",
}

_VALID_EVENTS = {
    # User actions
    "user_submitted_phone",
    "user_submitted_code",
    "user_submitted_password",
    "user_scanned_qr",
    "user_reloaded_qr",
    "user_submitted_bot_token",
    "reauth_initiated",
    # System actions
    "code_requested",
    "code_validated",
    "password_validated",
    "qr_session_created",
    "qr_login_confirmed",
    "qr_expired",
    "session_established",
    "cleanup_completed",
}

_VALID_ERRORS = {
    "invalid_code",
    "code_expired",
    "2fa_wrong_password",
    "flood_wait",
    "phone_banned",
    "phone_invalid",
    "phone_unoccupied",
    "connect_failed",
    "timeout",
    "reauth_password_required",
    "qr_session_error",
    "already_authorized",
    "unknown",
}

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def _normalize_auth_payload_dict(data: dict[str, Any]) -> dict[str, Any]:
    """Coerce numeric ts fields before dataclass construction."""
    normalized = dict(data)
    if "ts" in normalized:
        normalized["ts"] = coerce_ts(normalized["ts"])
    events = normalized.get("events")
    if isinstance(events, list):
        normalized_events: list[dict[str, Any]] = []
        for ev in events:
            if not isinstance(ev, dict):
                normalized_events.append(ev)
                continue
            ev_copy = dict(ev)
            if "ts" in ev_copy:
                ev_copy["ts"] = coerce_ts(ev_copy["ts"])
            normalized_events.append(ev_copy)
        normalized["events"] = normalized_events
    return normalized


@dataclass
class AuthPayload:
    """Schema for auth telemetry payloads (ADR 0008).

    Each payload represents one auth flow. The ``events`` array contains
    all atomic events from that flow. ``flow_id`` groups events from the
    same flow — critical for concurrent users.
    """

    type: Literal["auth"]
    iid: str
    flow_id: str
    ts: int
    ver: str
    method: str
    branch: str
    events: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        """Validate all field constraints."""
        errors: list[str] = []

        # --- type ---
        if self.type != "auth":
            errors.append(f"type must be 'auth', got {self.type!r}")

        # --- iid ---
        validate_iid(self.iid, errors)

        # --- flow_id (UUID) ---
        if not isinstance(self.flow_id, str) or not self.flow_id:
            errors.append("flow_id must be a non-empty string")
        elif not _UUID_RE.match(self.flow_id):
            errors.append(f"flow_id must be a valid UUID, got {self.flow_id!r}")

        # --- ts ---
        validate_ts(self.ts, errors)

        # --- ver ---
        validate_ver(self.ver, errors)

        # --- method ---
        if not isinstance(self.method, str) or not self.method:
            errors.append("method must be a non-empty string")
        elif self.method not in _VALID_METHODS:
            errors.append(
                f"method must be one of {sorted(_VALID_METHODS)}, got {self.method!r}"
            )

        # --- branch ---
        if not isinstance(self.branch, str) or not self.branch:
            errors.append("branch must be a non-empty string")
        elif self.branch not in _VALID_BRANCHES:
            errors.append(
                f"branch must be one of {sorted(_VALID_BRANCHES)}, got {self.branch!r}"
            )
        elif self.method in _VALID_METHODS and self.branch in _VALID_BRANCHES:
            expected_method = _BRANCH_METHODS.get(self.branch)
            if expected_method and expected_method != self.method:
                errors.append(
                    f"branch {self.branch!r} belongs to method {expected_method!r}, "
                    f"not {self.method!r}"
                )

        # --- events ---
        if not isinstance(self.events, list):
            errors.append("events must be a list")
        elif len(self.events) == 0:
            errors.append("events must not be empty")
        else:
            for i, ev in enumerate(self.events):
                if not isinstance(ev, dict):
                    errors.append(f"events[{i}] must be a dict")
                    continue
                # ts required
                if "ts" not in ev:
                    errors.append(f"events[{i}].ts is required")
                elif not isinstance(ev["ts"], int) or isinstance(ev["ts"], bool):
                    errors.append(f"events[{i}].ts must be an integer")
                # event name required
                if "event" not in ev:
                    errors.append(f"events[{i}].event is required")
                elif ev["event"] not in _VALID_EVENTS:
                    errors.append(
                        f"events[{i}].event must be one of {sorted(_VALID_EVENTS)}, "
                        f"got {ev['event']!r}"
                    )
                # error optional but must be valid if present
                if "error" in ev and ev["error"] not in _VALID_ERRORS:
                    errors.append(
                        f"events[{i}].error must be one of {sorted(_VALID_ERRORS)}, "
                        f"got {ev['error']!r}"
                    )

        if errors:
            raise ValidationError("; ".join(errors))

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible dict."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AuthPayload:
        """Construct and validate from a raw dict.

        Rejects extra keys not in the schema.
        """
        return construct_from_dict(cls, data, normalizer=_normalize_auth_payload_dict)

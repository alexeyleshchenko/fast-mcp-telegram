"""Tests for auth telemetry event model validation."""

from __future__ import annotations

import time

import pytest
from app.auth_models import AuthEvent, ValidationError


class TestAuthEvent:
    """Schema validation for auth telemetry events."""

    @pytest.fixture
    def valid_auth_data(self):
        now = int(time.time())
        return {
            "type": "auth",
            "iid": "550e8400-e29b-41d4-a716-446655440000",
            "ts": now,
            "ver": "0.38.0",
            "event": "auth_started",
            "method": "phone",
            "branch": "phone_code",
            "duration_ms": 0.0,
            "error": None,
        }

    def test_valid_auth_event(self, valid_auth_data):
        event = AuthEvent.from_dict(valid_auth_data)
        assert event.type == "auth"
        assert event.event == "auth_started"
        assert event.method == "phone"
        assert event.branch == "phone_code"
        assert event.duration_ms == 0.0
        assert event.error is None

    def test_valid_completed_event(self, valid_auth_data):
        valid_auth_data["event"] = "auth_completed"
        valid_auth_data["duration_ms"] = 12340.5
        event = AuthEvent.from_dict(valid_auth_data)
        assert event.event == "auth_completed"
        assert event.duration_ms == 12340.5

    def test_valid_failed_event_with_error(self, valid_auth_data):
        valid_auth_data["event"] = "auth_failed"
        valid_auth_data["error"] = "flood_wait"
        valid_auth_data["duration_ms"] = 500.0
        event = AuthEvent.from_dict(valid_auth_data)
        assert event.event == "auth_failed"
        assert event.error == "flood_wait"

    def test_valid_abandoned_event(self, valid_auth_data):
        valid_auth_data["event"] = "auth_abandoned"
        valid_auth_data["duration_ms"] = 300000.0
        event = AuthEvent.from_dict(valid_auth_data)
        assert event.event == "auth_abandoned"

    def test_valid_methods(self, valid_auth_data):
        for method in ("phone", "qr", "reauth", "bearer_check"):
            valid_auth_data["method"] = method
            event = AuthEvent.from_dict(valid_auth_data)
            assert event.method == method

    def test_valid_branches(self, valid_auth_data):
        for branch in (
            "phone_code", "phone_2fa", "qr_scan", "qr_2fa",
            "reauth_phone", "bearer_valid", "bearer_no_session", "bearer_invalid",
        ):
            valid_auth_data["branch"] = branch
            event = AuthEvent.from_dict(valid_auth_data)
            assert event.branch == branch

    def test_valid_error_categories(self, valid_auth_data):
        for error in (
            "flood_wait", "invalid_code", "2fa_wrong_password",
            "timeout", "connect_failed", "session_expired", "unknown",
        ):
            valid_auth_data["error"] = error
            event = AuthEvent.from_dict(valid_auth_data)
            assert event.error == error

    # --- Validation failures ---

    def test_missing_type_fails(self, valid_auth_data):
        del valid_auth_data["type"]
        with pytest.raises(ValidationError):
            AuthEvent.from_dict(valid_auth_data)

    def test_wrong_type_fails(self, valid_auth_data):
        valid_auth_data["type"] = "heartbeat"
        with pytest.raises(ValidationError):
            AuthEvent.from_dict(valid_auth_data)

    def test_missing_event_fails(self, valid_auth_data):
        del valid_auth_data["event"]
        with pytest.raises(ValidationError):
            AuthEvent.from_dict(valid_auth_data)

    def test_invalid_event_fails(self, valid_auth_data):
        valid_auth_data["event"] = "auth_unknown"
        with pytest.raises(ValidationError):
            AuthEvent.from_dict(valid_auth_data)

    def test_missing_method_fails(self, valid_auth_data):
        del valid_auth_data["method"]
        with pytest.raises(ValidationError):
            AuthEvent.from_dict(valid_auth_data)

    def test_invalid_method_fails(self, valid_auth_data):
        valid_auth_data["method"] = "sms"
        with pytest.raises(ValidationError):
            AuthEvent.from_dict(valid_auth_data)

    def test_missing_branch_fails(self, valid_auth_data):
        del valid_auth_data["branch"]
        with pytest.raises(ValidationError):
            AuthEvent.from_dict(valid_auth_data)

    def test_missing_duration_fails(self, valid_auth_data):
        del valid_auth_data["duration_ms"]
        with pytest.raises(ValidationError):
            AuthEvent.from_dict(valid_auth_data)

    def test_negative_duration_fails(self, valid_auth_data):
        valid_auth_data["duration_ms"] = -100.0
        with pytest.raises(ValidationError):
            AuthEvent.from_dict(valid_auth_data)

    def test_missing_iid_fails(self, valid_auth_data):
        del valid_auth_data["iid"]
        with pytest.raises(ValidationError):
            AuthEvent.from_dict(valid_auth_data)

    def test_missing_ts_fails(self, valid_auth_data):
        del valid_auth_data["ts"]
        with pytest.raises(ValidationError):
            AuthEvent.from_dict(valid_auth_data)

    def test_missing_ver_fails(self, valid_auth_data):
        del valid_auth_data["ver"]
        with pytest.raises(ValidationError):
            AuthEvent.from_dict(valid_auth_data)

    def test_extra_field_fails(self, valid_auth_data):
        valid_auth_data["leaked_data"] = "should not be here"
        with pytest.raises(ValidationError):
            AuthEvent.from_dict(valid_auth_data)

    def test_iid_too_long_fails(self, valid_auth_data):
        valid_auth_data["iid"] = "a" * 200
        with pytest.raises(ValidationError):
            AuthEvent.from_dict(valid_auth_data)

    def test_ver_too_long_fails(self, valid_auth_data):
        valid_auth_data["ver"] = "a" * 100
        with pytest.raises(ValidationError):
            AuthEvent.from_dict(valid_auth_data)

    # --- Serialization ---

    def test_to_dict(self, valid_auth_data):
        event = AuthEvent.from_dict(valid_auth_data)
        d = event.to_dict()
        assert d["type"] == "auth"
        assert d["event"] == "auth_started"
        assert d["method"] == "phone"
        assert d["branch"] == "phone_code"
        assert d["duration_ms"] == 0.0
        assert d["error"] is None

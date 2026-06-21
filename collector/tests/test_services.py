"""Tests for the telemetry collector service layer."""


import pytest
from app.models import ValidationError as ServiceValidationError
from app.services import (
    RateLimitError,
    process_event,
)

from collector.tests._helpers import make_nested_payload


class TestProcessEvent:
    """Core business logic for processing telemetry events."""

    def test_valid_event_stored(self, storage, valid_payload_data):
        """A valid event is stored in the backend."""
        process_event(valid_payload_data, "192.168.1.1", storage)
        assert len(storage.events) == 1
        stored = storage.events[0]["payload"]
        assert stored.iid == valid_payload_data["iid"]
        assert stored.counters["total_calls"] == 0

    def test_invalid_event_raises(self, storage, valid_payload_data):
        """An invalid event raises and is not stored."""
        valid_payload_data["counters"] = {"total_calls": -1, "errors": 0}
        with pytest.raises(ServiceValidationError):
            process_event(valid_payload_data, "192.168.1.1", storage)
        assert len(storage.events) == 0

    def test_duplicate_within_window_is_deduped(
        self, storage, valid_payload_data
    ):
        """Same payload within 5 min window is silently deduped."""
        process_event(valid_payload_data, "192.168.1.1", storage)
        process_event(valid_payload_data, "192.168.1.1", storage)
        assert len(storage.events) == 1

    def test_duplicate_from_different_ip_stored(
        self, storage, valid_payload_data
    ):
        """Same payload from different IP is still deduped (hash-based)."""
        process_event(valid_payload_data, "192.168.1.1", storage)
        process_event(valid_payload_data, "10.0.0.1", storage)
        assert len(storage.events) == 1

    def test_rate_limit_exceeded_raises(
        self, storage, valid_payload_data
    ):
        """Too many events from one iid in 24h raises RateLimitError."""
        from app.services import INSTANCE_RATE_LIMIT
        process_event(valid_payload_data, "10.0.0.1", storage)
        for i in range(1, INSTANCE_RATE_LIMIT):
            data = make_nested_payload()
            # vary counters to bypass dedup
            data["counters"] = {"total_calls": i, "errors": 0}
            process_event(data, "10.0.0.1", storage)
        assert len(storage.events) == INSTANCE_RATE_LIMIT
        with pytest.raises(RateLimitError):
            new_data = make_nested_payload()
            new_data["counters"] = {"total_calls": 9999, "errors": 0}
            process_event(new_data, "10.0.0.1", storage)
        assert len(storage.events) == INSTANCE_RATE_LIMIT

    def test_different_instance_not_rate_limited(
        self, storage, valid_payload_data
    ):
        """Events from different iids don't interfere."""
        from app.services import INSTANCE_RATE_LIMIT
        for i in range(INSTANCE_RATE_LIMIT):
            data = make_nested_payload()
            data["iid"] = f"550e8400-e29b-41d4-a716-4466554400{i:02d}"
            data["counters"] = {"total_calls": i, "errors": 0}
            process_event(data, "10.0.0.1", storage)
        assert len(storage.events) == INSTANCE_RATE_LIMIT

    def test_row_cap_enforced(self, storage, valid_payload_data):
        """Storage enforces maximum row count by dropping oldest."""
        cap = 5
        for i in range(cap):
            data = make_nested_payload()
            data["iid"] = f"550e8400-e29b-41d4-a716-44665544000{i}"
            data["counters"] = {"total_calls": i, "errors": 0}
            process_event(data, "10.0.0.1", storage, max_rows=cap)
        assert len(storage.events) == cap
        # One more triggers the cap removal
        new_data = make_nested_payload()
        new_data["iid"] = "550e8400-e29b-41d4-a716-446655449999"
        new_data["counters"] = {"total_calls": 9999, "errors": 0}
        process_event(new_data, "10.0.0.1", storage, max_rows=cap)
        # Still at cap (oldest got removed)
        assert len(storage.events) == cap
        # The newest event should still be present
        assert storage.events[-1]["payload"].iid.endswith("9999")

    def test_storage_backend_close(self, storage):
        """close() cleans up."""
        storage.close()
        assert len(storage.events) == 0


class TestProcessAuthEvent:
    """Auth event processing through process_event."""

    @pytest.fixture
    def valid_auth_data(self):
        import time
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

    def test_auth_event_stored(self, storage, valid_auth_data):
        """A valid auth event is stored."""
        process_event(valid_auth_data, "192.168.1.1", storage)
        assert len(storage.events) == 1
        stored = storage.events[0]["payload"]
        assert stored.type == "auth"
        assert stored.event == "auth_started"

    def test_auth_event_completed_stored(self, storage, valid_auth_data):
        """Auth completed event is stored."""
        valid_auth_data["event"] = "auth_completed"
        valid_auth_data["duration_ms"] = 12340.5
        process_event(valid_auth_data, "192.168.1.1", storage)
        assert len(storage.events) == 1
        stored = storage.events[0]["payload"]
        assert stored.event == "auth_completed"
        assert stored.duration_ms == 12340.5

    def test_auth_event_failed_stored(self, storage, valid_auth_data):
        """Auth failed event with error is stored."""
        valid_auth_data["event"] = "auth_failed"
        valid_auth_data["error"] = "flood_wait"
        valid_auth_data["duration_ms"] = 500.0
        process_event(valid_auth_data, "192.168.1.1", storage)
        assert len(storage.events) == 1
        stored = storage.events[0]["payload"]
        assert stored.event == "auth_failed"
        assert stored.error == "flood_wait"

    def test_auth_event_abandoned_stored(self, storage, valid_auth_data):
        """Auth abandoned event is stored."""
        valid_auth_data["event"] = "auth_abandoned"
        valid_auth_data["duration_ms"] = 300000.0
        process_event(valid_auth_data, "192.168.1.1", storage)
        assert len(storage.events) == 1

    def test_auth_event_invalid_rejected(self, storage, valid_auth_data):
        """Invalid auth event is rejected."""
        valid_auth_data["event"] = "auth_unknown"
        with pytest.raises(ServiceValidationError):
            process_event(valid_auth_data, "192.168.1.1", storage)
        assert len(storage.events) == 0

    def test_auth_event_rate_limited(self, storage, valid_auth_data):
        """Auth events are subject to rate limiting."""
        from app.services import INSTANCE_RATE_LIMIT
        for i in range(INSTANCE_RATE_LIMIT):
            data = dict(valid_auth_data)
            data["duration_ms"] = float(i)
            process_event(data, "10.0.0.1", storage)
        assert len(storage.events) == INSTANCE_RATE_LIMIT
        with pytest.raises(RateLimitError):
            process_event(valid_auth_data, "10.0.0.1", storage)

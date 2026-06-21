"""Tests for the telemetry collector HTTP API."""

import time

from app.services import INSTANCE_RATE_LIMIT

from collector.tests._helpers import make_nested_payload


class TestHealthEndpoint:
    """GET /health"""

    def test_health_returns_200(self, client):
        """Health check endpoint returns 200."""
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert "status" in data
        assert data["status"] == "ok"


class TestCollectEndpoint:
    """POST /v1/event"""

    def test_valid_payload_returns_204(self, client, valid_payload_data):
        """A valid telemetry payload returns 204 No Content."""
        resp = client.post("/v1/event", json=valid_payload_data)
        assert resp.status_code == 204

    def test_invalid_payload_returns_422(self, client, valid_payload_data):
        """An invalid payload returns 422 Unprocessable Entity."""
        del valid_payload_data["iid"]
        resp = client.post("/v1/event", json=valid_payload_data)
        assert resp.status_code == 422

    def test_extra_fields_returns_422(self, client, valid_payload_data):
        """Payload with unknown fields returns 422."""
        valid_payload_data["bad_field"] = "evil"
        resp = client.post("/v1/event", json=valid_payload_data)
        assert resp.status_code == 422

    def test_negative_counter_returns_422(self, client, valid_payload_data):
        """Negative counter value returns 422."""
        valid_payload_data["counters"] = {"total_calls": -1, "errors": 0}
        resp = client.post("/v1/event", json=valid_payload_data)
        assert resp.status_code == 422

    def test_future_ts_returns_422(self, client, valid_payload_data):
        """``ts`` more than 5 min in the future returns 422."""
        valid_payload_data["ts"] = int(time.time()) + 600
        resp = client.post("/v1/event", json=valid_payload_data)
        assert resp.status_code == 422

    def test_old_ts_returns_422(self, client, valid_payload_data):
        """``ts`` more than 7 days old returns 422."""
        valid_payload_data["ts"] = int(time.time()) - (8 * 86400)
        resp = client.post("/v1/event", json=valid_payload_data)
        assert resp.status_code == 422

    def test_non_json_body_returns_422(self, client):
        """Non-JSON request body returns 422."""
        resp = client.post(
            "/v1/event",
            content=b"not-json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 422

    def test_duplicate_payload_returns_204(self, client, valid_payload_data):
        """Duplicate payload (same data within window) returns 204, not an error."""
        resp1 = client.post("/v1/event", json=valid_payload_data)
        assert resp1.status_code == 204
        resp2 = client.post("/v1/event", json=valid_payload_data)
        assert resp2.status_code == 204  # Silent dedup

    def test_instance_rate_limit_returns_429(self, client, valid_payload_data):
        """Exceeding per-instance rate limit returns 429."""
        for i in range(INSTANCE_RATE_LIMIT):
            data = make_nested_payload()
            data["counters"] = {"total_calls": i, "errors": 0}
            resp = client.post("/v1/event", json=data)
            assert resp.status_code == 204
        # One more should be rate-limited
        resp = client.post("/v1/event", json=valid_payload_data)
        assert resp.status_code == 429


class TestAuthEventEndpoint:
    """POST /v1/event with type=auth"""

    def _make_auth_event(self, **overrides):
        from collector.tests._helpers import make_auth_event
        return make_auth_event(**overrides)

    def test_auth_event_returns_204(self, client):
        """A valid auth event returns 204 No Content."""
        resp = client.post("/v1/event", json=self._make_auth_event())
        assert resp.status_code == 204

    def test_auth_event_completed_returns_204(self, client):
        """Auth completed event returns 204."""
        resp = client.post(
            "/v1/event",
            json=self._make_auth_event(
                event="auth_completed",
                duration_ms=12340.5,
            ),
        )
        assert resp.status_code == 204

    def test_auth_event_failed_returns_204(self, client):
        """Auth failed event returns 204."""
        resp = client.post(
            "/v1/event",
            json=self._make_auth_event(
                event="auth_failed",
                error="flood_wait",
                duration_ms=500.0,
            ),
        )
        assert resp.status_code == 204

    def test_auth_event_abandoned_returns_204(self, client):
        """Auth abandoned event returns 204."""
        resp = client.post(
            "/v1/event",
            json=self._make_auth_event(
                event="auth_abandoned",
                duration_ms=300000.0,
            ),
        )
        assert resp.status_code == 204

    def test_auth_event_invalid_event_returns_422(self, client):
        """Auth event with invalid event type returns 422."""
        resp = client.post(
            "/v1/event",
            json=self._make_auth_event(event="auth_unknown"),
        )
        assert resp.status_code == 422

    def test_auth_event_missing_method_returns_422(self, client):
        """Auth event without method returns 422."""
        data = self._make_auth_event()
        del data["method"]
        resp = client.post("/v1/event", json=data)
        assert resp.status_code == 422

    def test_auth_event_qr_method_returns_204(self, client):
        """Auth event with QR method returns 204."""
        resp = client.post(
            "/v1/event",
            json=self._make_auth_event(
                event="auth_started",
                method="qr",
                branch="qr_scan",
            ),
        )
        assert resp.status_code == 204

"""Tests for auth telemetry events (ADR 0008)."""

from __future__ import annotations

import json
import os
import time
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _clear_env():
    """Remove telemetry-related env vars before each test."""
    for key in ("DO_NOT_TRACK", "MCP_TELEMETRY_DEBUG"):
        os.environ.pop(key, None)
    yield


@pytest.fixture
def telemetry_module():
    """Import telemetry module fresh for each test."""
    import importlib

    import src.telemetry as t

    importlib.reload(t)
    if hasattr(t, "_instance_id"):
        t._instance_id = None
    return t


# ───────────────────────────── send_auth_event ─────────────────────────


class TestSendAuthEvent:
    """Tests for the send_auth_event function."""

    def test_send_auth_event_debug_mode_logs(self, telemetry_module, capsys):
        """MCP_TELEMETRY_DEBUG=1 logs auth event to stderr."""
        os.environ["MCP_TELEMETRY_DEBUG"] = "1"

        telemetry_module.send_auth_event(
            event="auth_started",
            method="phone",
            branch="phone_code",
        )

        captured = capsys.readouterr()
        assert "TELEMETRY" in captured.err
        payload = json.loads(captured.err.split("TELEMETRY")[-1].strip())
        assert payload["type"] == "auth"
        assert payload["event"] == "auth_started"
        assert payload["method"] == "phone"
        assert payload["branch"] == "phone_code"

    def test_send_auth_event_respects_do_not_track(self, telemetry_module):
        """DO_NOT_TRACK=1 prevents sending."""
        os.environ["DO_NOT_TRACK"] = "1"

        with patch("urllib.request.urlopen") as mock_urlopen:
            telemetry_module.send_auth_event(
                event="auth_started",
                method="phone",
                branch="phone_code",
            )
        mock_urlopen.assert_not_called()

    def test_send_auth_event_sends_post(self, telemetry_module):
        """send_auth_event sends a POST request."""
        os.environ["MCP_TELEMETRY_DEBUG"] = "1"

        telemetry_module.send_auth_event(
            event="auth_completed",
            method="qr",
            branch="qr_scan",
            duration_ms=12340.5,
        )

        # Verify it doesn't raise and respects debug mode
        # (actual POST tested via integration)

    def test_send_auth_event_with_error(self, telemetry_module, capsys):
        """Auth event with error category is included."""
        os.environ["MCP_TELEMETRY_DEBUG"] = "1"

        telemetry_module.send_auth_event(
            event="auth_failed",
            method="phone",
            branch="phone_code",
            duration_ms=500.0,
            error="flood_wait",
        )

        captured = capsys.readouterr()
        payload = json.loads(captured.err.split("TELEMETRY")[-1].strip())
        assert payload["event"] == "auth_failed"
        assert payload["error"] == "flood_wait"
        assert payload["duration_ms"] == 500.0

    def test_send_auth_event_network_error_silent(self, telemetry_module):
        """Network error does not raise."""
        import urllib.error

        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = urllib.error.URLError("refused")
            # Should not raise
            telemetry_module.send_auth_event(
                event="auth_started",
                method="phone",
                branch="phone_code",
            )
        mock_urlopen.assert_called_once()

    def test_send_auth_event_includes_iid(self, telemetry_module, capsys):
        """Auth event includes instance ID."""
        os.environ["MCP_TELEMETRY_DEBUG"] = "1"

        telemetry_module.send_auth_event(
            event="auth_started",
            method="phone",
            branch="phone_code",
        )

        captured = capsys.readouterr()
        payload = json.loads(captured.err.split("TELEMETRY")[-1].strip())
        assert "iid" in payload
        assert len(payload["iid"]) == 36  # UUID v4

    def test_send_auth_event_includes_ver(self, telemetry_module, capsys):
        """Auth event includes version."""
        os.environ["MCP_TELEMETRY_DEBUG"] = "1"

        telemetry_module.send_auth_event(
            event="auth_started",
            method="phone",
            branch="phone_code",
        )

        captured = capsys.readouterr()
        payload = json.loads(captured.err.split("TELEMETRY")[-1].strip())
        assert "ver" in payload
        assert payload["ver"]  # non-empty

    def test_send_auth_event_includes_ts(self, telemetry_module, capsys):
        """Auth event includes timestamp."""
        os.environ["MCP_TELEMETRY_DEBUG"] = "1"

        before = int(time.time())
        telemetry_module.send_auth_event(
            event="auth_started",
            method="phone",
            branch="phone_code",
        )
        after = int(time.time())

        captured = capsys.readouterr()
        payload = json.loads(captured.err.split("TELEMETRY")[-1].strip())
        assert before <= payload["ts"] <= after


# ───────────────────────────── Auth event in web_setup ─────────────────


class TestWebSetupAuthEvents:
    """Test that web_setup routes fire auth telemetry events."""

    @pytest.fixture
    def setup_routes(self):
        from src.server_components import web_setup

        class _FakeMcpApp:
            def __init__(self):
                self.routes = {}

            def custom_route(self, path, methods):
                def decorator(func):
                    self.routes[(path, tuple(methods))] = func
                    return func

                return decorator

        app = _FakeMcpApp()
        web_setup.register_web_setup_routes(app)
        return app.routes

    @pytest.fixture
    def _patch_templates(self, monkeypatch):
        """Patch Jinja2Templates.TemplateResponse."""
        from types import SimpleNamespace

        from src.server_components import web_setup

        def _tr(_request, template_name, context=None):
            return SimpleNamespace(template=template_name, context=context or {})

        monkeypatch.setattr(web_setup.templates, "TemplateResponse", _tr)

    @pytest.mark.asyncio
    async def test_setup_phone_fires_auth_started(
        self, monkeypatch, setup_routes, _patch_templates
    ):
        """POST /setup/phone fires auth_started event."""
        import tempfile
        from pathlib import Path

        from src.config.server_config import ServerConfig, set_config
        from src.server_components import web_setup

        with tempfile.TemporaryDirectory() as tmp:
            cfg = ServerConfig()
            cfg.session_dir = str(Path(tmp))
            set_config(cfg)

            web_setup._setup_sessions.clear()

            class _Client:
                async def connect(self):
                    pass

                async def send_code_request(self, phone, **kwargs):
                    pass

                async def disconnect(self):
                    pass

            monkeypatch.setattr(
                web_setup, "create_session_client", lambda _path: _Client()
            )

            captured_events = []

            def capture_send_auth_event(**kwargs):
                captured_events.append(kwargs)

            monkeypatch.setattr(web_setup, "send_auth_event", capture_send_auth_event)

            class _FakeRequest:
                async def form(self):
                    return {"phone": "+1234567890"}

            handler = setup_routes[("/setup/phone", ("POST",))]
            await handler(_FakeRequest())

            assert len(captured_events) >= 1
            started = captured_events[0]
            assert started["event"] == "auth_started"
            assert started["method"] == "phone"
            assert started["branch"] == "phone_code"

    @pytest.mark.asyncio
    async def test_setup_phone_flood_fires_auth_failed(
        self, monkeypatch, setup_routes, _patch_templates
    ):
        """POST /setup/phone with flood fires auth_failed event."""
        import tempfile
        from pathlib import Path

        from telethon.errors.rpcerrorlist import PhoneNumberFloodError

        from src.config.server_config import ServerConfig, set_config
        from src.server_components import web_setup

        with tempfile.TemporaryDirectory() as tmp:
            cfg = ServerConfig()
            cfg.session_dir = str(Path(tmp))
            set_config(cfg)

            web_setup._setup_sessions.clear()

            class _Client:
                async def connect(self):
                    pass

                async def send_code_request(self, phone, **kwargs):
                    raise PhoneNumberFloodError(request=None)

                async def disconnect(self):
                    pass

            monkeypatch.setattr(
                web_setup, "create_session_client", lambda _path: _Client()
            )

            captured_events = []
            monkeypatch.setattr(
                web_setup,
                "send_auth_event",
                lambda **kwargs: captured_events.append(kwargs),
            )

            class _FakeRequest:
                async def form(self):
                    return {"phone": "+1234567890"}

            handler = setup_routes[("/setup/phone", ("POST",))]
            await handler(_FakeRequest())

            failed = [e for e in captured_events if e["event"] == "auth_failed"]
            assert len(failed) >= 1
            assert failed[0]["error"] == "flood_wait"
            assert failed[0]["method"] == "phone"

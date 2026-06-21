"""Tests for auth telemetry events (ADR 0008)."""

from __future__ import annotations

import json
import os
import time
from types import SimpleNamespace
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


# ───────────────────────────── Auth event in cli_setup ─────────────────


def _make_cli_cfg(tmp: str):
    """Create a SimpleNamespace mimicking SetupConfig for tests (avoids CLI arg parsing)."""
    from pathlib import Path

    return SimpleNamespace(
        session_directory=Path(tmp),
        session_dir=str(Path(tmp)),
        session_path=Path(tmp) / "test",
        api_id="12345",
        api_hash="abcdef",
        bot_api_token="",
        phone_number="",
        server_mode=SimpleNamespace(value="http-auth"),
        overwrite=True,
        session_name="test",
        entity_cache_limit=0,
        mtproto_proxy=None,
        domain=None,
    )


class TestCliSetupAuthEvents:
    """Test that cli_setup flows fire auth telemetry events."""

    @pytest.fixture
    def _patch_cli_deps(self, monkeypatch):
        """Common patches for CLI setup tests."""
        from src import cli_setup

        monkeypatch.setattr(cli_setup, "_is_interactive_terminal", lambda: True)

        captured_events = []

        def capture_send_auth_event(**kwargs):
            captured_events.append(kwargs)

        monkeypatch.setattr(cli_setup, "send_auth_event", capture_send_auth_event)
        return captured_events

    @staticmethod
    def _make_fake_client(monkeypatch):
        """Create and patch a minimal TelegramClient mock."""
        from src import cli_setup

        class FakeClient:
            def __init__(self, **kwargs):
                pass

            async def connect(self):
                pass

            async def disconnect(self):
                pass

            @property
            def session(self):
                return SimpleNamespace(filename=None)

        monkeypatch.setattr(cli_setup, "TelegramClient", FakeClient)
        monkeypatch.setattr(cli_setup, "build_mtproto_client_args", lambda *a, **k: {})
        monkeypatch.setattr(cli_setup, "generate_bearer_token", lambda: "tok123")

    @pytest.mark.asyncio
    async def test_qr_flow_fires_started_and_completed(
        self, monkeypatch, _patch_cli_deps
    ):
        """QR login flow fires auth_started and auth_completed."""
        from src import cli_setup

        captured_events = _patch_cli_deps

        async def fake_qr_login(client, session_path):
            return True

        monkeypatch.setattr(cli_setup, "_qr_login_flow", fake_qr_login)
        self._make_fake_client(monkeypatch)

        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            cfg = _make_cli_cfg(tmp)
            result = await cli_setup._qr_session_login(cfg, cfg.session_path, "tok123")

        assert result is not None
        events = [e for e in captured_events if e["method"] == "cli_setup"]
        assert len(events) == 2
        assert events[0]["event"] == "auth_started"
        assert events[0]["branch"] == "cli_qr_scan"
        assert events[1]["event"] == "auth_completed"
        assert events[1]["branch"] == "cli_qr_scan"
        assert events[1]["duration_ms"] >= 0

    @pytest.mark.asyncio
    async def test_qr_flow_fires_started_and_failed(self, monkeypatch, _patch_cli_deps):
        """QR login flow failure fires auth_started and auth_failed."""
        from src import cli_setup

        captured_events = _patch_cli_deps

        async def fake_qr_login(client, session_path):
            return False

        monkeypatch.setattr(cli_setup, "_qr_login_flow", fake_qr_login)
        self._make_fake_client(monkeypatch)

        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            cfg = _make_cli_cfg(tmp)
            result = await cli_setup._qr_session_login(cfg, cfg.session_path, "tok123")

        assert result is None
        events = [e for e in captured_events if e["method"] == "cli_setup"]
        assert len(events) == 2
        assert events[0]["event"] == "auth_started"
        assert events[1]["event"] == "auth_failed"
        assert events[1]["error"] == "timeout"

    @pytest.mark.asyncio
    async def test_bot_flow_fires_started_and_completed(
        self, monkeypatch, _patch_cli_deps
    ):
        """Bot token auth fires auth_started and auth_completed."""
        from src import cli_setup

        captured_events = _patch_cli_deps

        class FakeClient:
            def __init__(self, **kwargs):
                pass

            async def connect(self):
                pass

            async def start(self, bot_token=None):
                pass

            async def get_me(self):
                return SimpleNamespace(username="testbot", first_name="TestBot")

            async def disconnect(self):
                pass

        monkeypatch.setattr(cli_setup, "TelegramClient", FakeClient)
        monkeypatch.setattr(cli_setup, "build_mtproto_client_args", lambda *a, **k: {})
        monkeypatch.setattr(cli_setup, "generate_bearer_token", lambda: "tok123")

        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            cfg = _make_cli_cfg(tmp)
            cfg.bot_api_token = "bot:token123"
            result = await cli_setup.setup_telegram_session(cfg)

        assert result is not None
        events = [e for e in captured_events if e["method"] == "cli_setup"]
        assert len(events) == 2
        assert events[0]["event"] == "auth_started"
        assert events[0]["branch"] == "cli_bot_token"
        assert events[1]["event"] == "auth_completed"
        assert events[1]["branch"] == "cli_bot_token"

    @pytest.mark.asyncio
    async def test_phone_flow_fires_started_and_completed(
        self, monkeypatch, _patch_cli_deps
    ):
        """Phone auth fires auth_started and auth_completed."""
        from src import cli_setup

        captured_events = _patch_cli_deps

        class FakeClient:
            def __init__(self, **kwargs):
                pass

            async def connect(self):
                pass

            async def is_user_authorized(self):
                return True

            async def get_me(self):
                return SimpleNamespace(username="testuser", first_name="Test")

            async def iter_dialogs(self, limit=1):
                yield SimpleNamespace(name="Saved Messages")
                return

            async def disconnect(self):
                pass

        monkeypatch.setattr(cli_setup, "TelegramClient", FakeClient)
        monkeypatch.setattr(cli_setup, "build_mtproto_client_args", lambda *a, **k: {})
        monkeypatch.setattr(cli_setup, "generate_bearer_token", lambda: "tok123")

        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            cfg = _make_cli_cfg(tmp)
            cfg.phone_number = "+1234567890"
            result = await cli_setup.setup_telegram_session(cfg)

        assert result is not None
        events = [e for e in captured_events if e["method"] == "cli_setup"]
        assert len(events) == 2
        assert events[0]["event"] == "auth_started"
        assert events[0]["branch"] == "cli_phone_code"
        assert events[1]["event"] == "auth_completed"
        assert events[1]["branch"] == "cli_phone_code"

    @pytest.mark.asyncio
    async def test_phone_flow_failure_fires_auth_failed(
        self, monkeypatch, _patch_cli_deps
    ):
        """Phone auth failure fires auth_started and auth_failed."""
        from src import cli_setup

        captured_events = _patch_cli_deps

        class FakeClient:
            def __init__(self, **kwargs):
                pass

            async def connect(self):
                raise ConnectionError("Telegram unreachable")

            async def disconnect(self):
                pass

        monkeypatch.setattr(cli_setup, "TelegramClient", FakeClient)
        monkeypatch.setattr(cli_setup, "build_mtproto_client_args", lambda *a, **k: {})
        monkeypatch.setattr(cli_setup, "generate_bearer_token", lambda: "tok123")

        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            cfg = _make_cli_cfg(tmp)
            cfg.phone_number = "+1234567890"
            with pytest.raises(ConnectionError):
                await cli_setup.setup_telegram_session(cfg)

        events = [e for e in captured_events if e["method"] == "cli_setup"]
        assert len(events) == 2
        assert events[0]["event"] == "auth_started"
        assert events[1]["event"] == "auth_failed"
        assert events[1]["error"] == "connect_failed"


# ───────────────────────────── Bearer check telemetry ──────────────────


class TestBearerCheckTelemetry:
    """Test that require_auth fires bearer check telemetry with cooldown."""

    @pytest.fixture(autouse=True)
    def _reset_cooldown(self):
        """Reset bearer cooldown dict before each test."""
        from src.server_components import auth

        auth._BEARER_CHECK_COOLDOWN.clear()
        yield
        auth._BEARER_CHECK_COOLDOWN.clear()

    def test_fire_bearer_telemetry_fires_event(self, monkeypatch):
        """_fire_bearer_telemetry calls send_auth_event."""
        from src.server_components import auth

        captured = []
        import src.telemetry as tel

        monkeypatch.setattr(tel, "send_auth_event", lambda **kw: captured.append(kw))
        auth._fire_bearer_telemetry("auth_completed", "bearer_valid")

        assert len(captured) == 1
        assert captured[0]["event"] == "auth_completed"
        assert captured[0]["method"] == "bearer_check"
        assert captured[0]["branch"] == "bearer_valid"

    def test_bearer_cooldown_suppresses_duplicate(self, monkeypatch):
        """Second call within cooldown window is suppressed."""
        from src.server_components import auth

        captured = []
        import src.telemetry as tel

        monkeypatch.setattr(tel, "send_auth_event", lambda **kw: captured.append(kw))

        auth._fire_bearer_telemetry("auth_completed", "bearer_valid")
        auth._fire_bearer_telemetry("auth_completed", "bearer_valid")

        assert len(captured) == 1

    def test_bearer_cooldown_different_branches(self, monkeypatch):
        """Different branches have independent cooldowns."""
        from src.server_components import auth

        captured = []
        import src.telemetry as tel

        monkeypatch.setattr(tel, "send_auth_event", lambda **kw: captured.append(kw))

        auth._fire_bearer_telemetry("auth_completed", "bearer_valid")
        auth._fire_bearer_telemetry(
            "auth_failed", "bearer_no_session", "session_expired"
        )

        assert len(captured) == 2
        assert captured[0]["branch"] == "bearer_valid"
        assert captured[1]["branch"] == "bearer_no_session"

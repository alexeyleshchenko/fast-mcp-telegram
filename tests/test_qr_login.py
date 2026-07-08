"""Tests for QrLoginManager — in-memory QR session management."""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.server_components.qr_login import QrLoginError, QrLoginManager, SessionState


@pytest.fixture
def mock_telethon_client():
    """Create a mock Telethon client with qr_login support."""
    client = MagicMock()
    # Telethon's qr_login() is a coroutine
    qr_login_mock = MagicMock()
    qr_login_mock.url = "tg://login?token=abc123"
    client.qr_login = AsyncMock(return_value=qr_login_mock)
    client.disconnect = AsyncMock()
    return client


@pytest.fixture
def manager():
    """QrLoginManager with default 60s timeout."""
    return QrLoginManager(timeout_seconds=60)


@pytest.mark.asyncio
async def test_create_session(manager, mock_telethon_client):
    """Creating a QR session returns a session_id and QR URL."""
    mock_qr_login = MagicMock()
    mock_qr_login.url = "tg://login?token=abc123"
    mock_telethon_client.qr_login.return_value = mock_qr_login

    session_id, qr_url = await manager.create_session(mock_telethon_client)

    assert session_id is not None
    assert len(session_id) > 0
    assert qr_url == "tg://login?token=abc123"
    assert session_id in manager._sessions
    mock_telethon_client.qr_login.assert_awaited_once()


@pytest.mark.asyncio
async def test_create_session_generates_unique_ids(manager, mock_telethon_client):
    """Each call to create_session generates a unique session_id."""
    mock_qr_login = MagicMock()
    mock_qr_login.url = "tg://login?token=abc"
    mock_telethon_client.qr_login.return_value = mock_qr_login

    id1, _ = await manager.create_session(mock_telethon_client)
    id2, _ = await manager.create_session(mock_telethon_client)

    assert id1 != id2


@pytest.mark.asyncio
async def test_create_session_raises_qr_login_error_on_failure(
    manager, mock_telethon_client
):
    """create_session raises QrLoginError when Telethon qr_login() fails."""
    mock_telethon_client.qr_login.side_effect = RuntimeError("Telethon error")

    with pytest.raises(
        QrLoginError, match="Failed to create Telegram QR login session"
    ):
        await manager.create_session(mock_telethon_client)


@pytest.mark.asyncio
async def test_create_session_raises_qr_login_error_on_empty_url(
    manager, mock_telethon_client
):
    """create_session raises QrLoginError when Telethon returns empty URL."""
    mock_qr_login = MagicMock()
    mock_qr_login.url = ""
    mock_telethon_client.qr_login.return_value = mock_qr_login

    with pytest.raises(QrLoginError, match="did not return a valid QR URL"):
        await manager.create_session(mock_telethon_client)


@pytest.mark.asyncio
async def test_poll_status_pending(manager, mock_telethon_client):
    """poll_status returns 'pending' before the QR is scanned."""
    mock_qr_login = MagicMock()
    mock_qr_login.url = "tg://login?token=abc"
    mock_telethon_client.qr_login.return_value = mock_qr_login

    session_id, _ = await manager.create_session(mock_telethon_client)
    status = await manager.poll_status(session_id)

    assert status == "pending"


@pytest.mark.asyncio
async def test_poll_status_completed(manager, mock_telethon_client):
    """poll_status returns 'completed' and sets client when QR is scanned."""
    mock_tg_client = MagicMock()
    mock_tg_client.is_user_authorized.return_value = True
    mock_tg_client.disconnect = AsyncMock()

    mock_qr_login = MagicMock()
    mock_qr_login.url = "tg://login?token=abc"
    mock_qr_login.wait = AsyncMock(return_value=mock_tg_client)
    mock_telethon_client.qr_login.return_value = mock_qr_login

    session_id, _ = await manager.create_session(mock_telethon_client)

    # First poll kicks off background wait task
    status = await manager.poll_status(session_id)
    assert status == "pending"

    # Yield to event loop so background task can complete
    await asyncio.sleep(0)

    # Second poll should show completed
    status = await manager.poll_status(session_id)
    assert status == "completed"
    assert manager._sessions[session_id].resulting_client is mock_tg_client


@pytest.mark.asyncio
async def test_poll_status_expired(manager, mock_telethon_client):
    """poll_status returns 'expired' on timeout, and the QR can be regenerated."""
    mock_qr_login = MagicMock()
    mock_qr_login.url = "tg://login?token=abc"
    mock_qr_login.wait = AsyncMock(side_effect=TimeoutError("QR login timed out"))
    mock_telethon_client.qr_login.return_value = mock_qr_login

    session_id, _ = await manager.create_session(mock_telethon_client)
    status = await manager.poll_status(session_id)

    # First poll kicks off background task, may still be pending
    assert status in ("pending", "expired")

    # Yield so the background task finishes
    await asyncio.sleep(0)

    status = await manager.poll_status(session_id)
    assert status == "expired"
    mock_telethon_client.disconnect.assert_awaited_once()


@pytest.mark.asyncio
async def test_poll_status_non_timeout_error(manager, mock_telethon_client):
    """poll_status returns 'expired' when qr_login.wait() raises a non-timeout error."""
    mock_qr_login = MagicMock()
    mock_qr_login.url = "tg://login?token=abc"
    mock_qr_login.wait = AsyncMock(side_effect=RuntimeError("Unexpected error"))
    mock_telethon_client.qr_login.return_value = mock_qr_login

    session_id, _ = await manager.create_session(mock_telethon_client)
    status = await manager.poll_status(session_id)

    # First poll kicks off background task, may still be pending
    assert status in ("pending", "expired")

    # Yield so the background task finishes
    await asyncio.sleep(0)

    status = await manager.poll_status(session_id)
    assert status == "expired"
    mock_telethon_client.disconnect.assert_awaited_once()


@pytest.mark.asyncio
async def test_poll_status_not_found(manager):
    """poll_status returns 'not_found' for unknown session IDs."""
    status = await manager.poll_status("nonexistent")
    assert status == "not_found"


@pytest.mark.asyncio
async def test_get_client(manager, mock_telethon_client):
    """get_client returns the connected Telethon client after successful login."""
    mock_tg_client = MagicMock()
    mock_tg_client.is_user_authorized.return_value = True
    mock_tg_client.disconnect = AsyncMock()

    mock_qr_login = MagicMock()
    mock_qr_login.url = "tg://login?token=abc"
    mock_qr_login.wait = AsyncMock(return_value=mock_tg_client)
    mock_telethon_client.qr_login.return_value = mock_qr_login

    session_id, _ = await manager.create_session(mock_telethon_client)

    # First poll kicks off background wait task
    await manager.poll_status(session_id)

    # Yield so the background task finishes
    await asyncio.sleep(0)

    client = manager.get_client(session_id)
    assert client is mock_tg_client


def test_get_client_not_found(manager):
    """get_client returns None for unknown sessions."""
    client = manager.get_client("nonexistent")
    assert client is None


@pytest.mark.asyncio
async def test_get_client_not_completed(manager, mock_telethon_client):
    """get_client returns None for sessions that haven't completed."""
    mock_qr_login = MagicMock()
    mock_qr_login.url = "tg://login?token=abc"
    mock_telethon_client.qr_login.return_value = mock_qr_login

    session_id, _ = await manager.create_session(mock_telethon_client)
    client = manager.get_client(session_id)

    assert client is None


@pytest.mark.asyncio
async def test_regenerate_qr(manager, mock_telethon_client):
    """regenerate_qr creates a new QR for an existing session."""
    mock_qr_login = MagicMock()
    mock_qr_login.url = "tg://login?token=abc"
    mock_telethon_client.qr_login.return_value = mock_qr_login

    session_id, first_url = await manager.create_session(mock_telethon_client)

    # New QR login
    mock_qr_login2 = MagicMock()
    mock_qr_login2.url = "tg://login?token=def"
    mock_telethon_client.qr_login.return_value = mock_qr_login2

    second_url = await manager.regenerate_qr(session_id, mock_telethon_client)

    assert second_url is not None
    assert second_url != first_url
    mock_telethon_client.qr_login.assert_awaited()


@pytest.mark.asyncio
async def test_regenerate_qr_unknown_session_returns_none(
    manager, mock_telethon_client
):
    """regenerate_qr returns None when called with an unknown session id."""
    result = await manager.regenerate_qr("non-existent-session", mock_telethon_client)
    assert result is None


@pytest.mark.asyncio
async def test_cleanup_expired(manager, mock_telethon_client):
    """cleanup_expired removes expired sessions."""
    mock_qr_login = MagicMock()
    mock_qr_login.url = "tg://login?token=abc"
    mock_telethon_client.qr_login.return_value = mock_qr_login

    # Create a session and put it in the past
    session_id, _ = await manager.create_session(mock_telethon_client)
    manager._sessions[session_id].created_at = time.time() - 120  # 2 min ago

    manager.cleanup_expired()

    assert session_id not in manager._sessions


@pytest.mark.asyncio
async def test_cleanup_expired_keeps_fresh(manager, mock_telethon_client):
    """cleanup_expired does NOT remove sessions within the timeout."""
    mock_qr_login = MagicMock()
    mock_qr_login.url = "tg://login?token=abc"
    mock_telethon_client.qr_login.return_value = mock_qr_login

    session_id, _ = await manager.create_session(mock_telethon_client)

    manager.cleanup_expired()

    assert session_id in manager._sessions


@pytest.mark.asyncio
async def test_create_session_integration_flow():
    """Full integration-style test of QrLoginManager flow."""
    manager = QrLoginManager(timeout_seconds=30)
    mock_client = MagicMock()
    qr_login_mock = MagicMock()
    qr_login_mock.url = "tg://login?token=flowtest"
    qr_login_mock.wait = AsyncMock(return_value=MagicMock())
    mock_client.qr_login = AsyncMock(return_value=qr_login_mock)

    # Test: create → poll → complete → get_client
    session_id, qr_url = await manager.create_session(mock_client)
    assert qr_url == "tg://login?token=flowtest"

    # Async poll
    status = await manager.poll_status(session_id)
    assert status in ("pending", "completed")

    _ = manager.get_client(session_id)  # Client may/may not be set


# ── 2FA (Two-Factor Authentication) Tests ──────────────────────────────────────


@pytest.mark.asyncio
async def test_poll_status_2fa_required(manager, mock_telethon_client):
    """poll_status returns '2fa_required' when SessionPasswordNeededError is raised
    during QR scan, and the telethon_client is NOT disconnected."""
    from telethon.errors import SessionPasswordNeededError

    mock_qr_login = MagicMock()
    mock_qr_login.url = "tg://login?token=abc"
    mock_qr_login.wait = AsyncMock(side_effect=SessionPasswordNeededError(request=None))
    mock_telethon_client.qr_login.return_value = mock_qr_login
    mock_telethon_client.disconnect = AsyncMock()

    session_id, _ = await manager.create_session(mock_telethon_client)

    # First poll kicks off background wait task
    status = await manager.poll_status(session_id)
    assert status in ("pending", "2fa_required")

    # Yield so the background task finishes
    await asyncio.sleep(0)

    status = await manager.poll_status(session_id)
    assert status == "2fa_required"
    # Client must NOT be disconnected — it's needed for sign_in(password=)
    mock_telethon_client.disconnect.assert_not_called()


@pytest.mark.asyncio
async def test_poll_status_2fa_required_preserves_status_across_calls(
    manager, mock_telethon_client
):
    """Once '2fa_required', poll_status continues returning it."""
    from telethon.errors import SessionPasswordNeededError

    mock_qr_login = MagicMock()
    mock_qr_login.url = "tg://login?token=abc"
    mock_qr_login.wait = AsyncMock(side_effect=SessionPasswordNeededError(request=None))
    mock_telethon_client.qr_login.return_value = mock_qr_login

    session_id, _ = await manager.create_session(mock_telethon_client)
    await manager.poll_status(session_id)
    await asyncio.sleep(0)

    for _ in range(5):
        assert await manager.poll_status(session_id) == "2fa_required"


def test_get_password_hint_unknown_session(manager):
    """get_password_hint returns empty string for sessions that don't exist."""
    assert manager.get_password_hint("nonexistent-session-id") == ""


@pytest.mark.asyncio
async def test_get_password_hint_before_2fa(manager, mock_telethon_client):
    """get_password_hint returns empty string before 2FA is required."""
    mock_qr_login = MagicMock()
    mock_qr_login.url = "tg://login?token=abc"
    mock_telethon_client.qr_login.return_value = mock_qr_login

    session_id, _ = await manager.create_session(mock_telethon_client)
    assert manager.get_password_hint(session_id) == ""


def test_is_completed_false_for_2fa_required():
    """SessionState.is_completed returns False when status is '2fa_required'."""
    state = SessionState(telethon_client=MagicMock(), qr_url="tg://login?token=x")
    state.mark_2fa_required()
    assert state.status == "2fa_required"
    assert not state.is_completed


def test_mark_2fa_required_sets_hint():
    """mark_2fa_required stores the password hint."""
    state = SessionState(telethon_client=MagicMock(), qr_url="tg://login?token=x")
    state.mark_2fa_required(hint="test hint")
    assert state.password_hint == "test hint"
    assert state.status == "2fa_required"


def test_mark_2fa_required_empty_hint():
    """mark_2fa_required with no hint stores empty string."""
    state = SessionState(telethon_client=MagicMock(), qr_url="tg://login?token=x")
    state.mark_2fa_required()
    assert state.password_hint == ""


@pytest.mark.asyncio
async def test_cleanup_expired_removes_2fa_sessions(manager, mock_telethon_client):
    """cleanup_expired removes sessions stuck in '2fa_required' beyond timeout."""
    from telethon.errors import SessionPasswordNeededError

    mock_qr_login = MagicMock()
    mock_qr_login.url = "tg://login?token=abc"
    mock_qr_login.wait = AsyncMock(side_effect=SessionPasswordNeededError(request=None))
    mock_telethon_client.qr_login.return_value = mock_qr_login
    mock_telethon_client.disconnect = AsyncMock()

    session_id, _ = await manager.create_session(mock_telethon_client)
    await manager.poll_status(session_id)
    await asyncio.sleep(0)

    assert await manager.poll_status(session_id) == "2fa_required"

    # Age the session past 2x timeout so cleanup removes it
    manager._sessions[session_id].created_at = time.time() - 180

    removed = manager.cleanup_expired()
    assert removed >= 1
    assert session_id not in manager._sessions

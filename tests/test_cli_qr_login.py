"""Tests for QR login in CLI setup — TDD: these tests should fail until implementation."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telethon.errors import SessionPasswordNeededError

from src.cli_setup import SetupConfig, setup_telegram_session


def _make_setup_config(tmp_path, **overrides):
    """Create SetupConfig with CLI parsing disabled (test environment)."""
    defaults = {
        "api_id": "12345",
        "api_hash": "abc123def",
        "session_dir": str(tmp_path),
        "session_name": "test-qr",
    }
    defaults.update(overrides)
    # Disable CLI parsing to avoid pytest arg conflicts
    with patch.object(
        SetupConfig,
        "model_config",
        {**SetupConfig.model_config, "cli_parse_args": False},
    ):
        return SetupConfig(**defaults)


@pytest.fixture
def qr_setup_config(tmp_path):
    """SetupConfig with api_id/api_hash but NO phone_number and NO bot_api_token — triggers QR."""
    cfg = _make_setup_config(tmp_path)
    # Ensure no phone/bot flags are set — this should infer QR auth
    assert not cfg.phone_number
    assert not cfg.bot_api_token
    return cfg


@pytest.fixture(autouse=False)
def mock_tty():
    """Mock _is_interactive_terminal to return True — simulates interactive terminal for QR tests."""
    with patch("src.cli_setup._is_interactive_terminal", return_value=True):
        yield


@pytest.fixture
def mock_telethon_client():
    """Mock Telethon client with QR login support."""
    client = MagicMock()
    client.connect = AsyncMock()
    client.disconnect = AsyncMock()
    client.get_me = AsyncMock(
        return_value=SimpleNamespace(username="testuser", first_name="Test")
    )
    client.is_user_authorized = AsyncMock(return_value=False)
    client.get_password_hint = AsyncMock(return_value="hint")

    # QR login mock
    qr_login = MagicMock()
    qr_login.url = "tg://login?token=test_token_abc123"
    qr_login.wait = AsyncMock(
        return_value=SimpleNamespace(username="testuser", first_name="Test")
    )
    qr_login.recreate = AsyncMock()
    client.qr_login = AsyncMock(return_value=qr_login)

    return client


# ── Auth Method Inference ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_validate_required_fields_accepts_no_phone_no_bot(qr_setup_config):
    """When neither phone_number nor bot_api_token is set, validate should NOT raise.
    This means QR auth is accepted as a valid method."""
    # This will fail because current validate_required_fields requires phone OR bot token
    qr_setup_config.validate_required_fields()  # Should not raise


@pytest.mark.asyncio
async def test_validate_required_fields_still_requires_api_id(tmp_path):
    """Even with QR auth (no phone/bot), api_id is still required."""
    cfg = _make_setup_config(tmp_path, api_id="")
    with pytest.raises(ValueError, match="API ID is required"):
        cfg.validate_required_fields()


@pytest.mark.asyncio
async def test_validate_required_fields_still_requires_api_hash(tmp_path):
    """Even with QR auth (no phone/bot), api_hash is still required."""
    cfg = _make_setup_config(tmp_path, api_hash="")
    with pytest.raises(ValueError, match="API Hash is required"):
        cfg.validate_required_fields()


# ── QR Login Flow ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_qr_flow_renders_url_to_terminal(
    qr_setup_config, mock_telethon_client, mock_tty, capsys
):
    """QR login flow prints the tg:// URL to stdout as a fallback."""
    with patch("src.cli_setup.TelegramClient", return_value=mock_telethon_client):
        result = await setup_telegram_session(qr_setup_config)

    assert result is not None
    captured = capsys.readouterr()
    assert "tg://login?token=" in captured.out


@pytest.mark.asyncio
async def test_qr_flow_renders_scan_instructions(
    qr_setup_config, mock_telethon_client, mock_tty, capsys
):
    """QR login flow tells the user how to scan."""
    with patch("src.cli_setup.TelegramClient", return_value=mock_telethon_client):
        await setup_telegram_session(qr_setup_config)

    captured = capsys.readouterr()
    assert (
        "Settings" in captured.out
        or "Devices" in captured.out
        or "Link" in captured.out
    )


@pytest.mark.asyncio
async def test_qr_flow_returns_session_path_and_none_bearer(
    qr_setup_config, mock_telethon_client, mock_tty
):
    """QR login returns (session_path, None) — no bearer token for QR auth."""
    with patch("src.cli_setup.TelegramClient", return_value=mock_telethon_client):
        result = await setup_telegram_session(qr_setup_config)

    assert result is not None
    session_path, bearer_token = result
    assert bearer_token is None
    assert session_path is not None


@pytest.mark.asyncio
async def test_qr_flow_calls_qr_login_on_client(
    qr_setup_config, mock_telethon_client, mock_tty
):
    """QR flow calls client.qr_login() to generate the QR code."""
    with patch("src.cli_setup.TelegramClient", return_value=mock_telethon_client):
        await setup_telegram_session(qr_setup_config)

    mock_telethon_client.qr_login.assert_awaited_once()


@pytest.mark.asyncio
async def test_qr_flow_waits_for_scan(qr_setup_config, mock_telethon_client, mock_tty):
    """QR flow calls qr_login.wait() to block until the user scans."""
    with patch("src.cli_setup.TelegramClient", return_value=mock_telethon_client):
        await setup_telegram_session(qr_setup_config)

    mock_telethon_client.qr_login.return_value.wait.assert_awaited_once()


@pytest.mark.asyncio
async def test_qr_flow_validates_auth_with_get_me(
    qr_setup_config, mock_telethon_client, mock_tty
):
    """QR flow calls get_me() to verify auth succeeded before saving session."""
    with patch("src.cli_setup.TelegramClient", return_value=mock_telethon_client):
        await setup_telegram_session(qr_setup_config)

    mock_telethon_client.get_me.assert_awaited_once()


@pytest.mark.asyncio
async def test_qr_flow_disconnects_before_rename(
    qr_setup_config, mock_telethon_client, mock_tty
):
    """QR flow disconnects the client before renaming the temp session file.
    This prevents SQLite corruption from open file handles."""
    with patch("src.cli_setup.TelegramClient", return_value=mock_telethon_client):
        await setup_telegram_session(qr_setup_config)

    mock_telethon_client.disconnect.assert_awaited()


@pytest.mark.asyncio
async def test_qr_flow_creates_session_file(
    qr_setup_config, mock_telethon_client, mock_tty, tmp_path
):
    """QR flow returns a valid session_path on success."""
    # Mock client.session.filename to point to a fake temp file
    mock_session = MagicMock()
    mock_session.filename = str(tmp_path / "fake-temp")
    mock_telethon_client.session = mock_session

    with (
        patch("src.cli_setup.TelegramClient", return_value=mock_telethon_client),
        patch("src.cli_setup.os.rename"),
    ):
        result = await setup_telegram_session(qr_setup_config)

    assert result is not None
    session_path, bearer_token = result
    assert session_path is not None
    assert bearer_token is None


@pytest.mark.asyncio
async def test_qr_flow_cleans_up_temp_on_failure(
    qr_setup_config, mock_telethon_client, mock_tty, tmp_path
):
    """QR flow cleans up temp session file if auth fails."""
    # Make all 5 retries timeout — each recreate() returns a new QR whose wait() also times out
    timeout_qr = MagicMock()
    timeout_qr.url = "tg://login?token=timeout"
    timeout_qr.wait = AsyncMock(side_effect=TimeoutError("timeout"))
    timeout_qr.recreate = AsyncMock(return_value=timeout_qr)  # Returns itself

    mock_telethon_client.qr_login.return_value = timeout_qr

    with (
        patch("src.cli_setup.TelegramClient", return_value=mock_telethon_client),
        patch("src.cli_setup._cleanup_temp_session") as mock_cleanup,
    ):
        result = await setup_telegram_session(qr_setup_config)

    assert result is None
    mock_cleanup.assert_called_once()


# ── QR Expiry and Regeneration ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_qr_flow_regenerates_on_timeout(
    qr_setup_config, mock_telethon_client, mock_tty, capsys
):
    """When QR expires, flow should regenerate and re-render."""
    # First wait times out, second succeeds
    original_qr = mock_telethon_client.qr_login.return_value
    original_qr.wait = AsyncMock(
        side_effect=[
            TimeoutError("timeout"),
            SimpleNamespace(username="testuser", first_name="Test"),
        ]
    )
    # recreate() returns a new QR login object
    new_qr = MagicMock()
    new_qr.url = "tg://login?token=new_token_xyz"
    new_qr.wait = AsyncMock(
        return_value=SimpleNamespace(username="testuser", first_name="Test")
    )
    original_qr.recreate = AsyncMock(return_value=new_qr)

    with patch("src.cli_setup.TelegramClient", return_value=mock_telethon_client):
        result = await setup_telegram_session(qr_setup_config)

    assert result is not None
    # Should have printed both URLs
    captured = capsys.readouterr()
    assert "tg://login?token=" in captured.out

    # Assert QR timeout/regeneration behavior
    assert original_qr.wait.await_count == 1  # Initial QR awaited once, timed out
    original_qr.recreate.assert_awaited_once()  # recreate() called exactly once
    assert new_qr.wait.await_count == 1  # Regenerated QR awaited once


# ── 2FA After QR Scan ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_qr_flow_handles_2fa_after_scan(
    qr_setup_config, mock_telethon_client, mock_tty
):
    """When QR scan triggers 2FA, flow prompts for password."""
    mock_telethon_client.qr_login.return_value.wait = AsyncMock(
        side_effect=SessionPasswordNeededError(request=None)
    )
    mock_telethon_client.sign_in = AsyncMock()

    with (
        patch("src.cli_setup.TelegramClient", return_value=mock_telethon_client),
        patch("src.cli_setup._print_2fa_password_hint", new_callable=AsyncMock),
        patch("src.cli_setup.getpass.getpass", return_value="mypassword"),
    ):
        result = await setup_telegram_session(qr_setup_config)

    assert result is not None
    mock_telethon_client.sign_in.assert_awaited_with(password="mypassword")


@pytest.mark.asyncio
async def test_qr_flow_shows_2fa_hint(
    qr_setup_config, mock_telethon_client, mock_tty, capsys
):
    """When 2FA is required, flow shows the password hint."""
    mock_telethon_client.qr_login.return_value.wait = AsyncMock(
        side_effect=SessionPasswordNeededError(request=None)
    )
    mock_telethon_client.sign_in = AsyncMock()

    # Patch _print_2fa_password_hint to print the hint directly
    async def fake_print_hint(_client):
        print("2FA password hint: my hint")

    with (
        patch("src.cli_setup.TelegramClient", return_value=mock_telethon_client),
        patch("src.cli_setup._print_2fa_password_hint", side_effect=fake_print_hint),
        patch("src.cli_setup.getpass.getpass", return_value="mypassword"),
    ):
        await setup_telegram_session(qr_setup_config)

    captured = capsys.readouterr()
    assert "my hint" in captured.out


@pytest.mark.asyncio
async def test_qr_flow_retries_on_wrong_2fa_password(
    qr_setup_config, mock_telethon_client, mock_tty
):
    """When 2FA password is wrong, flow retries up to 3 times."""
    from telethon.errors import PasswordHashInvalidError

    mock_telethon_client.qr_login.return_value.wait = AsyncMock(
        side_effect=SessionPasswordNeededError(request=None)
    )
    # First two attempts wrong, third correct
    mock_telethon_client.sign_in = AsyncMock(
        side_effect=[
            PasswordHashInvalidError(request=None),
            PasswordHashInvalidError(request=None),
            SimpleNamespace(username="testuser"),
        ]
    )

    with (
        patch("src.cli_setup.TelegramClient", return_value=mock_telethon_client),
        patch("src.cli_setup._print_2fa_password_hint", new_callable=AsyncMock),
        patch(
            "src.cli_setup.getpass.getpass", side_effect=["wrong1", "wrong2", "correct"]
        ),
    ):
        result = await setup_telegram_session(qr_setup_config)

    assert result is not None
    assert mock_telethon_client.sign_in.await_count == 3


@pytest.mark.asyncio
async def test_qr_flow_fails_on_all_2fa_passwords_wrong(
    qr_setup_config, mock_telethon_client, mock_tty, capsys
):
    """When all 3 2FA password attempts are wrong, flow returns None."""
    from telethon.errors import PasswordHashInvalidError

    mock_telethon_client.qr_login.return_value.wait = AsyncMock(
        side_effect=SessionPasswordNeededError(request=None)
    )
    mock_telethon_client.sign_in = AsyncMock(
        side_effect=PasswordHashInvalidError(request=None)
    )

    with (
        patch("src.cli_setup.TelegramClient", return_value=mock_telethon_client),
        patch("src.cli_setup._print_2fa_password_hint", new_callable=AsyncMock),
        patch(
            "src.cli_setup.getpass.getpass", side_effect=["wrong1", "wrong2", "wrong3"]
        ),
    ):
        result = await setup_telegram_session(qr_setup_config)

    assert result is None
    assert mock_telethon_client.sign_in.await_count == 3
    captured = capsys.readouterr()
    assert "Too many wrong password attempts" in captured.out


# ── Non-TTY Detection ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_non_tty_raises_actionable_error(tmp_path):
    """In non-TTY environments (CI), setup should fail fast with an actionable message."""
    cfg = _make_setup_config(tmp_path)

    with patch("src.cli_setup._is_interactive_terminal", return_value=False):
        result = await setup_telegram_session(cfg)

    assert result is None


# ── Display Config Instructions ───────────────────────────────────────���────────


@pytest.mark.asyncio
async def test_qr_flow_displays_account_info(
    qr_setup_config, mock_telethon_client, mock_tty, capsys
):
    """After successful QR auth, shows the authenticated user's info."""
    mock_telethon_client.get_me.return_value = SimpleNamespace(
        username="testuser", first_name="Test"
    )
    mock_telethon_client.iter_dialogs = MagicMock()
    mock_telethon_client.iter_dialogs.return_value.__aiter__ = AsyncMock(
        return_value=iter([SimpleNamespace(name="Saved Messages")])
    )

    with patch("src.cli_setup.TelegramClient", return_value=mock_telethon_client):
        await setup_telegram_session(qr_setup_config)

    captured = capsys.readouterr()
    assert "testuser" in captured.out or "Test" in captured.out

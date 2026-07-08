"""Stdio / no-auth default session path (reserved session_name, e.g. telegram)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from src.client.connection import (
    _resolve_session_path_for_token,
    get_connected_client,
    set_request_token,
)
from src.config.server_config import ServerConfig, ServerMode, set_config
from src.server_components.session_token_validation import (
    InvalidSessionTokenError,
    validated_session_file_path,
)


@pytest.fixture
def stdio_config(tmp_path: Path):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    config = ServerConfig(_cli_parse_args=[])
    config.server_mode = ServerMode.STDIO
    config.session_dir = str(session_dir)
    config.session_name = "telegram"
    set_config(config)
    return config


def test_resolve_session_path_uses_config_path_for_reserved_name(stdio_config):
    """Reserved session_name must not go through bearer token validation in stdio mode."""
    path = _resolve_session_path_for_token("telegram")
    assert path == stdio_config.session_path
    with pytest.raises(InvalidSessionTokenError):
        validated_session_file_path(stdio_config.session_directory, "telegram")


@pytest.mark.asyncio
async def test_get_connected_client_stdio_default_session_name(stdio_config):
    """get_connected_client with no request token uses session_path, not bearer rules."""
    set_request_token(None)
    mock_client = AsyncMock()
    mock_client.is_connected.return_value = True

    with (
        patch(
            "src.client.connection._get_client_by_token",
            new_callable=AsyncMock,
            return_value=mock_client,
        ) as get_mock,
        patch(
            "src.client.connection.ensure_connection",
            new_callable=AsyncMock,
            return_value=True,
        ),
    ):
        client = await get_connected_client()

    assert client is mock_client
    get_mock.assert_awaited_once()
    token_arg = get_mock.await_args[0][0]
    assert token_arg == stdio_config.session_name

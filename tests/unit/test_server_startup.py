"""Tests for server startup: banner suppression, stdio transport, etc."""

from unittest.mock import patch

import pytest

from src.config.server_config import reset_cfg_for_tests, set_config


@pytest.fixture(autouse=True)
def _reset():
    yield
    reset_cfg_for_tests()


def test_show_banner_suppressed_in_stdio_mode():
    """mcp.run() receives show_banner=False in stdio mode to suppress FastMCP banner.

    The FastMCP banner emits ANSI text to stderr on every uvx invocation,
    which fills gateway journals (Hermes, etc.) with noise.
    """
    # Build a config for stdio mode
    from src.config.server_config import ServerConfig

    cfg = ServerConfig(
        server_mode="stdio",
        api_id="12345",
        api_hash="a" * 32,
    )
    set_config(cfg)

    # Patch mcp.run on the module — the global instance is already created
    with patch("src.server.mcp.run") as mock_run:
        # Import main inside the patch so it picks up our config
        from src.server import main

        main()

    mock_run.assert_called_once()
    kwargs = mock_run.call_args.kwargs
    assert kwargs.get("show_banner") is False, (
        f"Expected show_banner=False, got kwargs={kwargs}"
    )

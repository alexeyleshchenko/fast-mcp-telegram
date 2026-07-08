"""Tests for server_config: host defaults, validation, and mode-specific behaviour."""

import os
from unittest.mock import patch

from src.config.server_config import ServerConfig


def test_default_host_is_localhost():
    """Default host is 127.0.0.1 regardless of server mode."""
    cfg = ServerConfig(
        server_mode="stdio",
        api_id="12345",
        api_hash="a" * 32,
    )
    assert cfg.host == "127.0.0.1", "stdio mode should default to 127.0.0.1"


def test_http_auth_host_not_overridden():
    """HTTP mode no longer overrides host to 0.0.0.0 — default stays 127.0.0.1."""
    cfg = ServerConfig(
        server_mode="http-auth",
        api_id="12345",
        api_hash="a" * 32,
    )
    assert cfg.host == "127.0.0.1", (
        "http-auth mode should respect default 127.0.0.1, was overridden to 0.0.0.0"
    )


def test_http_no_auth_host_not_overridden():
    """HTTP-no-auth mode also keeps 127.0.0.1 by default."""
    cfg = ServerConfig(
        server_mode="http-no-auth",
        api_id="12345",
        api_hash="a" * 32,
    )
    assert cfg.host == "127.0.0.1", "http-no-auth mode should default to 127.0.0.1"


def test_explicit_host_override():
    """Explicit --host or HOST env var still works to set 0.0.0.0."""
    cfg = ServerConfig(
        server_mode="http-auth",
        host="0.0.0.0",
        api_id="12345",
        api_hash="a" * 32,
    )
    assert cfg.host == "0.0.0.0", "explicit host override should be honoured"


def test_host_env_var():
    """HOST env var still overrides the default."""
    env = {
        "SERVER_MODE": "http-auth",
        "HOST": "0.0.0.0",
        "API_ID": "12345",
        "API_HASH": "a" * 32,
    }
    with patch.dict(os.environ, env, clear=False):
        cfg = ServerConfig()
    assert cfg.host == "0.0.0.0", "HOST env var should be honoured"


def test_empty_host_resolves_to_localhost():
    """Empty host string should fall back to 127.0.0.1."""
    cfg = ServerConfig(
        server_mode="http-auth",
        host="",
        api_id="12345",
        api_hash="a" * 32,
    )
    assert cfg.host == "127.0.0.1"

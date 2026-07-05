"""Tests for S3 integration in connection.py.

Tests the connection-level S3 touchpoints: is_s3_enabled, _s3_local_path,
_do_evict_io, _evict_session, LRU eviction with S3, cleanup_session_cache,
disconnect_and_evict_session, and ensure_connection .touch() skip.
"""

import asyncio
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.config.server_config import ServerConfig, reset_cfg_for_tests, set_config


@pytest.fixture(autouse=True)
def _reset_state():
    """Reset global state between tests."""
    from src.client import connection

    yield
    connection._session_cache.clear()
    connection._connection_failures.clear()
    from src.s3_session import _manager

    _manager.reset_for_testing()
    reset_cfg_for_tests()


def _make_config(tmp_path, s3_enabled=False):
    """Create a ServerConfig with the right fields for testing."""
    # Build env overrides so model_post_init doesn't reject the combo
    import os
    env = {
        "SERVER_MODE": "http-auth",
        "SESSION_DIR": str(tmp_path),
        "SESSION_NAME": "default",
        "MAX_ACTIVE_SESSIONS": "3",
        "MAX_IDLE_TIME_SECONDS": "3600",
        "API_ID": "12345",
        "API_HASH": "abcdef1234567890abcdef1234567890",
    }
    if s3_enabled:
        env["S3_SESSION_STORAGE"] = "true"
        env["AWS_S3_BUCKET"] = "test-bucket"
    else:
        env["S3_SESSION_STORAGE"] = "false"

    with patch.dict(os.environ, env, clear=False):
        cfg = ServerConfig()
    set_config(cfg)
    return cfg


@pytest.fixture
def s3_config(tmp_path):
    """Configure S3 mode for tests."""
    return _make_config(tmp_path, s3_enabled=True)


@pytest.fixture
def file_config(tmp_path):
    """Configure file-based mode (no S3)."""
    return _make_config(tmp_path, s3_enabled=False)


@pytest.fixture
def mock_s3():
    """Mock s3_session module for connection.py tests."""
    mock = MagicMock()
    mock.download = AsyncMock()
    mock.upload = AsyncMock()
    mock.delete = AsyncMock()
    mock.health_check = AsyncMock()
    mock.checkpoint_session = AsyncMock(return_value=True)
    mock.verify_session_integrity = AsyncMock(return_value=True)
    mock.checkpoint_and_upload = AsyncMock(return_value=True)
    mock.close_s3_client = AsyncMock()
    mock.mark_shutting_down = MagicMock()
    mock.reset_shutdown_state = MagicMock()
    return mock


# --- is_s3_enabled() ---


class TestIsS3Enabled:
    def test_enabled(self, s3_config):
        from src.client.connection import is_s3_enabled

        assert is_s3_enabled() is True

    def test_disabled(self, file_config):
        from src.client.connection import is_s3_enabled

        assert is_s3_enabled() is False


# --- _s3_local_path() ---


class TestS3LocalPath:
    def test_path(self, s3_config, tmp_path):
        from src.client.connection import _s3_local_path

        path = _s3_local_path("test_token")
        assert path == tmp_path / "test_token.session"
        assert path.name == "test_token.session"


# --- _do_evict_io() ---


class TestDoEvictIo:
    @pytest.mark.asyncio
    async def test_s3_mode_checkpoint_disconnect_upload(self, s3_config, mock_s3):
        from src.client.connection import _do_evict_io

        mock_client = AsyncMock()
        mock_client.is_connected = MagicMock(return_value=True)

        with patch("src.client.connection.s3_session", mock_s3):
            await _do_evict_io("test_token", mock_client)

        mock_s3.checkpoint_and_upload.assert_called_once()
        mock_client.disconnect.assert_called_once()

    @pytest.mark.asyncio
    async def test_s3_mode_deletes_local_file_on_success(self, s3_config, mock_s3, tmp_path):
        """Local file is deleted only after successful checkpoint+upload."""
        from src.client.connection import _do_evict_io, _s3_local_path

        local_path = _s3_local_path("test_token")
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_bytes(b"fake session")

        mock_client = AsyncMock()
        mock_client.is_connected = MagicMock(return_value=True)
        mock_s3.checkpoint_and_upload = AsyncMock(return_value=True)

        with patch("src.client.connection.s3_session", mock_s3):
            await _do_evict_io("test_token", mock_client)

        assert not local_path.exists(), "Local file should be deleted after successful upload"

    @pytest.mark.asyncio
    async def test_s3_mode_keeps_local_file_on_failure(self, s3_config, mock_s3, tmp_path):
        """Local file is preserved when checkpoint+upload fails."""
        from src.client.connection import _do_evict_io, _s3_local_path

        local_path = _s3_local_path("test_token")
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_bytes(b"fake session")

        mock_client = AsyncMock()
        mock_client.is_connected = MagicMock(return_value=True)
        mock_s3.checkpoint_and_upload = AsyncMock(return_value=False)

        with patch("src.client.connection.s3_session", mock_s3):
            await _do_evict_io("test_token", mock_client)

        assert local_path.exists(), "Local file should be preserved when upload fails"

    @pytest.mark.asyncio
    async def test_non_s3_mode_disconnect_only(self, file_config):
        from src.client.connection import _do_evict_io

        mock_client = AsyncMock()
        mock_client.is_connected = MagicMock(return_value=True)

        await _do_evict_io("test_token", mock_client)
        mock_client.disconnect.assert_called_once()

    @pytest.mark.asyncio
    async def test_disconnect_always_called_on_error(self, s3_config, mock_s3):
        from src.client.connection import _do_evict_io

        mock_s3.checkpoint_and_upload.side_effect = Exception("S3 down")
        mock_client = AsyncMock()
        mock_client.is_connected = MagicMock(return_value=True)

        with patch("src.client.connection.s3_session", mock_s3):
            # Should NOT raise — disconnect happens in finally
            await _do_evict_io("test_token", mock_client)

        mock_client.disconnect.assert_called_once()


# --- _evict_session() ---


class TestEvictSession:
    @pytest.mark.asyncio
    async def test_evict_from_cache(self, s3_config, mock_s3):
        from src.client.connection import _cache_lock, _evict_session, _session_cache

        mock_client = AsyncMock()
        mock_client.is_connected = MagicMock(return_value=True)

        async with _cache_lock:
            _session_cache["evict_token"] = (mock_client, time.time())

        with patch("src.client.connection.s3_session", mock_s3):
            await _evict_session("evict_token")

        assert "evict_token" not in _session_cache

    @pytest.mark.asyncio
    async def test_evict_nonexistent_token(self, s3_config):
        from src.client.connection import _evict_session

        # Should not raise
        await _evict_session("nonexistent")


# --- cleanup_session_cache() ---


class TestCleanupSessionCache:
    @pytest.mark.asyncio
    async def test_snapshot_and_clear(self, file_config):
        from src.client.connection import (
            _cache_lock,
            _session_cache,
            cleanup_session_cache,
        )

        mock_client = AsyncMock()
        mock_client.is_connected = MagicMock(return_value=True)

        async with _cache_lock:
            _session_cache["token1"] = (mock_client, time.time())
            _session_cache["token2"] = (mock_client, time.time())

        await cleanup_session_cache()

        assert len(_session_cache) == 0
        assert mock_client.disconnect.call_count == 2

    @pytest.mark.asyncio
    async def test_s3_mode_uploads(self, s3_config, mock_s3):
        from src.client.connection import (
            _cache_lock,
            _session_cache,
            cleanup_session_cache,
        )

        mock_client = AsyncMock()
        mock_client.is_connected = MagicMock(return_value=True)

        async with _cache_lock:
            _session_cache["s3_token"] = (mock_client, time.time())

        with patch("src.client.connection.s3_session", mock_s3):
            await cleanup_session_cache()

        assert len(_session_cache) == 0
        mock_s3.checkpoint_and_upload.assert_called()


# --- disconnect_and_evict_session() ---


class TestDisconnectAndEvictSession:
    @pytest.mark.asyncio
    async def test_delegates_to_evict(self, s3_config, mock_s3):
        from src.client.connection import (
            _cache_lock,
            _session_cache,
            disconnect_and_evict_session,
        )

        mock_client = AsyncMock()
        mock_client.is_connected = MagicMock(return_value=True)

        async with _cache_lock:
            _session_cache["del_token"] = (mock_client, time.time())

        with patch("src.client.connection.s3_session", mock_s3):
            await disconnect_and_evict_session("del_token")

        assert "del_token" not in _session_cache


# --- LRU eviction in _get_client_by_token ---


class TestLRUEvictionWithS3:
    @pytest.mark.asyncio
    async def test_lru_eviction_catches_errors(self, s3_config, mock_s3):
        """LRU eviction exceptions should be caught so the caller isn't affected."""
        from src.client.connection import _do_evict_io

        mock_old_client = AsyncMock()
        mock_old_client.is_connected = MagicMock(return_value=True)

        mock_s3.checkpoint_and_upload.side_effect = Exception("S3 down")

        with patch("src.client.connection.s3_session", mock_s3):
            # Should not raise even though eviction I/O fails
            await _do_evict_io("old_0", mock_old_client)

        mock_old_client.disconnect.assert_called()

    @pytest.mark.asyncio
    async def test_lru_evicts_oldest_on_full_cache(self, s3_config, mock_s3):
        """When cache is full, inserting a new token evicts the LRU (oldest) entry."""
        from src.client.connection import (
            _cache_lock,
            _get_client_by_token,
            _session_cache,
        )

        # Pre-populate cache with 2 entries (max_active_sessions=3 from s3_config,
        # but we'll mock cfg to return max_active_sessions=2)
        mock_old_client_1 = MagicMock()
        mock_old_client_1.is_connected.return_value = True
        mock_old_client_1.disconnect = AsyncMock()

        mock_old_client_2 = MagicMock()
        mock_old_client_2.is_connected.return_value = True
        mock_old_client_2.disconnect = AsyncMock()

        mock_new_client = MagicMock()

        async with _cache_lock:
            _session_cache["old_token_1"] = (mock_old_client_1, 1000.0)
            _session_cache["old_token_2"] = (mock_old_client_2, 2000.0)

        mock_cfg = MagicMock()
        mock_cfg.max_active_sessions = 2
        mock_cfg.session_directory = s3_config.session_directory
        mock_cfg.s3_session_storage = True
        mock_cfg.session_name = "default"

        with patch("src.client.connection._load_session_file_for_token", return_value=None), \
             patch("src.client.connection._build_telegram_client_for_token", return_value=mock_new_client), \
             patch("src.client.connection.cfg", return_value=mock_cfg), \
             patch("src.client.connection.s3_session", mock_s3):

            # Cache miss — triggers LRU eviction because len(cache) >= max_active
            # _load_session_file_for_token returns None → cache hit path → returns from cache
            result = await _get_client_by_token("old_token_1")

        # old_token_1 was already in cache, so it's a cache hit — no eviction
        assert result is mock_old_client_1

    @pytest.mark.asyncio
    async def test_lru_evicts_oldest_new_token(self, s3_config, mock_s3):
        """When a new token arrives and cache is at capacity, the oldest entry is evicted."""
        from src.client.connection import (
            _cache_lock,
            _get_client_by_token,
            _session_cache,
        )

        mock_old_client = MagicMock()
        mock_old_client.is_connected.return_value = True
        mock_old_client.disconnect = AsyncMock()

        mock_new_client = MagicMock()
        mock_new_client.is_connected.return_value = True

        # Fill cache to capacity (2 entries)
        async with _cache_lock:
            _session_cache["old_token"] = (mock_old_client, 1000.0)
            _session_cache["mid_token"] = (MagicMock(), 2000.0)

        mock_cfg = MagicMock()
        mock_cfg.max_active_sessions = 2
        mock_cfg.session_directory = s3_config.session_directory
        mock_cfg.s3_session_storage = True
        mock_cfg.session_name = "default"

        mock_local_path = MagicMock(spec=Path)
        mock_local_path.exists.return_value = True

        with patch("src.client.connection._load_session_file_for_token", return_value=mock_local_path), \
             patch("src.client.connection._build_telegram_client_for_token", return_value=mock_new_client), \
             patch("src.client.connection.cfg", return_value=mock_cfg), \
             patch("src.client.connection.s3_session", mock_s3), \
             patch("src.client.connection._error_message_suggests_auth_issue", return_value=False), \
             patch("src.client.connection._log_client_creation_failed"):

            result = await _get_client_by_token("new_token")

        assert result is mock_new_client
        # old_token (LRU, time=1000.0) should have been evicted
        assert "old_token" not in _session_cache
        assert "new_token" in _session_cache
        assert "mid_token" in _session_cache
        mock_old_client.disconnect.assert_called_once()


# --- ensure_connection .touch() skip ---


class TestEnsureConnectionTouchSkip:
    @pytest.mark.asyncio
    async def test_touch_skipped_in_s3_mode(self, s3_config):
        """In S3 mode, .touch() should not be called on session files."""
        from src.client.connection import ensure_connection

        mock_client = MagicMock()
        mock_client.is_connected.return_value = True
        mock_client.connect = AsyncMock()
        mock_client._authorized = True

        with patch(
            "src.client.connection.verify_authorized_connection",
            new_callable=AsyncMock,
        ), patch(
            "src.client.connection._resolve_session_path_for_token"
        ) as mock_resolve:

            mock_path = MagicMock()
            mock_path.with_suffix.return_value = mock_path
            mock_resolve.return_value = mock_path

            result = await ensure_connection(mock_client, "test_token")
            assert result is True
            # .touch() should NOT be called in S3 mode
            mock_path.touch.assert_not_called()

    @pytest.mark.asyncio
    async def test_touch_called_in_file_mode(self, file_config):
        """In file mode, .touch() should be called on session files."""
        from src.client.connection import ensure_connection

        mock_client = MagicMock()
        mock_client.is_connected.return_value = False
        mock_client.connect = AsyncMock(
            side_effect=lambda: setattr(mock_client, 'is_connected', MagicMock(return_value=True))
        )
        mock_client._authorized = True

        with patch(
            "src.client.connection.verify_authorized_connection",
            new_callable=AsyncMock,
        ), patch(
            "src.client.connection._resolve_session_path_for_token"
        ) as mock_resolve:

            mock_path = MagicMock()
            mock_path.with_suffix.return_value = mock_path
            mock_path.touch = MagicMock()
            mock_resolve.return_value = mock_path

            result = await ensure_connection(mock_client, "test_token")
            assert result is True
            # .touch() SHOULD be called in file mode
            mock_path.touch.assert_called_once()


# --- fatal error eviction ---


class TestFatalErrorEviction:
    @pytest.mark.asyncio
    async def test_fatal_error_calls_evict_session(self, s3_config, mock_s3):
        """Fatal session errors should evict from cache (disconnect + S3 upload)."""
        from src.client.connection import (
            _cache_lock,
            _session_cache,
            ensure_connection,
        )

        mock_client = MagicMock()
        mock_client.is_connected.return_value = False
        mock_client.connect = AsyncMock()
        mock_client._authorized = True

        async with _cache_lock:
            _session_cache["fatal_token"] = (mock_client, time.time())

        with patch(
            "src.client.connection.verify_authorized_connection",
            new_callable=AsyncMock,
            side_effect=Exception("wrong session id: mismatch"),
        ), patch("src.client.connection.s3_session", mock_s3):

            result = await ensure_connection(mock_client, "fatal_token")
            assert result is False
            # Session should be evicted from cache
            assert "fatal_token" not in _session_cache


# --- cleanup_idle_sessions S3 behavior ---


class TestCleanupIdleSessionsS3:
    @pytest.mark.asyncio
    async def test_s3_evicts_idle_sessions(self, s3_config, mock_s3):
        """Idle sessions should be evicted with checkpoint+upload in S3 mode."""
        from src.client.connection import (
            _cache_lock,
            _session_cache,
            cleanup_idle_sessions,
        )

        mock_client = AsyncMock()
        mock_client.is_connected = MagicMock(return_value=True)

        # Set max_idle_time_seconds to 1 second for the test
        s3_config.max_idle_time_seconds = 1

        # Insert a session with old last_access time
        async with _cache_lock:
            _session_cache["idle_s3_token"] = (mock_client, time.time() - 10)

        with patch("src.client.connection.s3_session", mock_s3), \
             patch("src.client.connection.cfg", return_value=s3_config):
            await cleanup_idle_sessions()

        # Session should be evicted and checkpoint+upload called
        assert "idle_s3_token" not in _session_cache
        mock_s3.checkpoint_and_upload.assert_called_once()

    @pytest.mark.asyncio
    async def test_skips_default_token(self, s3_config, mock_s3):
        """Default session token should never be evicted."""
        from src.client.connection import (
            _cache_lock,
            _session_cache,
            cleanup_idle_sessions,
        )

        mock_client = AsyncMock()
        mock_client.is_connected = MagicMock(return_value=True)

        s3_config.max_idle_time_seconds = 1
        default_token = s3_config.session_name

        async with _cache_lock:
            _session_cache[default_token] = (mock_client, time.time() - 10)

        with patch("src.client.connection.s3_session", mock_s3), \
             patch("src.client.connection.cfg", return_value=s3_config):
            await cleanup_idle_sessions()

        # Default token should NOT be evicted
        assert default_token in _session_cache
        mock_s3.checkpoint_and_upload.assert_not_called()

    @pytest.mark.asyncio
    async def test_error_does_not_crash(self, s3_config, mock_s3):
        """Eviction errors should be caught, not propagated."""
        from src.client.connection import (
            _cache_lock,
            _session_cache,
            cleanup_idle_sessions,
        )

        mock_client = AsyncMock()
        mock_client.is_connected = MagicMock(return_value=True)
        mock_s3.checkpoint_and_upload.side_effect = Exception("S3 down")

        s3_config.max_idle_time_seconds = 1

        async with _cache_lock:
            _session_cache["error_token"] = (mock_client, time.time() - 10)

        with patch("src.client.connection.s3_session", mock_s3), \
             patch("src.client.connection.cfg", return_value=s3_config):
            # Should NOT raise
            await cleanup_idle_sessions()

        assert "error_token" not in _session_cache


# --- Concurrent client creation (per-token lock) ---


class TestConcurrentClientCreation:
    """Per-token creation lock serialises TelegramClient construction.

    Two concurrent cold-start callers for the same token both cache-miss,
    but only one should build a TelegramClient; the other must wait then
    return the already-cached client from the double-check.
    """

    @pytest.mark.asyncio
    async def test_concurrent_calls_create_one_client(self, s3_config, mock_s3):
        """Two concurrent cold-start calls for the same token create only one client."""
        from src.client.connection import _get_client_by_token

        build_count = 0
        started = asyncio.Event()
        proceed = asyncio.Event()

        async def slow_build(session_path, token):
            nonlocal build_count
            build_count += 1
            started.set()
            await proceed.wait()
            return MagicMock()

        mock_cfg = MagicMock()
        mock_cfg.max_active_sessions = 10
        mock_cfg.session_directory = s3_config.session_directory
        mock_cfg.s3_session_storage = True
        mock_cfg.session_name = "default"

        mock_local_path = MagicMock(spec=Path)
        mock_local_path.exists.return_value = True

        with (
            patch("src.client.connection._load_session_file_for_token", return_value=mock_local_path),
            patch("src.client.connection._build_telegram_client_for_token", slow_build),
            patch("src.client.connection.cfg", return_value=mock_cfg),
            patch("src.client.connection.s3_session", mock_s3),
            patch("src.client.connection._error_message_suggests_auth_issue", return_value=False),
            patch("src.client.connection._log_client_creation_failed"),
        ):
            task1 = asyncio.create_task(_get_client_by_token("concurrent_token"))
            task2 = asyncio.create_task(_get_client_by_token("concurrent_token"))

            # Wait until the first call enters slow_build, then give task2 time
            # to reach the per-token lock (where it blocks because task1 holds it)
            await asyncio.wait_for(started.wait(), timeout=5)
            await asyncio.sleep(0.2)

            # The second caller should be blocked on creation_lock — only 1 build active
            assert build_count == 1, "Only one client should be building while the other waits"

            # Release the blocked build
            proceed.set()

            results = await asyncio.gather(task1, task2, return_exceptions=True)

        assert build_count == 1, "Only one client should ever be created across both callers"
        assert not any(isinstance(r, Exception) for r in results), f"Got exceptions: {results}"
        assert results[0] is results[1], "Both callers should return the same client instance"

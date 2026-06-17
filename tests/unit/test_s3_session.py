"""Unit tests for src/s3_session.py.

Uses mock S3 client (unittest.mock) instead of moto, since moto is
incompatible with aiobotocore's async HTTP layer.
"""

import asyncio
import os
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


TEST_BUCKET = "test-session-bucket"


@pytest.fixture(autouse=True)
def _reset_s3_state():
    """Reset global state between tests."""
    from src import s3_session

    yield
    s3_session._bucket = ""
    s3_session._shutting_down = False
    s3_session._client = None


@pytest.fixture
def mock_client():
    """Create a mock aiobotocore S3 client."""
    client = AsyncMock()
    return client


@pytest.fixture
def configure():
    """Configure S3 bucket for tests."""
    from src import s3_session

    s3_session.configure(TEST_BUCKET)


@pytest.fixture
def sample_session(tmp_path):
    """Create a minimal valid SQLite session file."""
    db_path = tmp_path / "test.session"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE version (ver INTEGER)")
    conn.execute("INSERT INTO version VALUES (1)")
    conn.commit()
    conn.close()
    return db_path


@pytest.fixture
def sample_wal_session(tmp_path):
    """Create a SQLite session file with WAL mode enabled."""
    db_path = tmp_path / "test_wal.session"
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE version (ver INTEGER)")
    conn.execute("INSERT INTO version VALUES (1)")
    conn.commit()
    conn.close()
    return db_path


# --- configure() ---


class TestConfigure:
    def test_sets_bucket(self, configure):
        from src import s3_session

        assert s3_session._bucket == TEST_BUCKET

    def test_rejects_empty(self):
        from src import s3_session

        with pytest.raises(ValueError, match="required"):
            s3_session.configure("")

    def test_rejects_whitespace_only(self):
        from src import s3_session

        with pytest.raises(ValueError, match="required"):
            s3_session.configure("   ")


# --- _s3_key() ---


class TestS3Key:
    def test_valid_token(self, configure):
        from src import s3_session

        assert s3_session._s3_key("abc123") == "abc123.session"

    def test_rejects_empty(self):
        from src import s3_session

        with pytest.raises(ValueError, match="Invalid token"):
            s3_session._s3_key("")

    def test_rejects_slash(self, configure):
        from src import s3_session

        with pytest.raises(ValueError, match="Invalid token"):
            s3_session._s3_key("../etc/passwd")

    def test_rejects_backslash(self, configure):
        from src import s3_session

        with pytest.raises(ValueError, match="Invalid token"):
            s3_session._s3_key("foo\\bar")

    def test_rejects_null_byte(self, configure):
        from src import s3_session

        with pytest.raises(ValueError, match="Invalid token"):
            s3_session._s3_key("foo\x00bar")

    def test_rejects_too_long(self, configure):
        from src import s3_session

        with pytest.raises(ValueError, match="Invalid token"):
            s3_session._s3_key("a" * 257)


# --- download() ---


class TestDownload:
    @pytest.mark.asyncio
    async def test_roundtrip(self, configure, sample_session, tmp_path, mock_client):
        from src import s3_session

        data = sample_session.read_bytes()
        mock_client.get_object.return_value = {
            "Body": _MockStream(data),
            "ContentLength": len(data),
        }

        with patch.object(s3_session, "_get_client", return_value=mock_client):
            dest = tmp_path / "downloaded.session"
            await s3_session.download("test_token", dest)

        assert dest.exists()
        assert dest.read_bytes() == data

    @pytest.mark.asyncio
    async def test_not_found_client_error(self, configure, tmp_path, mock_client):
        """S3 raises ClientError(NoSuchKey), download converts to FileNotFoundError."""
        from src import s3_session
        from botocore.exceptions import ClientError

        error = ClientError(
            {"Error": {"Code": "NoSuchKey", "Message": "Not Found"}},
            "GetObject",
        )
        mock_client.get_object.side_effect = error

        with patch.object(s3_session, "_get_client", return_value=mock_client):
            dest = tmp_path / "missing.session"
            with pytest.raises(FileNotFoundError):
                await s3_session.download("nonexistent_token", dest)

    @pytest.mark.asyncio
    async def test_oversized(self, configure, tmp_path, mock_client):
        from src import s3_session

        mock_client.get_object.return_value = {
            "Body": _MockStream(b"x" * 100),
            "ContentLength": s3_session.MAX_DOWNLOAD_BYTES + 1,
        }

        with patch.object(s3_session, "_get_client", return_value=mock_client):
            dest = tmp_path / "big.session"
            with pytest.raises(ValueError, match="too large"):
                await s3_session.download("big", dest)

    @pytest.mark.asyncio
    async def test_empty_content_length_zero(self, configure, tmp_path, mock_client):
        from src import s3_session

        mock_client.get_object.return_value = {
            "Body": _MockStream(b""),
            "ContentLength": 0,
        }

        with patch.object(s3_session, "_get_client", return_value=mock_client):
            dest = tmp_path / "empty.session"
            with pytest.raises(FileNotFoundError, match="Empty"):
                await s3_session.download("empty", dest)

    @pytest.mark.asyncio
    async def test_post_read_size_check(self, configure, tmp_path, mock_client):
        """ContentLength can be None on some S3-compatible services; post-read check catches oversized."""
        from src import s3_session

        huge_data = b"x" * (s3_session.MAX_DOWNLOAD_BYTES + 1)
        mock_client.get_object.return_value = {
            "Body": _MockStream(huge_data),
            "ContentLength": None,  # S3-compatible services may omit this
        }

        with patch.object(s3_session, "_get_client", return_value=mock_client):
            dest = tmp_path / "huge.session"
            with pytest.raises(ValueError, match="too large"):
                await s3_session.download("huge", dest)

    @pytest.mark.asyncio
    async def test_atomic_write(self, configure, sample_session, tmp_path, mock_client):
        """Download writes to temp file then renames atomically."""
        from src import s3_session

        data = sample_session.read_bytes()
        mock_client.get_object.return_value = {
            "Body": _MockStream(data),
            "ContentLength": len(data),
        }

        with patch.object(s3_session, "_get_client", return_value=mock_client):
            dest = tmp_path / "atomic.session"
            await s3_session.download("atomic_token", dest)
            # Temp file should not exist after successful download
            tmp_file = dest.with_suffix(".session.tmp")
            assert not tmp_file.exists()
            assert dest.exists()

    @pytest.mark.asyncio
    async def test_timeout(self, configure, tmp_path, mock_client):
        """get_object timeout is handled by botocore config, not our code."""
        from src import s3_session

        mock_client.get_object.side_effect = asyncio.TimeoutError("read timeout")

        with patch.object(s3_session, "_get_client", return_value=mock_client):
            dest = tmp_path / "timeout.session"
            with pytest.raises(asyncio.TimeoutError):
                await s3_session.download("timeout_token", dest)


# --- upload() ---


class TestUpload:
    @pytest.mark.asyncio
    async def test_success(self, configure, sample_session, mock_client):
        from src import s3_session

        with patch.object(s3_session, "_get_client", return_value=mock_client):
            await s3_session.upload("upload_token", sample_session)

        mock_client.put_object.assert_called_once()
        call_kwargs = mock_client.put_object.call_args[1]
        assert call_kwargs["Bucket"] == TEST_BUCKET
        assert call_kwargs["Key"] == "upload_token.session"
        assert call_kwargs["Body"] == sample_session.read_bytes()

    @pytest.mark.asyncio
    async def test_size_limit(self, configure, tmp_path, mock_client):
        from src import s3_session

        large_file = tmp_path / "large.session"
        large_file.write_bytes(b"x" * (s3_session.MAX_UPLOAD_BYTES + 1))

        with patch.object(s3_session, "_get_client", return_value=mock_client):
            with pytest.raises(ValueError, match="too large"):
                await s3_session.upload("large_token", large_file)

    @pytest.mark.asyncio
    async def test_failure_raises(self, configure, sample_session, mock_client):
        from src import s3_session

        mock_client.put_object.side_effect = Exception("S3 error")

        with patch.object(s3_session, "_get_client", return_value=mock_client):
            with pytest.raises(Exception, match="S3 error"):
                await s3_session.upload("fail_token", sample_session)


# --- delete() ---


class TestDelete:
    @pytest.mark.asyncio
    async def test_success(self, configure, mock_client):
        from src import s3_session

        with patch.object(s3_session, "_get_client", return_value=mock_client):
            await s3_session.delete("del_token")

        mock_client.delete_object.assert_called_once_with(
            Bucket=TEST_BUCKET, Key="del_token.session"
        )

    @pytest.mark.asyncio
    async def test_failure_raises(self, configure, mock_client):
        from src import s3_session

        mock_client.delete_object.side_effect = Exception("S3 error")

        with patch.object(s3_session, "_get_client", return_value=mock_client):
            with pytest.raises(Exception, match="S3 error"):
                await s3_session.delete("fail_token")


# --- health_check() ---


class TestHealthCheck:
    @pytest.mark.asyncio
    async def test_success(self, configure, mock_client):
        from src import s3_session

        mock_client.head_bucket.return_value = {}
        mock_client.put_object.return_value = {}

        with patch.object(s3_session, "_get_client", return_value=mock_client):
            await s3_session.health_check()

        mock_client.head_bucket.assert_called_once_with(Bucket=TEST_BUCKET)
        mock_client.put_object.assert_called_once()

    @pytest.mark.asyncio
    async def test_403_raises(self, configure, mock_client):
        from src import s3_session
        from botocore.exceptions import ClientError

        error = ClientError(
            {"Error": {"Code": "403", "Message": "Forbidden"}},
            "HeadBucket",
        )
        mock_client.head_bucket.side_effect = error

        with patch.object(s3_session, "_get_client", return_value=mock_client):
            with pytest.raises(RuntimeError, match="access denied"):
                await s3_session.health_check()

    @pytest.mark.asyncio
    async def test_404_raises(self, configure, mock_client):
        from src import s3_session
        from botocore.exceptions import ClientError

        error = ClientError(
            {"Error": {"Code": "404", "Message": "Not Found"}},
            "HeadBucket",
        )
        mock_client.head_bucket.side_effect = error

        with patch.object(s3_session, "_get_client", return_value=mock_client):
            with pytest.raises(RuntimeError, match="not found"):
                await s3_session.health_check()

    @pytest.mark.asyncio
    async def test_write_failure_raises(self, configure, mock_client):
        from src import s3_session

        mock_client.head_bucket.return_value = {}
        mock_client.put_object.side_effect = Exception("AccessDenied")

        with patch.object(s3_session, "_get_client", return_value=mock_client):
            with pytest.raises(RuntimeError, match="not writable"):
                await s3_session.health_check()


# --- checkpoint_session() ---


class TestCheckpointSession:
    @pytest.mark.asyncio
    async def test_success(self, sample_wal_session):
        from src import s3_session

        result = await s3_session.checkpoint_session(sample_wal_session)
        assert isinstance(result, bool)

    @pytest.mark.asyncio
    async def test_nonexistent_returns_false(self, tmp_path):
        from src import s3_session

        # Use a directory path — SQLite can't open a directory as a database
        dir_path = tmp_path / "subdir"
        dir_path.mkdir()
        result = await s3_session.checkpoint_session(dir_path)
        assert result is False


# --- verify_session_integrity() ---


class TestVerifySessionIntegrity:
    @pytest.mark.asyncio
    async def test_ok(self, sample_session):
        from src import s3_session

        assert await s3_session.verify_session_integrity(sample_session) is True

    @pytest.mark.asyncio
    async def test_corrupt(self, tmp_path):
        from src import s3_session

        corrupt = tmp_path / "corrupt.session"
        corrupt.write_bytes(b"not a sqlite file")
        assert await s3_session.verify_session_integrity(corrupt) is False

    @pytest.mark.asyncio
    async def test_nonexistent(self, tmp_path):
        from src import s3_session

        # Use a directory path — SQLite can't open a directory as a database
        dir_path = tmp_path / "subdir"
        dir_path.mkdir()
        assert await s3_session.verify_session_integrity(dir_path) is False


# --- checkpoint_and_upload() ---


class TestCheckpointAndUpload:
    @pytest.mark.asyncio
    async def test_roundtrip(self, configure, sample_session, mock_client):
        from src import s3_session

        with patch.object(s3_session, "_get_client", return_value=mock_client):
            result = await s3_session.checkpoint_and_upload("cpu_token", sample_session)

        assert result is True
        mock_client.put_object.assert_called_once()

    @pytest.mark.asyncio
    async def test_missing_local_file(self, configure, tmp_path):
        from src import s3_session

        result = await s3_session.checkpoint_and_upload("nope", tmp_path / "nope.session")
        assert result is False

    @pytest.mark.asyncio
    async def test_corrupt_skips_upload(self, configure, tmp_path):
        from src import s3_session

        corrupt = tmp_path / "corrupt.session"
        corrupt.write_bytes(b"not sqlite")
        result = await s3_session.checkpoint_and_upload("bad", corrupt)
        assert result is False


# --- close_s3_client() ---


class TestCloseS3Client:
    @pytest.mark.asyncio
    async def test_close(self, configure, mock_client):
        from src import s3_session

        s3_session._client = mock_client
        await s3_session.close_s3_client()
        assert s3_session._client is None

    @pytest.mark.asyncio
    async def test_close_idempotent(self, configure):
        from src import s3_session

        # Close when no client exists — should not raise
        await s3_session.close_s3_client()
        await s3_session.close_s3_client()

    @pytest.mark.asyncio
    async def test_close_handles_error(self, configure, mock_client):
        from src import s3_session

        mock_client.__aexit__ = AsyncMock(side_effect=Exception("close error"))
        s3_session._client = mock_client
        # Should not raise
        await s3_session.close_s3_client()
        assert s3_session._client is None


# --- mark_shutting_down() ---


class TestShutdownBehavior:
    @pytest.mark.asyncio
    async def test_shutdown_rejects_new_client(self, configure):
        from src import s3_session

        s3_session.mark_shutting_down()
        with pytest.raises(RuntimeError, match="shutting down"):
            await s3_session._get_client()
        s3_session.reset_shutdown_state()

    @pytest.mark.asyncio
    async def test_shutdown_returns_cached_client(self, configure, mock_client):
        from src import s3_session

        s3_session._client = mock_client
        s3_session.mark_shutting_down()
        result = await s3_session._get_client()
        assert result is mock_client
        s3_session.reset_shutdown_state()

    @pytest.mark.asyncio
    async def test_reset_shutdown_state(self, configure, mock_client):
        from src import s3_session

        s3_session.mark_shutting_down()
        s3_session.reset_shutdown_state()
        assert s3_session._shutting_down is False


# --- Constants ---


class TestConstants:
    def test_max_download_bytes(self):
        from src import s3_session

        assert s3_session.MAX_DOWNLOAD_BYTES == 10 * 1024 * 1024

    def test_max_upload_bytes(self):
        from src import s3_session

        assert s3_session.MAX_UPLOAD_BYTES == 20 * 1024 * 1024


# --- Helpers ---


class _MockStream:
    """Mock async stream for S3 Body responses."""

    def __init__(self, data: bytes):
        self._data = data

    async def read(self):
        return self._data

    async def close(self):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

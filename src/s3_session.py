"""S3-backed session storage for ephemeral Docker deployments.

All S3 logic lives here — connection.py never touches aiobotocore directly.
No circuit breaker for v1 (connection.py already has one for Telegram).
No manual retry loops (botocore handles retries with max_attempts=3).
"""

import asyncio
import logging
import os
import re
import sqlite3
from pathlib import Path

import aiobotocore.session
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)


# --- S3 client lifecycle manager ---

class _S3ClientManager:
    """Encapsulates S3 client lifecycle: creation, caching, shutdown coordination.

    Replaces scattered globals (_client, _client_lock, _shutting_down, _bucket)
    with a single cohesive class.
    """

    def __init__(self) -> None:
        self._client = None
        self._lock = asyncio.Lock()
        self._bucket: str = ""
        self._shutting_down = False

    def configure(self, bucket: str) -> None:
        """Set the S3 bucket name. Called once at startup."""
        bucket = bucket.strip() if bucket else ""
        if not bucket:
            raise ValueError("AWS_S3_BUCKET is required when S3_SESSION_STORAGE=true")
        self._bucket = bucket

    def mark_shutting_down(self) -> None:
        """Called once during graceful shutdown. After this, get_client() only
        returns cached clients (needed for eviction uploads) — new client creation
        is blocked."""
        self._shutting_down = True

    def reset_shutdown_state(self) -> None:
        """Reset shutdown state — used in tests."""
        self._shutting_down = False

    def reset_for_testing(self) -> None:
        """Reset all state to factory defaults. Used in test fixtures."""
        self._client = None
        self._shutting_down = False
        self._bucket = ""

    async def get_client(self):
        """Return cached S3 client, or create new one.

        During shutdown (_shutting_down=True): returns cached client (needed for
        eviction uploads), but blocks creation of NEW clients.
        """
        async with self._lock:
            # Return cached client even during shutdown — eviction uploads need it
            if self._client is not None:
                return self._client
            # No cached client — block new creation during shutdown
            if self._shutting_down:
                raise RuntimeError(
                    "S3 client shutting down — cannot create new client"
                )
            session = aiobotocore.session.get_session()
            self._client = await session.create_client(
                "s3",
                config=BotoConfig(
                    read_timeout=5,
                    connect_timeout=3,
                    retries={"max_attempts": 3, "mode": "adaptive"},
                ),
            ).__aenter__()
            return self._client

    async def close(self) -> None:
        """Close S3 client. Used for both reset (stale TCP) and shutdown cleanup."""
        async with self._lock:
            old = self._client
            self._client = None  # Clear reference BEFORE __aexit__ — avoids stale ref on exception
            if old is not None:
                try:
                    await old.__aexit__(None, None, None)
                except Exception as e:
                    logger.warning(f"S3 client close error: {e}")


_manager = _S3ClientManager()

MAX_DOWNLOAD_BYTES = 10 * 1024 * 1024  # 10MB
MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # 20MB — reject corrupted runaway sessions
_S3_KEY_RE = re.compile(r'^[a-zA-Z0-9._-]{1,256}$')


# --- Module-level convenience functions (delegate to _manager) ---

def configure(bucket: str):
    """Set the S3 bucket name. Called once at startup."""
    _manager.configure(bucket)


def mark_shutting_down():
    """Called once during graceful shutdown."""
    _manager.mark_shutting_down()


def reset_shutdown_state():
    """Reset shutdown state — used in tests."""
    _manager.reset_shutdown_state()


async def _get_client():
    """Return cached S3 client, or create new one. Delegates to _manager."""
    return await _manager.get_client()


async def close_s3_client():
    """Close S3 client. Used for both reset (stale TCP) and shutdown cleanup."""
    await _manager.close()


def _s3_key(token: str) -> str:
    """Convert bearer token to S3 key: `{token}.session`."""
    if not token or not _S3_KEY_RE.match(token):
        raise ValueError(f"Invalid token for S3 key: {token[:8]}...")
    return f"{token}.session"


def _validate_download_sizes(
    content_length: int | None, data: bytes, token_prefix: str
) -> None:
    """Check download size against MAX_DOWNLOAD_BYTES.

    Validates both the ContentLength header (pre-read) and actual data length
    (post-read). ContentLength can be None on some S3-compatible services or
    spoofed on non-TLS connections, so both checks are needed.

    Raises:
        ValueError: if data exceeds MAX_DOWNLOAD_BYTES.
        FileNotFoundError: if data is empty.
    """
    if content_length is not None:
        if content_length > MAX_DOWNLOAD_BYTES:
            raise ValueError(
                f"S3 object too large ({content_length} bytes, max {MAX_DOWNLOAD_BYTES}) "
                f"for token {token_prefix}..."
            )
        if content_length == 0:
            raise FileNotFoundError(f"Empty session for token {token_prefix}...")
    if len(data) > MAX_DOWNLOAD_BYTES:
        raise ValueError(
            f"S3 object too large ({len(data)} bytes, max {MAX_DOWNLOAD_BYTES}) "
            f"for token {token_prefix}..."
        )
    if len(data) == 0:
        raise FileNotFoundError(f"Empty session for token {token_prefix}...")


async def download(token: str, dest: Path) -> None:
    """Download session file to dest. Raises FileNotFoundError if not found.

    Downloads to temp file then atomically renames to dest.
    Botocore handles retries (max_attempts=3, adaptive backoff).
    """
    client = await _get_client()
    tmp_path = dest.with_suffix(".session.tmp")
    try:
        resp = await client.get_object(Bucket=_manager._bucket, Key=_s3_key(token))
        content_length = resp.get("ContentLength")
        # Pre-read size check (ContentLength header)
        if content_length is not None and content_length > MAX_DOWNLOAD_BYTES:
            await resp["Body"].close()
            raise ValueError(
                f"S3 object too large ({content_length} bytes, max {MAX_DOWNLOAD_BYTES}) "
                f"for token {token[:8]}..."
            )
        if content_length is not None and content_length == 0:
            await resp["Body"].close()
            raise FileNotFoundError(f"Empty session for token {token[:8]}...")
        async with resp["Body"] as stream:
            data = await stream.read()
        # Post-read size check (actual data — ContentLength can be None/spoofed)
        _validate_download_sizes(content_length, data, token[:8])
        # Atomic write: temp file then rename
        try:
            await asyncio.to_thread(tmp_path.write_bytes, data)
        except OSError as e:
            raise OSError(
                f"Failed to write session file to disk for {token[:8]}... "
                f"(disk full?): {e}"
            ) from e
        await asyncio.to_thread(os.rename, str(tmp_path), str(dest))
    except ClientError as e:
        # S3 raises ClientError(NoSuchKey), not FileNotFoundError
        if e.response['Error']['Code'] == 'NoSuchKey':
            raise FileNotFoundError(f"Session {token[:8]}... not found in S3") from e
        raise
    except FileNotFoundError:
        raise
    except Exception as e:
        logger.warning(f"S3 download failed for {token[:8]}...: {e}")
        raise
    finally:
        # Clean up temp file on any failure (including CancelledError/BaseException).
        # On success, tmp_path was already renamed to dest — unlink is a no-op.
        tmp_path.unlink(missing_ok=True)


async def upload(token: str, src: Path) -> None:
    """Upload session file to S3. Raises on failure.

    Botocore handles retries (max_attempts=3, adaptive backoff).
    """
    client = await _get_client()
    try:
        data = await asyncio.to_thread(src.read_bytes)
        if len(data) > MAX_UPLOAD_BYTES:
            raise ValueError(
                f"Session file too large ({len(data)} bytes, max {MAX_UPLOAD_BYTES}) "
                f"for token {token[:8]}..."
            )
        await client.put_object(Bucket=_manager._bucket, Key=_s3_key(token), Body=data)
    except Exception as e:
        logger.warning(f"S3 upload failed for {token[:8]}...: {e}")
        raise


async def delete(token: str) -> None:
    """Delete session file from S3. Raises on failure (caller decides handling)."""
    client = await _get_client()
    try:
        await client.delete_object(Bucket=_manager._bucket, Key=_s3_key(token))
    except Exception as e:
        logger.warning(f"S3 delete failed for {token[:8]}...: {e}")
        raise


async def health_check() -> None:
    """Verify S3 connectivity, credentials, and read+write access. Raises on failure."""
    client = await _get_client()
    bucket = _manager._bucket
    # Verify read access
    try:
        await client.head_bucket(Bucket=bucket)
    except ClientError as e:
        code = e.response['Error']['Code']
        if code == '403':
            raise RuntimeError(f"S3 bucket '{bucket}' access denied — check credentials") from e
        if code == '404':
            raise RuntimeError(f"S3 bucket '{bucket}' not found") from e
        raise
    # Verify write access (small put, will be overwritten on next health check)
    try:
        await client.put_object(
            Bucket=bucket, Key=".health-check", Body=b"",
            Metadata={"purpose": "health"},
        )
    except Exception as e:
        raise RuntimeError(f"S3 bucket '{bucket}' readable but not writable: {e}") from e


# --- SQLite helpers (separate connection, not Telethon's _conn) ---

async def checkpoint_session(session_path: Path) -> bool:
    """Flush SQLite WAL using a separate connection.

    Telethon's _conn is not thread-safe with asyncio.to_thread and
    accessing private attributes is fragile across Telethon versions.

    Returns False if checkpoint fails (caller should skip S3 upload).
    """
    def _checkpoint():
        conn = sqlite3.connect(str(session_path), timeout=5)
        try:
            result = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            row = result.fetchone()
            if row is None:
                return False
            # row[0]: 0=success, 1=busy(write txn), 2=busy(WAL read)
            if row[0] != 0:
                logger.warning(f"Checkpoint busy for {session_path.name}: code={row[0]}")
            return row[0] == 0
        finally:
            conn.close()

    try:
        return await asyncio.to_thread(_checkpoint)
    except Exception as e:
        logger.warning(f"WAL checkpoint failed for {session_path.name}: {e}")
        return False


async def verify_session_integrity(session_path: Path) -> bool:
    """Check SQLite integrity. Runs in thread pool."""
    def _check():
        conn = sqlite3.connect(str(session_path), timeout=5)
        try:
            result = conn.execute("PRAGMA integrity_check")
            return result.fetchone()[0] == "ok"
        finally:
            conn.close()
    try:
        return await asyncio.to_thread(_check)
    except Exception:
        return False


async def checkpoint_and_upload(token: str, local_path: Path) -> bool:
    """Checkpoint WAL then upload to S3. Returns False if checkpoint fails (skip upload).

    Shared by _do_evict_io and /setup flow to avoid duplication.
    Verifies local file exists and passes integrity check before uploading.
    """
    if not local_path.exists():
        logger.error(f"Cannot checkpoint+upload: local file missing for {token[:8]}...")
        return False
    if not await verify_session_integrity(local_path):
        logger.error(f"Pre-upload integrity check failed for {token[:8]}..., skipping upload")
        return False
    if not await checkpoint_session(local_path):
        logger.warning(f"Checkpoint failed for {token[:8]}..., skipping S3 upload")
        return False
    await upload(token, local_path)
    return True

# S3 Session Storage — Implementation Design

**Status:** Proposed
**ADR:** [0009-s3-session-storage.md](../adr/0009-s3-session-storage.md)

This document covers implementation details only. For context, decisions, alternatives, platform setup, and security considerations, see the ADR.

---

## 1. Components

### 1.1 S3 Local Path Helper (`src/client/connection.py`)

No `CachedSession` dataclass. The existing `_session_cache: dict[str, tuple[TelegramClient, float]]` is unchanged — zero existing callers break. The local path for S3-downloaded files is derived from the token:

```python
def _s3_local_path(token: str) -> Path:
    """Local path for S3-downloaded session files. Only used when S3 mode is active."""
    return cfg().session_directory / f"{token}.session"
```

### 1.2 S3 Session Module (`src/s3_session.py`)

Single file. All S3 logic lives here — `connection.py` never touches `aiobotocore` directly. No circuit breaker for v1 (connection.py already has one for Telegram). No manual retry loops (botocore handles retries with `max_attempts=3`). No `list_stale()` for v1 (S3 `LastModified` ≠ session activity time).

```python
import asyncio
import logging
import os
import re
from pathlib import Path

import aiobotocore.session
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

# --- S3 client singleton (thread-safe) ---
_client = None
_client_lock = asyncio.Lock()
_bucket: str = ""
_shutting_down = False  # Set during graceful shutdown — reject new S3 operations

MAX_DOWNLOAD_BYTES = 10 * 1024 * 1024  # 10MB
MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # 20MB — reject corrupted runaway sessions
_S3_KEY_RE = re.compile(r'^[a-zA-Z0-9._-]{1,256}$')


def configure(bucket: str):
    global _bucket
    bucket = bucket.strip() if bucket else ""
    if not bucket:
        raise ValueError("AWS_S3_BUCKET is required when S3_SESSION_STORAGE=true")
    _bucket = bucket


def mark_shutting_down():
    """Called once during graceful shutdown. After this, _get_client() only
    returns cached clients (needed for eviction uploads) — new client creation
    is blocked."""
    global _shutting_down
    _shutting_down = True


async def _get_client():
    """Return cached S3 client, or create new one.

    During shutdown (_shutting_down=True): returns cached client (needed for
    eviction uploads), but blocks creation of NEW clients.
    """
    global _client
    async with _client_lock:
        # Return cached client even during shutdown — eviction uploads need it
        if _client is not None:
            return _client
        # No cached client — block new creation during shutdown
        if _shutting_down:
            raise RuntimeError(
                "S3 client shutting down — cannot create new client"
            )
        session = aiobotocore.session.get_session()
        _client = await session.create_client(
            "s3",
            config=BotoConfig(
                read_timeout=5,
                connect_timeout=3,
                retries={"max_attempts": 3, "mode": "adaptive"},
            ),
        ).__aenter__()
        return _client


async def close_s3_client():
    """Close S3 client. Used for both reset (stale TCP) and shutdown cleanup."""
    global _client
    async with _client_lock:
        old = _client
        _client = None  # Clear reference BEFORE __aexit__ — avoids stale ref on exception
        if old is not None:
            try:
                await old.__aexit__(None, None, None)
            except Exception as e:
                logger.warning(f"S3 client close error: {e}")


def _s3_key(token: str) -> str:
    """Convert bearer token to S3 key: `{token}.session`."""
    if not token or not _S3_KEY_RE.match(token):
        raise ValueError(f"Invalid token for S3 key: {token[:8]}...")
    return f"{token}.session"


async def download(token: str, dest: Path) -> None:
    """Download session file to dest. Raises FileNotFoundError if not found.

    Downloads to temp file then atomically renames to dest.
    Botocore handles retries (max_attempts=3, adaptive backoff).
    """
    client = await _get_client()
    tmp_path = dest.with_suffix(".session.tmp")
    try:
        resp = await client.get_object(Bucket=_bucket, Key=_s3_key(token))
        # Check size before reading — ContentLength can be None on some
        # S3-compatible services
        content_length = resp.get("ContentLength") or 0
        if content_length > MAX_DOWNLOAD_BYTES:
            await resp["Body"].close()
            raise ValueError(
                f"S3 object too large ({content_length} bytes, max {MAX_DOWNLOAD_BYTES}) "
                f"for token {token[:8]}..."
            )
        if content_length == 0:
            await resp["Body"].close()
            raise FileNotFoundError(f"Empty session for token {token[:8]}...")
        async with resp["Body"] as stream:
            data = await stream.read()
        # Post-read size check — ContentLength can be None on some S3-compatible
        # services, and can be spoofed on non-TLS connections
        if len(data) > MAX_DOWNLOAD_BYTES:
            raise ValueError(
                f"S3 object too large ({len(data)} bytes, max {MAX_DOWNLOAD_BYTES}) "
                f"for token {token[:8]}..."
            )
        if len(data) == 0:
            raise FileNotFoundError(f"Empty session for token {token[:8]}...")
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
        await client.put_object(Bucket=_bucket, Key=_s3_key(token), Body=data)
    except Exception as e:
        logger.warning(f"S3 upload failed for {token[:8]}...: {e}")
        raise


async def delete(token: str) -> None:
    """Delete session file from S3. Raises on failure (caller decides handling)."""
    client = await _get_client()
    try:
        await client.delete_object(Bucket=_bucket, Key=_s3_key(token))
    except Exception as e:
        logger.warning(f"S3 delete failed for {token[:8]}...: {e}")
        raise


async def health_check() -> None:
    """Verify S3 connectivity, credentials, and read+write access. Raises on failure."""
    client = await _get_client()
    # Verify read access
    try:
        await client.head_bucket(Bucket=_bucket)
    except ClientError as e:
        code = e.response['Error']['Code']
        if code == '403':
            raise RuntimeError(f"S3 bucket '{_bucket}' access denied — check credentials") from e
        if code == '404':
            raise RuntimeError(f"S3 bucket '{_bucket}' not found") from e
        raise
    # Verify write access (small put, will be overwritten on next health check)
    try:
        await client.put_object(
            Bucket=_bucket, Key=".health-check", Body=b"",
            Metadata={"purpose": "health"},
        )
    except Exception as e:
        raise RuntimeError(f"S3 bucket '{_bucket}' readable but not writable: {e}") from e
```

### 1.3 SQLite Helpers (`src/s3_session.py` — same file)

Uses a separate SQLite connection (not Telethon's `_conn`) to avoid thread-safety issues. Validates checkpoint result:

```python
import sqlite3

async def checkpoint_session(session_path: Path) -> bool:
    """Flush SQLite WAL using a separate connection.

    Telethon's `_conn` is not thread-safe with `asyncio.to_thread` and
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


async def _checkpoint_and_upload(token: str, local_path: Path) -> bool:
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
```

### 1.4 Config Changes (`src/config/server_config.py`)

New field:

```python
s3_session_storage: bool = Field(
    default=False,
    alias="S3_SESSION_STORAGE",
    description="Enable S3-backed session storage.",
)
```

Standard AWS env vars (read by boto3, not by our code): `AWS_S3_BUCKET`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_REGION`, `AWS_ENDPOINT_URL`.

---

## 2. Connection Lifecycle Changes (`src/client/connection.py`)

### New declarations

```python
import s3_session

# Global download lock — serializes S3 downloads. Only held for S3 download (~200ms),
# NOT for Telegram I/O (2-5s). Acceptable for v1.
# Lock ordering: _download_lock → _cache_lock. Never acquire _download_lock
# while holding _cache_lock.
_download_lock = asyncio.Lock()

def is_s3_enabled() -> bool:
    return cfg().s3_session_storage
```

### Eviction helpers (avoid deadlock)

`asyncio.Lock` is NOT reentrant. LRU eviction is triggered while holding `_cache_lock` — it must NOT call any function that re-acquires `_cache_lock`. Split into pop-under-lock and I/O-outside-lock:

```python
async def _do_evict_io(token: str, client: TelegramClient) -> None:
    """Checkpoint, disconnect, upload. NO lock held. Always disconnects in finally."""
    local_path = _s3_local_path(token)
    s3_mode = is_s3_enabled()

    try:
        # Checkpoint BEFORE disconnect — Telethon's connection still open
        if s3_mode:
            if not await s3_session._checkpoint_and_upload(token, local_path):
                return  # Checkpoint/integrity failed, skip upload, but finally still disconnects
    finally:
        # Always disconnect — even if checkpoint/upload failed
        try:
            if client.is_connected():
                await client.disconnect()
        except Exception as e:
            logger.warning(f"Disconnect failed for {token[:8]}...: {e}")
    # Delete local file ONLY after successful checkpoint+upload (outside try/finally)
    if s3_mode and local_path.exists():
        local_path.unlink(missing_ok=True)
```

### Touchpoint 1: Cache miss → S3 download

```python
async def _get_client_by_token(token: str) -> TelegramClient:
    # Check cache (under lock)
    async with _cache_lock:
        current_time = time.time()
        if token in _session_cache:
            client, _ = _session_cache[token]
            _session_cache[token] = (client, current_time)
            return client

    # Cache miss — release _cache_lock before client building (network I/O)
    evicted = None

    if is_s3_enabled():
        # Acquire _download_lock ONLY for S3 download (~200ms), NOT Telegram I/O.
        # This prevents duplicate downloads for the same token while allowing
        # parallel client building for different tokens.
        async with _download_lock:
            # Double-check cache after acquiring download lock
            async with _cache_lock:
                if token in _session_cache:
                    client, _ = _session_cache[token]
                    _session_cache[token] = (client, time.time())
                    return client
            # Download from S3
            local_path = _s3_local_path(token)
            local_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                await s3_session.download(token, local_path)
            except FileNotFoundError:
                raise SessionNotAuthorizedError(token)
            except Exception as e:
                # S3 unavailable — fall back to local file if it exists
                logger.warning(f"S3 download failed for {token[:8]}..., trying local fallback: {e}")
                if not local_path.exists():
                    raise SessionNotAuthorizedError(token) from e
                # Local file exists from a previous session — verify it
                if not await s3_session.verify_session_integrity(local_path):
                    local_path.unlink(missing_ok=True)
                    raise SessionNotAuthorizedError(token) from e
                logger.info(f"Using local fallback for {token[:8]}...")
            else:
                if not await s3_session.verify_session_integrity(local_path):
                    local_path.unlink(missing_ok=True)
                    raise TelegramTransportError("Downloaded session file is corrupt")
    else:
        local_path = _resolve_session_path_for_token(token)

    # Build client (NO lock — Telegram I/O, 2-5s)
    client = await _build_telegram_client_for_token(local_path, token)

    # LRU eviction + cache insert (single _cache_lock acquisition)
    async with _cache_lock:
        max_active = cfg().max_active_sessions
        if len(_session_cache) >= max_active:
            oldest_token = min(_session_cache, key=lambda k: _session_cache[k][1])
            evicted = (oldest_token, _session_cache.pop(oldest_token, None))
            if evicted[1]:
                logger.info(f"Evicting LRU session: {oldest_token[:8]}...")
        _session_cache[token] = (client, time.time())

    # Eviction I/O outside lock — catch exceptions so the requesting caller
    # doesn't see an unrelated S3 error for a different session
    if evicted and evicted[1]:
        evict_token, (evict_client, _) = evicted
        try:
            await _do_evict_io(evict_token, evict_client)
        except Exception as e:
            logger.warning(f"LRU eviction I/O failed for {evict_token[:8]}...: {e}")

    return client
```

### Touchpoint 2: All eviction paths use pop→release→I/O

```python
async def _evict_session(token: str) -> None:
    """Pop from cache, checkpoint, disconnect, upload to S3, clean up."""
    async with _cache_lock:
        cached = _session_cache.pop(token, None)
    if cached is None:
        return
    client, _ = cached
    await _do_evict_io(token, client)
```

| Existing function | Change |
|---|---|
| `_evict_lru_if_session_cache_full()` | **Removed** — LRU eviction inline in `_get_client_by_token` (see Touchpoint 1) |
| `cleanup_idle_sessions()` | Snapshot idle tokens under lock → pop them all → release lock → `_do_evict_io()` each |
| `cleanup_session_cache()` | Snapshot all entries + clear under one `_cache_lock`, then `_do_evict_io()` each |
| `disconnect_and_evict_session()` | Replaced by `_evict_session()` |
| `ensure_connection` fatal-error path | Call `_evict_session(token)` instead of bare `pop()` |

### Touchpoint 3: `ensure_connection` → skip `.touch()` in S3 mode

Keep existing signature `ensure_connection(client, token) -> bool`. Only change the `.touch()` behavior:

```python
# In ensure_connection(), after successful reconnect:
# Before:
    with contextlib.suppress(InvalidSessionTokenError, OSError):
        _resolve_session_path_for_token(token).with_suffix(".session").touch(exist_ok=True)

# After:
    if not is_s3_enabled():
        with contextlib.suppress(InvalidSessionTokenError, OSError):
            _resolve_session_path_for_token(token).with_suffix(".session").touch(exist_ok=True)
    # In S3 mode, last_access is updated on cache hit (Touchpoint 1)
```

### Known Limitations (pre-existing, not S3-specific)

| Issue | Impact | Fix |
|-------|--------|-----|
| **`_session_file_exists()` returns False after S3 eviction** | Reauthorization flow breaks — checks local file existence | Add S3-aware check or document that reauthorization in S3 mode requires `/setup` |
| **Reauthorization flow copies from disk** | No local file after S3 eviction, so reauthorize fails | Acceptable for v1 — user re-does `/setup` |
| **`disable_auth` + S3 mode** | Path divergence: `_resolve_session_path_for_token` uses default path in disable_auth mode, but S3 uses token-based paths | Validate config at startup: `disable_auth + S3_SESSION_STORAGE` → log warning |
| **`cleanup_idle_sessions` holds lock during I/O** (pre-existing) | Blocks all cache operations during disconnect | Fix: snapshot idle tokens under lock → pop → release → `_do_evict_io()` each |
| **Duplicate client building race** (S3 mode) | If two requests for the same token arrive during the window between `_download_lock` release and `_session_cache` insert, both download and build — first client becomes orphaned | Add to cache inside `_download_lock` (requires moving cache insert before client build), or add post-build cache check. Deferred — orphaned client is garbage-collected, not a leak |
| **Shutdown S3 client leak** | After `close_s3_client()`, a concurrent `_get_client()` call could create a new client that never gets closed | Fixed — `mark_shutting_down()` blocks new client creation while still returning cached client for eviction uploads |
| **SIGKILL between eviction pop and S3 upload** | Process killed after `_session_cache.pop()` but before S3 upload — session data lost. No cleanup code runs on SIGKILL | Acceptable for v1 — session recreated via `/setup`. Auth key stable, entity cache rebuilds. Alternatives (dirty markers, local backup) add complexity without guaranteeing no data loss |
| **Disk-full during S3 download** | Raw `OSError` when disk is full during `write_bytes`. Error message may not clearly indicate disk full | Fixed — wrapped with explicit message: "Failed to write session file to disk (disk full?)" |
| **Runtime S3 permission changes** | If S3 permissions are revoked after startup (e.g. IAM policy change), every request fails with no recovery | Acceptable for v1 — operator must fix IAM policy. Future: add circuit breaker or periodic recheck |
| **Bearer token as S3 key exposed in logs** | Token appears in S3 access logs, CloudTrail, and bucket listing | Acceptable for v1 — buckets are private, logs are access-controlled. Future: hash-based keys |
| **S3 eventual consistency on /setup** | On MinIO/R2, read-after-write may return stale data. Container restart immediately after /setup may not find session | Acceptable for v1 — rare edge case, retry on next request |
| **TRUNCATE checkpoint blocks on active Telethon writes** | If Telethon has an open write transaction, `TRUNCATE` blocks until it completes. Checkpoint may report `busy` | Acceptable for v1 — checkpoint runs after disconnect (no active writes). `PASSIVE` mode doesn't truncate WAL |
| **`_connection_failures` dict grows unboundedly** | Pre-existing in current code — error counts never cleared | Deferred — separate PR |
| **`health.py` iterates `_session_cache` without lock** | Pre-existing — can see mid-eviction state | Deferred — separate PR, wrap in `async with _cache_lock` or snapshot |
| **S3 client leaked on startup failure** | If `health_check()` fails after `_get_client()` created a client, that client is never closed | Acceptable for v1 — `SystemExit(1)` follows, process dies |
| **`setup_delete` checkpoint failure → data loss** | If `_checkpoint_and_upload` returns False (checkpoint fails), `_evict_session` returns early without uploading. S3 delete + local unlink then removes both copies | Acceptable for v1 — checkpoint failure is rare (file locked by another process). Log warning added |

All of these are pre-existing bugs in the current code. They are documented here for awareness but will be fixed in separate PRs.

---

## 3. `/setup` Flow Changes (`src/server_components/web_setup.py`)

**After session persist — upload to S3:**

```python
async def _persist_session_and_generate_config(...):
    # ... existing code ...

    if is_s3_enabled():
        try:
            if not await s3_session._checkpoint_and_upload(token, session_path):
                raise TelegramTransportError(
                    "Session created but WAL checkpoint failed. "
                    "Session will work now but won't survive container restart."
                )
        except TelegramTransportError:
            raise
        except Exception as e:
            # On ephemeral platforms, failing to upload means session is lost on restart
            logger.error(f"S3 upload after /setup failed: {e}")
            # Surface error to user — session is usable now but won't survive restart
            raise TelegramTransportError(
                f"Session created but S3 backup failed: {e}. "
                "Session will work now but won't survive container restart."
            )

    # ... show bearer token ...
```

**`setup_delete` — evict then delete:**

```python
async def setup_delete(token: str) -> None:
    # Evict from cache (checkpoint + disconnect + upload to preserve state)
    await _evict_session(token)
    # Delete from S3 (raises on failure — caller surfaces error to user)
    if is_s3_enabled():
        await s3_session.delete(token)
    # Delete local file
    session_path = _resolve_session_path_for_token(token)
    session_path.unlink(missing_ok=True)
```

No `_pending_deletions` — synchronous delete is sufficient.

If `s3_session.delete()` fails, the exception propagates to the `/setup` handler, which surfaces the error to the user. The session remains in S3. On retry, `_evict_session` returns early (already evicted), then `s3_session.delete()` retries.

---

## 4. Server Changes (`src/server.py`)

**Startup health check + configure + orphan cleanup:**

```python
async def _startup_s3_check() -> None:
    if not is_s3_enabled():
        return
    try:
        import aiobotocore  # noqa: F401
    except ImportError:
        logger.critical("S3_SESSION_STORAGE=true but aiobotocore not installed. "
                        "Install with: pip install fast-mcp-telegram[s3]")
        raise SystemExit(1)

    # Validate required env vars
    bucket = os.environ.get("AWS_S3_BUCKET", "")
    if not bucket:
        logger.critical("S3_SESSION_STORAGE=true but AWS_S3_BUCKET is not set")
        raise SystemExit(1)

    # Fail-fast: disable_auth + S3 is not supported (path divergence)
    if cfg().disable_auth:
        logger.critical("S3_SESSION_STORAGE=true with disable_auth=true is not supported")
        raise SystemExit(1)

    # Configure s3_session module
    s3_session.configure(bucket)

    try:
        await s3_session.health_check()
        logger.info("S3 session storage: connected")
    except Exception as e:
        logger.critical(f"S3 health check failed: {e}")
        raise SystemExit(1)


# Also called unconditionally at startup (not just in S3 mode):
async def _cleanup_orphaned_tmp_files() -> None:
    """Remove orphaned .session.tmp files from previous crashes. Always runs."""
    session_dir = cfg().session_directory
    if session_dir.exists():
        for tmp_file in session_dir.glob("*.session.tmp"):
            logger.info(f"Removing orphaned temp file: {tmp_file.name}")
            tmp_file.unlink(missing_ok=True)
```

**Graceful shutdown (with dynamic timeout):**

```python
async def _graceful_shutdown() -> None:
    if not is_s3_enabled():
        return
    logger.info("Shutdown: uploading cached sessions to S3...")
    # Reject new S3 operations — prevents race where a new _get_client()
    # creates a client after close_s3_client() clears the reference
    s3_session.mark_shutting_down()
    tokens = list(_session_cache.keys())
    if not tokens:
        await s3_session.close_s3_client()
        return

    # Dynamic timeout: 10s per session, min 25s, max 120s
    # Each session: checkpoint ~1s + disconnect ~1s + upload ~2s + margin
    timeout = min(max(len(tokens) * 10, 25), 120)

    try:
        results = await asyncio.wait_for(
            asyncio.gather(*[_evict_session(t) for t in tokens], return_exceptions=True),
            timeout=timeout,
        )
        failures = sum(1 for r in results if isinstance(r, Exception))
        if failures:
            logger.warning(f"{failures}/{len(tokens)} sessions failed to upload during shutdown")
    except asyncio.TimeoutError:
        logger.warning(f"Shutdown: S3 upload timed out after {timeout}s. Some sessions may not be saved.")

    # Clean up S3 client after all evictions (with its own timeout)
    try:
        await asyncio.wait_for(s3_session.close_s3_client(), timeout=5)
    except asyncio.TimeoutError:
        logger.warning("Shutdown: S3 client close timed out")
```

**Inactive session cleanup (S3 mode skipped for v1):**

```python
async def _cleanup_inactive_sessions() -> None:
    if is_s3_enabled():
        # S3 LastModified ≠ session activity time. Skip for v1.
        # S3 lifecycle policies can expire objects later.
        return
    # Existing local file cleanup (unchanged)
    ...
```

---

## 5. `health.py` Changes

**None required.** The existing `health.py` unpacks `_session_cache` as `(client, last_access)` tuple. Since we keep the existing tuple type (no `CachedSession` dataclass), `health.py` is unchanged.

**Known issue (pre-existing):** `health.py` iterates `_session_cache` without holding `_cache_lock`. This can see mid-eviction state. Fix: wrap the iteration in `async with _cache_lock:` or take a snapshot via `list(_session_cache.items())`. Deferred to a separate PR — pre-existing bug unrelated to S3.

---

## 6. File Changes

### New files:

| File | Purpose |
|------|---------|
| `src/s3_session.py` | All S3 logic: download (atomic), upload (size-limited), delete, health_check (read+write verify), close_s3_client (consolidated reset+close), shutdown flag, checkpoint, integrity verify, `_checkpoint_and_upload` helper, S3 client singleton |

### Modified files:

| File | Change |
|------|--------|
| `src/config/server_config.py` | Add `s3_session_storage` field |
| `src/client/connection.py` | `_s3_local_path()` helper, `_download_lock` global, `is_s3_enabled()`, `_do_evict_io()`, `_evict_session()`. 3 touchpoints: cache miss download, eviction refactor, skip `.touch()` in S3 mode. `_evict_lru_if_session_cache_full()` removed (inline). `disconnect_and_evict_session()` replaced by `_evict_session()`. |
| `src/server_components/web_setup.py` | S3 upload after `/setup`, evict+delete in `setup_delete` |
| `src/server.py` | Startup health check, graceful shutdown with timeout + S3 client close, skip inactive cleanup in S3 mode |

### Unchanged files:

`health.py`, `auth.py`, `auth_middleware.py`, `server_modes.py`, `conftest.py`, `src/tools/` — all unchanged.

---

## 7. Data Flows

### 7.1 First-Time Auth

```
/setup → QR/phone → Telethon session → checkpoint → bearer token → S3 PUT {token}.session → show token
```

### 7.2 Tool Call (existing session)

```
MCP request → cache hit → return client
            → cache miss → download lock → S3 GET (size check) → atomic write → integrity check → release lock → TelegramClient → cache
            → S3 GET fails (non-404) → local fallback → TelegramClient → cache (or SessionNotAuthorizedError)
```

### 7.3 Cache Eviction

```
_pop from _session_cache  ← under _cache_lock
(lock released)
_do_evict_io(token, client) ← _checkpoint_and_upload (integrity check → checkpoint → S3 PUT) → disconnect → delete local
On checkpoint/upload/integrity failure: keep local file, log warning (don't re-add disconnected client)
```

Triggered by: LRU, idle timeout, shutdown, `setup_delete`.

### 7.4 Container Restart

```
Start → S3 health check → MCP request → cache miss → S3 GET → integrity check → TelegramClient → connect → works
```

### 7.5 Graceful Shutdown

```
SIGTERM → mark_shutting_down() → evict all sessions (dynamic timeout: min 25s, max 120s) → close_s3_client → exit
```

---

## 8. Testing

### Unit tests (with `moto`):

| Test | Covers |
|------|--------|
| `test_s3_download_upload_roundtrip` | Upload → download → content matches |
| `test_s3_download_not_found` | `FileNotFoundError` for missing token |
| `test_s3_download_oversized` | Object >10MB rejected |
| `test_s3_download_empty` | ContentLength=0 rejected |
| `test_s3_download_atomic` | Crash mid-download leaves no corrupt file |
| `test_s3_checkpoint_before_upload` | WAL flushed using separate connection (no Telethon `_conn`) |
| `test_s3_checkpoint_failure_skips_upload` | No upload if checkpoint fails, local file kept |
| `test_s3_checkpoint_separate_conn_timeout` | Separate connection with timeout=5 |
| `test_s3_checkpoint_validates_result` | PRAGMA result checked, failure logged |
| `test_s3_integrity_check_after_download` | Corrupt download detected, local file deleted |
| `test_s3_upload_failure_keeps_local` | PUT fails → local file preserved |
| `test_s3_download_timeout` | Hung S3 → timeout after 5s |
| `test_s3_close_client` | close_s3_client releases connections |
| `test_s3_async_file_io` | write_bytes/read_bytes wrapped in asyncio.to_thread |

### Integration tests:

| Test | Covers |
|------|--------|
| `test_cache_miss_downloads_from_s3` | First request downloads from S3 |
| `test_cache_eviction_uploads_to_s3` | LRU eviction → checkpoint → S3 upload |
| `test_idle_eviction_uploads_to_s3` | Idle timeout → checkpoint → S3 upload |
| `test_shutdown_uploads_all` | SIGTERM → all sessions uploaded within timeout |
| `test_shutdown_timeout` | S3 slow → 25s timeout fires, partial upload logged |
| `test_web_setup_uploads_to_s3` | /setup → S3 upload |
| `test_setup_delete_evicts_then_deletes` | /setup delete → evict (checkpoint+upload) → S3 delete |
| `test_startup_health_check_fails` | S3 unreachable → server exits |
| `test_startup_missing_aiobotocore` | No aiobotocore → clear error |
| `test_concurrent_requests_same_token` | Two requests → one S3 download (global lock serializes) |
| `test_concurrent_same_token_race_after_lock` | Two requests same token during lock-release→cache-insert window → no crash, both work (known orphan) |
| `test_concurrent_requests_different_tokens` | Two tokens → sequential downloads (global lock) |
| `test_ensure_connection_no_touch_s3` | S3 mode → no empty file created |
| `test_lock_discipline_no_block` | Eviction I/O doesn't block cache ops |
| `test_no_deadlock_lru_eviction` | LRU eviction under _cache_lock doesn't deadlock |
| `test_no_deadlock_client_build` | _get_client_by_token doesn't hold lock during network I/O |
| `test_fatal_error_evicts_s3` | Fatal session error → evict with S3 upload |
| `test_cleanup_session_cache_uploads` | Shutdown cleanup uploads all sessions |
| `test_cleanup_no_dict_mutation` | Iteration over keys copy, no RuntimeError |
| `test_idle_cleanup_no_lock_held` | Disconnect I/O outside _cache_lock |
| `test_local_file_kept_on_s3_failure` | S3 down → local file preserved |
| `test_s3_miss_falls_back_to_error` | S3 404 → SessionNotAuthorizedError |
| `test_s3_singleton_no_race` | Two coroutines creating S3 client → only one client created |
| `test_s3_singleton_health_recovery` | `close_s3_client()` resets stale client, next call creates fresh |
| `test_orphan_temp_cleanup` | Startup removes `*.session.tmp` files from previous crashes |
| `test_s3_configure_validation` | `configure("")` and `configure("  ")` raise ValueError |
| `test_s3_delete_raises_on_failure` | S3 delete raises on error (not silently swallowed) |
| `test_shutdown_rejects_new_s3_ops` | `mark_shutting_down()` → new `_get_client()` raises RuntimeError (no cached client) |
| `test_eviction_failure_no_readd` | S3 upload failure → disconnected client NOT re-added to cache |
| `test_no_deadlock_cleanup_idle` | Idle cleanup pops tokens under lock, releases, then does I/O |
| `test_s3_fallback_to_local` | S3 download fails (non-404) + local file exists → uses local file |
| `test_upload_size_limit` | Session file >20MB rejected before PUT |
| `test_s3_key_rejects_bad_tokens` | `_s3_key` rejects tokens with `/`, `..`, null bytes, backslashes |

---

## 9. Dependencies

```toml
[project.optional-dependencies]
s3 = ["aiobotocore>=2.15"]
```

When `S3_SESSION_STORAGE=false` (default), `aiobotocore` is not imported.

---

## 10. Implementation Order

| Step | Task | Est. |
|------|------|------|
| 1 | `src/s3_session.py` — all S3 logic, client singleton, close, checkpoint (separate conn), integrity check, atomic download, `_checkpoint_and_upload` helper, shutdown flag | 2h |
| 2 | `server_config.py` — add `s3_session_storage` field | 15m |
| 3 | `connection.py` — `_s3_local_path()`, `_download_lock` global, `is_s3_enabled()`, `_do_evict_io()`, `_evict_session()`, 3 touchpoints, S3 fallback, fix `_get_client_by_token` deadlock, fix `cleanup_idle_sessions`, fix `cleanup_session_cache`, remove `_evict_lru_if_session_cache_full`, remove `disconnect_and_evict_session` | 3h |
| 4 | `web_setup.py` — S3 upload after /setup, evict+delete in setup_delete | 45m |
| 5 | `server.py` — startup check, shutdown with timeout + S3 client close, skip inactive cleanup | 1.5h |
| 6 | Tests | 3.5h |
| 7 | Smithery config schema | 30m |

**Total: ~12h**

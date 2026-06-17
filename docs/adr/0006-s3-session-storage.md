# ADR 0006: S3 Session Storage for Ephemeral Deployments

## Status

Proposed

## Date

2026-06-17

## Context

fast-mcp-telegram currently stores Telethon sessions as SQLite `.session` files on local disk at `~/.config/fast-mcp-telegram/{token}.session`. Each file contains the MTProto auth key, DC connection info, entity cache (username→ID mappings, access hashes), update state (pts/qts/date), and file upload cache. Sessions are persisted via Docker volumes in self-hosted deployments.

This works for the 96% of users running local stdio mode, but breaks for ephemeral hosted deployments (Smithery Hosted, Fly.io, Railway) where containers have no persistent storage. Session files are lost on every container restart, forcing re-authentication and losing the entity cache.

### Problem

1. **Ephemeral containers lose session state** — no Docker volumes. Every restart = full re-auth via QR/phone.
2. **Entity cache is lost** — losing it means every entity resolution requires a fresh Telegram API call. Slow and rate-limit-prone for user sessions with many contacts.
3. **Update state is lost** — pts/qts/date tracking for catching up on missed messages is gone after restart.
4. **MCP registries require hosted deployments** — Smithery Hosted, Glama, and other registries expect containers that can be replaced without data loss.

### Why not StringSession?

Telethon's `StringSession` only stores: DC ID, server IP, port, and auth key (256 bytes). It does NOT store entity cache, update state, or file upload cache — these are in-memory only. Every restart with StringSession loses them. Additionally, the auth key can change (DC migration), making a static env var unreliable.

### Why not self-contained bearer tokens?

Encrypting session data into the token was considered. Token grows from ~44 chars to ~700 chars, conflates identity credential with session data, and makes rotation complex.

### Why S3 over Redis/PostgreSQL?

Session files are touched **twice per connection** — download on connect, upload on disconnect. S3's ~200ms latency is acceptable. Redis in RAM is wasteful for ~50KB blobs. PostgreSQL requires schema/ORM for opaque blobs. S3 is the natural tool for "store files externally" — every cloud platform offers it.

### Telemetry Findings

Telemetry from 31 active instances (25 stdio, 6 http-auth):

| Feature | stdio (25) | http-auth (6) |
|---------|-----------|---------------|
| `PREFIX_MCP_TOOLS_WITH_ACCOUNT` | 0 | 2 |
| `bot_api_token` | 0 | 0 |

Multi-account is exclusively http-auth. Bot token usage is zero in telemetry, but Telethon creates `.session` files for bot tokens too. Both use the same S3 storage — no exceptions.

### Reviewer Round 5 Findings

| # | Finding | Severity | Resolution |
|---|---------|----------|------------|
| 1 | **LRU eviction doesn't upload to S3 + leaks connected client** — pops from cache but never disconnects or uploads | **Critical** | All eviction paths go through `_do_evict_io()` (disconnect + checkpoint + upload). LRU eviction returns `(token, client)` for caller to evict after releasing lock. |
| 2 | **Eviction failure re-adds disconnected client** — `_do_evict_io` re-adds after `client.disconnect()` | **Critical** | On failure, keep local file. Don't re-add disconnected client — avoids infinite eviction loop. Next request creates fresh client from local file. |
| 3 | **`setup_delete` uploads to S3 then immediately deletes** — wasteful | **Critical** | `setup_delete` calls `_evict_session()` (which does checkpoint+upload), then `s3_session.delete()`, then local unlink. Upload-then-delete is intentional: ensures Telethon state is flushed to disk before S3 delete. |
| 4 | **`_do_evict_io` disconnects before checkpoint** — `client.disconnect()` closes `_conn`, then checkpoint fails | **Critical** | Checkpoint BEFORE disconnect. Telethon's connection still open during checkpoint. |
| 5 | **Pre-existing deadlock in `_get_client_by_token`** — holds `_cache_lock` during `_build_telegram_client_for_token` (network I/O) | **Critical** | Release `_cache_lock` before client building. Re-acquire to add to cache. Double-check cache after re-acquire. |
| 6 | **Bearer token as S3 key — no sanitization** | ~~Critical~~ | **Skipped** — tokens don't need sanitization. |
| 7 | **`cleanup_session_cache` at shutdown does NOT upload** — does `clear()` at end | **Critical** | `cleanup_session_cache` iterates over `list(_session_cache)`, calls `_evict_session()` each (which does checkpoint+upload). |
| 8 | **`_download_lock` is global** — serializes all downloads across tokens | High | Global `_download_lock` — only ~50ms each, acceptable for v1. Per-token locks deferred to avoid unbounded dict growth. |
| 9 | **Drop `CachedSession` dataclass** — `local_path` always derivable from token | High | Keep existing `(client, last_access)` tuple. Add `_s3_local_path(token)` helper. Zero existing callers change. |
| 10 | **60% content overlap between ADR and design doc** | High | Code stripped from ADR — ADR has decisions + rationale only, design doc has all code. |
| 11 | **54 accumulated reviewer findings bloat the ADR** | High | Move older findings to appendix. Keep Round 5 in main body. |
| 12 | **`cleanup_idle_sessions` holds lock during I/O** | High | Pop idle tokens under lock, release lock, then disconnect each outside lock. |
| 13 | **Fatal-error path pops from cache without S3 upload** | High | Fatal-error path calls `_evict_session()` instead of bare `pop()`. |
| 14 | **`health.py` unpacks `_session_cache` as tuple** | High | **No migration needed** — keep tuple. `health.py` unchanged. |
| 15 | **Dict mutation during iteration** in `cleanup_session_cache` | High | Iterate over `list(_session_cache.keys())` instead of `_session_cache.items()`. |
| 16 | **Download not atomic** — crash mid-write = corrupt file | High | Download to temp file (`{token}.session.tmp`), then `os.rename()` to final path. |
| 17 | **`checkpoint_session` fallback opens second SQLite connection** | High | Use Telethon's own connection first. Fallback with `timeout=5`. |
| 18 | **Server crash mid-upload** — local file may be unlinked before S3 confirms | High | Never delete local file until S3 PUT response received. |
| 19 | **Two instances download same session** — split-brain writes | High | Acceptable for v1 ("Last sync wins"). Document risk. |
| 20 | **WAL checkpoint silent failure** — PRAGMA result not validated | High | Validate `result.fetchone()[0]`. Log warning on failure. |
| 21 | **S3 download ContentLength=0 from MinIO** | High | Check `ContentLength` before reading. Reject if >10MB or 0. |

## Decision

### Decision 1: S3-Compatible Object Storage for Session Files

Store Telethon `.session` files in S3-compatible object storage. When S3 is not configured, existing local file handling is used unchanged.

**S3 object key**: `{token}.session` — the bearer token used directly as the filename. Simple, debuggable, no hashing complexity. Hardening (hashed keys, bucket policies) deferred to a later version once the feature is proven in production.

**All S3 logic lives in `src/s3_session.py`** — a single module that handles download, upload, delete, health check, close, and checkpoint. `connection.py` gets only 3 thin touchpoints:

1. Cache miss → call `s3_session.download(token, local_path)`
2. Cache eviction → call `s3_session.checkpoint_session()` + `s3_session.upload()`
3. `ensure_connection` → skip `.touch()` when S3 mode active

This minimizes changes to `connection.py` and reduces the risk of breaking existing file-based session handling.

**Download** from S3 on cache miss (before creating TelegramClient). **Upload** to S3 on cache eviction (after checkpoint, before local file deletion). **Never delete local file until S3 PUT is confirmed.**

**Session cache**: Keep existing `_session_cache: dict[str, tuple[TelegramClient, float]]` unchanged. No `CachedSession` dataclass — the local path is always derivable from the token via `_s3_local_path(token)` (returns `session_directory / {token}.session`). This avoids touching 8+ existing callers that unpack `(client, last_access)`.

**`mtime` touch in S3 mode**: The existing `ensure_connection()` calls `.touch()` on the local session file. In S3 mode, the local file is deleted after eviction — `.touch()` creates an empty `.session` file. Fix: skip `.touch()` when S3 mode is active. Track `last_access_time` via the existing tuple's second element.

### Decision 2: Standard AWS Environment Variables

No custom URL format. Use standard AWS env vars that boto3/aiobotocore natively understand:

| Variable | Required | Example |
|----------|----------|---------|
| `S3_SESSION_STORAGE` | Yes | `true` |
| `AWS_S3_BUCKET` | Yes | `fast-mcp-telegram-sessions` |
| `AWS_ACCESS_KEY_ID` | Yes | `AKIA...` |
| `AWS_SECRET_ACCESS_KEY` | Yes | `...` |
| `AWS_REGION` | No | `us-east-1` |
| `AWS_ENDPOINT_URL` | No | `http://minio:9000` (for MinIO/R2/Tigris) |

### Decision 3: Connection Lifecycle with Proper Lock Discipline

All eviction logic follows a strict pattern: **pop under lock → release → I/O**. Two helpers implement this: `_evict_session(token)` pops from `_cache_lock`, then calls `_do_evict_io(token, client)` which does checkpoint → disconnect → S3 upload with NO lock held.

```
MCP request arrives (bearer token in header)
  │
  ├─ _get_client_by_token(token)
  │    ├─ Cache hit (under _cache_lock) → return cached client
  │    └─ Cache miss:
  │         ├─ Release _cache_lock (client building is slow — don't hold lock)
  │         ├─ Acquire global _download_lock
  │         ├─ Double-check cache (under _cache_lock)
  │         ├─ s3_session.download(token, local_path)  ← I/O outside _cache_lock
  │         ├─ verify_session_integrity(local_path)
  │         ├─ TelegramClient(session=local_path)
  │         ├─ Re-acquire _cache_lock
  │         ├─ LRU eviction: pop oldest under lock
  │         ├─ _session_cache[token] = (client, time.time())
  │         └─ (caller does I/O on evicted entry after releasing lock)
  │
  ├─ Tool executes (Telethon writes to local SQLite normally)
  │
  └─ Eviction (idle timeout / LRU / shutdown):
       ├─ async with _cache_lock:
       │     cached = _session_cache.pop(token, None)  ← pop under lock
       ├─ (lock released)
       ├─ await _do_evict_io(token, client)  ← all I/O outside lock
       └─ On failure: keep local file, log warning (don't re-add disconnected client)
```

**Critical**: `asyncio.Lock` is NOT reentrant. Eviction is called while holding `_cache_lock` — it must NOT call any function that re-acquires `_cache_lock` (deadlock). Instead: pop the LRU entry under the existing lock, return the token+client. The caller releases the lock, then calls `_do_evict_io()`.

**Critical**: `_get_client_by_token` must NOT hold `_cache_lock` during `_build_telegram_client_for_token` (network I/O to Telegram). This is a pre-existing deadlock risk — fixed by releasing the lock before client building and re-acquiring to add to cache.

**Global download lock**: `_download_lock = asyncio.Lock()` — serializes all S3 downloads (~200ms each, acceptable for v1). Per-token locks deferred to avoid unbounded dict growth.

**On S3 upload failure**: Don't re-add the disconnected client to cache (causes infinite eviction loop). Keep local file — next request creates a fresh client from it.

**All eviction paths use the same pattern** — pop under lock, release, I/O:

| Existing function | Change |
|---|---|
| `_evict_lru_if_session_cache_full()` | Pop LRU entry under existing lock, return `(token, client)`. Caller does I/O after releasing lock. |
| `cleanup_idle_sessions()` | Snapshot idle tokens and pop under lock → release → disconnect + upload each. |
| `cleanup_session_cache()` | `for token in list(_session_cache): pop under lock → _do_evict_io()` |
| `disconnect_and_evict_session()` | Pop under lock → `_do_evict_io()` (handles checkpoint + S3 upload) |
| `setup_delete` | Pop + evict via `_do_evict_io()`, then `s3_session.delete()`, then local unlink |

### Decision 4: SQLite WAL Checkpoint Before Upload

Telethon uses SQLite's write-ahead journal. Uploading a `.session` file while Telethon has it open may produce a corrupt snapshot.

Before every S3 upload, run `PRAGMA wal_checkpoint(TRUNCATE)` to flush the WAL. Always use a separate connection with `timeout=5` — Telethon's `_conn` is not thread-safe with `asyncio.to_thread` and accessing private attributes is fragile across versions. Validate the PRAGMA result — only `row[0] == 0` counts as success. If checkpoint fails: log warning, do NOT upload, keep local file. All checkpoint I/O runs in `asyncio.to_thread()`.

A shared `_checkpoint_and_upload(token, local_path)` helper combines checkpoint + upload to avoid duplication between `_do_evict_io` and the `/setup` flow. The helper verifies the local file exists before attempting checkpoint.

Detailed code: see design doc §1.3.

### Decision 5: S3 Client Singleton

Create one `aiobotocore` S3 client at startup, reuse for all operations. Don't create a new client per call — each `create_client()` does DNS resolution + TLS handshake. Protected by `_client_lock` to prevent race conditions on first creation. `close_s3_client()` handles both stale TCP recovery (call then `_get_client()` creates fresh client) and graceful shutdown cleanup. Includes `mark_shutting_down()` to reject new operations during shutdown.

Detailed code: see design doc §1.2.

### Decision 6: Error Handling

No manual retry loops — botocore's adaptive retry (configured with `max_attempts=3`) handles transient S3 errors. No S3-specific circuit breaker for v1 — `connection.py` already has a Telegram connection circuit breaker. S3 failures raise/log and the caller handles gracefully (sessions stay local).

| Scenario | Behavior |
|----------|----------|
| S3 GET returns 404 (NoSuchKey) | New token — session doesn't exist yet. Return `SessionNotAuthorizedError`. |
| S3 GET returns 403 (AccessDenied) | Misconfiguration. Log error, fail fast. Do NOT retry (permanent error). |
| S3 GET returns 5xx / timeout | Botocore handles retry (3 attempts, adaptive backoff). If all fail, raise to caller. |
| S3 GET hangs | Per-attempt timeout: `read_timeout=5, connect_timeout=3` on S3 client config. |
| S3 GET returns oversized object | Check `ContentLength` before reading. Reject if >10MB. |
| S3 GET returns corrupt file | After download, run `PRAGMA integrity_check`. If fails, delete local file, return error. |
| S3 PUT fails on eviction | Botocore handles retry. **Keep local file regardless** — never delete until PUT succeeds. Log warning. Don't re-add disconnected client to cache. |
| SQLite checkpoint fails | Log error, do NOT upload — uploading a corrupt checkpoint produces an unrecoverable S3 object. |
| `aiobotocore` not installed | Try-import at startup with clear error. |

### Decision 7: `/setup` Auth Flow — S3 Upload and Delete

**Upload after setup:**
1. User visits `/setup` → QR / phone+code+2FA
2. Telethon session created (SQLite)
3. PRAGMA wal_checkpoint(TRUNCATE)
4. Bearer token generated (32 random bytes → base64url → 43-char string)
5. S3 PUT `{token}.session` ← session file
6. User copies bearer token into MCP client config

**Delete on setup_delete:**
1. User requests session deletion via `/setup`
2. Pop from cache + `_do_evict_io()` (disconnect + checkpoint + upload) — ensures any unsaved Telethon state is flushed
3. S3 DELETE `{token}.session`
4. Delete local file
5. If S3 delete fails: log warning, still delete local file

No `_pending_deletions` set — just delete synchronously. If an MCP request is in-flight, it will error on the next Telegram API call. That's acceptable for a deletion action.

### Decision 8: Startup S3 Health Check

On server startup, if `S3_SESSION_STORAGE=true`:

1. Check `aiobotocore` is installed (try-import)
2. Attempt `head_bucket()` to verify connectivity and credentials
3. If fails → log CRITICAL error, fail fast (don't start server)

### Decision 9: Inactive Session Cleanup

The existing `_cleanup_inactive_sessions()` uses local file `mtime`. **For v1, S3 mode skips inactive session cleanup.** S3 `LastModified` reflects upload time, not session activity — using it would incorrectly delete active sessions whose S3 objects haven't been updated since initial upload.

At ~50KB per session, storage cost is negligible. S3 lifecycle policies can auto-expire objects later if needed. The local file cleanup path is unchanged.

### Decision 10: Migration and Rollback

**Forward migration**: Upload local sessions to S3. Simple script.

**Rollback**: No reverse-migration script needed. S3-miss→local fallback handles it naturally:
- Set `S3_SESSION_STORAGE=false`
- Server reverts to local file mode
- Existing local files still work (they were never deleted if the container had persistent storage)
- For ephemeral containers: the next `/setup` creates a new local session

This is sufficient because the rollback scenario is "S3 is broken, go back to local" — not "migrate data out of S3". Local sessions are recreated by the user on next `/setup`.

### Decision 11: Multi-Instance Deployments

In autoscaled deployments, two containers can serve the same token simultaneously. "Last sync wins" — the last container to evict uploads its version.

Acceptable because:
- Entity cache differences are self-healing (rebuilt from Telegram on next connection)
- Auth key is stable across instances (same Telegram account)
- True concurrent writes to the same session are rare (one MCP client per token)

### Decision 12: Graceful Shutdown

On SIGTERM:
1. Set shutdown flag (`mark_shutting_down()` — reject new S3 operations)
2. For each cached session: pop from cache, disconnect, checkpoint, S3 PUT
3. `asyncio.wait_for(gather(...), timeout=min(max(len(tokens) * 10, 25), 120))` — dynamic timeout per session count
4. Log failures
5. `await s3_session.close_s3_client()` — release HTTP connections

Deployment config: `terminationGracePeriodSeconds` ≥ 130s (timeout + margin).

### Decision 13: Use `aiobotocore`

The codebase is fully async. `boto3` is synchronous and would require `asyncio.to_thread()` wrapping for every call. `aiobotocore` is the native async S3 client.

## Consequences

### Positive

- ✅ Ephemeral deployments work (Smithery Hosted, Fly.io, Railway)
- ✅ Entity cache preserved across restarts
- ✅ Update state preserved
- ✅ Bearer token unchanged — same UX
- ✅ S3 is universal — every platform has it
- ✅ Backward compatible (empty `S3_SESSION_STORAGE` = current behavior)
- ✅ Unblocks MCP registry listing
- ✅ Plain token keys — simple, debuggable
- ✅ `s3_session.py` isolates S3 logic — minimal `connection.py` changes

### Negative

- ⚠️ S3 dependency for hosted mode
- ⚠️ ~200ms latency on cold start (acceptable)
- ⚠️ Crash data loss possible (no periodic sync — auth key stable, entity cache rebuilds)
- ⚠️ `aiobotocore` dependency (~2MB)
- ⚠️ S3 PUT failure on eviction means session stays local until next retry

## Alternatives Considered

| Alternative | Rejected because |
|-------------|-----------------|
| **StringSession env var** | Loses entity cache, update state. Auth key unstable. |
| **Self-contained bearer tokens** | Conflates concerns. Huge tokens. Rotation complexity. |
| **Redis** | RAM-wasteful for file blobs. Wrong tool. |
| **PostgreSQL** | Schema/ORM for opaque blobs. Overkill. |
| **NFS** | Not available on hosted platforms. |
| **Gateway-managed sessions** | Smithery stores creds write-only. Need read-write. |
| **Periodic background sync** | Race conditions with SQLite writes, graceful shutdown complexity. Add later if needed. |
| **Abstract `SessionStorage` interface** | Over-engineering. Just add S3 directly. When S3 not configured, existing code runs unchanged. |
| **Hashed S3 keys** | Adds complexity for no real security gain — S3 buckets are already private. Defer to later version. |
| **S3 circuit breaker (v1)** | `connection.py` already has Telegram circuit breaker. S3 failures raise/log; sessions stay local on failure. Add unified circuit breaker later with production data. |
| **Manual retry loops** | Botocore's adaptive retry (`max_attempts=3`) handles transient errors. Manual loops + botocore = 9 attempts. Remove manual loops. |
| **S3 stale-key cleanup (v1)** | S3 `LastModified` ≠ session activity time. Would delete active sessions. Skip for v1; use S3 lifecycle policies. |

## Platform Setup

| Platform | S3-compatible | How |
|----------|--------------|-----|
| **Fly.io** | Tigris | `fly storage create` → auto-sets `AWS_*` vars |
| **Railway** | S3 add-on | One-click → auto-sets `AWS_*` vars |
| **Cloudflare** | R2 | Free egress. `AWS_ENDPOINT_URL=https://<account>.r2.cloudflarestorage.com` |
| **AWS** | S3 | IAM role or `AWS_*` vars |
| **Self-hosted** | MinIO | Docker sidecar, `AWS_ENDPOINT_URL=http://minio:9000` |
| **Hetzner** | Object Storage | Cheap. `AWS_ENDPOINT_URL=https://fsn1.your-objectstorage.com` |

## Security Considerations

| Concern | Mitigation |
|---------|-----------|
| **Bearer token as S3 key** | Plain token in key. Bucket is private — no public listing. Hardening (hashed keys) deferred to later version. |
| **Session file contents** | Contains auth key, entity cache. S3 server-side encryption (SSE-S3) is sufficient. No client-side encryption. |
| **Token in transit** | Bearer token only in Authorization header (HTTPS). Never in URLs or query params. |

## References

- [ADR 0004](./0004-qr-login-auth.md) — QR Login Auth
- [Telethon Sessions](https://docs.telethon.dev/en/stable/modules/sessions.html)
- [Smithery Architecture](https://smithery.ai/docs/architecture)
- [aiobotocore](https://aiobotocore.readthedocs.io/)
- [SQLite WAL Checkpoint](https://www.sqlite.org/wal.html)

---

## Appendix: Reviewer Findings (Rounds 2–4, Consolidated)

Rounds 2–4 identified 35 unique findings across 9 reviewers (3 per round). The most impactful findings shaped the core architecture:

**Critical decisions from Rounds 2–4:**
- Extracted all S3 logic to `s3_session.py` (was all in `connection.py`)
- Added `PRAGMA integrity_check` for corrupt file detection
- Switched to global `_download_lock` (per-token locks abandoned)
- Added `aiobotocore` try-import at startup with `SystemExit(1)` on failure
- Added `ContentLength` check before reading S3 objects (>10MB rejected)
- Implemented atomic download (temp file + rename)
- Added S3 client singleton with `_client_lock`
- Pop-under-lock eviction pattern to prevent deadlocks
- Synchronous delete (no `_pending_deletions`)
- Plain token keys (hashed keys deferred)
- `close_s3_client()` for graceful shutdown
- Skipped `list_stale()`, circuit breaker, monitoring thresholds for v1

Full finding tables archived in design doc revision history.

### Round 6

30 findings (3 reviewers). Major outcomes:
- Fixed pre-existing deadlocks: `_get_client_by_token` and `cleanup_idle_sessions` now release `_cache_lock` before I/O
- Changed per-token `_download_locks` dict to global `_download_lock`
- Fixed `_do_evict_io` re-add bug on failure (don't re-add disconnected client)
- Added S3 client singleton with `_client_lock` and health-recovery via `close_s3_client()`
- Added startup validation: `configure()` called, `AWS_S3_BUCKET` validated, `s3_session.delete()` raises on failure
- Fixed atomic download: orphan `.session.tmp` cleanup at startup
- Added 47 tests (14 unit + 33 integration) including 5 lock-discipline tests
- ADR stripped of all code (60%+ overlap eliminated)

### Round 7

7 findings. Major outcomes:
- Fixed connection leaks: `checkpoint_session` and `verify_session_integrity` now use try/finally for `conn.close()`
- Fixed temp file leak: `download()` finally block with `tmp_path.unlink(missing_ok=True)`
- Fixed stale ref: `_reset_client`/`close_s3_client` clear `_client = None` before `__aexit__`
- Added `_s3_key` token validation (no `/`, `..`, empty)
- `disable_auth` + S3 → fail-fast at startup

### Round 8

16 findings. Major outcomes:
- Fixed S3 404 handling: `download()` catches `ClientError(NoSuchKey)` → `FileNotFoundError`
- Removed Telethon `_conn` path from `checkpoint_session` — always separate connection
- Narrowed `_download_lock` scope from 3-8s to ~200ms (released before Telegram I/O)
- Extracted `_close_client_internal()` helper, removed duplicate orphan cleanup
- Updated ADR Decision 4 and lock ordering comment

### Round 9

14 findings. Major outcomes:
- Removed unused `client` parameter from `checkpoint_session`
- `/setup` now checks checkpoint return value, raises `TelegramTransportError` on failure
- Replaced `_s3_key` assert with `if/raise ValueError` (survives `python -O`)
- Added `head_bucket` write-permission verification via `put_object`
- Extracted `_checkpoint_and_upload()` helper (deduplicates `_do_evict_io` and `/setup`)
- Added `test_concurrent_same_token_race_after_lock`

### Round 10

42 findings. Major outcomes:
- Fixed `_do_evict_io` — `local_path.unlink()` moved outside try/finally (only on success)
- Added `mark_shutting_down()` flag to block new S3 client creation during shutdown
- Added S3-to-local fallback on cache miss when S3 unavailable
- Added pre-upload integrity check in `_checkpoint_and_upload`
- Added `MAX_UPLOAD_BYTES` (20MB) upload limit
- Added post-read size check for `ContentLength=None` (S3-compatible services)
- Tightened `_s3_key` to regex allowlist `^[a-zA-Z0-9._-]{1,256}$`
- `configure()` now rejects whitespace-only bucket names
- Added `timeout=5` to `verify_session_integrity` SQLite connection
- Wrapped disk-full `OSError` with explicit message
- Documented SIGKILL data loss, runtime permission changes, bearer token exposure in S3 logs

### Round 11

12 findings. Major outcomes:
- **Critical fix:** `_get_client()` now returns cached client during shutdown — `mark_shutting_down()` blocks new client creation but doesn't break eviction uploads. Previously, shutdown evictions all failed with RuntimeError
- Fixed `ContentLength=None` crash: `resp.get("ContentLength") or 0` (was `resp.get("ContentLength", 0)`)
- Fixed unawaited `resp["Body"].close()` — now `await resp["Body"].close()`
- LRU eviction exceptions now caught — requesting caller no longer sees unrelated S3 errors
- S3 fallback path now runs `verify_session_integrity` on local file (was skipped)
- Lock ordering comment corrected: "never acquire `_download_lock` while holding `_cache_lock`"
- §7.5 data flow updated to dynamic timeout (min 25s, max 120s)
- Rounds 6-10 ADR appendix consolidated from detailed tables to summary paragraphs

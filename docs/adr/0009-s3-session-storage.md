# ADR 0009: S3 Session Storage for Ephemeral Deployments

**Status:** proposed
**Date:** 2026-06-17

## Context

fast-mcp-telegram stores Telethon sessions as SQLite `.session` files on local disk. This works for the 96% running local stdio mode, but breaks for ephemeral hosted deployments (Smithery Hosted, Fly.io, Railway) where containers have no persistent storage. Session files are lost on every restart, forcing re-authentication and losing the entity cache.

Telethon's `StringSession` only stores the auth key (256 bytes) — not the entity cache, update state, or file upload cache. It's insufficient.

## Decision

### 1. S3-Compatible Object Storage

Store `.session` files in S3. Download on cache miss (before creating TelegramClient), upload on eviction (after checkpoint, before local file deletion). **Never delete local file until S3 PUT is confirmed.** When S3 is not configured, existing local file handling is unchanged.

**S3 key**: `{token}.session` — plain bearer token as filename. Hardening (hashed keys) deferred.

All S3 logic isolated in `src/s3_session.py`. `connection.py` gets 3 thin touchpoints: cache miss → download, eviction → checkpoint+upload, `ensure_connection` → skip `.touch()` in S3 mode.

### 2. Standard AWS Environment Variables

| Variable | Required | Example |
|----------|----------|---------|
| `S3_SESSION_STORAGE` | Yes | `true` |
| `AWS_S3_BUCKET` | Yes | `fast-mcp-telegram-sessions` |
| `AWS_ACCESS_KEY_ID` | Yes | `AKIA...` |
| `AWS_SECRET_ACCESS_KEY` | Yes | `...` |
| `AWS_ENDPOINT_URL` | No | `http://minio:9000` (MinIO/R2/Tigris) |

### 3. Lock Discipline — Pop Under Lock, I/O Outside

All eviction paths follow: pop from `_cache_lock` → release → checkpoint → disconnect → S3 upload. `_do_evict_io()` handles the I/O with NO lock held. `asyncio.Lock` is not reentrant — eviction must not re-acquire `_cache_lock`.

`_get_client_by_token` releases `_cache_lock` before client building (network I/O), re-acquires to add to cache. Global `_download_lock` serializes S3 downloads (~200ms each, acceptable for v1).

On S3 upload failure: keep local file, don't re-add disconnected client to cache.

### 4. SQLite WAL Checkpoint Before Upload

`PRAGMA wal_checkpoint(TRUNCATE)` via a separate connection with `timeout=5` before every S3 upload. Validate result. If checkpoint fails: log warning, do NOT upload, keep local file.

### 5. Error Handling

Botocore's adaptive retry (`max_attempts=3`) handles transient errors. No manual retry loops, no S3 circuit breaker for v1.

| Scenario | Behavior |
|----------|----------|
| S3 GET 404 | New token. Return `SessionNotAuthorizedError`. |
| S3 GET 403 | Misconfiguration. Fail fast, no retry. |
| S3 GET 5xx | Botocore retries (3 attempts). |
| S3 PUT fails on eviction | Keep local file. Log warning. |
| Checkpoint fails | Do NOT upload. Keep local file. |
| Object >10MB or `ContentLength=0` | Reject. |

### 6. `/setup` Flow

Upload after setup (checkpoint → S3 PUT → user copies token). On `setup_delete`: evict (checkpoint+upload) → S3 DELETE → local unlink.

### 7. Graceful Shutdown

On SIGTERM: `mark_shutting_down()` → evict all cached sessions (checkpoint+upload each) → close S3 client. Dynamic timeout: `min(max(len(tokens) * 10, 25), 120)` seconds. Deployment config: `terminationGracePeriodSeconds` ≥ 130s.

### 8. Use `aiobotocore`

Codebase is fully async. `boto3` is synchronous. `aiobotocore` is the native async S3 client (~2MB dependency).

### 9. Inactive Session Cleanup — Skip for v1

S3 `LastModified` reflects upload time, not session activity. Using it would incorrectly delete active sessions. S3 lifecycle policies can auto-expire later.

### 10. Multi-Instance — "Last Sync Wins"

In autoscaled deployments, two containers can serve the same token. Last to evict uploads its version. Acceptable: entity cache differences are self-healing, auth key is stable.

## Consequences

**Positive:**
- Ephemeral deployments work (Smithery Hosted, Fly.io, Railway)
- Entity cache and update state preserved across restarts
- Bearer token unchanged — same UX
- Backward compatible (empty `S3_SESSION_STORAGE` = current behavior)
- Unblocks MCP registry listing

**Negative:**
- S3 dependency for hosted mode
- ~200ms latency on cold start
- `aiobotocore` dependency (~2MB)
- Crash data loss possible (no periodic sync — auth key stable, entity cache rebuilds)

## Alternatives Considered

| Alternative | Rejected because |
|-------------|-----------------|
| StringSession env var | Loses entity cache, update state. Auth key unstable. |
| Self-contained bearer tokens | Huge tokens. Conflates concerns. Rotation complexity. |
| Redis | RAM-wasteful for file blobs. |
| PostgreSQL | Schema/ORM for opaque blobs. Overkill. |
| Abstract `SessionStorage` interface | Over-engineering. Add S3 directly. |
| Hashed S3 keys | Adds complexity, no real security gain. Defer. |
| Periodic background sync | Race conditions, shutdown complexity. Add later if needed. |

## References

- [ADR 0004](./0004-qr-login-auth.md) — QR Login Auth
- [ADR 0005](./0005-anonymous-tool-telemetry.md) — Telemetry
- [design/s3-session-storage-design.md](../design/s3-session-storage-design.md) — full code, platform setup, security, and 11 rounds of reviewer findings
- [Telethon Sessions](https://docs.telethon.dev/en/stable/modules/sessions.html)
- [aiobotocore](https://aiobotocore.readthedocs.io/)

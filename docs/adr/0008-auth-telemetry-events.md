# ADR 0008: Auth Telemetry Events

**Status:** accepted
**Date:** 2026-06-19

## Context

### Problem

Users authenticate through the web setup flow (phone+code, QR scan, 2FA, reauthorize) or the CLI setup flow (QR, phone, bot token), but the maintainer has zero visibility into whether these flows succeed, fail, or stall. The existing heartbeat telemetry (ADR 0005) aggregates tool-call metrics over 6-hour windows — useful for feature adoption trends, but completely blind to individual auth attempts.

Specific problems to detect:
1. **Exceptions** — Telethon errors, network failures, unexpected crashes during auth
2. **Abandoned flows** — users who start auth (enter phone, scan QR) but never complete
3. **Slow flows** — auth processes that take unusually long (Telegram API delays, user hesitation)
4. **Code path** — which auth method was used (phone, QR, reauthorize) and where it branched (2FA needed, QR expired, etc.)

### Why not extend heartbeats

Auth events are **rare and high-signal** — each user authenticates once (or very rarely). Aggregating into 6-hour heartbeat windows loses:
- Individual attempt timelines
- The exact error that killed a flow
- Whether a flow was abandoned vs. completed later
- Duration of each specific attempt

Heartbeats are the right tool for "how many tool calls happened today." Auth needs individual events.

### Constraints

- Must use the same collection infrastructure (same endpoint, same PostgreSQL, same collector container)
- Must respect `DO_NOT_TRACK=1` opt-out
- Must never block the auth flow (fire-and-forget, like heartbeats)
- No new dependencies (stdlib `urllib` only, same as existing telemetry)
- No PII — no phone numbers, no tokens, no chat IDs

## Decision

### Event model

Add a new `type` field to the telemetry payload. The existing heartbeat payload implicitly has no `type` (or `type: "heartbeat"` by convention). Auth events use `type: "auth"`.

```json
{
  "type": "auth",
  "iid": "a1b2c3d4-...",
  "ts": 1718030000,
  "ver": "0.38.0",
  "event": "auth_completed",
  "method": "qr",
  "branch": "qr_scan",
  "duration_ms": 12340,
  "error": null
}
```

### Fields

| Field | Type | Description |
|-------|------|-------------|
| `type` | `"auth"` | Discriminator — distinguishes from heartbeat payloads |
| `iid` | `str` | Instance ID (same as heartbeat) |
| `ts` | `int` | Unix timestamp when the event was recorded |
| `ver` | `str` | Library version (same as heartbeat) |
| `event` | `str` | What happened: `auth_started`, `auth_completed`, `auth_failed`, `auth_abandoned` |
| `method` | `str` | Auth method: `phone`, `qr`, `reauth`, `bearer_check`, `cli_setup` |
| `branch` | `str` | Code path / step: `phone_code`, `phone_2fa`, `qr_scan`, `qr_2fa`, `reauth_phone`, `bearer_valid`, `bearer_no_session`, `bearer_invalid` |
| `duration_ms` | `float` | Wall-clock duration from start to this event |
| `error` | `str \| null` | Categorized error: `flood_wait`, `invalid_code`, `2fa_wrong_password`, `timeout`, `connect_failed`, `session_expired`, `unknown` |

### Event lifecycle

| Event | When fired | Fields |
|-------|-----------|--------|
| `auth_started` | User initiates auth (POST to /setup/phone, /setup/qr, /setup/reauthorize) | `method`, `branch`, `duration_ms=0` |
| `auth_completed` | Auth succeeds (session persisted, token generated) | `method`, `branch`, `duration_ms`, `error=null` |
| `auth_failed` | Auth fails with an exception or error | `method`, `branch`, `duration_ms`, `error=<category>` |
| `auth_abandoned` | Stale session cleanup detects an unfinished flow | `method`, `branch`, `duration_ms` (time since started) |

### Branch tags

| Branch | Meaning |
|--------|---------|
| `phone_code` | Phone entered, code sent, waiting for verification |
| `phone_2fa` | Code verified, 2FA password required |
| `qr_scan` | QR displayed, waiting for scan |
| `qr_2fa` | QR scanned, 2FA password required |
| `reauth_phone` | Reauthorize flow, phone entered |
| `bearer_valid` | Bearer token validated successfully |
| `bearer_no_session` | Bearer token valid but no session file |
| `bearer_invalid` | Bearer token format invalid |
| `cli_qr_scan` | CLI: QR code displayed, waiting for scan |
| `cli_qr_2fa` | CLI: QR scanned, 2FA required |
| `cli_phone_code` | CLI: Phone entered, code sent |
| `cli_phone_2fa` | CLI: Code verified, 2FA required |
| `cli_bot_token` | CLI: Bot token authentication |

### Error categories

| Error | Maps to |
|-------|---------|
| `flood_wait` | `PhoneNumberFloodError` |
| `invalid_code` | Verification code wrong |
| `2fa_wrong_password` | `PasswordHashInvalidError` |
| `timeout` | QR expired, setup session expired |
| `connect_failed` | Telethon client.connect() failed |
| `session_expired` | Setup session TTL exceeded |
| `unknown` | Any other exception |

### Transport

Same endpoint (`/v1/event`), same HTTP POST, same fire-and-forget pattern. The collector discriminates on `type`:
- No `type` or `type: "heartbeat"` → validate as `TelemetryPayload` (existing)
- `type: "auth"` → validate as `AuthEvent` (new)

### Collector changes

1. **`collector/app/auth_models.py`** — add `AuthEvent` dataclass with validation
2. **`collector/app/services.py`** — `process_event()` dispatches on `type` field
3. **`collector/app/main.py`** — no change (same endpoint)
4. **`collector/app/database.py`** — same table (JSONB stores both event types), add index on `payload->>'type'`

### Query patterns

```sql
-- All auth failures in last 7 days
SELECT * FROM telemetry
WHERE payload->>'type' = 'auth'
  AND payload->>'event' = 'auth_failed'
  AND received_at >= NOW() - INTERVAL '7 days';

-- Failure rate by method
SELECT payload->>'method', 
       COUNT(*) FILTER (WHERE payload->>'event' = 'auth_failed') AS failures,
       COUNT(*) AS total
FROM telemetry
WHERE payload->>'type' = 'auth'
GROUP BY payload->>'method';

-- Abandoned flows
SELECT * FROM telemetry
WHERE payload->>'type' = 'auth'
  AND payload->>'event' = 'auth_abandoned';

-- Slow auth (>30s)
SELECT * FROM telemetry
WHERE payload->>'type' = 'auth'
  AND (payload->>'duration_ms')::float > 30000;

-- Errors by version
SELECT payload->>'ver', payload->>'error', COUNT(*)
FROM telemetry
WHERE payload->>'type' = 'auth'
  AND payload->>'event' = 'auth_failed'
GROUP BY payload->>'ver', payload->>'error';
```

### Instrumentation points

| File | Route/Function | Events fired |
|------|---------------|-------------|
| `web_setup.py` | `/setup/phone` | `auth_started(method=phone, branch=phone_code)`, `auth_failed(method=phone, branch=phone_code, error=flood_wait)` |
| `web_setup.py` | `/setup/verify` | `auth_completed(method=phone, branch=phone_code)`, `auth_failed(method=phone, branch=phone_code, error=invalid_code)`, `auth_started(method=phone, branch=phone_2fa)` |
| `web_setup.py` | `/setup/2fa` | `auth_completed(method=phone, branch=phone_2fa)`, `auth_failed(method=phone, branch=phone_2fa, error=2fa_wrong_password)` |
| `web_setup.py` | `/setup/qr` | `auth_started(method=qr, branch=qr_scan)` |
| `web_setup.py` | `/setup/qr/status` | `auth_completed(method=qr, branch=qr_scan)`, `auth_failed(method=qr, branch=qr_scan, error=timeout)` |
| `web_setup.py` | `/setup/qr/2fa` | `auth_completed(method=qr, branch=qr_2fa)`, `auth_failed(method=qr, branch=qr_2fa, error=2fa_wrong_password)` |
| `web_setup.py` | `/setup/reauthorize` | `auth_started(method=reauth, branch=reauth_phone)`, `auth_completed(method=reauth, branch=reauth_phone)` |
| `web_setup.py` | `cleanup_stale_setup_sessions` | `auth_abandoned(method=..., branch=...)` |
| `auth.py` | `require_auth` | `auth_completed(method=bearer_check, branch=bearer_valid)`, `auth_failed(method=bearer_check, branch=bearer_no_session)`, `auth_failed(method=bearer_check, branch=bearer_invalid)` |
| `cli_setup.py` | `_qr_session_login` | `auth_started(method=cli_setup, branch=cli_qr_scan)`, `auth_completed(method=cli_setup, branch=cli_qr_scan)`, `auth_failed(method=cli_setup, branch=cli_qr_scan, error=timeout)` |
| `cli_setup.py` | `_handle_qr_2fa` | `auth_completed(method=cli_setup, branch=cli_qr_2fa)`, `auth_failed(method=cli_setup, branch=cli_qr_2fa, error=2fa_wrong_password)` |
| `cli_setup.py` | `setup_telegram_session` (phone) | `auth_started(method=cli_setup, branch=cli_phone_code)`, `auth_completed(method=cli_setup, branch=cli_phone_code)`, `auth_failed(method=cli_setup, branch=cli_phone_code, error=...)` |
| `cli_setup.py` | `setup_telegram_session` (bot) | `auth_started(method=cli_setup, branch=cli_bot_token)`, `auth_completed(method=cli_setup, branch=cli_bot_token)`, `auth_failed(method=cli_setup, branch=cli_bot_token, error=...)` |

### Bearer check telemetry

The `require_auth` decorator runs on every tool call. To avoid flooding telemetry:
- Only fire `auth_completed` / `auth_failed` events for **bearer_check** on the first successful auth per session (not every tool call)
- Use a module-level set of validated tokens to suppress duplicates: `{token_prefix: last_fire_ts}` with a cooldown window (e.g., 1 hour)

## Consequences

### Positive

- Visibility into auth flow success/failure rates across the user base
- Abandoned flow detection identifies UX friction points
- Duration tracking catches Telegram API slowdowns
- Error categorization enables targeted fixes (e.g., "80% of failures are flood_wait → add rate limiting")
- Version-tagged errors catch regressions early

### Neutral

- Same collection infrastructure, no new services
- Same table (JSONB stores both event types), same retention
- Auth events are low-volume (~1 per user per install), minimal storage impact

### Negative

- Slightly more code in web_setup.py (instrumentation calls)
- Collector needs to discriminate on `type` field (minor branching in services.py)
- New index on `payload->>'type'` for efficient filtering

### Risks

- Bearer check events could be high-volume if every tool call fires one (mitigated by cooldown)
- Error categories may need refinement as real data arrives (mitigated by `unknown` catch-all)

## References

- [ADR 0005](0005-anonymous-tool-telemetry.md) — existing heartbeat telemetry design
- [ADR 0006](0006-abuse-prevention-for-collection-endpoint.md) — rate limiting and dedup

## Current Work Focus

**Rich message send (2026-07-28):** `parse_mode="rich"` on `send_message` / `edit_message` / `send_message_to_phone` via `InputRichMessageMarkdown` or `InputRichMessageHTML` (dialect: strip code, whitelist tags). Success returns flattened `text`, `rich: true`, `rich_format`. Classic parse modes unchanged. Rich+files rejected. Telethon `>=1.44.0`. Tests: `tests/test_rich_message_send.py`.

**Deferred (parked):** forced `rich_markdown`/`rich_html` literals; blocks JSON; draft streaming; rich+media refs; classic entity unparse hardening; mcp.json→`.venv`; live forum rich reply.

---

**Rich message read (2026-07-12):** `get_messages` flattens MTProto `Message.rich_message` PageBlocks to `text` with `rich: true`; rich-only bot posts no longer filtered from browse. Rich media uses ID-keyed attachment tickets (`rich_kind` + `rich_media_id`); `attachments[]` in tree order. Telethon floor `>=1.44.0`. Tests: `tests/test_rich_message_format.py`, extended `test_attachment_streaming.py`.

---

**message_ids missing-id contract (2026-07-12):** Explicit `message_ids` returns per-id stubs under `{messages, has_more: false}` (single = multi); operational `ok: false` still top-level. Internal batched id fetches omit deleted gaps silently. `FromUser` annotation documents no display-name search. Tests in `tests/test_read_messages_by_ids_gaps.py`.

---

**Web setup reauth QR (2026-07-09):** After token validation, reauthorize shows QR (recommended) + phone chooser. `/setup/qr` accepts `setup_id` for reauth and preserves the existing bearer token on completion. Success page no longer displays the token. Four regression tests in `tests/test_web_setup.py`.

---

**find_chats / search auth error surfacing (2026-07-09):** Combined-path fix deployed (`0.42.0-2-g51a1d71`). Follow-up in working tree: multi-term `_gather_term_results`, combined prefers auth over misleading no-contacts errors, `_gather_global_batch` + generator paths propagate session/transport failures. Uncommitted on `master`.

---

**GetPeerDialogsRequest hang fix — v0.28.2 (2026-06-03):** Released — fix for issue #51 merged and deployed.

- **Root cause:** `iter_dialogs` returns entities with stale `access_hash` (deleted/expired accounts). GetPeerDialogsRequest hangs ~30s per chunk when given these entities. Fixed by storing `dialog.date` directly during flags iteration and skipping GetPeerDialogsRequest for ~200 flags-matched entities.
- **Performance:** folder+date query from ~39s to ~6.9s mean (5 iterations).
- **Debugging findings:** `asyncio.wait_for` on Telethon calls corrupts MTProto (msg_id desync, server reconnect). Batch `get_entity([71])` triggers `FLOOD_WAIT_178`. Cross-DC delays on 8/71 users (41-42s, unfixable client-side).
- **Regression test:** `test_find_chats_by_include_peers_uses_dialog_date_for_flags_entities` — verifies flags entities skip GetPeerDialogsRequest.

### Active Decisions

- **dialog.date from iter_dialogs is sufficient** for flags-matched entities — GetPeerDialogsRequest is unnecessary when the entity already has a dialog date.
- **asyncio.wait_for is DANGEROUS with Telethon** — cancellation leaves MTProto sender state inconsistent; the delayed response corrupts the next request's msg_id.
- **Individual get_entity calls are preferred** over batch — batch triggers flood wait for large peer lists.

---

**Completed**: README Features table audit + copy fixes (2026-05-04)

- Verified all 18 feature rows against code; adjusted links (Multi-User → `#remote-setup-http-auth`, Dual Transport → `#overview` per user, MTProto Proxy → `#mtproto-proxy`, Bot Chat → Tools Reference `#uniform-entity-schema`), distinct emoji for Secure File Handling, and wording (no unsubstantiated “connection pooling”; “configurable logging” vs “structured”). Dual Transport description now mentions optional `http-no-auth`.

---

**Completed**: Todo list `completed_by` JSON safety (2026-04-14)

- `MessageMediaToDo` stored raw Telethon `Peer` in `media.items[].completed_by`, breaking `pydantic_core.to_jsonable_python` and MCP `structuredContent` when `outputSchema` is set.
- Fix: [`_todo_completed_by_to_int`](src/utils/message_format/core.py) via [`_forward_peer_id_and_type_label`](src/utils/entity.py); only sets `completed_by` when resolvable to `int`.
- Tests: [`tests/test_todo_media_placeholder.py`](tests/test_todo_media_placeholder.py).

---

**Completed**: Agent-friendly MCP tool metadata (2026-04-13)

- All eight tools use explicit `description=`, `ToolAnnotations(title=...)`, and short docstrings pointing to the Tools Reference (`TOOLS_REFERENCE_DOC_URL` in `tools_register.py`, GitHub `main` blob URL).
- New `src/server_components/mcp_tool_types.py`: `Annotated[..., Field(description=...)]` aliases for MCP input schemas (DRY parameter help).
- `find_chats` and `find_chats_impl` return type aligned to `dict[str, Any]` (success `chats` key or standardized error dict).

---

**Completed**: Bot Chat Type Split and Folder Filtering (2026-04-01) — see `progress.md` for detail.

---

**Completed**: MTProto Fake TLS Integration (2026-03-31)

**Implementation**:
- Added `TelethonFakeTLS` as dependency in `pyproject.toml`
- Updated `src/utils/proxy.py` with fake TLS detection and secret processing:
  - Base64 secrets with `7` prefix: strip leading `7` (e.g., `7i/UefJk...` → `i/UefJk...`)
  - Hex secrets with `ee` prefix: strip leading `ee` (e.g., `ee2fd479...` → `2fd479...`)
- Integrated `ConnectionTcpMTProxyFakeTLS` into all client creation paths:
  - `src/client/connection.py` (main client)
  - `src/server_components/web_setup.py` (web setup)
  - `src/cli_setup.py` (CLI setup)
- Graceful fallback with warning if `TelethonFakeTLS` not installed

**Verified Working**: CLI setup authenticated successfully via MTG proxy `144.31.188.163:443`

---

**Completed**: MTProto Proxy Support (2026-03-31)

**Implementation**:
- Added `mtproto_proxy` config field to `ServerConfig` via `MTPROTO_PROXY` env var
- Created `src/utils/proxy.py` with `MTProtoProxy` NamedTuple and URL parsing
- Supports `tg://proxy?server=...&port=...&secret=...` and `host:port:secret` formats
- Integrated `ConnectionTcpMTProxyRandomizedIntermediate` into:
  - `src/client/connection.py` (main client)
  - `src/server_components/web_setup.py` (setup flow)
  - `src/cli_setup.py` (CLI setup)

---

**Completed**: CI/CD pipeline migration to GitHub Actions + GHCR (2026-03-30)

**Implementation**:
- Migrated from manual `deploy-mcp.sh` to GitHub Actions workflow matching pdf-extract pattern
- Created `.github/workflows/deploy.yml` with build-push and deploy jobs
- Updated `docker-compose.yml` to use GHCR image + named volume for sessions
- Created `scripts/sync-remote-config.sh` for manual sync
- Updated `.vscode/tasks.json` with new tasks
- Updated README.md, docs/Deployment.md, docs/Installation.md
- Marked `scripts/deploy-mcp.sh` as legacy

**Operational notes**: Deployment is automatic on push to main. Sessions persist in Docker named volume.

---

**Completed**: Web setup UX, consistency, and same-step errors (2026-03-27)

**Implementation**:
- Nested `#setup-flow` HTMX target; toolbar `showForm` reloads with `?branch=` when branch sections are missing after swaps
- Phone/reauthorize/delete token steps use inline `<div class="error">` on the matching fragment
- Reauthorize token step and delete branch no longer use `error.html` for validation or missing session
- Terminal `error.html` uses `.error` for the message and **Back to setup**
- Tests in `tests/test_web_setup.py` cover delete/reauthorize same-step responses

**Operational notes**: Setup sessions remain in-memory and process-local; single-replica assumptions still apply.

---

**Completed**: FastMCP 3 Bearer Token Fix (2026-03-15)
SessionFileTokenVerifier validates tokens via session file existence. `with_auth_context` uses `get_access_token()` bypassing upstream bug #596. All 250 tests pass.

---

## Active Decisions and Considerations

### Web Setup Interface Enhancement (2025-11-18)
Session management via `/setup` endpoint with Create, Reauthorize, Delete options. Token-based security with phone verification for reauth.

### Public Visibility Filtering (2025-11-19)
`public` parameter excludes private chats (DMs) from visibility filtering - they always appear. Groups/channels filtered normally.

### invoke_mtproto TL Construction (2025-11-25)
Automatic TL object construction from JSON dictionaries with `"_"` key. Recursive nested support for complex types.

### Multiple Chat Type Filtering (2025-11-20)
`chat_type` accepts comma-separated values (`"private,group"`). Case-insensitive, whitespace-tolerant validation.

### Connection Stability (2025-10-17)
Exponential backoff (2^failure_count, max 60s), circuit breaker (5 failures/5 min), session health monitoring.

### Unified Session Configuration (2025-10-11)
SessionConfig with session_name/session_path. HTTP_AUTH uses random tokens, STDIO/HTTP_NO_AUTH use configured names.

---

## Important Patterns and Preferences

### Web Interface Styling Patterns
1. **Visual Hierarchy**: Larger interactive elements (inputs, buttons) with smaller instructional text
2. **Clean Layout**: Minimal text, clear form structure, no empty visual elements
3. **Responsive Design**: Mobile-friendly interface with proper spacing and sizing
4. **Error Handling**: Clear error messages with context-specific guidance

### Authentication Flow Patterns
1. **Progressive Disclosure**: Show only necessary information at each step
2. **Session Persistence**: Maintain setup sessions throughout the flow
3. **Error Recovery**: Allow retry with clear error messages
4. **Automatic Cleanup**: TTL-based session cleanup prevents resource leaks

### VDS Testing and Diagnosis Methodology
1. **Environment Access**: SSH with credentials from `.env` file (`SSH_USER`, `SSH_HOST`)
2. **Deployment Process**: GitHub Actions auto-deploys on push to main. Manual sync via `./scripts/sync-remote-config.sh`
3. **Container Management**: Use `docker compose` commands for container status, logs, and health checks
4. **Authentication Testing**: Use `curl` with proper MCP protocol headers and bearer tokens
5. **Log Analysis**: Monitor server logs for authentication flow and error patterns
6. **Session Management**: Sessions stored in Docker named volume `telegram-sessions`
7. **Health Monitoring**: Container health checks and endpoint monitoring
8. **Debugging Approach**: Systematic issue elimination through targeted testing

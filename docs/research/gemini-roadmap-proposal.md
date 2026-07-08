# Gemini Research: Proposed Roadmap and Implementation Phases

> **Source:** [Gemini shared research](https://gemini.google.com/share/b1d8cb8b23c2) (2026-05-26). Items in **Proposed** are not implemented unless marked **Shipped**. See [Strategic-Market-Positioning.md](../Strategic-Market-Positioning.md) for verified current state.

## Already shipped (media and security baseline)

The original report listed these under “future” roadmap item 5. They exist today:

| Capability | Location |
| --- | --- |
| SSRF-protected URL downloads | [src/tools/messages/security.py](../../src/tools/messages/security.py), [SECURITY.md](../../SECURITY.md) |
| Attachment streaming | `GET /v1/attachments/{uuid}/{filename}` — [src/server_components/attachment_routes.py](../../src/server_components/attachment_routes.py) |
| Premium voice transcription | [src/utils/message_format/transcription.py](../../src/utils/message_format/transcription.py) |
| Attachment URLs in tool output | [src/utils/message_format/attachments.py](../../src/utils/message_format/attachments.py) |

## Proposed next-generation features

### 1. Dynamic zero-trust ACL engine

Transition from static configuration to dynamic ACL for secure multi-user deployments.

- **Mechanism:** Load `acl.yaml` or JSON ACL at session init; map tokens to permitted `chat_id` values
- **Target tool:** `configure_session_acl`
- **Enforcement:** Block `get_messages` / `send_message` when `chat_id` is not whitelisted

### 2. Pre-execution payload filtering and indirect prompt injection defense

- **Mechanism:** Scan payloads from `get_messages` and `search_messages_globally` before they reach the LLM
- **Target tool:** `sanitize_chat_context`
- **Note:** today only log param sanitization exists in [src/utils/logging_utils.py](../../src/utils/logging_utils.py) — not message-content scanning

### 3. Asynchronous SQLite archiving and differential synchronization

Inspired by kfastov message-sync:

- **Mechanism:** Background worker per session; incremental SQLite archive with sync cursors
- **Target tool:** `sync_chat_history`
- **Impact:** Local queries instead of outbound MTProto; reduced `FLOOD_WAIT` risk

### 4. Dynamic multi-tenant OAuth2 and identity broker integration

- **Mechanism:** OAuth2 Token Exchange (RFC 8693) with Keycloak, Okta, etc.
- **Target tool:** `broker_user_session`
- **Goal:** Short-lived MTProto sessions mapped from enterprise JWTs

### 5. Extended media processing (net-new only)

Shipped baseline covers download, SSRF checks, and voice transcription. Remaining proposals:

- **Target tool:** `extract_and_process_media` — unified download + validate + OCR/transcription pipeline
- **Future:** sandboxed attachment storage, non-voice media pipelines, document OCR

## Implementation phases

| Phase | Horizon | Planned scope | Already shipped in this area |
| --- | --- | --- | --- |
| Phase 1: Security hardening | Short-term | Default-deny ACL, directory sandbox for stdio paths, I/O injection scanner | SSRF URL validation; HTTP local-path blocking; attachment tickets |
| Phase 2: Offline archiving | Medium-term | SQLite background sync; local query path | — |
| Phase 3: Identity federation | Long-term | OAuth2 token exchange; Keycloak/Vault credential mapping | Bearer tokens + web setup only |

## Strategic outcome (research summary)

Repositioning as a secure multi-tenant gateway addresses real ecosystem gaps (context overhead, session brokerage). The phased plan above separates **shipped** security primitives from **planned** ACL, archiving, and IdP work.

---

[← Strategy & monetization](gemini-strategy-monetization.md) · [Index](../Strategic-Market-Positioning.md)

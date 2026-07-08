# Architecture Decision Records

Concise records of significant technical decisions for fast-mcp-telegram.

## Convention

| Field | Rule |
| --- | --- |
| **Location** | `docs/adr/NNNN-short-title.md` |
| **Numbering** | Four-digit sequence (`0001`, `0002`, …) |
| **Status** | `proposed`, `accepted`, `superseded`, `deprecated` |
| **Length** | Prefer under ~100 lines; English only |
| **Links** | Reference research notes, code paths, and [Roadmap.md](../Roadmap.md) lanes when relevant |

## Index

| ADR | Title | Status |
| --- | --- | --- |
| [0001](0001-agent-scoped-session-acl.md) | Agent-scoped session ACL (guardrails, not account lockdown) | accepted (2026-05-27) |
| [0002](0002-oidc-self-service-auth.md) | OIDC Self-Service Authentication | superseded (2026-06-08) |
| [0003](0003-oidc-phase4-scope-based-auth.md) | OIDC Phase 4 — In-Tool Auth & Elicitation | superseded (2026-06-09) |
| [0004](0004-qr-login-auth.md) | QR Login Auth — Simplified Self-Service Onboarding | accepted (2026-06-09, implemented v0.30.0) |
| [0005](0005-anonymous-tool-telemetry.md) | Anonymous Tool Telemetry | accepted (2026-06-13) |
| [0006](0006-abuse-prevention-for-collection-endpoint.md) | Abuse Prevention for the Open Collection Endpoint | proposed (2026-06-10) |
| [0007](0007-qr-login-cli-setup.md) | QR Login in CLI Setup — QR-First Auth with Terminal Rendering | proposed (2026-06-18) |
| [0008](0008-auth-telemetry-events.md) | Auth Telemetry Events — Buffered flow-level auth instrumentation | accepted (2026-06-19) |
| [0009](0009-s3-session-storage.md) | S3 Session Storage for Ephemeral Deployments | proposed (2026-06-17) |
| [0010](0010-north-star-pivot.md) | North Star Pivot — Best Telegram Bridge for AI Agents | accepted (2026-06-15) |
| [0011](0011-context-enrichment-for-search.md) | Context Enrichment for Message Search Results | accepted (2026-07-09) |
| [0012](0012-empty-result-consistency.md) | Empty Result Consistency — Return Success, Not Error, for Zero-Result Queries | accepted (2026-07-09) |

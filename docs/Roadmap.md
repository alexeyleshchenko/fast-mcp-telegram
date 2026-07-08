# Product Roadmap

Official priorities for fast-mcp-telegram. Capability facts live in [Strategic-Market-Positioning.md](Strategic-Market-Positioning.md). Third-party Gemini research is under [research/](research/).

Last updated: 2026-06-17.

## North star

**The best Telegram bridge for AI agents** — 8 consolidated tools, MTProto access, zero-friction setup. Optimize for reliability, agent correctness, and distribution. Multi-user `http-auth` hosting is a downstream capability, not the organizing principle.

> **Why the shift?** Telemetry (ADR 0005) — collected from 24 instances over the 4-day window 2026-06-11 to 2026-06-15, stored in the anonymous telemetry PostgreSQL DB at fast-mcp-telegram-telemetry.l1979.ru — shows 96% of instances (23/24) run stdio local mode. Nobody uses ACL, bot tokens, or MTProto proxy. The actual user base wants a local Telegram MCP tool — not a multi-tenant gateway. The previous North Star ("shared http-auth multi-user hosting") organized the roadmap around a user segment that doesn't exist yet. This version reflects reality: make the tool excellent first, then extend to multi-user when there's evidence of demand.

## Roadmap lanes

Work is organized in **priority order**. The first three lanes reflect where the actual user base is (96% stdio local). Multi-user trust infrastructure is deferred until there's evidence of demand.

> **Glossary:** A **lane** is a thematic work area (e.g. "Quality & Reliability", "Distribution"). A **priority** (P0–P3) determines sequencing — P0 is worked on first, P3 is deferred. A **branch** is the Git branch where that lane's code lives. Multiple lanes can share a branch (e.g. Quality and Distribution both use `master`). Lanes are priority-ordered, not branch-ordered — the priority, not the branch, determines what gets worked on next.

| Priority | Lane | Branch | Purpose |
| --- | --- | --- | --- |
| **P0** | **Quality & Reliability** | `master` | Fix real errors surfaced by telemetry: `get_messages` entity resolution, `search_messages_globally` query failures, reduce 25% error rate on production (57 calls / 14 errors, 2026-06-11 to 2026-06-15) |
| **P1** | **Distribution** | `master` | Smithery listing, PyPI/uvx polish, README/Installation quality — get the tool in front of more users |
| **P2** | **QA / Gategrid** | `feature/evals` | Benchmark tool behavior with [Gategrid](https://github.com/leshchenko1979/gategrid) — **GG** — and enforce regressions via **GG gating** |
| **P3** | **Trust** | `feature/acl` | Agent guardrails per Bearer token — **deferred** until multi-user http-auth adoption grows. See [ADR 0001](adr/0001-agent-scoped-session-acl.md) |
| **Ongoing** | **Telemetry** | `master` *(shipped)* | Production signals that tell QA where agents and users struggle — see [ADR 0005](adr/0005-anonymous-tool-telemetry.md) |

### QA loop: telemetry → benchmark → gate

Telemetry and Gategrid serve different steps of the same **QA function**. Telemetry observes real usage; Gategrid proves fixes and blocks regressions.

```mermaid
flowchart LR
  subgraph prod [Production]
    users[Agents and users]
    server[MCP server]
    users --> server
  end
  subgraph telemetry_lane [Telemetry lane]
    signals[Usage signals]
    triage[Problem triage]
    server --> signals
    signals --> triage
  end
  subgraph qa_lane [QA / Gategrid lane]
    cases[Case design]
    bench[GG benchmark]
    gate[GG gating]
    triage --> cases
    cases --> bench
    bench --> gate
  end
  ship[Merge and deploy]
  gate --> ship
  ship --> server
```

| Step | Lane | What happens |
| --- | --- | --- |
| **1. Observe** | Telemetry | Collect structured signals from production: tool errors, `FLOOD_WAIT`, latency outliers, auth failures, repeated tool-selection mistakes. Surfaces *where* quality breaks in the wild. |
| **2. Decide** | QA | Turn telemetry findings into hypotheses and **GG case** candidates — new prompts, matrices, or baseline updates. Prioritize cases that match real failure modes. |
| **3. Benchmark** | QA / Gategrid | Run **GG** matrices (`smoke`, `gate`, optional live model) to measure pass rate, tool choice, and regressions vs baselines. Informs whether a tool or doc change actually helps agents. |
| **4. Assure** | QA / Gategrid | **GG gating** on PRs: `gategrid gate` against [evals/ci/baselines/main.json](../evals/ci/baselines/main.json) blocks merges when pass rate or like-for-like cells regress. |
| **5. Ship** | All lanes | Merge when trust, telemetry hooks, and gate are aligned for the change scope. |

**Today:** Steps 1–2 are live in production (telemetry DB collecting signals). Steps 3–4 are scaffolded on `feature/evals`. The next action is using telemetry data to drive P0 quality fixes and P2 case design.

## Branches

| Branch | Lane | Contents |
| --- | --- | --- |
| `master` | All active work | Roadmap, telemetry, QR login, feature work, Gemini research under [research/](research/) |
| `feature/evals` | QA / Gategrid | [evals/](../evals/), [gategrid-eval.yml](../.github/workflows/gategrid-eval.yml) |
| `feature/acl` | Trust *(deferred)* | Session ACL implementation and design docs — waiting for multi-user demand signal |
| `postponed/oidc-elicitation-storage` | OIDC *(superseded)* | Storage layer for OIDC + elicitation. Superseded by QR login. See [ADR 0004](adr/0004-qr-login-auth.md) |

> **Note:** The previous lane model assumed parallel feature branches for Trust, Telemetry, and QA. In practice, telemetry and QR login shipped directly to `master` via PRs. The lane model is now priority-ordered, not branch-ordered.

## Decisions (2026-06-15)

| Topic | Decision |
| --- | --- |
| **North Star** | Best Telegram bridge for AI agents — not multi-user hosting. Multi-user is a downstream capability. |
| **Priority model** | P0 Quality → P1 Distribution → P2 QA → P3 Trust. Lanes are priority-ordered, not branch-ordered. |
| **ACL** | Shipped but opt-in; deferred as a roadmap priority until multi-user http-auth adoption grows. |
| **Telemetry** | Shipped on `master` (ADR 0005). Now feeding QA triage with real production data. |
| **QA model** | Telemetry informs case design; **GG benchmark** validates; **GG gating** enforces on PR |
| **Evals** | Gategrid harness on `feature/evals`; not merged until gate is stable |
| **QR Login** | Shipped v0.30.0. Killed OIDC+elicitation complexity. The right auth model for the user base. |

### Previous decisions (2026-05-26, superseded)

<details>
<summary>Original lane model decisions</summary>

| Topic | Decision |
| --- | --- |
| ACL | Opt-in via `ACL_ENABLED`; per-token rules in static file — `feature/acl` |
| QA model | Telemetry informs case design; **GG benchmark** validates; **GG gating** enforces on PR |
| Evals | Gategrid harness on `feature/evals`; not merged until gate is stable |
| Telemetry | Separate lane; must feed QA triage before cases are added blindly |
| Post-ACL focus | Telemetry spike + eval expansion before rate limits / SQLite |

</details>

## Current sequence

| Step | Status | Priority | Lane | Branch | Deliverable |
| --- | --- | --- | --- | --- | --- |
| 1. Roadmap and research | Done | — | Docs | `master` | This doc, Gemini under [research/](research/) |
| 2. QR login auth | ✅ **Shipped (v0.30.0)** | — | Auth | `master` | Telethon QR login — scan QR from mobile, no phone/code/2FA. See [ADR 0004](adr/0004-qr-login-auth.md) |
| 3. Telemetry | ✅ **Shipped** | — | Telemetry | `master` | Anonymous telemetry collection + DB. See [ADR 0005](adr/0005-anonymous-tool-telemetry.md) |
| 4. Fix top telemetry errors | **Next** | **P0** | Quality | `master` | `get_messages` entity resolution, `search_messages_globally` query failures — 25% error rate on production (57 calls / 14 errors, 2026-06-11 to 2026-06-15) |
| 5. Smithery URL-based listing | In progress | **P1** | Distribution | `master` | Register tg-mcp.l1979.ru on Smithery as URL-based deployment |
| 6. GG scaffold | In progress | **P2** | QA | `feature/evals` | Six gate cases, mock baseline, PR workflow |
| 7. GG depth + live eval | Planned | **P2** | QA | `feature/evals` | Cases driven by telemetry; optional VDS live matrix |
| 8. S3 session storage | Planned | **P1** | Infrastructure | `master` | Store Telethon `.session` files in S3-compatible object storage. Download on connect, upload on disconnect. Unblocks ephemeral hosted deployments. See [ADR 0009](adr/0009-s3-session-storage.md) |
| 9. Smithery Hosted migration | Planned | **P1** | Infrastructure | `master` | Move from URL-based to Smithery-hosted containers |
| 10. ACL audit and design | Done | **P3** | Trust | `feature/acl` | [acl-operator-research.md](research/acl-operator-research.md), [acl-design-brief.md](research/acl-design-brief.md) |
| 11. ACL MVP | Done (not merged) | **P3** | Trust | `feature/acl` | [session_acl.py](../src/server_components/session_acl.py) — deferred until multi-user demand grows |
| 12. Principal identifier forms | Planned | **P3** | Trust | `feature/acl` | Human-readable `principals:` keys — deferred |

## Shipped on `master`

- QR Login Auth (v0.30.0) — Telethon QR login, unified `/setup` page, `@require_auth` decorator. Backward compatible with existing bearer tokens. See [ADR 0004](adr/0004-qr-login-auth.md)
- Anonymous Telemetry (ADR 0005) — opt-in production signal collection, PostgreSQL storage, per-tool error/latency tracking. See [ADR 0005](adr/0005-anonymous-tool-telemetry.md)
- [Strategic-Market-Positioning.md](Strategic-Market-Positioning.md) capability index
- Gemini research under [research/](research/) (third-party reference)
- Roadmap lane model (this document)

Trust and Gategrid evals merge from their feature branches when each lane is ready.

## Trust lane — planned scope

Operator-facing enhancements beyond ACL enforcement (not implemented):

| Item | Purpose |
| --- | --- |
| **Sensitive peer denylist (Phase 1.5)** | Operator-configured `blocked_peers` denylist for **all** tokens when non-empty — independent of `chats` allowlist. Dual pre/post enforcement; recommended defaults in `acl.yaml.example` + SECURITY.md. See [ADR 0001](adr/0001-agent-scoped-session-acl.md), [acl-design-brief.md](research/acl-design-brief.md). |
| **Chat metadata registry** | Operator-curated metadata for whitelisted/shared chats so team agents can navigate `find_chats` results — human titles, descriptions, tags, and “look here for X” hints. Complements ACL **workspace lanes** (which chats a token may use) with **navigation hints** (what each chat is for); does not replace lane allowlists. Likely config alongside `acl.yaml` or a sibling file; enrichment at the tool boundary (e.g. post-filter on `find_chats`). |
| **Principal identifier forms** | **Admin ergonomics:** operators can key `principals:` entries by Telegram `@username` or numeric `user_id` instead of copying opaque Bearer strings — easier to assign lanes to people and audit who has which profile. **Semantics:** these are **alternative principal identifiers** for the same lane rules; they identify the **Telegram account** bound to a session (`{id}.session`), not chat peers listed under `chats:`. **Runtime unchanged:** HTTP clients still send `Authorization: Bearer <token>`; the server resolves identifiers to the matching session at config load or setup time. |
| **ACL chat ref resolution** | Resolve `@username` lane entries against numeric peer ids in tool args and list results (entity lookup), so operators need not duplicate id and handle in `chats`. |

See [acl-design-brief.md](research/acl-design-brief.md) Phase 1.5 and Phase 3 for related ACL work.

## Telemetry lane — current and planned signals

Shipped signals (ADR 0005): heartbeat, tool calls with latency/errors, session file counts, RSS memory, platform/version.

Candidate signals for next iteration (QA triage):

| Signal | QA use |
| --- | --- |
| Tool error rate by tool name | New or tightened GG cases for that tool |
| `FLOOD_WAIT` / connection errors | Rate-limit and caching decisions; stress cases |
| P95 tool latency | Performance regressions; matrix timing budgets |
| Wrong-tool patterns | Case prompts that require disambiguation |
| Auth / ACL denials | Security docs and negative-path cases |

## QA / Gategrid lane — current scope

See [evals/README.md](../evals/README.md) on branch `feature/evals`.

| Artifact | Role in QA loop |
| --- | --- |
| [evals/cases/](../evals/cases/) | User-language prompts → **benchmark** scenarios |
| [evals/matrices/](../evals/matrices/) | `smoke` vs `gate` run profiles |
| [evals/ci/baselines/main.json](../evals/ci/baselines/main.json) | Regression baseline for **GG gating** |
| [.github/workflows/gategrid-eval.yml](../.github/workflows/gategrid-eval.yml) | PR mock gate + manual live dispatch |

## Backlog (not sequenced)

| Item | Lane | Notes |
| --- | --- | --- |
| Sensitive peer denylist | Trust | Phase 1.5 — operator `blocked_peers` list + dual enforcement; see Trust lane planned scope |
| Principal identifier forms | Trust | Human-readable principal identifiers (`@username`, `user_id`) as alternatives to opaque ids — see [step 4](#current-sequence) and Trust lane |
| Per-token rate limits | Trust / ops | Complements telemetry FLOOD_WAIT signals |
| SQLite read cache | Performance | Pairs with ACL whitelists |
| ACL v2 permission matrix | Trust | Prgebish-style read/send per chat |
| Prompt-injection scanner | Trust | After ACL + QA coverage |
| OAuth2 / IdP | Enterprise | Federation path |
| **S3 session storage** | Infrastructure | **P1 — see step 8.** Store Telethon `.session` files in S3-compatible object storage (AWS S3, MinIO, R2, Tigris). Download on connect, upload on disconnect. Preserves entity cache and update state across ephemeral container restarts. Unblocks Smithery Hosted and other MCP registry deployments. See [ADR 0009](adr/0009-s3-session-storage.md) |
| **Remote file upload (base64)** | Features | ✅ PR #103, #104, #105 — `data:...;base64` inline payloads in `files` param (all modes). `;filename=name.ext` preserves original names. Images sent as inline photos, docs as documents. `tg-mcp-call` auto-inlines local paths as data: URIs. |
| Stdio path sandbox | Trust | Local stdio users |
| Multi-replica attachment tickets | Ops | Shared ticket store |
| Media OCR pipeline | Features | Beyond voice transcription |
| **Refactoring** | All lanes | Post-Phase-3 cleanup: consolidate session config, extract shared logic from `connection.py`/`server.py`, reduce duplication across transport modes, standardise error types. Unblocks faster iteration in subsequent phases. |
| **Docs review** | Docs / strategy | Post-Phase-3 audit: verify every public function has a docstring, every tool has usage examples, every ADR is up to date with code, all `TODO`s are intentional. Publish API reference. |
| Split `message_format` package | Architecture | Presentation vs attachment/ticket I/O split; `extract_topic_metadata` promoted to public API (2026-07-09) |

## Where to record future work

| Kind of item | Where to write it |
| --- | --- |
| Prioritized capability, lane, or backlog item | **This doc** — lane sections (`Trust`, `Telemetry`, `QA`) or [Backlog](#backlog-not-sequenced) |
| Design detail for an approved lane (phases, enforcement, config shape) | `docs/research/*-design-brief.md` (e.g. [acl-design-brief.md](research/acl-design-brief.md)) |
| Architectural decision with tradeoffs and consequences | `docs/adr/NNNN-*.md` (new ADR when the decision is settled) |
| Competitor notes, spikes, third-party research | `docs/research/` (reference only; link from Roadmap) |
| Current sprint focus and immediate next steps | `.cursor/memory-bank/activeContext.md` (3–5 items; not a substitute for Roadmap) |

**Rule of thumb:** Roadmap names *what* and *which lane*; research briefs spell *how*; ADRs record *why* a direction was chosen.

## References

- [Strategic-Market-Positioning.md](Strategic-Market-Positioning.md)
- [research/gemini-roadmap-proposal.md](research/gemini-roadmap-proposal.md)
- [Installation.md](Installation.md)
- [Gategrid](https://github.com/leshchenko1979/gategrid) — external eval and gating harness

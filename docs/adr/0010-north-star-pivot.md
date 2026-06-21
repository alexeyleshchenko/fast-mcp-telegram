# ADR 0010: North Star Pivot — Best Telegram Bridge for AI Agents

**Status:** accepted
**Date:** 2026-06-15

## Context

The original North Star was "shared http-auth multi-user hosting" — organizing the roadmap around multi-user gateway capabilities (ACL, OIDC, session isolation). Telemetry from 24 instances over 4 days (2026-06-11 to 2026-06-15) shows:

| Mode | Instances | Share |
|------|-----------|-------|
| stdio local | 23 | 96% |
| http-auth | 1 | 4% |

Zero instances use ACL, bot tokens, or MTProto proxy. The roadmap was organized around a user segment that doesn't exist yet.

## Decision

**"The best Telegram bridge for AI agents"** — 8 consolidated tools, MTProto access, zero-friction setup. Optimize for reliability, agent correctness, and distribution. Multi-user http-auth hosting is a downstream capability, not the organizing principle.

### Priority Model

| Priority | Lane | Focus |
|----------|------|-------|
| **P0** | Quality & Reliability | Fix real errors: 25% error rate (57 calls / 14 errors) |
| **P1** | Distribution | Smithery listing, PyPI/uvx polish |
| **P2** | QA / Gategrid | Benchmark tool behavior, enforce regressions |
| **P3** | Trust | Agent guardrails — deferred until multi-user demand grows |

Lanes are priority-ordered, not branch-ordered. Telemetry and QR login shipped directly to `master` via PRs, not parallel feature branches.

### What Changed

| Aspect | Before | After |
|--------|--------|-------|
| **Organizing principle** | Multi-user gateway | Best Telegram bridge |
| **ACL priority** | Active development | Deferred (P3) until demand signal |
| **OIDC** | In progress | Superseded by QR login (ADR 0004) |
| **Branch model** | Parallel feature branches | Priority-ordered, `master` primary |
| **QA** | Manual | Telemetry → GG benchmark → GG gating loop |

### What Didn't Change

- ACL code ships and works — it's just not a roadmap priority
- http-auth mode remains supported
- Telemetry continues collecting (ADR 0005)
- QR login is the auth model (ADR 0004)

## Consequences

**Positive:**
- Roadmap reflects actual user base (96% stdio local)
- Quality becomes P0 — the 25% error rate is the real problem
- Distribution (P1) gets the tool in front of more users before investing in multi-user infrastructure
- ACL investment preserved — ready when demand arrives

**Negative:**
- Multi-user features deprioritized — if http-auth adoption grows quickly, the Trust lane is behind
- Smithery Hosted (step 9) depends on S3 session storage (ADR 0009), which is P1 infrastructure work

## References

- [Roadmap.md](../Roadmap.md) — the roadmap reorganized by this decision
- [ADR 0001](./0001-agent-scoped-session-acl.md) — ACL design (deferred to P3)
- [ADR 0004](./0004-qr-login-auth.md) — QR login (replaced OIDC)
- [ADR 0005](./0005-anonymous-tool-telemetry.md) — telemetry that surfaced the 96% finding

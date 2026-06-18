# Published Resources

This file tracks where **fast-mcp-telegram** has been published or listed, with status and links.

## Published ✅

| Resource | URL | Status | Notes |
|----------|-----|--------|-------|
| **PyPI** | https://pypi.org/project/fast-mcp-telegram/ | ✅ Live (v0.34.0) | `pip install fast-mcp-telegram` |
| **Glama** | https://glama.ai/mcp/servers/leshchenko1979/fast-mcp-telegram | ⏳ Submitted for review | Connector submitted by user — awaiting Glama review to index tools |
| **Docker (GHCR)** | `ghcr.io/leshchenko1979/fast-mcp-telegram:*` | ✅ Live | Published alongside releases |
| **RemoteMCPList** | https://github.com/remotemcplist/servers/issues/22 | ⏳ Issue open | GitHub issue #22 — not yet merged |
| **ToolSDK Registry** | https://github.com/toolsdk-ai/toolsdk-mcp-registry/pull/324 | ✅ MERGED | PR #324 merged |
| **Official MCP Registry** | https://registry.modelcontextprotocol.io | ❌ 404 — listing gone | Was published at v0.22.2, now returns 404. Blocked: `mcp-publisher login github` needs interactive GitHub OAuth. Mac Chrome has no GitHub session. |
| **mcp.so** | https://mcp.so/servers/678f0b7fc72dda6b377d9800 | ✅ 200 — search broken | Direct URL works but site search returns 404 (site issue, not ours) |
| **Smithery** | https://smithery.ai/servers/leshchenko/fast-mcp-telegram | ✅ Live | Re-published 2026-06-17 via CLI. Namespace: `leshchenko` (not `leshchenko1979`). API key: `553a7ea1-...` in Smithery console. |

## Submitted — Awaiting Review / Merge ⏳

| Resource | URL | Status | Notes |
|----------|-----|--------|-------|
| **awesome-mcp-servers** | https://github.com/punkpeye/awesome-mcp-servers/pull/7019 | ✅ MERGED | PR #7019 merged by @punkpeye |
| **MCPFind** | https://github.com/MCPFind/mcp-find/pull/53 | ❌ Closed | MCPFind moved to automated curation — PR #53 closed without merge |
| **MCP.Directory** | https://mcp.directory | ✅ Submitted | "Server Submitted!" — auto-pulls metadata from GitHub, publishes within 24h |

## Failed — Blocks Automation ❌

| Resource | URL | Status | Notes |
|----------|-----|--------|-------|
| **MCPMarket** | https://app.mcpmarket.com/servers/fast-mcp-telegram | ❌ Dead end | Custom MCP deployments from GitHub require Pro plan — user declined |
| **PulseMCP** | https://pulsemcp.com/servers | ❌ Blocked | Cloudflare blocks automated checks |
| **MCPForge** | https://mcpforge.org | ❌ Not a directory | Managed hosting service (like Smithery), not a listing directory |

## Requires User Action 🔲

| Resource | URL | Status | Notes |
|----------|-----|--------|-------|
| **GitHub MCP Registry** | — | 🔲 Needs email | Requires email to `partnerships@github.com` — agent cannot send email |

## Adding a new listing

1. Submit the package to the registry/directory
2. Verify the listing is live
3. Add a row to the table above
4. Commit and push

## Updating listings

When a listing status changes (e.g. PR merged, listing removed), update this table accordingly.

# Discoverability Plan

## Problem

When a user searches for "telegram mcp" on any registry, **fast-mcp-telegram does not appear** in results on the biggest indexes.

## Current Status (2026-06-17 verified)

| Registry | Status | Search visibility | Action needed |
|----------|--------|------------------|---------------|
| **Glama** | Indexed, `tools:[]` | ❌ NOT in search | Re-claim + register connector |
| **Official MCP Registry** | 404 (listing gone) | ❌ Not listed | Re-login + publish (token expired) |
| **Smithery** | 404 (listing gone) | ❌ Not listed | Re-login + publish |
| **awesome-mcp-servers** | ✅ MERGED | ✅ Discoverable | Docs update only |
| **ToolSDK Registry** | ✅ MERGED | ✅ Discoverable | Docs update only |
| **mcp.so** | 200 (direct URL) | ⚠️ Site search broken | Site issue, not ours |
| **MCP.Directory** | Submitted | ⏳ Pending | Wait |
| **RemoteMCPList** | Issue #22 open | ⏳ Pending | Wait |
| **PyPI** | ✅ v0.34.0 | ✅ `pip install` | Docs update only |

## Root Cause Analysis

### Glama (~37K servers — biggest index)

**Problem:** Our server IS indexed (direct URL works, glama.json exists in repo) but shows `tools: []` — no tools are discovered. This means we don't appear in search for "telegram" or even "fast-mcp-telegram".

**Why:** Glama discovers tools via two paths:
1. **Repo servers (open-source):** Build & run in sandbox → introspect via MCP
2. **Connectors (remote):** Connect to live endpoint → introspect via MCP

Our server is registered as a repo server. Glama's sandbox can't build/run it because it needs `API_ID`/`API_HASH` credentials. So tools are empty.

**Evidence:**
- Server-card.json at `/.well-known/mcp/server-card.json` has ALL 8 tools (HTTP 200, no auth needed)
- MCP endpoint at `/v1/mcp` returns ALL 8 tools via `tools/list` (no Bearer token needed, just Accept header)
- Glama API shows `tools: []` for our server AND every other server tested (likely a search index issue, not API display)
- We don't appear in search results for "telegram" (20 results, none ours) or "fast-mcp-telegram" (10 unrelated results)

**Fix (requires user action):**
1. **Register as a connector on Glama** — Go to https://glama.ai/mcp/connectors → "Add MCP Server" → Connector → paste `https://tg-mcp.l1979.ru/v1/mcp`. This lets Glama introspect the live endpoint directly (no sandbox needed).
2. **Re-claim the GitHub listing** — Go through the "Claim ownership" flow on Glama with GitHub OAuth. This triggers a re-scan of the repo.
3. **Contact Glama on Discord** — If the above doesn't work, ask their team to manually trigger a scan. Discord: https://discord.gg/C3eCXhYWtJ

### Official MCP Registry

**Problem:** Listing was published at v0.22.2 but is now 404.

**Why:** Token expired (`Invalid or expired Registry JWT token`).

**Fix:**
1. Run `mcp-publisher login github` (interactive — needs browser)
2. Run `mcp-publisher publish server.json`
3. `server.json` has been updated to schema `2025-12-11` with v0.34.0 ✅

### Smithery

**Problem:** Listing is 404.

**Fix:**
1. Run `smithery auth login` (interactive)
2. Run `smithery mcp publish "https://tg-mcp.l1979.ru/v1/mcp" -n @leshchenko1979/fast-mcp-telegram`

## Implementation Steps

### Step 1: Docs updates (DONE)
- [x] Update `published-resources.md` with actual statuses
- [x] Update `server.json` to schema 2025-12-11 with v0.34.0

### Step 2: Glama (requires user)
- [ ] Register as connector at https://glama.ai/mcp/connectors
- [ ] Re-claim GitHub listing on Glama
- [ ] Verify tools appear in search within 24-48h

### Step 3: Official MCP Registry (requires user)
- [ ] Run `mcp-publisher login github` (interactive auth)
- [ ] Run `mcp-publisher publish server.json`
- [ ] Verify listing at https://registry.modelcontextprotocol.io

### Step 4: Smithery (requires user)
- [ ] Run `smithery auth login`
- [ ] Run `smithery mcp publish "https://tg-mcp.l1979.ru/v1/mcp" -n @leshchenko1979/fast-mcp-telegram`
- [ ] Verify listing at https://smithery.ai

### Step 5: Server-side improvements (can do now)
- [x] Server-card.json already has all 8 tools with full schemas
- [x] MCP endpoint already returns tools without auth
- [x] glama.json exists with categories, env vars, description

## What We've Already Done Right

1. **Server-card.json** — Full metadata at `/.well-known/mcp/server-card.json` with all 8 tools
2. **No-auth tools/list** — The MCP endpoint returns tools without requiring Bearer token
3. **glama.json** — Rich metadata with categories, env vars, related servers
4. **server.json** — Updated to schema 2025-12-11 with correct version
5. **Output schemas** — All 8 tools have specific TypedDict return types (not generic `dict`)
6. **Annotations** — All tools have MCP annotations (readOnlyHint, destructiveHint, etc.)
7. **awesome-mcp-servers** — Merged and discoverable
8. **ToolSDK Registry** — Merged and discoverable

## Key Insight

The biggest blocker is **Glama** (37K servers, biggest index). The fix requires interactive web action: registering as a connector so Glama can introspect our live endpoint. Once that's done, tools should populate and search visibility should follow within 24-48 hours.

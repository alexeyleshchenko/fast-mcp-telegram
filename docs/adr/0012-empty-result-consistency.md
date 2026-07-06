# ADR 0012: Empty Result Consistency — Return Success, Not Error, for Zero-Result Queries

**Status:** proposed
**Date:** 2026-07-06

## Context

When search and discovery tools find zero results, they currently return an error via `log_and_build_error()` with `ok: false`.

For MCP clients (LLM agents), this creates a problem: a **successful query that matched nothing** and a **real failure** (network error, auth, rate-limit) both arrive as `ok: false` error dicts. The agent cannot distinguish them without parsing the `error` string.

The REST/HTTP consensus is clear on this distinction:

| Scenario | Correct status |
|----------|---------------|
| Collection search — `GET /items?q=foo` → 0 items | **200 with `[]`** |
| Single resource — `GET /items/42` → not found | **404** |
| Real failure (auth, timeout, rate-limit) | **Error** |

The MCP-equivalent rule: **a search that ran successfully and found nothing should return a valid success response with an empty collection**, not an error.

## Affected Tools

All paths below currently return `log_and_build_error(...)` when no results are found.

### `get_messages` — search/browse mode (`search_mode.py`)

| Scenario | File:Line | Returns |
|----------|-----------|---------|
| Per-chat search, empty query, date range | `_handle_query_mode` : L725-728 | error |
| Per-chat search, empty query, no dates | `_handle_query_mode` : L729-730 | error |
| Per-chat search, non-empty query | `_handle_query_mode` : L731 | error |
| Global search, empty query | `_handle_query_mode` : L726-728 | error |

### `get_messages` — replies mode (`replies.py`)

| Scenario | File:Line | Returns |
|----------|-----------|---------|
| No replies to a message | `_handle_reply_mode` : L379-383 | error |

### `search_contacts` (`contact_search.py`)

| Scenario | File:Line | Returns |
|----------|-----------|---------|
| No contacts match query | `_search_contacts_as_list` : L108-112 | error dict |

### `find_chats` — global multi-term (`find_chats.py`)

| Scenario | File:Line | Returns |
|----------|-----------|---------|
| No results from any term | `_find_chats_global_multi_term` : L386-391 | error dict |
| No results, dialog-based | `_find_chats_by_dialogs` : L458-462 | error dict |

### Not affected (real errors, kept as-is)

These remain errors because they represent actual failures:

- **Invalid ISO date format** (`search_mode.py`, `replies.py`, `date_helpers.py`)
- **Missing required parameters** — `chat_id`, `message_ids`, `reply_to_id`, `query` for global search (`core.py`)
- **`from_user` without `chat_id`** (`core.py`)
- **Chat not found** (`chat_info.py`, `sending.py`, `reading.py`)
- **Connection / auth failures** (`search_mode.py`, `replies.py`, `mtproto.py`)
- **Invalid filter name**, **empty `from_user`**, **invalid limit** (`find_chats.py`)
- **FloodWait, RpcError** (`mtproto.py`)

## Decision

Convert the 5 affected tools/paths to return valid success responses with empty collections instead of error dicts.

| Tool | Current (error) | New (success) |
|------|----------------|---------------|
| `get_messages` (search/browse) | `ok: false, error: "No messages found..."` | `{"messages": [], "has_more": false}` |
| `get_messages` (replies) | `ok: false, error: "No replies found..."` | `{"messages": [], "has_more": false, "reply_to_id": N}` |
| `search_contacts` | `ok: false, error: "No contacts found..."` | `[]` |
| `find_chats` (global multi-term) | `ok: false, error: "No contacts found..."` | `{"chats": []}` |
| `find_chats` (dialog-based) | `ok: false, error: "No chats found..."` | `{"chats": []}` |

### Informational `note` field

When returning an empty collection, include an optional `note` field in the response dict to give the AI client context about why nothing was found.

**Rationale:** An empty list `[]` carries no semantics. The AI client sees `messages: []` but doesn't know *why* — was the query too specific? The date range too narrow? No chats exist with that name? `note` provides a human-readable diagnostic without turning success into an error.

**Where `note` is added:**

| Tool response shape | Where note goes |
|---------------------|----------------|
| `{"messages": [], "has_more": false}` | `note` key in the same dict |
| `{"messages": [], "has_more": false, "reply_to_id": N}` | `note` key in the same dict |
| `{"chats": []}` | `note` key in the same dict |

**Where `note` is NOT added:**

- `_search_contacts_as_list` returns a raw `list[dict]` — no dict to attach `note` to. The callers (`_find_chats_global`, `find_chats_impl`) wrap it into `{"chats": []}` and may add `note` there.
- Normal (non-empty) responses — `note` is absent, so clients that don't check it see no change.

**MCP spec compatibility:**

- MCP `CallToolResult` does not define a `note` field — but `structured_content` is arbitrary JSON.
- FastMCP converts return dicts to `structured_content`, so any keys in the dict (including `note`) appear in `structured_content`.
- The alternative — FastMCP's `ToolResult(meta={"note": "..."})` — would require switching every affected return from plain dict to `ToolResult`. This is more invasive and inconsistent with the rest of the codebase.
- **Decision:** Add `note` directly to the response dict as a plain string key. No new types, no `ToolResult` imports.

### Detailed returns

#### `_handle_query_mode` (search_mode.py)

Current (simplified):
```python
if not window:
    return log_and_build_error(...)

response = {"messages": window, "has_more": has_more}
return response
```

New:
```python
# Remove the if-not-window error block entirely.
# window is already sliced earlier as: window = collected[:limit].
# When collected is empty, window is [], has_more is False.
# The response is already correct for empty results.
```

The existing response construction below naturally produces `{"messages": [], "has_more": false}` — the guarding error block just prevents it from reaching the return. Removing the guard is sufficient.

#### `_handle_reply_mode` (replies.py)

Same pattern — remove the `if not window:` error block. The existing response construction already produces the correct shape with `reply_to_id`.

#### `_search_contacts_as_list` (contact_search.py)

Current:
```python
if not results:
    return log_and_build_error(...)
return results
```

New:
```python
# Just let the empty list flow through — it's valid data.
return results
```

#### `_find_chats_global_multi_term` (find_chats.py)

Current:
```python
if term_results is None:
    return log_and_build_error(...)
return {"chats": _merge_results_round_robin(term_results, limit)}
```

New:
```python
if not term_results:
    return {"chats": []}
return {"chats": _merge_results_round_robin(term_results, limit)}
```

The `isinstance(result, list)` check in `_gather_term_results` now succeeds for empty `[]` returned by each term (since `_search_contacts_as_list` returns a list, not an error dict). So `term_results` will be `[[], [], ...]` instead of `None`. The `_merge_results_round_robin` handles empty input lists correctly (returns `[]`).

#### `_find_chats_by_dialogs` (find_chats.py)

Current:
```python
if results:
    return {"chats": results}

date_str = "..."
return log_and_build_error(...)
```

New:
```python
return {"chats": results}
```

The `results` list (possibly empty) is the canonical success shape.

### `_normalize_gather_result` interaction

The `_normalize_gather_result` helper (used in `_find_chats_combined`) has:
```python
if isinstance(chats, list) and chats:
    return chats
return None
```

When `_find_chats_global` returns `{"chats": []}`, this hits `and chats:` → `False` → returns `None`. So the combined search's `term_results` stays empty, falls to `if not term_results:`, then looks for an error dict (`.get("ok") is False`) — which `{"chats": []}` doesn't have — and returns `{"chats": []}`. This is already the correct behavior for empty results; no changes needed for this helper.

## Consequences

### Positive

- MCP clients get consistent success-response format for all successful queries, regardless of result count
- Downstream processing simplified — no error-checking gate for "nothing found"
- Backward compatible: `TypedDict` result types (`SearchResult`, `FindChatsResult`) already have `messages`/`chats` as optional `total=False` fields, so `{"messages": []}` is schema-valid
- Real errors remain crisp — only the "empty result" special case is changed

### Negative

- Existing MCP clients that pattern-match on `ok: false` for empty results will need to adapt (but this is the incorrect pattern — they should match on `messages: []` or `chats: []`)
- `isinstance(result, list)` guards in `find_chats_impl` and `_find_chats_global` become dead code (always True) — harmless but could be cleaned up in a follow-up

### Edge cases

- **Actual exceptions** during term search (FloodWait, network) still propagate as `Exception` instances through `_gather_term_results`, so those errors remain correctly reported
- **Empty query with no chat_id** (global search, query is empty) remains an error — `if not chat_id and not queries: return log_and_build_error(...)` — because it's a parameter validation error, not an empty result
- **Date filter with no query** in per-chat browse mode: previously returned a descriptive error, now returns `{"messages": [], "has_more": false}` — the client can see `messages` is empty and inform the user

## Implementation files

| File | Lines to change | Change type |
|------|----------------|-------------|
| `src/tools/search/search_mode.py` | L724–734 | Remove `if not window:` error guard. Add `note` with query/date context when window is empty. |
| `src/tools/search/replies.py` | L379–383 | Remove `if not window:` error guard. Add `note` with reply context when window is empty. |
| `src/tools/chat_discovery/contact_search.py` | L108–112 | Remove `if not results:` error guard — `return results` directly (empty list is valid). Raw list return — no `note`. |
| `src/tools/chat_discovery/find_chats.py` | L382–393 | `_find_chats_global_multi_term`: change `if term_results is None:` → `if not term_results: return {"chats": []}` with note. |
| `src/tools/chat_discovery/find_chats.py` | L455–462 | `_find_chats_by_dialogs`: change to unconditional `return {"chats": results}` with note when empty. |

## References

- REST API best practices (empty collection = 200, not 404)
- MCP protocol: tools return typed results, error is for exceptional conditions
- [ADR 0011](0011-context-enrichment-for-search.md) — prior search change; also builds `messages`/`has_more` response format
- `src/tools/return_types.py` — `SearchResult`, `FindChatsResult`, `_ErrorFields` type definitions
- `src/utils/error_handling.py` — `log_and_build_error()` implementation

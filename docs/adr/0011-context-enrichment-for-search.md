# ADR 0011: Context Enrichment for Message Search Results

**Status:** accepted
**Date:** 2026-06-26
**Accepted:** 2026-07-09

## Context

When LLM agents search messages via `get_messages`, results are flat — each message is an isolated object with no surrounding conversation context. This makes search results less useful for understanding conversation flow, reply chains, and what was said around a match.

Industry precedent:
- **Slack** `assistant.search.context` returns `context_messages` with `before[]` and `after[]` lists per result
- **RAG best practices** (Anthropic, PIXION) recommend sentence-window retrieval — find small chunks, expand to neighbors on retrieval

Telegram API capabilities already partially used by fast-mcp-telegram:
- `client.iter_messages(entity, offset_id=N)` — fetch messages by position (neighbors)
- `MessageReplyHeader.reply_to_msg_id` — identify what a message replies to
- `reply_to.forum_topic` + `extract_topic_metadata` — forum topic handling
- `_fetch_direct_replies(client, entity, msg_id)` in `replies.py` — fetch reply threads

## Decision

Add a `context` parameter to `get_messages` (int, default 0 = disabled) that enriches search results with surrounding conversation context, and an `include_replies` flag for reply thread fetching.

### Cost analysis

| Enrichment | Extra API calls | Notes |
|-----------|----------------|-------|
| Neighbors (±N) | 1 batched `getMessages` per result set | Cheap — one call for all IDs |
| Reply chain | 1 batched `getMessages` per result set | Cheap — batched with neighbors |
| Reply threads | `_fetch_direct_replies` per result (max 5 replies each) | Expensive — per-result calls |

### Cost cap

Hard cap on total extra API calls per enrichment:
- **Neighbor + reply chain IDs**: max 500 per batch (50 results × 10 window)
- **Reply thread fetches**: max 20 per call (beyond that, reply threads skipped with warning)
- **Reply thread replies**: capped at 5 per result via `_fetch_direct_replies(limit=5)`

When caps are hit, the response includes a note: `"Context enrichment partially applied (hit cap). Narrow your query for full context."`

### Design choices

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Window size | Configurable (int param, 1–10) | User's requirement; clamped at param boundary to prevent abuse |
| Reply chain depth | One level only | Recursive is expensive and rarely needed |
| Forum topic scoping | Filter neighbors by `top_msg_id` | Prevents cross-topic context bleed in forum supergroups |
| Service message filter | Skip `reply_to.forum_topic=True` + no displayable text | Uses existing `message_has_displayable_content()` |
| Reply threads | Separate `include_replies` flag (default: False) | Decouples expensive per-result fetches; opt-in to avoid surprising latency |
| Reply thread cap | 5 replies per result | Prevents unbounded response growth |
| Token budget | No limit | User's requirement |
| Failure handling | Partial — return results-without-context on error | Enrichment failure must not lose search results |
| `chat_id` requirement | Required when `context > 0` | Context enrichment needs a specific chat to fetch neighbors from |

### Implementation

Post-processing step after search results are collected:

1. Run search as normal → N results
2. If `context > 0`:
   a. Collect neighbor IDs: for each result, compute `result.id ± context` window
   b. Collect `reply_to_msg_id` values from results
   c. Batch-fetch all needed messages via `_get_messages_by_ids_batched(client, entity, all_ids, CHUNK=100)`
   d. For forum supergroups: filter neighbors by matching `top_msg_id` to result's topic
   e. Build context envelope per result (see below)
3. If `include_replies=True`:
   a. For results with `replies.replies > 0`, call `_fetch_direct_replies(limit=5)` from `replies.py`
   b. Attach to result's context envelope
4. Filter out forum topic service messages using `message_has_displayable_content()`
5. Attach `context` envelope to each result (absent when context=0)

**Failure handling:** wrap steps 2-4 in try/catch. On `FloodWaitError`, include wait duration in warning. Return results without context enrichment on error.

**Timeout budget:** 30s total for enrichment phase. If exceeded, return partial results with warning.

### Result format

When `context=0` (default): no change to existing result format.

When `context > 0`, each result gains a `context` envelope:

```json
{
  "id": 123,
  "text": "the matched message",
  "sender": {...},
  ...existing fields...,
  "context": {
    "before": [{"id": 120, "text": "...", "sender": "..."}, ...],
    "after": [{"id": 124, "text": "...", "sender": "..."}, ...],
    "reply_to": {"id": 100, "text": "...", "sender": "..."} | null,
    "replies": [{"id": 125, "text": "...", "sender": "..."}, ...]
  }
}
```

Context messages use a **lightweight format** (id, date, text, sender_id) to save tokens — not the full message result format.

When `context=0`, the `"context"` key is absent entirely. This groups all enrichment into one namespace, makes it obvious which fields are enrichment vs native, and reduces top-level field count.

### Reply-to field relationship

When context is enabled and `reply_to_message` is resolved:
- `reply_to_msg_id` (existing int field) remains — it's the raw ID for downstream use
- `context.reply_to` (new dict) is the resolved message — lightweight format, populated only when context > 0

## Consequences

### New parameters

- `context: int = 0` on `get_messages` — window size for before/after context (clamped 1–10)
- `include_replies: bool = False` — whether to fetch reply threads (opt-in to avoid surprising latency)
- Only applies to search/browse mode (query + chat_id); ignored for `message_ids` and `reply_to_id` modes
- Enrichment wrapper in `_dispatch_search_mode` — no threading through `_handle_query_mode` or `_collect_messages_in_chat`

### Code changes

- `mcp_tool_types.py` — import existing `ContextWindow` type (already at line 218); add `IncludeReplies` type
- `tools_register.py` — add `context` and `include_replies` params to `get_messages`
- `core.py` — thread both params through `search_messages_impl` and `_build_search_params`
- `search_mode.py` — add enrichment wrapper in `_dispatch_search_mode` after result collection
- `search_generators.py` — no changes (enrichment is post-processing)
- `results.py` — no changes (enrichment builds on existing result format)

### Backward compatibility

- `context=0` (default) = current behavior, no context fields added
- `include_replies=False` (default) = reply threads not fetched unless explicitly requested
- No changes to existing result schema when context is disabled

### Deferred

- Global search context enrichment (requires cross-chat neighbor fetching)
- Recursive reply chain depth
- Parallel reply thread fetches (asyncio.gather for _fetch_direct_replies)

## Resolved findings

### Round 1

| Reviewer | Finding | Resolution |
|----------|---------|------------|
| Simplification | Reply threads should be separate flag | Adopted: `include_replies` flag |
| Simplification | Replace count-based threshold with cost-based cap | Adopted: hard cap on IDs and reply thread fetches |
| Simplification | Merge context_before/after into single list | Adopted: single `context` envelope with `before`/`after` |
| Simplification | Use lightweight context format | Adopted: id, date, text, sender_id only |
| Simplification | Reuse `_fetch_direct_replies` from replies.py | Adopted |
| Simplification | Nest enrichment under `context` envelope | Adopted |
| Edge cases | Reply threads unbounded | Adopted: cap at 5 replies per result |
| Edge cases | No partial-failure resilience | Adopted: try/catch, return results-without-context |
| Edge cases | Context spans unrelated forum topics | Adopted: filter by `top_msg_id` |
| Edge cases | Deleted neighbor message None handling | Adopted: skip None results |
| Edge cases | FloodWaitError from sequential getReplies | Mitigated: sequential but with cap; deferred parallelism |
| Edge cases | No bounds on context parameter | Adopted: cost caps in implementation |

### Round 2

| Reviewer | Finding | Resolution |
|----------|---------|------------|
| Simplification | Thread through 4 files → wrapper in `_dispatch_search_mode` | Adopted: enrichment wrapper, not threaded through query handler |
| Simplification | `ContextWindow` type already exists at mcp_tool_types.py:218 | Adopted: import, don't redefine |
| Edge cases | Clamp `context` at parameter boundary (1–10) | Adopted: clamped in param definition |
| Edge cases | Use batched fetching (CHUNK=100) | Adopted: reuse `_get_messages_by_ids_batched` |
| Edge cases | Propagate `FloodWaitError` with wait duration | Adopted: include in warning message |
| Edge cases | Validate `chat_id` required when `context > 0` | Adopted: validation in `_build_search_params` |
| Edge cases | Default `include_replies` to `False` | Adopted: opt-in to avoid surprising latency |
| Edge cases | Add timeout budget (30s) | Adopted: enrichment phase timeout |

## References

- Slack `assistant.search.context` API
- Anthropic contextual retrieval research
- PIXION sentence-window retrieval
- `src/tools/search/search_generators.py` — search implementation
- `src/tools/search/context_enrichment.py` — post-search context wrapper
- `src/tools/search/replies.py` — `_fetch_replies` for reply thread enrichment
- `src/tools/search/forum_replies.py` — forum-specific reply handling
- Design review notes: [0011-context-enrichment-review.md](../research/0011-context-enrichment-review.md), [0011-review-findings.md](../research/0011-review-findings.md)

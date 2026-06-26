# Design Review: ADR 0011 — Context Enrichment for Message Search Results

**Reviewer:** MiMo (automated review)
**Date:** 2026-06-26
**ADR:** `docs/adr/0011-context-enrichment-for-search.md`
**Source reviewed:**
- `src/tools/search/search_mode.py` — search orchestration
- `src/tools/search/results.py` — result building
- `src/tools/search/replies.py` — reply handling
- `src/tools/search/forum_replies.py` — forum reply logic
- `src/tools/search/core.py` — mode dispatch
- `src/server_components/tools_register.py` — MCP tool surface
- `src/utils/message_format.py` — message formatting & displayability checks
- `src/tools/search/types.py` — constants and types

---

## Finding 1 — Chat boundary: no before/after messages

**Question:** What happens when a search result is at the beginning/end of a chat?

**Severity:** ⚠️ nitpick — likely handled, but ADR should state it explicitly

**Analysis:** The ADR says to compute `result.id ± context` window and batch-fetch all IDs. When a message is at a chat boundary (e.g., ID=5 with context=3 → requesting IDs 2,3,4,6,7,8 where only 5-8 exist), `client.get_messages(entity, ids=[...])` returns `None` for non-existent IDs. The existing `_build_result_for_message` in `results.py` (line 13-14: `if not message: return None`) already filters these out. The `context_before`/`context_after` lists will simply be shorter than the requested window size.

**Recommendation:** Add a sentence to the Implementation section: *"Non-existent neighbor IDs (at chat boundaries) are naturally excluded — `client.get_messages` returns None for missing IDs, and the result builder filters them. `context_before`/`context_after` may be shorter than the requested window."*

---

## Finding 2 — `reply_to_msg_id` points to a deleted message

**Question:** What happens when the message being replied to has been deleted?

**Severity:** 🔴 critical — missing from ADR

**Analysis:** The ADR says (step 6): *"For each result with `reply_to_msg_id`, attach `reply_to_message` from fetched messages."* But if the replied-to message is deleted, `client.get_messages(entity, ids=[deleted_id])` returns `None`. The ADR does not specify what `reply_to_message` should be in this case.

Two sub-concerns:
1. **Null handling:** The ADR's schema says `reply_to_message: dict | None`. This is correct — it should be `None` when the original message is deleted. But the implementation plan should explicitly say: *"If the batch fetch returns None for a `reply_to_msg_id`, set `reply_to_message = None` (message deleted or inaccessible)."*
2. **Distinguishing deleted vs. not-yet-fetched:** There's a subtle risk that a caller conflates "context enrichment was disabled" (`reply_to_message` field absent from the response entirely) with "the replied-to message was deleted" (`reply_to_message: null` present). The ADR should mandate: when `context > 0`, the field `reply_to_message` is **always present** (as `null` or a dict), so its presence signals enrichment was attempted.

**Recommendation:** Add explicit handling note and mandate that `reply_to_message` presence/absence distinguishes "enrichment off" from "deleted message."

---

## Finding 3 — Reply thread with hundreds of replies

**Question:** What happens when `replies.replies` is 500?

**Severity:** 🔴 critical — unbounded data growth

**Analysis:** The ADR says (step 7): *"For each result with `replies.replies > 0`, call `messages.getReplies`."* The ADR's Design Choices table says *"Reply threads: Always included (no opt-in flag)"* and *"Token budget: No limit."*

`messages.getReplies` is a per-result Telegram API call that can return hundreds of messages. Consider:
- 10 search results at the context threshold (max), each with 200 replies → 10 × 200 = **2,000 extra messages** in the response, plus neighbor context.
- `client.iter_messages(entity, reply_to=msg_id)` (used by `_fetch_direct_replies` in `replies.py` line 109-138) has a `limit` parameter. But the ADR does not specify a limit for `reply_thread`.

Additionally, `messages.getReplies` (the raw Telethon method) is **per-result**, not batched. The ADR's cost table acknowledges this: "1 `getReplies` per result — Expensive — per-result calls." But it doesn't propose a mitigation.

**Recommendation:**
1. Add a configurable `reply_thread_limit` (default e.g., 10-20) to cap reply thread size per message. This is critical to prevent token explosion.
2. Consider making reply threads opt-in (a boolean `include_reply_threads: bool = False`) rather than always-on, or at minimum make the limit explicit in the schema.
3. The "Token budget: No limit" decision is risky. Even without a hard token cap, a soft cap on `reply_thread` count is necessary for API-call and response-size sanity.

---

## Finding 4 — Partial failure during context enrichment

**Question:** What happens when some messages fetch successfully and some don't?

**Severity:** 🟡 warning — not addressed in ADR

**Analysis:** The ADR describes a clean happy path but doesn't address failure modes for context enrichment:

1. **Batch fetch of neighbors/reply_to messages (step 4):** If `client.get_messages(entity, ids=all_ids)` throws a `FloodWaitError` or network error, the entire enrichment fails. The ADR should mandate **graceful degradation**: if the batch fetch fails, return the original results without context fields, with a warning.

2. **Reply thread fetch (step 7):** `messages.getReplies` is a per-result call. If it fails for message #3 out of 10, should we:
   - Skip reply_thread for just that message (partial success)?
   - Abort all reply threads?
   - Abort all enrichment?

   The ADR should specify: individual `getReplies` failures produce `reply_thread: []` (empty) for that message with a logged warning, while other messages proceed normally.

3. **Rate limiting:** Multiple `getReplies` calls in sequence risk Telegram's `FloodWaitError`. The existing codebase already handles this (e.g., the transcription cache in `message_format.py`). The ADR should mention that reply thread fetching should respect rate limits and possibly serialize with backoff.

**Recommendation:** Add a "Failure handling" section: neighbor/reply_to batch failures → graceful degradation (return results without context); individual `getReplies` failures → empty `reply_thread` for that message + warning; rate limiting → respect FloodWaitError with backoff.

---

## Finding 5 — Is post-processing in `_handle_query_mode` the right place?

**Question:** Should enrichment live in `_handle_query_mode` or elsewhere?

**Severity:** ⚠️ nitpick — correct placement, but could be cleaner as a separate function

**Analysis:** `_handle_query_mode` in `search_mode.py` is the correct location because:
- It handles both query search and browse mode (no query + chat_id) — both should get context enrichment.
- It has access to the `client`, `entity` (via chat_id), and the collected result window.
- It runs after results are collected but before the response is returned (line 210+ in search_mode.py).

However, `_handle_query_mode` is already ~150 lines with complex control flow (global vs. chat search, error handling, date validation, etc.). Adding 60-100 lines of enrichment logic directly would make it harder to maintain.

**Recommendation:** Extract context enrichment into a separate `async def _enrich_with_context(window, client, entity, context_size)` function called from `_handle_query_mode`. This keeps the function focused and testable. The ADR's code changes section already lists `search_mode.py` as the target, but should specify the extraction pattern.

---

## Finding 6 — Should context enrichment work for browse mode (no query)?

**Question:** Browse mode = `chat_id` without `query` (fetch latest messages). Should it get context?

**Severity:** 🟢 info — ADR correctly includes it, but could be more explicit

**Analysis:** Looking at `_handle_query_mode` (search_mode.py), when `chat_id` is set and `query` is empty/None, the code falls through to `_collect_messages_in_chat` with `queries = [""]`, which effectively does a browse (latest messages). The ADR says context *"applies to search/browse mode (query + chat_id)"* — but the parenthetical "(query + chat_id)" is misleading because browse mode has **no query**. The actual intent (context works for any `_handle_query_mode` call) is correct.

Browse mode would benefit from context because the user might be viewing recent messages and want to understand conversation flow.

**Recommendation:** Clarify the ADR language to: *"Only applies to SEARCH mode (chat_id with or without query); ignored for MESSAGE_IDS and REPLIES modes."* The existing code path handles this correctly — no functional change needed.

---

## Finding 7 — Context threshold of 10: too aggressive or too lenient?

**Question:** Is automatically disabling context when results > 10 the right trade-off?

**Severity:** 🟡 warning — reasonable default but should be overridable

**Analysis:** Let's do the math:

| Results | Context | Neighbors fetched | Reply_to fetched | Reply threads | Total extra API calls |
|---------|---------|-------------------|------------------|---------------|-----------------------|
| 5       | 3       | 1 batched         | 1 batched        | ≤5 getReplies | ≤7                   |
| 10      | 3       | 1 batched         | 1 batched        | ≤10 getReplies| ≤12                  |
| 10      | 5       | 1 batched         | 1 batched        | ≤10 getReplies| ≤12                  |
| 20      | 3       | 1 batched         | 1 batched        | ≤20 getReplies| ≤22                  |
| 50      | 3       | 1 batched         | 1 batched        | ≤50 getReplies| ≤52                  |

The neighbor and reply_to fetches are batched (cheap), so the real cost driver is per-result `getReplies` calls. At 10 results, that's up to 10 extra API calls — significant but manageable. At 20, it's 20 — still within reason for a thorough search.

However:
- The threshold is **hardcoded and non-overridable**. A user who explicitly wants context on 15 results gets nothing.
- The "N results > 10" message suggests narrowing the query, which is a UX workaround, not a fix.
- With `limit=50` (the default in `tools_register.py`), most non-trivial searches will exceed 10 results, making context enrichment almost never activate for the default case.

**Recommendation:**
1. Make the threshold configurable with a sensible default (e.g., `context_max_results: int = 15`), or
2. Keep the hard threshold but allow the user to override it via an explicit `force_context: bool = False` parameter, or
3. Only disable reply thread fetching above the threshold (neighbors and reply_to are cheap and should always work).
4. Consider: at minimum, the response should still include `reply_to_message` even when the threshold kicks in, since that's a single batched call regardless of result count.

---

## Finding 8 — Global search context enrichment (no chat_id)

**Question:** Should context enrichment work for global search?

**Severity:** 🟢 info — correctly deferred

**Analysis:** Global search returns results from multiple chats. Context enrichment would require:
1. Grouping results by `chat_id`.
2. Running per-chat batch fetches for neighbors (can't batch across chats).
3. Potentially resolving chat entities for each result (already done in `_process_raw_message`).

This is a non-trivial architectural extension. The ADR correctly defers it.

One concern: the ADR's `_handle_query_mode` handles both per-chat and global search. If context enrichment is added as post-processing in `_handle_query_mode`, it needs to detect whether this is a global search and skip enrichment. The ADR should mention this guard: *"Context enrichment is skipped when `chat_id is None` (global search)."*

**Recommendation:** Add a guard clause note in the Implementation section. No functional change needed for the current proposal.

---

## Finding 9 — Forum topic filtering: "service message with no displayable text"

**Question:** What exactly gets filtered? Is the condition correct?

**Severity:** 🟡 warning — condition may be too broad or self-contradictory

**Analysis:** The ADR says: *"Filter service messages: `reply_to.forum_topic=True` + no displayable text = skip."*

Looking at `message_has_displayable_content` in `message_format.py` (line 79-87):
```python
def message_has_displayable_content(message):
    if (getattr(message, "text", None) or getattr(message, "message", None) 
            or getattr(message, "caption", None)):
        return True
    if _has_any_media(message):
        return True
    return _service_action_placeholder_text(message) is not None
```

And `_service_action_placeholder_text` (line 11-20) returns a placeholder like `"[Service: ChatTitle]"` for **any** message with an `action` field.

So a service message **always** has "displayable content" (the placeholder text). The condition "forum_topic=True + no displayable text" would **never** be true for service messages, because the service placeholder always counts as displayable.

The intent is likely to filter **forum topic creation stubs** or other structural messages that are technically messages but not useful context. Two interpretations:
1. Filter messages where `reply_to.forum_topic=True` and the message is a "structural" topic marker (e.g., the topic creation message with only a service action and no real conversation content).
2. Filter messages that are empty (no text/media/service) in forum contexts — but these don't exist in Telegram's data model.

The actual risk: **the filter never fires**, so forum topic service messages appear as context neighbors and add noise. This is a minor UX issue (a `[Service: TopicCreated]` message in context_before isn't harmful, just unhelpful).

**Recommendation:** Clarify the filtering intent:
- If the goal is to exclude **service actions** (title changes, pins, etc.) from context, the condition should be: `reply_to.forum_topic=True` and `_service_action_placeholder_text(message) is not None` (i.e., it IS a service message).
- If the goal is to exclude **empty/structural** messages, the condition should be: `not message.text and not message.message and not message.caption and not _has_any_media(message)` — i.e., messages with only a service placeholder.
- Either way, the ADR should use the existing `message_has_displayable_content` and/or `_service_action_placeholder_text` functions explicitly rather than describing a condition that doesn't match the implementation.

---

## Finding 10 — Circular reply chains (A→B→A)

**Question:** Can we get infinite loops when following reply chains?

**Severity:** 🟢 info — correctly mitigated by "one level only"

**Analysis:** The ADR states: *"Reply chain depth: One level only."* This means for each search result, we only follow `reply_to_msg_id` once. If message A replies to B, and B replies to A, we:
1. Start at A, see `reply_to_msg_id = B`.
2. Fetch B (one level).
3. **Stop** — we don't follow B's `reply_to_msg_id` back to A.

This is correct. No circular dependency risk.

However, the ADR says step 6 is: *"For each result with `reply_to_msg_id`, attach `reply_to_message` from fetched messages."* It's unclear whether the **fetched reply_to_message itself** should also get its own `reply_to_message` nested inside. If the schema is flat (each result has `reply_to_message: dict` but that dict doesn't itself have a `reply_to_message` sub-field), then there's no recursion risk. If the schema allows nesting, we need a depth marker.

Looking at the proposed schema: `reply_to_message: dict | None | "The message this one replies to (one level)"`. The dict would be built using `build_message_result`, which does include `reply_to_msg_id` as a field. But the context enrichment step wouldn't recursively enrich the nested dict — it would just build it from the raw Telethon message. So the nested `reply_to_message` would just be a plain message dict, not further enriched. This is safe.

**Recommendation:** No change needed. Just confirm in the ADR that `reply_to_message` is built using the existing `build_message_result` (which includes `reply_to_msg_id` but not a nested `reply_to_message` field).

---

## Summary

| # | Issue | Severity | Action |
|---|-------|----------|--------|
| 1 | Chat boundary handling | nitpick | Document explicitly |
| 2 | Deleted reply_to message | **critical** | Add null-handling note + presence/absence semantics |
| 3 | Unbounded reply threads | **critical** | Add `reply_thread_limit` or make reply threads opt-in |
| 4 | Partial enrichment failure | **warning** | Add graceful degradation section |
| 5 | Placement in `_handle_query_mode` | nitpick | Extract to helper function |
| 6 | Browse mode context | info | Clarify language (already works correctly) |
| 7 | Threshold of 10 is non-overridable | **warning** | Make configurable or partially override for cheap operations |
| 8 | Global search guard | info | Add guard clause note |
| 9 | Forum topic filter condition is self-contradictory | **warning** | Fix the condition to match intent |
| 10 | Circular reply chains | info | Correctly mitigated, no action needed |

### Blocking issues before approval

1. **Finding 3** (reply thread size) must be addressed — add a limit.
2. **Finding 2** (deleted reply_to) must be specified explicitly.
3. **Finding 9** (forum filter) should be corrected to match the implementation reality.

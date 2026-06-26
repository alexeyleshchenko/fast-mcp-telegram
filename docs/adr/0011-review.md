# ADR-0011 Design Review: Context Enrichment for Search Results

**Reviewer:** MiMo (ADR review sub-agent)
**Date:** 2026-06-26
**Status:** Found issues requiring revision before implementation

---

## Finding 1 — Neighbor fetching strategy is fundamentally broken for non-contiguous IDs

**Severity:** ⚠️ **Warning**

The ADR's implementation step 2 says:

> Collect all neighbor IDs: for each result, compute `result.id ± context` window

This assumes Telegram message IDs are contiguous within a chat. They are not. Deleted messages, service messages, and Telegram-internal allocations create gaps. If message ID 100 exists and IDs 97–99 were deleted, computing `100 - 3` and fetching IDs {97, 98, 99} will return 0 useful neighbors — not 3 preceding messages.

The codebase already uses the correct pattern everywhere: `client.iter_messages(entity, offset_id=N, limit=K)` which returns the K most recent messages *positionally* before offset_id, respecting actual message order. See `search_generators.py:50–54`, `replies.py:120–122`, and `forum_replies.py:114–117`.

**Recommendation:** Replace `id ± context` arithmetic with:
```python
# "Before" context:
before = await client.get_messages(entity, offset_id=result.id, limit=context)
# "After" context (reverse=False from oldest to newest):
after = await client.get_messages(entity, offset_id=result.id + 1, limit=context, reverse=True)
```
The batch-optimization in step 4 ("one batched `getMessages` call") then becomes per-entity `iter_messages` calls — still batchable across results in the same chat, but not a single `get_messages(ids=...)` call.

---

## Finding 2 — No reply thread size cap; "always include" is unbounded

**Severity:** 🔴 **Critical**

ADR design choice:

> Reply threads: Always included (no opt-in flag)

Combined with:

> Token budget: No limit

This means a search result from a viral announcement channel could have 500+ replies. If 5 of 50 results are popular posts, the response would balloon to thousands of context messages, crushing both the Telegram API latency and the LLM's token window.

The cost analysis table notes "1 `getReplies` per result — Expensive" but doesn't quantify the data volume risk. A single `messages.getReplies` call can return up to ~100 messages per page, and threads with thousands of replies require pagination.

**Recommendation:** Add a `reply_limit` parameter (int, default ~10) to cap how many reply thread messages are returned per result. Document that this is per-result, not total. This is a minimal change that prevents catastrophic responses without losing the "always include" intent.

---

## Finding 3 — Reply thread fetching is N serial API calls (performance cliff)

**Severity:** 🔴 **Critical**

Step 7 says:

> Fetch reply threads: for results with `replies.replies > 0`, call `messages.getReplies`

For a result set of 50 messages, if 30 have replies, this is 30 sequential `getReplies` calls. At ~200ms each (optimistic), that's ~6 seconds of added latency — before accounting for rate limits or Telegram server slowness.

The codebase handles analogous patterns by parallelizing: `_collect_messages_global` uses `asyncio.gather` with semaphore-bounded concurrency (`search_mode.py:263–266`). The ADR doesn't mention parallelization at all.

**Recommendation:** Parallelize reply thread fetches using `asyncio.gather` (or `asyncio.TaskGroup` for Python 3.11+), bounded by a semaphore. The existing `_DEFAULT_MAX_CONCURRENT = 2` pattern is a good model. With parallelism and a `reply_limit`, 30 threads could complete in ~1–2s instead of ~6s.

---

## Finding 4 — Global search cross-chat enrichment is silently broken

**Severity:** 🔴 **Critical**

The implementation steps describe a single batched `client.get_messages(entity, ids=all_ids)` call (step 4). But global search (`chat_id=None`) returns results from multiple chats. `get_messages` requires a single entity — you cannot batch IDs from different chats into one call.

The ADR defers "Global search context enrichment (requires cross-chat neighbor fetching)" in the Consequences section, but the implementation steps (1–9) don't mention this restriction. An implementer reading steps 1–9 would attempt to batch-fetch neighbors across chats and hit runtime errors.

**Recommendation:** Either:
- (a) Add an explicit guard: "When `chat_id is None`, context enrichment is skipped. Document this in the parameter description."
- (b) Add a grouping step: group results by `chat_id`, then batch-fetch per group. This is more work but makes global search enrichment functional.

Option (a) is simpler and matches the "deferred" note. Either way, the implementation steps must mention the constraint.

---

## Finding 5 — Deleted reply target silently produces `None`

**Severity:** ⚠️ **Warning**

Step 6 says:

> For each result with `reply_to_msg_id`, attach `reply_to_message` from fetched messages

But if the replied-to message was deleted, the batch fetch returns `None` for that ID. The ADR doesn't specify:
- Whether `reply_to_message` should be `None` (intuitive) or omitted entirely
- Whether to include `reply_to_msg_id: <id>` alongside `reply_to_message: null` so the consumer knows *which* message was deleted

The existing code in `build_message_result` (`message_format.py:557–561`) includes `reply_to_msg_id` as an int field unconditionally when present. The enrichment should follow the same pattern: always include `reply_to_msg_id` in the result, set `reply_to_message` to `None` when the target is missing/deleted.

**Recommendation:** Add explicit handling: `reply_to_message = fetched.get(reply_to_msg_id)` where `fetched` is a dict built from the batch. Missing keys produce `None` naturally.

---

## Finding 6 — `context=0` must truly omit enrichment keys from response

**Severity:** ⚠️ **Warning**

The ADR says:

> `context=0` (default) = current behavior, no context fields added
> No changes to existing result schema when context is disabled

This is correct in intent, but the implementation must be careful. If the enrichment code adds `context_before`, `context_after`, `reply_to_message`, and `reply_thread` keys to every result dict *regardless* of context value, consumers will see new keys they didn't ask for.

The existing codebase follows a "conditional key presence" pattern: `reply_to_msg_id` only appears when the message is a reply (`message_format.py:560–561`), `topic_id` only when applicable. The enrichment should follow the same pattern: skip the entire enrichment post-processing when `context=0`, not just set fields to empty lists.

**Recommendation:** Gate the enrichment with `if context > 0:` at the call site in `_handle_query_mode`. When disabled, no enrichment code runs and no new keys appear in results.

---

## Finding 7 — Placing enrichment in `_handle_query_mode` is correct but needs extraction

**Severity:** 💬 **Nitpick**

The ADR correctly places enrichment as post-processing in `_handle_query_mode` rather than in `search_generators.py`. Rationale:
- Generators yield individual results; enrichment needs the full result set (for batch-optimizing neighbor fetches)
- Generators deal with raw Telethon objects; enrichment operates on result dicts
- The function already has post-processing patterns (voice transcription at line ~190, response_attachment_warning at ~220)

However, `_handle_query_mode` is already 130+ lines. Adding 60–80 lines of enrichment logic would make it unwieldy.

**Recommendation:** Extract enrichment into a standalone `async def _enrich_with_context(results, client, entity, context_size)` function. Call it from `_handle_query_mode` between the result collection and response assembly.

---

## Finding 8 — Deduplication of neighbor IDs across overlapping windows

**Severity:** 💬 **Nitpick**

If search returns results at IDs 100, 102, and 105 with context=3, the neighbor sets overlap heavily (e.g., IDs 99, 101, 103 appear in multiple windows). The ADR step 2 says "collect all neighbor IDs" and step 4 says "one batched call," implying deduplication — but this is never explicitly stated.

With `iter_messages`-based fetching (see Finding 1), deduplication is per-entity: you only need one `iter_messages` call per unique `(entity, offset_id)` pair. Since results in the same chat share the entity, a smart implementation would sort results by ID and compute a union of non-overlapping windows.

**Recommendation:** Add a note: "Deduplicate neighbor IDs before fetching. For results in the same chat, sort by ID and merge overlapping windows."

---

## Finding 9 — Forum service message filter needs more specificity

**Severity:** 💬 **Nitpick**

ADR step 8:

> Filter out forum topic service messages (`reply_to.forum_topic=True` + no displayable text)

This is vague. The codebase has `message_has_displayable_content()` in `message_format.py` which already handles this filtering. The ADR should reference this existing function rather than inventing a new filter criterion.

Also, this filter applies to which messages?
- Neighbor context messages? (Probably yes — service messages in context are noise)
- Reply thread messages? (Possibly — depends on use case)
- Reply-to message? (No — the message you're replying to should always be shown)

**Recommendation:** Specify that the filter applies to context_before/context_after and reply_thread messages, using the existing `message_has_displayable_content()` function. The reply_to_message should be exempt (show it even if it's a service message, so the consumer knows the reply target exists).

---

## Summary

| # | Finding | Severity | Action |
|---|---------|----------|--------|
| 1 | ID ± context is wrong; use iter_messages | ⚠️ Warning | Fix before implementation |
| 2 | No reply thread size cap | 🔴 Critical | Add `reply_limit` param |
| 3 | N serial getReplies calls | 🔴 Critical | Parallelize with semaphore |
| 4 | Global search cross-chat batching | 🔴 Critical | Add explicit guard or grouping |
| 5 | Deleted reply target → None handling | ⚠️ Warning | Document None semantics |
| 6 | context=0 must omit enrichment keys | ⚠️ Warning | Gate with `if context > 0` |
| 7 | Extract enrichment from _handle_query_mode | 💬 Nitpick | Separate function |
| 8 | Dedup neighbor IDs across windows | 💬 Nitpick | Add dedup note |
| 9 | Forum filter scope and function reference | 💬 Nitpick | Reference existing helper |

**Verdict:** The ADR's high-level design (post-processing enrichment, new fields, backward compat) is sound. The implementation steps need revision: findings 1–4 are blocking issues that would cause incorrect behavior or production failures if implemented as written. The fix for each is straightforward; the overall architecture doesn't need to change.

# ADR 0011 — Context Enrichment: Edge Cases & Failure Mode Review

**Reviewer:** MiMo sub-agent  
**Date:** 2026-06-26  
**Status:** Thorough review of `docs/adr/0011-context-enrichment-for-search.md` against implementation in `src/tools/search/` and `src/server_components/`.

---

## Summary

The ADR proposes a sound high-level design but has **3 critical gaps**, **5 warnings**, and **3 nitpicks** that would cause production failures or degraded behavior. The most severe issues are: (1) forum topic context windows spanning unrelated topics, (2) unbounded reply thread fetches, and (3) no partial-failure resilience in the enrichment pipeline.

---

## Findings

### 1. Deleted / missing neighbor messages

**[EDGE CASE]** When the batch-fetch in step 4 (`client.get_messages(entity, ids=all_ids)`) includes IDs for deleted or inaccessible messages, Telethon returns `None` in that slot of the result list. The ADR says "build `context_before[context]` and `context_after[context]` from fetched neighbors" but does not mention filtering `None` values from the batch response or building a lookup dict keyed only on valid messages. — **severity: warning** — **Suggested mitigation:** Build a `dict[int, Message]` from the batch result, skipping `None` entries. When assembling context_before/after, skip IDs not in the lookup dict. This pattern already exists in `forum_replies.py::_get_messages_by_ids_batched` (`messages.extend(message for message in loaded if message)`) and `reading.py::_find_message_by_id`, so reuse or mirror it.

---

### 2. reply_to_msg_id pointing to a different chat

**[COVERED]** In Telegram's MTProto protocol, `reply_to_msg_id` always references a message within the same `InputPeer` (entity). Cross-chat replies do not exist — the discussion-group pattern uses a separate `GetDiscussionMessageRequest` to map channel-post IDs to discussion-group IDs, which is unrelated to `reply_to_msg_id` in search results. The batch-fetch scoped to a single `entity` is correct by design.

---

### 3. Context window extending beyond chat history

**[EDGE CASE]** For the first or last messages in a chat, the ±N window produces IDs ≤ 0 or IDs beyond the chat's highest message. Telethon returns `None` for these. The ADR does not document this expected behavior or specify that `context_before`/`context_after` may contain fewer than `context` entries. — **severity: warning** — **Suggested mitigation:** Document in the ADR that context lists may be shorter than `context` at conversation boundaries. In the implementation, the `None`-filtering from finding #1 handles this automatically; no special logic needed beyond that.

---

### 4. Very long reply threads (100+ replies)

**[EDGE_CASE]** The ADR specifies "Fetch reply threads: for results with `replies.replies > 0`, call `messages.getReplies`" with no per-thread cap. In Telegram, channel comment sections commonly have hundreds or thousands of replies. With 10 results (the context threshold), each having 500 replies, the response would contain ~5,000 extra message dicts — likely 2–5 MB of JSON. The "Token budget: No limit" design stance does not protect against this specific unbounded source. — **severity: critical** — **Suggested mitigation:** Add a configurable `reply_thread_limit` parameter (default e.g. 10–20). The ADR already acknowledges "Configurable reply thread limit" as deferred; it should be promoted to the initial implementation. At minimum, add a hardcoded safety cap (e.g. 50) even if not user-configurable.

---

### 5. Forum topic service messages as only available context

**[EDGE_CASE]** The ADR's filter rule is: "reply_to.forum_topic=True + no displayable text = skip." However, `message_has_displayable_content()` in `message_format.py` returns `True` for any message with a service action (via `_service_action_placeholder_text` which generates `[Service: ChatTitleChanged]` etc.). So service messages **do** pass the "has displayable content" check and would **not** be filtered by the ADR's rule as written.

Concretely: a user in a forum topic might get `context_before` filled with `[Service: TopicCreated]`, `[Service: ChatTitleChanged]`, `[Service: PinMessage]` — technically displayable but not useful conversation context. — **severity: warning** — **Suggested mitigation:** Refine the filter to: skip messages where `message.action is not None` AND the message has no user-authored text (`not message.text and not message.message and not message.caption`). Alternatively, filter messages whose only "text" is a `[Service: ...]` placeholder.

---

### 6. Messages with no sender (anonymous, deleted user)

**[COVERED]** The existing `get_sender_info()` in `message_format.py` returns `None` when `message.sender_id` is absent, and `build_message_result()` includes `"sender": null`. Context messages built through the same function inherit this behavior. Anonymous admins in groups and messages from deleted accounts will produce `sender: null` in context dicts — correct and consistent.

---

### 7. Context enrichment partial failures

**[EDGE_CASE]** The ADR describes enrichment as a linear pipeline (steps 2→9). If any step throws, the exception propagates through `_handle_query_mode` and the **entire search result is lost** — the user gets an error instead of results-without-context. Specific failure scenarios:

- **Batch fetch fails** (network error, entity inaccessible): All context lost, but search results are already collected.
- **`getReplies` fails for one result** (e.g., message was a channel post with discussion disabled, or a FloodWaitError): All reply threads lost for remaining results too if unhandled.
- **`getReplies` returns an RPCError** for one message (e.g., `MSG_ID_INVALID` for a just-deleted message): One failure kills all enrichment.

— **severity: critical** — **Suggested mitigation:** Wrap each enrichment step in try/except. For the batch-fetch, catch the exception and return results with empty context + a `_warning` field. For individual `getReplies` calls, catch per-result and set `reply_thread: null` with an error note. This mirrors the existing pattern in `search_generators.py` (`except Exception as e: logger.warning(...); continue`).

---

### 8. Context window spanning forum topics

**[EDGE_CASE]** In forum-enabled supergroups, all topics share a single sequential message ID space. Messages from different topics are interleaved in the ID sequence. Computing `result.id ± N` produces neighbor IDs that may belong to **entirely different topics** with unrelated conversations.

Example: Message 500 is in "General" topic. Message 499 is in "Off-Topic" topic. Message 501 is in "General". With `context=2`, the context_before for message 501 includes message 499 from "Off-Topic" — a completely unrelated conversation.

This is a **fundamental design issue** for forum groups. The ADR does not mention topic-aware context filtering. — **severity: critical** — **Suggested mitigation:** After fetching neighbors, filter context_before/after to only include messages that share the same `reply_to.reply_to_top_id` (topic root) as the result message. For non-forum chats, no filtering is needed. The existing `_extract_topic_metadata()` function provides the `topic_id` for this check. Messages without topic metadata (i.e., in the main chat timeline, not a topic) should be included when the result itself is also topic-less.

---

### 9. Race conditions — messages deleted between search and enrichment

**[EDGE_CASE]** TOCTOU window between step 1 (search returns result IDs) and step 4 (batch-fetch those IDs + neighbors). If a message is deleted in that window:

- **Result message deleted:** Already in the collected list; the batch-fetch returns `None` for it. Neighbor context still works (the result's ID is still valid for computing neighbor ranges). But `getReplies` for the deleted message would return empty or error.
- **Neighbor message deleted:** Returns `None` in batch; handled by #1.
- **Reply target deleted:** Returns `None` in batch; `reply_to_message` would be `None`. The ADR says `reply_to_message: dict | None`, so this is fine structurally.

— **severity: warning** — **Suggested mitigation:** Same as #7 — per-step try/except with graceful degradation. Additionally, after building the batch-fetch lookup dict, check that each result's `reply_to_msg_id` is present; if not, set `reply_to_message: null` instead of crashing.

---

### 10. Rate limiting — FloodWaitError from enrichment API calls

**[EDGE_CASE]** The ADR's cost analysis counts API calls but does not address Telegram rate limits. The enrichment pipeline makes:

| Call | Count | Rate-limit risk |
|------|-------|-----------------|
| `getMessages(ids=...)` | 1 | Low (single batched call) |
| `getReplies` per result | Up to 10 | **High** — each is a separate RPC call |

For 10 results in an active group, 10 sequential `getReplies` calls can easily trigger a `FloodWaitError`. The existing codebase handles `FloodWaitError` in `message_format.py` (voice transcription) and `contact_search.py`, but the enrichment path (which doesn't exist yet) has no such handling.

— **severity: warning** — **Suggested mitigation:** (1) Wrap `getReplies` calls in try/except for `FloodWaitError`; on catch, skip the reply thread for that result and log a warning. (2) Consider parallelizing `getReplies` with `asyncio.gather(return_exceptions=True)` to reduce wall-clock time. (3) Add an exponential backoff or at minimum respect the `FloodWaitError.seconds` value before retrying (or not retrying at all for enrichment — just skip).

---

### 11. No deduplication of neighbor IDs before batch fetch

**[EDGE_CASE]** When search results are close together (e.g., messages 100 and 102 with `context=3`), their ±N windows overlap: 97–103 and 99–105. The combined `all_ids` list contains duplicates (99, 100, 101, 102, 103 appear twice). While Telethon's `get_messages(ids=...)` handles duplicates without error, it wastes API bandwidth and processing. — **severity: nitpick** — **Suggested mitigation:** Deduplicate `all_ids` using a `set()` before the batch fetch. The response may still contain duplicates at the Telethon level (it returns one result per requested ID), so build the lookup dict as `{msg.id: msg for msg in fetched if msg}`.

---

### 12. No validation on `context` parameter value

**[EDGE_CASE]** The ADR specifies `context: int` with no stated bounds. Potential values:
- `context < 0`: Should be an error or treated as 0.
- `context = 0`: Disabled (correct).
- `context = 1000`: Extreme window — 10 results × 2001 IDs = 20,010 messages to fetch, plus 10 `getReplies` calls. Would almost certainly hit API limits and produce an unusably large response.

— **severity: warning** — **Suggested mitigation:** Validate `context` in the range `[0, max_context]` where `max_context` is a reasonable constant (e.g., 10 or 20). Return an error for out-of-range values. The existing `ContextWindow` Annotated type in `mcp_tool_types.py` should add `ge=0, le=20` constraints.

---

### 13. Reply thread fetches are sequential, not parallelized

**[EDGE_CASE]** The ADR says "1 getReplies per result" but does not specify whether these run sequentially or in parallel. For 10 results, sequential execution means O(10 × latency) for the reply-thread step alone. With typical Telegram API latency of 200–500ms, that's 2–5 seconds of additional wait. — **severity: nitpick** — **Suggested mitigation:** Use `asyncio.gather(*[getReplies(r) for r in results_with_replies], return_exceptions=True)` to parallelize reply-thread fetches. This matches the existing pattern in `_collect_messages_global` which parallelizes `SearchGlobalRequest` calls.

---

### 14. Context messages' output format not specified

**[EDGE_CASE]** The ADR says context messages are attached as `context_before: list[dict]` and `context_after: list[dict]` but does not specify which fields each dict contains. If built through `build_message_result()`, each context message includes links, media placeholders, forward info, reply markup — expensive to compute (link generation requires `generate_telegram_links`) and expensive to serialize.

For 10 results × 7 context messages (context=3) = 70 extra fully-enriched message dicts. — **severity: warning** — **Suggested mitigation:** Define a lightweight context message format: `{id, date, text, sender}` only. Skip link generation, media placeholders, and forward info for context messages. This dramatically reduces token count while preserving conversational understanding. Include a `_context_summary` field noting the lightweight format. Alternatively, make full vs. light context configurable.

---

## Summary Table

| # | Finding | Type | Severity |
|---|---------|------|----------|
| 1 | Deleted/missing neighbor messages need `None` filtering | EDGE CASE | warning |
| 2 | Cross-chat reply_to_msg_id | COVERED | — |
| 3 | Window beyond history bounds → short context lists | EDGE CASE | warning |
| 4 | Unbounded reply thread size | EDGE CASE | **critical** |
| 5 | Service message filter logic mismatch with `message_has_displayable_content` | EDGE CASE | warning |
| 6 | Sender-less messages | COVERED | — |
| 7 | No partial-failure resilience in enrichment pipeline | EDGE CASE | **critical** |
| 8 | Context window spans unrelated forum topics | EDGE CASE | **critical** |
| 9 | TOCTOU race between search and enrichment | EDGE CASE | warning |
| 10 | FloodWaitError from sequential getReplies calls | EDGE CASE | warning |
| 11 | Duplicate neighbor IDs in batch fetch | EDGE CASE | nitpick |
| 12 | No bounds validation on `context` parameter | EDGE CASE | warning |
| 13 | Reply thread fetches not parallelized | EDGE CASE | nitpick |
| 14 | Context message format unspecified (token-heavy vs. lightweight) | EDGE CASE | warning |

---

## Recommendations for ADR revision

1. **Promote reply thread limit from deferred to initial** — even a hardcoded cap of 20 replies per thread prevents catastrophic token explosion.
2. **Add forum-topic-aware neighbor filtering** — the most architecturally significant gap. Without this, context enrichment in forum groups actively harms signal-to-noise ratio.
3. **Add a partial-failure design section** — enrichment must be fire-and-forget: search results are the primary deliverable; context is best-effort.
4. **Specify the context message format** — decide between full and lightweight dicts; document the choice.
5. **Add `FloodWaitError` handling** to the implementation plan, not just as an afterthought.

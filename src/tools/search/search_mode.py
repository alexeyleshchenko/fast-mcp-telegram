"""Query and browse mode for get_messages (MessageRetrievalMode.SEARCH)."""

import asyncio
import logging
import time
from datetime import datetime
from typing import Any

from telethon.errors import FloodWaitError
from telethon.tl.functions.messages import SearchGlobalRequest
from telethon.tl.types import InputMessagesFilterEmpty, InputPeerEmpty

from src.client.connection import get_connected_client
from src.utils.datetime_parse import parse_iso_datetime_utc
from src.utils.entity import (
    _get_chat_message_count,
    _matches_chat_type,
    _matches_public_filter,
    get_entity_by_id,
)
from src.utils.error_handling import log_and_build_error, log_connection_error_response
from src.utils.helpers import _append_dedup_until_limit
from src.utils.message_format import (
    _extract_topic_metadata,
    message_has_displayable_content,
    response_attachment_warning,
    transcribe_voice_messages,
)

from . import results
from .forum_replies import _get_messages_by_ids_batched
from .replies import _fetch_direct_replies, _fetch_replies
from .types import ThreadScope
from .search_generators import _search_chat_messages_generator

logger = logging.getLogger(__name__)

# Default semaphore limits for global search parallelization
_DEFAULT_MAX_CONCURRENT: int = 2

# Context enrichment caps
_CONTEXT_TIMEOUT_BUDGET = 30  # seconds for entire enrichment phase
_CONTEXT_MAX_IDS = 500  # max neighbor + reply_to IDs per batch
_CONTEXT_MAX_REPLY_FETCHES = 20  # max _fetch_direct_replies calls
_CONTEXT_REPLY_LIMIT = 5  # max replies per result
_CONTEXT_PER_REPLY_TIMEOUT = 10  # seconds per individual reply fetch


def _is_valid_context_neighbor(
    message, is_forum: bool, result_topic_id: int | None
) -> bool:
    """Check if a neighbor message is valid for context inclusion."""
    if not message:
        return False
    if not message_has_displayable_content(message):
        return False
    if is_forum:
        neighbor_meta = _extract_topic_metadata(message)
        neighbor_topic = neighbor_meta.get("topic_id")
        if result_topic_id is not None:
            return neighbor_topic == result_topic_id
        return neighbor_topic is None
    return True


def _lightweight_from_raw(message) -> dict[str, Any]:
    """Build lightweight context dict from a raw Telethon message."""
    full_text = (
        getattr(message, "text", None)
        or getattr(message, "message", None)
        or getattr(message, "caption", None)
    )
    # Include media type hint when text is absent
    if not full_text:
        media = getattr(message, "media", None)
        if media:
            full_text = f"[{type(media).__name__}]"
    return {
        "id": message.id,
        "date": message.date.isoformat() if getattr(message, "date", None) else None,
        "text": full_text,
        "sender_id": getattr(message, "sender_id", None),
    }


def _lightweight_from_result(result: dict[str, Any]) -> dict[str, Any]:
    """Build lightweight context dict from a result dict."""
    sender = result.get("sender")
    sender_id = sender.get("id") if isinstance(sender, dict) else None
    return {
        "id": result.get("id"),
        "date": result.get("date"),
        "text": result.get("text"),
        "sender_id": sender_id,
    }


async def _enrich_with_context(
    client,
    entity,
    messages: list[dict[str, Any]],
    context: int,
    include_reply_threads: bool = False,
) -> tuple[list[dict[str, Any]], str | None]:
    """Enrich search results with surrounding context and optional reply threads.

    Adds a 'context' envelope to each result with:
    - before[]: N messages before each result (lightweight format)
    - after[]: N messages after each result (lightweight format)
    - reply_to: the message being replied to (lightweight format, or null)
    - replies[]: direct replies (if include_reply_threads=True, max 5)

    Caps: max 500 IDs, max 20 reply thread fetches, 30s timeout budget.
    Partial failure: returns results without context on error.
    Returns: (messages, warning_or_none) — warning set when IDs were capped.
    """
    if not messages:
        return messages, None

    start_time = time.monotonic()
    is_forum = bool(getattr(entity, "forum", False))

    # Step 1: Collect all unique IDs needed
    all_ids: set[int] = set()
    result_ids: set[int] = set()
    reply_to_ids: set[int] = set()

    for msg in messages:
        msg_id = msg.get("id")
        if not msg_id:
            continue
        result_ids.add(msg_id)
        for offset in range(1, context + 1):
            neighbor_id = msg_id - offset
            if neighbor_id > 0:
                all_ids.add(neighbor_id)
            all_ids.add(msg_id + offset)
        rt = msg.get("reply_to_msg_id")
        if rt:
            reply_to_ids.add(rt)

    # Include reply_to for resolution
    all_ids.update(reply_to_ids)
    # Result IDs needed for reply count checking only when reply threads requested
    if include_reply_threads:
        all_ids.update(result_ids)
    all_ids.discard(0)

    # Track original count before cap mutation for correct warning
    original_count = len(all_ids)
    warning = None

    # Cap total IDs — prioritize reply_to and result IDs over neighbors
    if original_count > _CONTEXT_MAX_IDS:
        neighbor_ids = all_ids - reply_to_ids - result_ids
        all_ids = reply_to_ids | result_ids
        remaining = _CONTEXT_MAX_IDS - len(all_ids)
        if remaining > 0:
            all_ids.update(list(neighbor_ids)[:remaining])
        warning = (
            f"Context enrichment: {original_count} unique message IDs exceeded "
            f"the {_CONTEXT_MAX_IDS} cap; some context may be missing."
        )
        logger.warning("Context enrichment: capped IDs to %d (was %d)", _CONTEXT_MAX_IDS, original_count)

    # Step 2: Batch-fetch all needed messages
    try:
        raw_messages = await _get_messages_by_ids_batched(
            client, entity, list(all_ids)
        )
    except FloodWaitError as e:
        wait = getattr(e, "seconds", 0) or 0
        logger.warning("Context enrichment FloodWaitError: wait %ds", wait)
        return messages, None
    except Exception as e:
        logger.warning("Context enrichment fetch failed: %s", e)
        return messages, None

    fetched: dict[int, Any] = {}
    for m in raw_messages:
        if m:
            fetched[m.id] = m

    # Step 3: Build context envelopes
    timed_out = False

    for msg in messages:
        msg_id = msg.get("id")
        if not msg_id:
            continue

        # Check timeout before processing each result
        if time.monotonic() - start_time > _CONTEXT_TIMEOUT_BUDGET:
            logger.warning(
                "Context enrichment: timeout budget exceeded (%.1fs), stopping",
                time.monotonic() - start_time,
            )
            timed_out = True
            break

        msg_topic_id = msg.get("topic_id")
        ctx: dict[str, Any] = {}

        # Before messages (most recent first → oldest last)
        before = []
        for offset in range(1, context + 1):
            neighbor = fetched.get(msg_id - offset)
            if _is_valid_context_neighbor(neighbor, is_forum, msg_topic_id):
                before.append(_lightweight_from_raw(neighbor))
        if before:
            ctx["before"] = before

        # After messages (oldest first → most recent last)
        after = []
        for offset in range(1, context + 1):
            neighbor = fetched.get(msg_id + offset)
            if _is_valid_context_neighbor(neighbor, is_forum, msg_topic_id):
                after.append(_lightweight_from_raw(neighbor))
        if after:
            ctx["after"] = after

        # Reply-to message
        rt = msg.get("reply_to_msg_id")
        if rt and rt in fetched:
            ctx["reply_to"] = _lightweight_from_raw(fetched[rt])
        else:
            ctx["reply_to"] = None

        # Reply threads — collect for parallel execution below
        if include_reply_threads:
            ctx["replies"] = []  # placeholder; populated after parallel fetch

        if ctx:
            msg["context"] = ctx

    # Step 4: Parallel reply thread fetches
    if include_reply_threads:
        reply_tasks: list[tuple[int, int]] = []  # (msg_index, msg_id)
        for i, msg in enumerate(messages[:_CONTEXT_MAX_REPLY_FETCHES]):
            msg_id = msg.get("id")
            if msg_id:
                reply_tasks.append((i, msg_id))

        if reply_tasks:
            reply_start = time.monotonic()

            async def _fetch_one_reply(idx: int, mid: int) -> tuple[int, list]:
                try:
                    async with asyncio.timeout(_CONTEXT_PER_REPLY_TIMEOUT):
                        replies, _disc_meta = await _fetch_replies(
                            client,
                            entity,
                            mid,
                            _CONTEXT_REPLY_LIMIT,
                            query=None,
                            include_chat_entity=False,
                            thread_scope="auto",
                        )
                        return idx, [
                            _lightweight_from_result(r)
                            for r in replies[:_CONTEXT_REPLY_LIMIT]
                        ]
                except (asyncio.TimeoutError, TimeoutError):
                    logger.warning("Reply fetch timeout for msg %d", mid)
                    return idx, []
                except FloodWaitError as e:
                    wait = getattr(e, "seconds", 0) or 0
                    logger.warning(
                        "Reply fetch FloodWaitError for msg %d: wait %ds",
                        mid,
                        wait,
                    )
                    return idx, []
                except Exception as e:
                    logger.warning("Reply fetch failed for msg %d: %s", mid, e)
                    return idx, []

            results_list = await asyncio.gather(
                *[_fetch_one_reply(i, mid) for i, mid in reply_tasks],
                return_exceptions=True,
            )

            reply_elapsed = time.monotonic() - reply_start
            logger.info(
                "Reply threads: %d fetches completed in %.1fs",
                len(reply_tasks),
                reply_elapsed,
            )

            for result in results_list:
                if isinstance(result, BaseException):
                    logger.warning("Reply gather exception: %s", result)
                    continue
                idx, replies_list = result
                if idx < len(messages) and "context" in messages[idx]:
                    messages[idx]["context"]["replies"] = replies_list

    if timed_out and not warning:
        warning = "Context enrichment: timed out; some results may lack context."

    return messages, warning


async def _execute_parallel_searches_generators(
    generators: list, collected: list[dict[str, Any]], seen_keys: set, limit: int
) -> None:
    """Round-robin parallel generators; collect limit+1 for has_more."""
    active_gens = list(enumerate(generators))
    target_limit = limit + 1

    while active_gens and len(collected) < target_limit:
        next_active = []

        for i, gen in active_gens:
            try:
                result = await gen.__anext__()
                _append_dedup_until_limit(collected, seen_keys, [result], target_limit)
                if len(collected) >= target_limit:
                    break
                next_active.append((i, gen))
            except StopAsyncIteration:
                continue
            except Exception as e:
                logger.warning(f"Error in search generator {i}: {e}")
                continue

        active_gens = next_active


async def _run_with_limits(coro, semaphore: asyncio.Semaphore | None) -> Any:
    """Run a coroutine with optional concurrency limiting semaphore."""
    if semaphore:
        async with semaphore:
            return await coro
    return await coro


async def _gather_global_batch(
    client,
    terms: list[dict],
    batch_limit: int,
    min_datetime: datetime | None,
    max_datetime: datetime | None,
    semaphore: asyncio.Semaphore | None = None,
) -> list[tuple[int, Any]]:
    """Execute SearchGlobalRequest for all active terms in parallel.

    Uses optional semaphore to limit concurrency. Returns list of
    (term_index, response) for successful responses. Failed or exhausted
    terms are skipped. Updates terms' offset_id/has_more.
    """
    requests = []
    term_indices = []
    for i, ts in enumerate(terms):
        if not ts["has_more"]:
            continue
        req = SearchGlobalRequest(
            q=ts["query"],
            filter=InputMessagesFilterEmpty(),
            min_date=min_datetime,
            max_date=max_datetime,
            offset_rate=0,
            offset_peer=InputPeerEmpty(),
            offset_id=ts["offset_id"],
            limit=batch_limit,
        )
        requests.append(client(req))
        term_indices.append(i)

    if not requests:
        return []

    wrapped = [_run_with_limits(r, semaphore=semaphore) for r in requests]
    responses = await asyncio.gather(*wrapped, return_exceptions=True)

    results: list[tuple[int, Any]] = []
    for req_idx, term_idx in enumerate(term_indices):
        response = responses[req_idx]

        if isinstance(response, Exception):
            logger.warning(
                "SearchGlobalRequest error for term '%s': %s",
                terms[term_idx]["query"],
                response,
            )
            terms[term_idx]["has_more"] = False
            continue

        if not hasattr(response, "messages") or not response.messages:
            terms[term_idx]["has_more"] = False
            continue

        # Update offset_id for pagination
        terms[term_idx]["offset_id"] = response.messages[-1].id  # type: ignore[union-attr]
        results.append((term_idx, response))

    return results


async def _process_raw_message(
    client,
    message,
    chat_type: str | None,
    public: bool | None,
    include_chat_entity: bool,
) -> dict[str, Any] | None:
    """Process a raw Telethon message into a result dict. Returns None if filtered out."""

    try:
        chat = await get_entity_by_id(message.peer_id)
        if not chat:
            logger.warning("Could not get entity for peer_id: %s", message.peer_id)
            return None

        if chat_type is not None and not _matches_chat_type(chat, chat_type):
            return None

        if not _matches_public_filter(chat, public):
            return None

        return await results._build_result_for_message(
            message, chat, include_chat_entity
        )
    except Exception as e:
        logger.warning(f"Error processing message: {e}")
        return None


async def _collect_messages_in_chat(
    client,
    chat_id: str,
    queries: list[str],
    limit: int,
    min_datetime: datetime | None,
    max_datetime: datetime | None,
    chat_type: str | None,
    public: bool | None,
    auto_expand_batches: int,
    include_total_count: bool,
    collected: list[dict[str, Any]],
    seen_keys: set[Any],
    include_chat_entity: bool = False,
    from_user: str | None = None,
) -> int | None:
    entity = await get_entity_by_id(chat_id)
    if not entity:
        raise ValueError(f"Could not find chat with ID '{chat_id}'")
    per_chat_queries = queries or [""]
    generators = [
        _search_chat_messages_generator(
            client,
            entity,
            (q or ""),
            limit,
            min_datetime,
            max_datetime,
            chat_type,
            public,
            auto_expand_batches,
            include_chat_entity,
            from_user=from_user,
        )
        for q in per_chat_queries
    ]
    await _execute_parallel_searches_generators(generators, collected, seen_keys, limit)
    await transcribe_voice_messages(collected, entity, client=client)
    return await _get_chat_message_count(chat_id) if include_total_count else None


async def _round_robin_merge_iters(
    term_iters: list[tuple[int, Any]],
    target_limit: int,
    collected: list[dict[str, Any]],
    seen_keys: set[Any],
    client: Any,
    chat_type: str | None,
    public: bool | None,
    include_chat_entity: bool,
) -> list[tuple[int, Any]]:
    """Process one round-robin pass over term iterators."""
    next_iters: list[tuple[int, Any]] = []
    for term_idx, it in term_iters:
        raw_msg = next(it, None)
        if raw_msg is None:
            continue

        msg_result = await _process_raw_message(
            client,
            raw_msg,
            chat_type,
            public,
            include_chat_entity,
        )
        if msg_result:
            _append_dedup_until_limit(
                collected,
                seen_keys,
                [msg_result],
                target_limit,
            )
            if len(collected) >= target_limit:
                break
        next_iters.append((term_idx, it))
    return next_iters


async def _collect_messages_global(
    client,
    queries: list[str],
    limit: int,
    min_datetime: datetime | None,
    max_datetime: datetime | None,
    chat_type: str | None,
    public: bool | None,
    auto_expand_batches: int,
    collected: list[dict[str, Any]],
    seen_keys: set[Any],
    include_chat_entity: bool = True,
    max_concurrent: int | None = None,
) -> None:
    """P0-style parallel gather for multi-term global search.

    Runs SearchGlobalRequest for all terms in parallel per batch,
    then lazily processes and round-robin merges results (only
    processes as many messages as needed for target_limit).

    Args:
        max_concurrent: Max parallel requests (None = no semaphore).

    """
    terms = [
        {"query": q, "offset_id": 0, "has_more": True}
        for q in queries
        if q and str(q).strip()
    ]
    if not terms:
        return

    batch_limit = min(limit * 2, 50)
    max_batches = 1 + (auto_expand_batches if chat_type else 0)
    target_limit = limit + 1

    # Create optional semaphore for concurrency limiting
    semaphore = asyncio.Semaphore(max_concurrent) if max_concurrent else None

    for _batch_idx in range(max_batches):
        if len(collected) >= target_limit:
            break
        if not any(ts["has_more"] for ts in terms):
            break

        # Gather search results from all active terms in parallel
        batch_results = await _gather_global_batch(
            client,
            terms,
            batch_limit,
            min_datetime,
            max_datetime,
            semaphore=semaphore,
        )

        if not batch_results:
            break

        term_iters: list[tuple[int, Any]] = [
            (term_idx, iter(response.messages))
            for term_idx, response in batch_results
            if hasattr(response, "messages") and response.messages
        ]
        if not term_iters:
            break

        # Lazy round-robin: process only what's needed
        while term_iters and len(collected) < target_limit:
            term_iters = await _round_robin_merge_iters(
                term_iters,
                target_limit,
                collected,
                seen_keys,
                client,
                chat_type,
                public,
                include_chat_entity,
            )

        if len(collected) >= target_limit:
            break


async def _handle_query_mode(
    *,
    query: str | None,
    chat_id: str | None,
    limit: int,
    min_date: str | None,
    max_date: str | None,
    chat_type: str | None,
    public: bool | None,
    auto_expand_batches: int,
    include_total_count: bool,
    params: dict[str, Any],
    from_user: str | None = None,
) -> dict[str, Any]:
    """Handle search/browse mode for messages (MessageRetrievalMode.SEARCH)."""
    queries: list[str] = (
        [q.strip() for q in query.split(",") if q.strip()] if query else []
    )

    if not chat_id and not queries:
        return log_and_build_error(
            operation="get_messages",
            error_message="Search query must not be empty for global search",
            params=params,
            exception=ValueError("Search query must not be empty for global search"),
        )

    min_datetime = parse_iso_datetime_utc(min_date) if min_date else None
    if min_date and min_datetime is None:
        return log_and_build_error(
            operation="get_messages",
            error_message=(
                f"Invalid min_date format: '{min_date}'. "
                "Use ISO format (e.g., '2024-01-01')"
            ),
            params=params,
            exception=ValueError(f"Invalid min_date format: '{min_date}'"),
        )

    max_datetime = parse_iso_datetime_utc(max_date) if max_date else None
    if max_date and max_datetime is None:
        return log_and_build_error(
            operation="get_messages",
            error_message=(
                f"Invalid max_date format: '{max_date}'. "
                "Use ISO format (e.g., '2024-12-31')"
            ),
            params=params,
            exception=ValueError(f"Invalid max_date format: '{max_date}'"),
        )

    def _connection_error_or_build(
        exc: Exception, fallback_message: str
    ) -> dict[str, Any]:
        if (
            r := log_connection_error_response("get_messages", params, exc)
        ) is not None:
            return r
        return log_and_build_error(
            operation="get_messages",
            error_message=fallback_message,
            params=params,
            exception=exc,
        )

    try:
        client = await get_connected_client()
        total_count = None
        collected: list[dict[str, Any]] = []
        seen_keys: set[Any] = set()

        if chat_id:
            try:
                total_count = await _collect_messages_in_chat(
                    client,
                    chat_id,
                    queries,
                    limit,
                    min_datetime,
                    max_datetime,
                    chat_type,
                    public,
                    auto_expand_batches,
                    include_total_count,
                    collected,
                    seen_keys,
                    include_chat_entity=False,
                    from_user=from_user,
                )
            except Exception as e:
                return _connection_error_or_build(
                    e, f"Failed to search in chat '{chat_id}': {e!s}"
                )
        else:
            max_concurrent = params.get("max_concurrent")

            try:
                await _collect_messages_global(
                    client,
                    queries,
                    limit,
                    min_datetime,
                    max_datetime,
                    chat_type,
                    public,
                    auto_expand_batches,
                    collected,
                    seen_keys,
                    include_chat_entity=True,
                    max_concurrent=max_concurrent,
                )
            except Exception as e:
                return _connection_error_or_build(
                    e, f"Failed to perform global search: {e!s}"
                )

        window = collected[:limit] if limit is not None else collected

        logger.info(f"Found {len(window)} messages matching query: {query}")

        has_more = len(collected) > len(window) or (
            len(collected) == limit and len(collected) > 0
        )

        if not window:
            q_nonempty = bool(query and query.strip())
            if chat_id and not q_nonempty:
                if min_date or max_date:
                    err = (
                        "No exportable messages found for the requested date range in this chat. "
                        "If Telegram shows recent dialog activity, it may be service-only "
                        "(e.g. pins, invites, title changes) now surfaced as [Service: …] rows."
                    )
                else:
                    err = "No exportable messages found in this chat."
            elif q_nonempty:
                err = f"No messages found matching query '{query}'"
            else:
                err = "No messages found for the given filters."

            return log_and_build_error(
                operation="get_messages",
                error_message=err,
                params=params,
                exception=ValueError(err),
            )

        response: dict[str, Any] = {"messages": window, "has_more": has_more}
        if total_count is not None:
            response["total_count"] = total_count

        warning = response_attachment_warning(window)
        if warning:
            response["_warning"] = warning

        return response

    except Exception as e:
        return _connection_error_or_build(e, f"Message retrieval failed: {e!s}")

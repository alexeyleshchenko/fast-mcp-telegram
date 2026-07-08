"""Flag-based dialog folder matching over iter_dialogs.

Uses two-pass batching for date fallback: first pass collects all matching
entities, second pass batches GetPeerDialogsRequest for entities that need
history fallback (dialog.date is None with active date filters).
"""

import logging
import time
from datetime import UTC, datetime
from typing import Any

from telethon.tl.functions.messages import GetPeerDialogsRequest
from telethon.tl.types import InputPeerChannel, InputPeerChat, InputPeerUser

from src.utils.entity import build_dialog_entity_dict, build_entity_dict

from .constants import FLAG_MATCH_MAX_DIALOGS, GET_PEER_DIALOGS_CHUNK_SIZE
from .date_helpers import (
    _dialog_top_date_outside_find_chats_bounds,
    _last_activity_datetime_in_range,
    _validate_find_chats_min_max_dates,
)
from .dialog_filters import _filter_matches_flags
from .include_peers import _extract_peer_id
from .views import (
    ChatView,
    _find_chats_query_matches_entity,
    _match_chat_type,
    _match_public,
)

logger = logging.getLogger(__name__)


async def _build_input_peer(pid: int, ent_dict: dict) -> Any | None:
    """Build InputPeer* from entity dict. Returns None if type is unknown."""
    ent_type = ent_dict.get("type")
    # access_hash is serialized as a string in entity dicts (JS int64 safety);
    # InputPeer* needs the raw int back.  Guard against malformed strings.
    try:
        access_hash = int(ent_dict.get("access_hash") or 0)
    except (ValueError, TypeError):
        access_hash = 0
    if ent_type == "channel":
        return InputPeerChannel(channel_id=pid, access_hash=access_hash)
    if ent_type == "group":
        return InputPeerChat(chat_id=pid)
    if ent_type in ("private", "bot"):
        return InputPeerUser(user_id=pid, access_hash=access_hash)
    return None


async def _batch_fetch_last_activity(
    client,
    entity_dicts: dict[int, dict],
) -> dict[int, datetime]:
    """Fetch last activity dates for entities in bulk via GetPeerDialogsRequest."""
    peer_ids = list(entity_dicts.keys())
    peer_id_to_activity: dict[int, datetime] = {}
    total_chunks = (
        len(peer_ids) + GET_PEER_DIALOGS_CHUNK_SIZE - 1
    ) // GET_PEER_DIALOGS_CHUNK_SIZE
    chunk_idx = 0

    for chunk_start in range(0, len(peer_ids), GET_PEER_DIALOGS_CHUNK_SIZE):
        chunk_idx += 1
        chunk_ids = peer_ids[chunk_start : chunk_start + GET_PEER_DIALOGS_CHUNK_SIZE]
        input_peers = []

        for pid in chunk_ids:
            ent_dict = entity_dicts.get(pid)
            if not ent_dict:
                continue
            peer = await _build_input_peer(pid, ent_dict)
            if peer:
                input_peers.append(peer)

        if not input_peers:
            continue

        t_chunk = time.monotonic()
        try:
            result = await client(GetPeerDialogsRequest(peers=input_peers))
            for d, m in zip(result.dialogs or [], result.messages or [], strict=False):
                peer_id = _extract_peer_id(d.peer)
                if not peer_id or m is None:
                    continue
                msg_date = getattr(m, "date", None)
                if msg_date:
                    if msg_date.tzinfo is None:
                        msg_date = msg_date.replace(tzinfo=UTC)
                    peer_id_to_activity[peer_id] = msg_date
            logger.info(
                "batch_fetch chunk %d/%d | peers=%d got=%d chunk_elapsed=%.3fs",
                chunk_idx,
                total_chunks,
                len(input_peers),
                len(
                    [d for d in result.dialogs if _extract_peer_id(d.peer) in chunk_ids]
                )
                if hasattr(result, "dialogs")
                else 0,
                time.monotonic() - t_chunk,
            )
        except Exception as e:
            logger.debug("GetPeerDialogsRequest chunk failed: %s", e)

    return peer_id_to_activity


async def _find_chats_by_filter_flags(
    client,
    filter_dict: dict,
    query: str | None,
    limit: int,
    chat_type: str | None,
    public: bool | None,
    min_date: str | None,
    max_date: str | None,
) -> dict[str, Any]:
    """Handle flag-based filter by iterating all dialogs and matching flags.

    Uses two-pass approach when date filters are active:
    1. First pass: iterate dialogs, collect metadata for entities passing
       flag/chat_type/public/query filters, but whose date needs history fallback.
       Entities with known dialog.date are included immediately.
    2. Second pass: batch GetPeerDialogsRequest for entities needing fallback,
       then apply date filtering.
    """
    t0 = time.monotonic()

    err, min_date_dt, max_date_dt = _validate_find_chats_min_max_dates(
        min_date, max_date
    )
    if err is not None:
        return err

    has_date_filter = min_date_dt is not None or max_date_dt is not None
    iter_limit = min(limit * 10, FLAG_MATCH_MAX_DIALOGS)
    logger.info(
        "find_chats_by_filter_flags start | filter=%s limit=%d date=%s iter_limit=%d",
        filter_dict.get("title", "?") if hasattr(filter_dict, "get") else "?",
        limit,
        "yes" if has_date_filter else "no",
        iter_limit,
    )

    # Pass 1: iterate dialogs, apply all non-date filters
    results: list[dict] = []
    pending_date: list[tuple[Any, Any, Any]] = []  # (entity, dialog, view)
    iter_count = 0

    async for dialog in client.iter_dialogs(limit=iter_limit):
        iter_count += 1
        entity = getattr(dialog, "entity", None)
        if not entity:
            continue

        dialog_date = getattr(dialog, "date", None)
        if _dialog_top_date_outside_find_chats_bounds(
            dialog_date, min_date_dt, max_date_dt
        ):
            continue

        if not _filter_matches_flags(entity, dialog, filter_dict):
            continue

        view = ChatView.from_entity(entity)

        if not _match_chat_type(view, chat_type):
            continue
        if not _match_public(view, public):
            continue

        if not _find_chats_query_matches_entity(view, query):
            continue

        if dialog_date is not None or not has_date_filter:
            # Date is known or no date filter — include immediately
            if result := build_dialog_entity_dict(dialog, entity):
                results.append(result)
                if len(results) >= limit:
                    t1 = time.monotonic()
                    logger.info(
                        "find_chats_by_filter_flags done early | "
                        "iterated=%d results=%d pending=%d elapsed=%.3fs",
                        iter_count,
                        len(results),
                        len(pending_date),
                        t1 - t0,
                    )
                    return {"chats": results}
        else:
            # Needs date fallback — defer to pass 2
            pending_date.append((entity, dialog, view))

    t1 = time.monotonic()
    logger.info(
        "find_chats_by_filter_flags pass1 done | "
        "iterated=%d results=%d pending=%d iter_limit=%d elapsed=%.3fs",
        iter_count,
        len(results),
        len(pending_date),
        iter_limit,
        t1 - t0,
    )

    if not pending_date:
        logger.info(
            "find_chats_by_filter_flags done (no pending) | total=%.3fs",
            time.monotonic() - t0,
        )
        return {"chats": results}

    # Pass 2: batch fetch last activity dates for pending entities
    logger.info(
        "find_chats_by_filter_flags pass2 start | pending=%d",
        len(pending_date),
    )
    entity_dicts: dict[int, dict] = {}
    for entity, _dialog, _view in pending_date:
        eid = getattr(entity, "id", None)
        if eid:
            ent_dict = build_entity_dict(entity)
            if ent_dict:
                entity_dicts[eid] = ent_dict

    t_batch_start = time.monotonic()
    peer_id_to_activity = (
        await _batch_fetch_last_activity(client, entity_dicts) if entity_dicts else {}
    )
    t_batch_end = time.monotonic()
    logger.info(
        "find_chats_by_filter_flags pass2 batch_done | "
        "dicts=%d results=%d batch_elapsed=%.3fs",
        len(entity_dicts),
        len(peer_id_to_activity),
        t_batch_end - t_batch_start,
    )

    for entity, dialog, _view in pending_date:
        if len(results) >= limit:
            break

        eid = getattr(entity, "id", None)
        activity = peer_id_to_activity.get(eid) if eid else None

        if activity is not None:
            if not _last_activity_datetime_in_range(activity, min_date_dt, max_date_dt):
                continue
            result = build_dialog_entity_dict(dialog, entity)
            if result:
                result["last_activity_date"] = activity.isoformat()
                results.append(result)
        # activity is None: no date info available from Telegram, skip this entity

    total = time.monotonic() - t0
    logger.info(
        "find_chats_by_filter_flags done | results=%d total=%.3fs",
        len(results),
        total,
    )

    return {"chats": results}

"""Folder filters with explicit include_peers and GetPeerDialogs batching."""

import asyncio
import logging
import time
from datetime import UTC
from typing import Any

from telethon.tl.functions.messages import GetPeerDialogsRequest

from src.utils.datetime_parse import parse_iso_datetime_utc
from src.utils.entity import build_entity_dict

from .constants import (
    FLAG_MATCH_MAX_DIALOGS,
    GET_ENTITY_CONCURRENCY,
    GET_PEER_DIALOGS_CHUNK_SIZE,
)
from .date_helpers import (
    _dialog_in_date_range,
    _last_activity_datetime_in_range,
)
from .dialog_filters import _filter_matches_flags
from .views import ChatView, _match_chat_type, _match_public, _match_query

logger = logging.getLogger(__name__)


def _extract_peer_id(peer) -> int | None:
    """Extract numeric peer ID from PeerUser/PeerChannel/PeerChat."""
    if hasattr(peer, "user_id"):
        return peer.user_id
    if hasattr(peer, "channel_id"):
        return peer.channel_id
    return peer.chat_id if hasattr(peer, "chat_id") else None


async def _find_chats_by_include_peers(
    client,
    filter_dict: dict,
    query: str | None,
    limit: int,
    chat_type: str | None,
    public: bool | None,
    min_date: str | None,
    max_date: str | None,
) -> dict[str, Any]:
    """Handle filter with explicit include_peers using GetPeerDialogsRequest."""
    t0 = time.monotonic()
    include_peers = filter_dict.get("include_peers", []) or []
    exclude_peers = filter_dict.get("exclude_peers", []) or []
    logger.info(
        "include_peers start | limit=%d date=%s include_n=%d exclude_n=%d",
        limit,
        "yes" if min_date or max_date else "no",
        len(include_peers),
        len(exclude_peers),
    )

    ordered_peer_ids: list[int] = []
    peer_entity_map: dict[int, dict] = {}
    peer_objects: dict[int, Any] = {}
    sem = asyncio.Semaphore(GET_ENTITY_CONCURRENCY)

    async def _get_include(inp_peer) -> tuple[Any | None, dict | None]:
        async with sem:
            t_start = time.monotonic()
            try:
                ent = await client.get_entity(inp_peer)
                elapsed = time.monotonic() - t_start
                eid = getattr(ent, "id", None)
                peer_label = repr(inp_peer)
                if elapsed > 5.0:
                    logger.info(
                        "get_entity SLOW: peer=%s id=%s elapsed=%.3fs",
                        peer_label,
                        eid,
                        elapsed,
                    )
                elif logger.isEnabledFor(logging.DEBUG):
                    logger.debug(
                        "get_entity: peer=%s id=%s elapsed=%.3fs",
                        peer_label,
                        eid,
                        elapsed,
                    )
                if eid is None:
                    return None, None
                ed = build_entity_dict(ent)
                return (ent, ed) if ed else (None, None)
            except Exception as e:
                elapsed = time.monotonic() - t_start
                logger.warning(
                    "get_entity FAIL: peer=%s elapsed=%.3fs error=%s",
                    repr(inp_peer),
                    elapsed,
                    e,
                )
                return None, None

    t_incl = time.monotonic()
    include_results: list = []
    if include_peers:
        include_results = list(
            await asyncio.gather(*(_get_include(p) for p in include_peers))
        )
    if include_peers and logger.isEnabledFor(logging.DEBUG):
        logger.debug(
            "find_chats include_peers get_entity: n=%d duration_s=%.3f",
            len(include_peers),
            time.monotonic() - t_incl,
        )

    for ent, ent_dict in include_results:
        if not ent or not ent_dict:
            continue
        eid = getattr(ent, "id", None)
        if eid is None or eid in ordered_peer_ids:
            continue
        ordered_peer_ids.append(eid)
        peer_entity_map[eid] = ent_dict
        peer_objects[eid] = ent

    async def _get_exclude_id(inp_peer) -> int | None:
        async with sem:
            try:
                e = await client.get_entity(inp_peer)
                eid = getattr(e, "id", None)
                return eid if isinstance(eid, int) else None
            except Exception as e:
                logger.debug("Failed to resolve exclude_peer %s: %s", inp_peer, e)
                return None

    if exclude_peers:
        for eid in await asyncio.gather(*(_get_exclude_id(p) for p in exclude_peers)):
            if eid and eid in ordered_peer_ids:
                ordered_peer_ids.remove(eid)
                peer_entity_map.pop(eid, None)
                peer_objects.pop(eid, None)
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("find_chats exclude_peers: n=%d", len(exclude_peers))

    has_flags = any(
        filter_dict.get(flag)
        for flag in (
            "contacts",
            "non_contacts",
            "groups",
            "broadcasts",
            "bots",
            "exclude_muted",
            "exclude_read",
            "exclude_archived",
        )
    )
    peer_dl_date: dict[int, Any] = {}
    if has_flags:
        t_flags_start = time.monotonic()
        async for dialog in client.iter_dialogs(
            limit=min(limit * 10, FLAG_MATCH_MAX_DIALOGS),
        ):
            ent = getattr(dialog, "entity", None)
            if not ent:
                continue
            eid = getattr(ent, "id", None)
            # Store dialog.date so flags entities can skip GetPeerDialogsRequest
            dl_date_raw = getattr(dialog, "date", None)
            if dl_date_raw and dl_date_raw.tzinfo is None:
                dl_date_raw = dl_date_raw.replace(tzinfo=UTC)
            if (
                eid
                and eid not in ordered_peer_ids
                and _filter_matches_flags(ent, dialog, filter_dict)
                and (entity_dict := build_entity_dict(ent))
            ):
                ordered_peer_ids.append(eid)
                peer_entity_map[eid] = entity_dict
                peer_objects[eid] = ent
                if dl_date_raw:
                    peer_dl_date[eid] = dl_date_raw
        t_flags_end = time.monotonic()
        logger.info(
            "include_peers flags_iter | limit=%d added=%d total_ids=%d elapsed=%.3fs",
            min(limit * 10, FLAG_MATCH_MAX_DIALOGS),
            len(ordered_peer_ids),  # after flags, includes include_peers
            len(ordered_peer_ids),
            t_flags_end - t_flags_start,
        )

    if not ordered_peer_ids:
        logger.info("include_peers no peers | total=%.3fs", time.monotonic() - t0)
        return {"chats": []}

    last_activity_by_peer: dict[int, Any] = {}
    t_dialogs_start = time.monotonic()
    n_failed_dialogs = 0
    chunk_idx = 0
    for chunk_start in range(0, len(ordered_peer_ids), GET_PEER_DIALOGS_CHUNK_SIZE):
        chunk_idx += 1
        chunk_ids = ordered_peer_ids[
            chunk_start : chunk_start + GET_PEER_DIALOGS_CHUNK_SIZE
        ]

        input_peers = []
        chunk_entities: list[tuple[int, str | None]] = []
        for pid in chunk_ids:
            ent = peer_entity_map.get(pid)
            if not ent:
                continue
            # Skip entities that already have dialog.date from flags iteration
            if pid in peer_dl_date:
                continue
            ent_type = ent.get("type")
            chunk_entities.append((pid, ent_type))
            # access_hash is serialized as a string (JS int64 safety); coerce
            # back to int for InputPeer* construction.  Guard against
            # malformed / non-numeric strings so a corrupt entity dict
            # cannot raise ValueError and crash chat discovery.
            try:
                access_hash = int(ent.get("access_hash") or 0)
            except (ValueError, TypeError):
                access_hash = 0
            is_min = ent.get("min", False)
            if ent_type == "channel":
                if is_min or not access_hash:
                    # Min entity (Layer 102+) has access_hash only for profile photos,
                    # or deleted entity (no access_hash at all).
                    # GetPeerDialogsRequest would timeout (~30s) trying to resolve them.
                    continue
                from telethon.tl.types import InputPeerChannel

                input_peers.append(
                    InputPeerChannel(channel_id=pid, access_hash=access_hash)
                )
            elif ent_type == "group":
                from telethon.tl.types import InputPeerChat

                input_peers.append(InputPeerChat(chat_id=pid))
            elif ent_type in ("private", "bot"):
                if is_min or not access_hash:
                    # Min entity (Layer 102+) has access_hash only for profile photos,
                    # or deleted entity (no access_hash at all).
                    # GetPeerDialogsRequest would timeout (~30s) trying to resolve them.
                    continue
                from telethon.tl.types import InputPeerUser

                input_peers.append(InputPeerUser(user_id=pid, access_hash=access_hash))

        if not input_peers:
            continue

        t_chunk = time.monotonic()
        try:
            result = await client(GetPeerDialogsRequest(peers=input_peers))
            dialogs = result.dialogs or []
            messages = result.messages or []
            n_d, n_m = len(dialogs), len(messages)
            if n_d != n_m:
                logger.warning(
                    "GetPeerDialogs: len(dialogs)=%s != len(messages)=%s; pairing by min length",
                    n_d,
                    n_m,
                )
            n_pair = min(n_d, n_m)
            for i in range(n_pair):
                d = dialogs[i]
                m = messages[i]
                peer_id = _extract_peer_id(d.peer)
                if not peer_id or m is None:
                    continue
                msg_date = getattr(m, "date", None)
                if not msg_date:
                    continue
                act = msg_date
                if act.tzinfo is None:
                    act = act.replace(tzinfo=UTC)
                last_activity_by_peer[peer_id] = act
            t_chunk_end = time.monotonic()
            elapsed = t_chunk_end - t_chunk
            if elapsed > 1.0:
                ent_summary = ",".join(f"{pid}:{t or '?'}" for pid, t in chunk_entities)
                logger.info(
                    "include_peers chunk %d/%d | peers=%d elapsed=%.3fs SLOW peer_ids=%s",
                    chunk_idx,
                    (len(ordered_peer_ids) + GET_PEER_DIALOGS_CHUNK_SIZE - 1)
                    // GET_PEER_DIALOGS_CHUNK_SIZE,
                    len(input_peers),
                    elapsed,
                    ent_summary,
                )
            else:
                logger.info(
                    "include_peers chunk %d/%d | peers=%d elapsed=%.3fs",
                    chunk_idx,
                    (len(ordered_peer_ids) + GET_PEER_DIALOGS_CHUNK_SIZE - 1)
                    // GET_PEER_DIALOGS_CHUNK_SIZE,
                    len(input_peers),
                    elapsed,
                )
        except Exception as e:
            n_failed_dialogs += 1
            logger.debug("GetPeerDialogsRequest failed: %s", e)

    t_dialogs_end = time.monotonic()
    logger.info(
        "include_peers dialogs_batch | total_ids=%d got_activity=%d n_failed=%d elapsed=%.3fs",
        len(ordered_peer_ids),
        len(last_activity_by_peer),
        n_failed_dialogs,
        t_dialogs_end - t_dialogs_start,
    )

    min_date_dt = parse_iso_datetime_utc(min_date) if min_date else None
    max_date_dt = parse_iso_datetime_utc(max_date) if max_date else None

    n_date_fallback = 0
    results = []
    for pid in ordered_peer_ids:
        ent_dict = peer_entity_map.get(pid)
        if not ent_dict:
            continue
        ent = peer_objects.get(pid)
        if ent is None:
            continue

        view = ChatView.from_entity(ent)

        if not _match_chat_type(view, chat_type):
            continue

        if not _match_public(view, public):
            continue

        if min_date_dt is not None or max_date_dt is not None:
            dl_date = peer_dl_date.get(pid)
            if dl_date is not None:
                # Entity from flags iteration with dialog.date from iter_dialogs
                if not _last_activity_datetime_in_range(
                    dl_date, min_date_dt, max_date_dt
                ):
                    continue
            else:
                act_dt = last_activity_by_peer.get(pid)
                if act_dt is not None:
                    if not _last_activity_datetime_in_range(
                        act_dt, min_date_dt, max_date_dt
                    ):
                        continue
                else:
                    n_date_fallback += 1
                    if not await _dialog_in_date_range(
                        ent, client, None, min_date_dt, max_date_dt
                    ):
                        continue

        if query:
            query_lower = query.lower().strip()
            if query_lower and not _match_query(view, query_lower):
                continue

        result_dict = dict(ent_dict)
        result_dict.pop("access_hash", None)
        dl_date = peer_dl_date.get(pid)
        if dl_date is not None:
            result_dict["last_activity_date"] = dl_date.isoformat()
        elif pid in last_activity_by_peer:
            result_dict["last_activity_date"] = last_activity_by_peer[pid].isoformat()

        results.append(result_dict)
        if len(results) >= limit:
            break

    total = time.monotonic() - t0
    logger.info(
        "include_peers done | results=%d fallback=%d total=%.3fs",
        len(results),
        n_date_fallback,
        total,
    )
    return {"chats": results}

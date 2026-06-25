"""Async generators for per-chat and global message search."""

import logging

from telethon.tl.functions.messages import SearchGlobalRequest
from telethon.tl.types import InputMessagesFilterEmpty, InputPeerEmpty

from src.utils.entity import (
    _matches_chat_type,
    _matches_public_filter,
    get_entity_by_id,
)

from . import results

logger = logging.getLogger(__name__)


async def _search_chat_messages_generator(
    client,
    entity,
    query,
    limit,
    min_datetime,
    max_datetime,
    chat_type,
    public,
    auto_expand_batches,
    include_chat_entity=False,
    from_user=None,
):
    """Async generator for per-chat message search."""
    # Resolve from_user through get_entity_by_id (same as send_message resolves chat_id).
    # This handles: t.me URLs, "me", numeric strings with multi-peer fallback.
    resolved_from_user = None
    if from_user:
        resolved_from_user = await get_entity_by_id(from_user)
        if not resolved_from_user:
            raise ValueError(
                f"Could not resolve from_user '{from_user}'. "
                "Supported: username, phone number, t.me URL, numeric ID, or 'me'."
            )

    batch_count = 0
    max_batches = 1 + auto_expand_batches if chat_type else 1
    next_offset_id = 0

    while batch_count < max_batches:
        last_id = None
        async for message in client.iter_messages(
            entity,
            search=query,
            offset_id=next_offset_id,
            offset_date=max_datetime,
            from_user=resolved_from_user,
        ):
            if not message:
                continue
            last_id = getattr(message, "id", None) or last_id

            if max_datetime and message.date and message.date > max_datetime:
                continue
            if min_datetime and message.date and message.date < min_datetime:
                return

            if not _matches_chat_type(entity, chat_type):
                continue

            if not _matches_public_filter(entity, public):
                continue

            result = await results._build_result_for_message(
                message, entity, include_chat_entity
            )
            if not result:
                continue

            yield result

        if not last_id:
            break

        next_offset_id = last_id
        batch_count += 1


async def _search_global_messages_generator(
    client,
    query,
    limit,
    min_datetime,
    max_datetime,
    chat_type,
    public,
    auto_expand_batches,
    include_chat_entity=True,
):
    """Async generator for global message search."""
    batch_count = 0
    max_batches = 1 + auto_expand_batches if chat_type else 1
    next_offset_id = 0

    while batch_count < max_batches:
        offset_id = next_offset_id
        result = await client(
            SearchGlobalRequest(
                q=query,
                filter=InputMessagesFilterEmpty(),
                min_date=min_datetime,
                max_date=max_datetime,
                offset_rate=0,
                offset_peer=InputPeerEmpty(),
                offset_id=offset_id,
                limit=min(limit * 2, 50),
            )
        )

        if not hasattr(result, "messages") or not result.messages:
            break

        for message in result.messages:
            try:
                chat = await get_entity_by_id(message.peer_id)
                if not chat:
                    logger.warning(
                        f"Could not get entity for peer_id: {message.peer_id}"
                    )
                    continue

                if not _matches_chat_type(chat, chat_type):
                    continue

                if not _matches_public_filter(chat, public):
                    continue

                msg_result = await results._build_result_for_message(
                    message, chat, include_chat_entity
                )
                if not msg_result:
                    continue

                yield msg_result
            except Exception as e:
                logger.warning(f"Error processing message: {e}")
                continue

        if result.messages:
            next_offset_id = result.messages[-1].id
        batch_count += 1

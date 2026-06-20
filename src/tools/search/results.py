"""Build MCP message dicts for search/reply listing."""

import logging
from typing import Any

from src.tools.links import generate_telegram_links
from src.utils.entity import compute_entity_identifier
from src.utils.message_format import (
    build_message_result,
    message_has_displayable_content,
)

logger = logging.getLogger(__name__)


async def _build_results_up_to_limit(
    messages,
    chat_entity,
    include_chat_entity: bool,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Build MCP dicts for messages; collect up to limit+1 when limit is set."""
    collected: list[dict[str, Any]] = []
    target = limit + 1 if limit is not None else None
    for message in messages:
        built = await _build_result_for_message(
            message, chat_entity, include_chat_entity
        )
        if not built:
            continue
        collected.append(built)
        if target is not None and len(collected) >= target:
            break
    return collected


async def _build_result_for_message(
    message,
    chat_entity,
    include_chat_entity: bool = False,
) -> dict[str, Any] | None:
    """Build result dict for a single message with link generation.

    Returns None if message is invalid or has no content.
    """
    if not message:
        return None

    if not message_has_displayable_content(message):
        return None

    try:
        identifier = compute_entity_identifier(chat_entity)
        if identifier is None:
            return None
        links = await generate_telegram_links(
            identifier, [message.id], resolved_entity=chat_entity
        )
        link = links.get("message_links", [None])[0]
        return await build_message_result(
            message, chat_entity, link, include_chat_entity
        )
    except Exception as e:
        logger.warning(f"Error processing message: {e}")
        return None

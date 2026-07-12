"""Attachment download URLs, rich placeholders, and response warnings."""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import quote

from src.client.connection import get_request_token
from src.config.server_config import cfg
from src.server_components.attachment_tickets import mint_attachment_ticket
from src.utils.message_format.rich import (
    RichMediaRef,
    rich_media_maps,
    tl_class_name,
)

logger = logging.getLogger(__name__)


def largest_photo_size(photo: Any) -> Any | None:
    sizes = getattr(photo, "sizes", None) or []
    sized = [
        s
        for s in sizes
        if getattr(s, "size", None) is not None
        and tl_class_name(s) != "PhotoStrippedSize"
    ]
    if not sized:
        return None
    return max(sized, key=lambda s: getattr(s, "size", 0))


def _str_field(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _parse_chat_id(chat_id: int | str | None, *, context: str) -> int | None:
    if chat_id is None:
        return None
    if isinstance(chat_id, str) and not chat_id.strip():
        return None
    try:
        return int(chat_id)
    except (TypeError, ValueError) as err:
        logger.warning("Skipping %s: invalid chat_id=%r (%s)", context, chat_id, err)
        return None


def _attachment_urls_enabled() -> bool:
    config = cfg()
    return config.transport == "http" and bool(config.public_base_url_normalized)


def _rich_ticket_kwargs(media_dict: dict[str, Any]) -> dict[str, Any]:
    kind = media_dict.get("rich_kind")
    media_id = media_dict.get("rich_media_id")
    if kind in ("photo", "document") and media_id is not None:
        return {"rich_kind": str(kind), "rich_media_id": int(media_id)}
    return {}


def _photo_placeholder(photo: Any) -> dict[str, Any]:
    placeholder: dict[str, Any] = {"type": "photo", "mime_type": "image/jpeg"}
    largest = largest_photo_size(photo)
    if largest is not None:
        placeholder["approx_size_bytes"] = largest.size
    return placeholder


def _document_subtype(document: Any, block_type: str) -> str:
    if block_type == "PageBlockVideo":
        return "video"
    if block_type == "PageBlockAudio":
        return "audio"
    for attr in getattr(document, "attributes", []) or []:
        ac = tl_class_name(attr)
        if ac == "DocumentAttributeAudio" and getattr(attr, "voice", False):
            return "voice"
        if ac == "DocumentAttributeVideo" and getattr(attr, "round_message", False):
            return "round_video"
        if ac in ("DocumentAttributeVideo", "DocumentAttributeAudio"):
            return "video" if ac == "DocumentAttributeVideo" else "audio"
    return "file"


def _document_placeholder(document: Any, block_type: str) -> dict[str, Any]:
    placeholder: dict[str, Any] = {"type": _document_subtype(document, block_type)}
    for attr in getattr(document, "attributes", []) or []:
        ac = tl_class_name(attr)
        if ac in ("DocumentAttributeAudio", "DocumentAttributeVideo") and (
            duration := getattr(attr, "duration", None)
        ):
            placeholder["duration_seconds"] = duration
        if hasattr(attr, "file_name") and attr.file_name:
            placeholder["filename"] = attr.file_name
    if mime := getattr(document, "mime_type", None):
        placeholder["mime_type"] = mime
    if size := getattr(document, "size", None):
        placeholder["approx_size_bytes"] = size
    return placeholder


def build_rich_attachment_placeholders(
    rich_message: Any, media_refs: list[RichMediaRef]
) -> list[dict[str, Any]]:
    """Build lightweight media dicts for each referenced rich embed."""
    photos, documents = rich_media_maps(rich_message)
    placeholders: list[dict[str, Any]] = []
    for ref in media_refs:
        if ref.kind == "photo":
            photo = photos.get(ref.media_id)
            if photo is None:
                continue
            entry = _photo_placeholder(photo)
        else:
            document = documents.get(ref.media_id)
            if document is None:
                continue
            entry = _document_placeholder(document, ref.block_type)
        entry["rich_kind"] = ref.kind
        entry["rich_media_id"] = ref.media_id
        placeholders.append(entry)
    return placeholders


def _message_supports_streaming_attachment(message) -> bool:
    """Whether attachment HTTP streaming is supported for this message (documents, photos, voice)."""
    media = getattr(message, "media", None)
    if not media:
        return False
    media_cls = media.__class__.__name__
    if media_cls == "MessageMediaPhoto":
        return True
    if media_cls == "MessageMediaDocument":
        return getattr(media, "document", None) is not None
    return media_cls == "MessageMediaVoice"


def _attachment_url(
    ticket_id: str,
    media_dict: dict[str, Any],
    message,
) -> str:
    base = cfg().public_base_url_normalized
    url = f"{base}/v1/attachments/{ticket_id}"
    if filename := media_dict.get("filename"):
        return f"{url}/{quote(filename, safe='')}"
    msg_id = getattr(message, "id", "unknown")
    if media_dict.get("type") == "photo":
        return f"{url}/photo_{msg_id}.jpg"
    return f"{url}/attachment_{msg_id}"


async def _mint_and_set_url(
    media_dict: dict[str, Any],
    message,
    chat_id: int,
) -> None:
    session_token = get_request_token()
    if session_token is None:
        session_token = cfg().session_name
    tid = await mint_attachment_ticket(
        session_token,
        chat_id,
        int(message.id),
        filename=_str_field(media_dict.get("filename")),
        mime_type=_str_field(media_dict.get("mime_type")),
        **_rich_ticket_kwargs(media_dict),
    )
    media_dict["attachment_download_url"] = _attachment_url(tid, media_dict, message)


async def _maybe_set_attachment_download_url(
    media_dict: dict[str, Any],
    message,
    chat_id: int | None,
) -> None:
    """Set media['attachment_download_url'] when HTTP mode and DOMAIN resolves to a public origin."""
    cid = _parse_chat_id(chat_id, context="attachment URL")
    if cid is None or not _attachment_urls_enabled():
        return
    if not _message_supports_streaming_attachment(message):
        return
    await _mint_and_set_url(media_dict, message, cid)


async def _maybe_set_rich_attachment_download_urls(
    attachments: list[dict[str, Any]],
    message,
    chat_id: int | None,
) -> None:
    """Mint capability URLs for RichMessage photo/document embeds."""
    cid = _parse_chat_id(chat_id, context="rich attachment URLs")
    if cid is None or not _attachment_urls_enabled():
        return
    for media_dict in attachments:
        if not _rich_ticket_kwargs(media_dict):
            continue
        await _mint_and_set_url(media_dict, message, cid)


def response_attachment_warning(messages: list[dict]) -> str | None:
    """Return a warning string if DOMAIN is missing and any message has media.

    One warning per entire response, not per message. Returns None when
    there is no problem (valid domain, stdio transport, or no media messages).
    """
    if not messages:
        return None
    config = cfg()
    if config.transport != "http" or config.public_base_url_normalized:
        return None
    has_media = any(
        bool(m.get("media")) or bool(m.get("attachments")) for m in messages
    )
    if not has_media:
        return None
    return (
        f"⚠️ DOMAIN is '{config.domain}' — attachment_download_url DISABLED for media messages. "
        "Set DOMAIN=<your-public-host> in .env to enable download links."
    )

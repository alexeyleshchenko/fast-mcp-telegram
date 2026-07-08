"""Format Telethon messages for MCP tool responses."""

from __future__ import annotations

import time

from .attachments import (
    _maybe_set_attachment_download_url,
    _message_supports_streaming_attachment,
    response_attachment_warning,
)
from .core import (
    _build_media_placeholder,
    _document_voice_and_round_note_flags,
    _extract_reply_markup,
    _fill_todo_media_placeholder,
    _todo_completed_by_to_int,
    build_message_result,
    build_send_edit_result,
    extract_topic_metadata,
    get_sender_info,
    message_has_displayable_content,
)
from .transcription import (
    PremiumRequiredError,
    _DONE_TTL_SECONDS,
    _PENDING_TTL_SECONDS,
    _TRANSCRIPTION_CACHE,
    _TRANSCRIPTION_CACHE_MAX,
    _TranscriptionCacheEntry,
    _transcribe_single_voice_message,
    _transcription_cache_get,
    _transcription_cache_key,
    _transcription_cache_set,
    transcribe_voice_messages,
)

__all__ = [
    "PremiumRequiredError",
    "_TRANSCRIPTION_CACHE",
    "_TranscriptionCacheEntry",
    "_build_media_placeholder",
    "_document_voice_and_round_note_flags",
    "_extract_reply_markup",
    "_maybe_set_attachment_download_url",
    "_message_supports_streaming_attachment",
    "_transcribe_single_voice_message",
    "build_message_result",
    "build_send_edit_result",
    "extract_topic_metadata",
    "get_sender_info",
    "message_has_displayable_content",
    "response_attachment_warning",
    "transcribe_voice_messages",
]

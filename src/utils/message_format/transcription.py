"""Voice message transcription with in-memory cache."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

from telethon.errors import FloodWaitError, RPCError
from telethon.tl.functions.messages import TranscribeAudioRequest

from src.client.connection import get_connected_client

logger = logging.getLogger(__name__)

class PremiumRequiredError(Exception):
    """Exception raised when transcription fails due to non-premium account."""


@dataclass
class _TranscriptionCacheEntry:
    """In-memory record of a TranscribeAudio attempt.

    Three states, all time-bound to avoid stale data:

    done(text, done_until_ts)
        Transcription succeeded; ``done_until_ts`` is when this entry
        expires (capped at ``_DONE_TTL_SECONDS``). After that the entry
        is treated as a cache miss and the API is re-issued.

    pending(transcription_id, pending_until_ts)
        First call returned a pending transcription. The id is recorded
        so a subsequent call within ``_PENDING_TTL_SECONDS`` can re-poll
        using the same id without starting a new transcription.

    rate_limited(until_ts)
        Telegram returned a FloodWaitError. Do not re-issue the request
        until ``time.time()`` exceeds ``until_ts``.
    """

    text: str | None = None
    transcription_id: str | None = None
    done_until_ts: float = 0.0
    pending_until_ts: float = 0.0
    until_ts: float = 0.0  # rate-limit expiry; 0 = not rate-limited

    def state(self, now: float | None = None) -> str:
        """Return the entry's current state evaluated at ``now``.

        Possible values: ``"done"``, ``"pending"``, ``"rate_limited"``, or
        ``"stale"`` when none of the above is active. ``now`` defaults to
        the current wall clock; pass an explicit value when you need every
        state check to be evaluated against the same instant (avoids TTL
        boundary races where ``is_done`` and ``is_pending`` would each
        read ``time.time()`` separately).
        """
        if now is None:
            now = time.time()
        if self.until_ts > 0.0 and now < self.until_ts:
            return "rate_limited"
        if (
            self.text is not None
            and self.transcription_id is None
            and now < self.done_until_ts
        ):
            return "done"
        if (
            self.text is None
            and self.transcription_id is not None
            and now < self.pending_until_ts
        ):
            return "pending"
        return "stale"

    @property
    def is_done(self) -> bool:
        return self.state() == "done"

    @property
    def is_pending(self) -> bool:
        return self.state() == "pending"

    @property
    def is_rate_limited(self) -> bool:
        return self.state() == "rate_limited"


# Module-level cache keyed by (peer_id, msg_id). Bounded by both a size
# cap and per-entry TTLs to avoid unbounded growth across long server
# uptimes. Keys are not security-sensitive — they are derived from the
# same chat_entity/message_id arguments passed to the function.
_TranscriptionCacheKey = tuple[str, int, int]
_TRANSCRIPTION_CACHE: dict[_TranscriptionCacheKey, _TranscriptionCacheEntry] = {}

# TTL for cached successful transcriptions. The actual Telegram cooldown
# is ~39 minutes per message, but we keep done entries around longer so
# that subsequent lookups for the same voice don't re-issue and trigger
# a fresh cooldown window.
_DONE_TTL_SECONDS = 3600

# How long to remember a pending transcription_id before giving up. The
# 30-iteration polling loop in _transcribe_single_voice_message sleeps
# 1s between attempts (so ~30s) — set the TTL slightly above that to
# survive cross-call polling when get_messages is called repeatedly.
_PENDING_TTL_SECONDS = 120

# Maximum entries to keep before pruning. Hard cap so the cache can't
# grow unbounded across long server uptimes on a busy account.
_TRANSCRIPTION_CACHE_MAX = 4096


def _transcription_cache_key(
    chat_entity: object, message_id: int
) -> _TranscriptionCacheKey | None:
    """Build a stable cache key from a Telethon chat entity and msg id.

    The key is ``(peer_kind, peer_id, message_id)`` where ``peer_kind`` is
    a short string ("user" / "channel" / "chat" / "unknown") that
    disambiguates peer types — Telethon's integer peer ids are not
    unique across user/channel/chat namespaces, so a key built from the
    bare integer would let a user message and a channel message collide
    when their ids happen to match.

    Returns None when the peer id cannot be determined — in that case the
    function will not cache, which preserves the previous (uncached)
    behaviour for unusual entity types.
    """
    peer_kind = "unknown"
    peer_id = getattr(chat_entity, "id", None)
    if peer_id is None:
        peer_id_obj = getattr(chat_entity, "peer_id", None)
        if peer_id_obj is not None:
            user_id = getattr(peer_id_obj, "user_id", None)
            channel_id = getattr(peer_id_obj, "channel_id", None)
            chat_id = getattr(peer_id_obj, "chat_id", None)
            if user_id is not None:
                peer_id = user_id
                peer_kind = "user"
            elif channel_id is not None:
                peer_id = channel_id
                peer_kind = "channel"
            elif chat_id is not None:
                peer_id = chat_id
                peer_kind = "chat"
    else:
        # The entity object itself is typed (User / Channel / Chat /
        # UserFull / etc.). Prefer its class name over the bare integer.
        cls_name = type(chat_entity).__name__.lower()
        if "user" in cls_name:
            peer_kind = "user"
        elif "channel" in cls_name or "forum" in cls_name:
            peer_kind = "channel"
        elif "chat" in cls_name:
            peer_kind = "chat"
    return None if peer_id is None else (peer_kind, int(peer_id), message_id)


def _transcription_cache_get(
    key: _TranscriptionCacheKey,
) -> _TranscriptionCacheEntry | None:
    """Return the cached entry if it's still useful, else evict and return None.

    On a hit, the key is moved to the end of the cache (pop + reinsert) so
    that frequent callers update their recency under the LRU eviction policy.
    """
    entry = _TRANSCRIPTION_CACHE.get(key)
    if entry is None:
        return None
    if entry.state() in ("done", "pending", "rate_limited"):
        # Pop and reinsert to mark as most-recently-used under LRU.
        _TRANSCRIPTION_CACHE[key] = _TRANSCRIPTION_CACHE.pop(key)
        return entry
    # Entry is fully stale (no live state): evict so the next call re-issues.
    _TRANSCRIPTION_CACHE.pop(key, None)
    return None


def _transcription_cache_set(
    key: _TranscriptionCacheKey, entry: _TranscriptionCacheEntry
) -> None:
    """Store an entry, pruning the oldest 25% of entries if the cap is hit."""
    if len(_TRANSCRIPTION_CACHE) >= _TRANSCRIPTION_CACHE_MAX:
        victims = max(1, _TRANSCRIPTION_CACHE_MAX // 4)
        for k in list(_TRANSCRIPTION_CACHE.keys())[:victims]:
            _TRANSCRIPTION_CACHE.pop(k, None)
    # Pop first so the new entry lands at the end of the dict — makes it
    # the most-recently-used under the LRU policy in _transcription_cache_get.
    _TRANSCRIPTION_CACHE.pop(key, None)
    _TRANSCRIPTION_CACHE[key] = entry


async def _is_user_premium(client) -> bool:
    """Check if the current user has Telegram Premium."""
    try:
        me = await client.get_me()
        return bool(getattr(me, "premium", False))
    except Exception as e:
        logger.warning("Failed to check user premium status: %s", e)
        return False


async def _transcribe_single_voice_message(
    client, chat_entity, message_id: int
) -> str | None:
    """Transcribe a single voice message; poll when pending. Raises PremiumRequiredError if required.

    Results are cached in ``_TRANSCRIPTION_CACHE`` so repeat calls for the same
    (peer_id, msg_id) within the Telegram per-message cooldown window do not
    re-issue TranscribeAudio and trip the 39-minute FloodWaitError penalty.
    """
    cache_key = _transcription_cache_key(chat_entity, message_id)
    transcription_id: str | None = None
    if cache_key is not None:
        cached = _transcription_cache_get(cache_key)
        if cached is not None:
            cache_state = cached.state()
            if cache_state == "done":
                return cached.text
            if cache_state == "pending":
                # A previous call kicked off a transcription that is still
                # pending. Treat the cached id as the active transcription
                # id and fall through to the polling loop below — the
                # existing TranscribeAudioRequest will re-poll and Telegram
                # will return text once the transcription is ready.
                transcription_id = cached.transcription_id
                logger.debug(
                    "Resuming pending transcription %s for message %s",
                    transcription_id,
                    message_id,
                )
            elif cache_state == "rate_limited":
                logger.debug(
                    "Transcription for message %s is rate-limited; skipping",
                    message_id,
                )
                return None

    try:
        if transcription_id is not None:
            # Resuming a cached pending transcription — skip the kick-off
            # call and let the polling loop below drive the next request.
            result = None
        else:
            result = await client(
                TranscribeAudioRequest(peer=chat_entity, msg_id=message_id)
            )

        # Extract completed text first (if the kick-off call already finished).
        if result is not None and (
            hasattr(result, "text")
            and result.text
            and not getattr(result, "pending", False)
        ):
            if cache_key is not None:
                _transcription_cache_set(
                    cache_key,
                    _TranscriptionCacheEntry(
                        text=result.text,
                        done_until_ts=time.time() + _DONE_TTL_SECONDS,
                    ),
                )
            return result.text

        # Capture the pending id from the kick-off call (if any).
        if (
            result is not None
            and hasattr(result, "pending")
            and result.pending
            and hasattr(result, "transcription_id")
        ):
            transcription_id = result.transcription_id

        # Run the polling loop whenever we have a transcription_id, whether
        # it came from this call's kick-off (result.pending) or from a
        # cached resume (transcription_id set at the top of the function).
        if transcription_id is not None:
            logger.debug(
                "Transcription pending for message %s, polling for completion...",
                message_id,
            )
            # Record (or refresh) the pending state in the cache.
            if cache_key is not None:
                _transcription_cache_set(
                    cache_key,
                    _TranscriptionCacheEntry(
                        transcription_id=transcription_id,
                        pending_until_ts=time.time() + _PENDING_TTL_SECONDS,
                    ),
                )

            max_attempts = 30
            for attempt in range(max_attempts):
                await asyncio.sleep(1)

                # If a previous poll recorded a FloodWaitError for this
                # message, honor the cooldown instead of burning more of it
                # on requests Telegram will reject.
                if cache_key is not None:
                    cached = _transcription_cache_get(cache_key)
                    if cached is not None and cached.state() == "rate_limited":
                        logger.debug(
                            "Polling stopped for message %s: rate-limited",
                            message_id,
                        )
                        return None

                try:
                    poll_result = await client(
                        TranscribeAudioRequest(peer=chat_entity, msg_id=message_id)
                    )

                    if (
                        hasattr(poll_result, "transcription_id")
                        and poll_result.transcription_id == transcription_id
                    ):
                        if hasattr(poll_result, "pending") and poll_result.pending:
                            continue
                        if hasattr(poll_result, "text") and poll_result.text:
                            logger.debug(
                                "Transcription completed for message %s after %s polls",
                                message_id,
                                attempt + 1,
                            )
                            if cache_key is not None:
                                _transcription_cache_set(
                                    cache_key,
                                    _TranscriptionCacheEntry(
                                        text=poll_result.text,
                                        done_until_ts=time.time() + _DONE_TTL_SECONDS,
                                    ),
                                )
                            return poll_result.text
                        logger.warning(
                            "Unexpected transcription state for message %s", message_id
                        )
                        return None

                except Exception as poll_error:
                    if isinstance(poll_error, FloodWaitError):
                        wait_seconds = getattr(poll_error, "seconds", 0) or 0
                        if cache_key is not None:
                            _transcription_cache_set(
                                cache_key,
                                _TranscriptionCacheEntry(
                                    until_ts=time.time() + wait_seconds
                                ),
                            )
                    logger.warning(
                        "Error polling transcription for message %s: %s",
                        message_id,
                        poll_error,
                    )
                    return None

            logger.warning(
                "Transcription polling timeout for message %s after %s attempts",
                message_id,
                max_attempts,
            )
            return None

        logger.debug("No transcription available for message %s", message_id)
        return None

    except FloodWaitError as e:
        # Telegram refuses to even start a fresh transcription for this
        # voice message until the cooldown expires. Record it so subsequent
        # calls within the window don't re-issue the request.
        wait_seconds = getattr(e, "seconds", 0) or 0
        if cache_key is not None:
            _transcription_cache_set(
                cache_key,
                _TranscriptionCacheEntry(until_ts=time.time() + wait_seconds),
            )
        logger.warning("Transcription rate-limited for message %s: %s", message_id, e)
        return None
    except RPCError as e:
        error_msg = str(e).lower()
        if "premium" in error_msg and "required" in error_msg:
            raise PremiumRequiredError(
                f"Premium account required for transcription: {e}"
            ) from None
        logger.warning("Transcription failed for message %s: %s", message_id, e)
        return None
    except Exception as e:
        logger.warning(
            "Unexpected error during transcription of message %s: %s",
            message_id,
            e,
        )
        return None


async def transcribe_voice_messages(
    messages: list[dict[str, Any]],
    chat_entity,
    *,
    client=None,
) -> None:
    """Transcribe voice and round video message dicts in parallel; TaskGroup cancels peers on PremiumRequiredError."""
    if client is None:
        client = await get_connected_client()

    is_premium = await _is_user_premium(client)

    if not is_premium:
        logger.debug(
            "Skipping voice transcription - user does not have Telegram Premium"
        )
        return

    media_messages = []
    for msg in messages:
        media = msg.get("media")
        has_video_type = (
            media
            and isinstance(media, dict)
            and media.get("type") in ("voice", "round_video")
        )
        has_transcription = "transcription" in msg

        if has_video_type and not has_transcription:
            media_messages.append(msg)

    if not media_messages:
        return

    logger.debug("Found %s voice/round messages to transcribe", len(media_messages))

    async def transcribe_task(msg_dict: dict[str, Any]) -> None:
        message_id = msg_dict["id"]
        transcription = await _transcribe_single_voice_message(
            client, chat_entity, message_id
        )
        if transcription:
            msg_dict["transcription"] = transcription
            logger.debug(
                "Transcribed voice/round message %s: %s...",
                message_id,
                transcription[:50],
            )

    try:
        async with asyncio.TaskGroup() as tg:
            for msg_dict in media_messages:
                tg.create_task(transcribe_task(msg_dict))
    except ExceptionGroup as eg:
        premium_errors = [
            e for e in eg.exceptions if isinstance(e, PremiumRequiredError)
        ]
        if premium_errors:
            logger.info(
                "Voice transcription cancelled - account lacks premium despite initial check"
            )
        else:
            raise
    except Exception as e:
        logger.warning("Voice transcription failed with unexpected error: %s", e)

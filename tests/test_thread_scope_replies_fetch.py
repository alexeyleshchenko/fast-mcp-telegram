"""Tests for get_messages thread_scope and full-thread SearchRequest path."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telethon.tl.functions.messages import GetForumTopicsByIDRequest, SearchRequest

from src.tools.search import search_messages_impl
from src.tools.search.replies import _fetch_replies
from src.utils.message_format import message_has_displayable_content
from tests.conftest import make_forum_channel


def _mock_message(msg_id: int, text: str = "hi"):
    msg = MagicMock()
    msg.id = msg_id
    msg.text = text
    msg.date = None
    return msg


def _search_result_with(*messages):
    result = MagicMock()
    result.messages = list(messages)
    return result


def _forum_topic_result(topic_id: int, *, title: str = "Topic", top_message: int = 1):
    topic = MagicMock(id=topic_id, title=title, top_message=top_message)
    return MagicMock(topics=[topic])


def _forum_in_topic_anchor(
    anchor_id: int,
    topic_id: int,
    *,
    reply_to_top_id: int | None = None,
):
    anchor = MagicMock()
    anchor.id = anchor_id
    anchor.reply_to = MagicMock(
        forum_topic=True,
        reply_to_msg_id=topic_id,
        reply_to_top_id=reply_to_top_id,
    )
    anchor.reply_to_msg_id = topic_id
    return anchor


def _forum_topic_root_anchor(topic_id: int):
    return _forum_in_topic_anchor(topic_id, topic_id, reply_to_top_id=None)


def _make_supergroup(chat_id: int, title: str = "Group"):
    """Non-forum megagroup (not broadcast) for supergroup thread tests."""
    return type(
        "Channel",
        (),
        {
            "id": chat_id,
            "title": title,
            "forum": False,
            "broadcast": False,
            "megagroup": True,
            "first_name": None,
            "last_name": None,
            "username": None,
            "phone": None,
        },
    )()


@pytest.mark.asyncio
async def test_thread_scope_full_supergroup_uses_search_with_top_msg_id():
    entity = _make_supergroup(100, "Group")
    client = AsyncMock()
    client.side_effect = [
        _search_result_with(_mock_message(10)),
        MagicMock(messages=[]),
    ]

    with (
        patch(
            "src.tools.search.results._build_result_for_message",
            new_callable=AsyncMock,
            return_value={"id": 10, "text": "hi"},
        ),
        patch(
            "src.tools.search.replies.transcribe_voice_messages",
            new_callable=AsyncMock,
        ),
    ):
        collected, _ = await _fetch_replies(
            client, entity, reply_to_id=500, limit=20, thread_scope="full"
        )

    assert len(collected) == 1
    req = client.call_args_list[-1][0][0]
    assert isinstance(req, SearchRequest)
    assert req.top_msg_id == 500


@pytest.mark.asyncio
async def test_thread_scope_full_forum_topic_uses_get_replies():
    entity = make_forum_channel(100, "Forum", forum=True)
    client = AsyncMock()
    client.get_messages = AsyncMock(return_value=_forum_topic_root_anchor(52))
    client.return_value = _forum_topic_result(52)

    async def fake_iter(*_args, **_kwargs):
        yield _mock_message(10)

    client.iter_messages = fake_iter

    with (
        patch(
            "src.tools.search.results._build_result_for_message",
            new_callable=AsyncMock,
            return_value={"id": 10, "text": "hi"},
        ),
        patch(
            "src.tools.search.replies.transcribe_voice_messages",
            new_callable=AsyncMock,
        ),
    ):
        collected, _ = await _fetch_replies(
            client, entity, reply_to_id=52, limit=20, thread_scope="full"
        )

    assert len(collected) == 1
    assert any(
        isinstance(c[0][0], GetForumTopicsByIDRequest) for c in client.call_args_list
    )
    assert not any(isinstance(c[0][0], SearchRequest) for c in client.call_args_list)


@pytest.mark.asyncio
async def test_thread_scope_direct_uses_iter_messages():
    entity = make_forum_channel(100, "Forum", forum=True)

    async def fake_iter(*_args, **_kwargs):
        yield _mock_message(11)

    client = AsyncMock()
    client.get_messages = AsyncMock(return_value=_forum_topic_root_anchor(52))
    client.return_value = _forum_topic_result(52)
    client.iter_messages = fake_iter

    with (
        patch(
            "src.tools.search.results._build_result_for_message",
            new_callable=AsyncMock,
            return_value={"id": 11, "text": "reply"},
        ),
        patch(
            "src.tools.search.replies.transcribe_voice_messages",
            new_callable=AsyncMock,
        ),
    ):
        collected, _ = await _fetch_replies(
            client, entity, reply_to_id=52, limit=20, thread_scope="direct"
        )

    assert len(collected) == 1
    assert any(
        isinstance(c[0][0], GetForumTopicsByIDRequest) for c in client.call_args_list
    )
    assert not any(isinstance(c[0][0], SearchRequest) for c in client.call_args_list)


@pytest.mark.asyncio
async def test_thread_scope_auto_forum_topic_id_uses_get_replies():
    entity = make_forum_channel(100, "Forum", forum=True)
    client = AsyncMock()
    client.get_messages = AsyncMock(return_value=_forum_topic_root_anchor(52))
    client.return_value = _forum_topic_result(52)

    async def fake_iter(*_args, **_kwargs):
        yield _mock_message(10)

    client.iter_messages = fake_iter

    with (
        patch(
            "src.tools.search.results._build_result_for_message",
            new_callable=AsyncMock,
            return_value={"id": 10, "text": "hi"},
        ),
        patch(
            "src.tools.search.replies.transcribe_voice_messages",
            new_callable=AsyncMock,
        ),
    ):
        collected, _ = await _fetch_replies(
            client, entity, reply_to_id=52, limit=20, thread_scope="auto"
        )

    assert len(collected) == 1
    assert any(
        isinstance(c[0][0], GetForumTopicsByIDRequest) for c in client.call_args_list
    )
    assert not any(isinstance(c[0][0], SearchRequest) for c in client.call_args_list)


@pytest.mark.asyncio
async def test_forum_topic_id_fallback_when_anchor_not_in_topic():
    """Topic ids misclassified as in-topic anchors fall back to GetReplies."""
    entity = make_forum_channel(100, "Forum", forum=True)
    anchor = MagicMock()
    anchor.id = 598
    anchor.reply_to = None
    anchor.reply_to_msg_id = None

    client = AsyncMock()
    client.get_messages = AsyncMock(return_value=anchor)
    client.return_value = MagicMock(topics=[])

    async def fake_iter(*_args, **kwargs):
        assert kwargs.get("reply_to") == 598
        yield _mock_message(600)

    client.iter_messages = fake_iter

    with (
        patch(
            "src.tools.search.forum_replies._extract_topic_metadata",
            return_value={},
        ),
        patch(
            "src.tools.search.results._build_result_for_message",
            new_callable=AsyncMock,
            return_value={"id": 600, "text": "topic reply"},
        ),
        patch(
            "src.tools.search.replies.transcribe_voice_messages",
            new_callable=AsyncMock,
        ),
        patch(
            "src.tools.search.replies._collect_forum_anchor_replies",
            new_callable=AsyncMock,
        ) as collect_mock,
    ):
        collected, _ = await _fetch_replies(
            client, entity, reply_to_id=598, limit=20, thread_scope="auto"
        )

    collect_mock.assert_not_called()
    assert len(collected) == 1
    assert collected[0]["id"] == 600
    assert not any(isinstance(c[0][0], SearchRequest) for c in client.call_args_list)


@pytest.mark.asyncio
async def test_forum_topic_id_skips_in_topic_collect_when_is_forum_topic_id():
    entity = make_forum_channel(100, "Forum", forum=True)
    client = AsyncMock()
    client.get_messages = AsyncMock(return_value=_forum_topic_root_anchor(52))
    client.return_value = _forum_topic_result(52)

    async def fake_iter(*_args, **_kwargs):
        yield _mock_message(10)

    client.iter_messages = fake_iter

    with (
        patch(
            "src.tools.search.results._build_result_for_message",
            new_callable=AsyncMock,
            return_value={"id": 10, "text": "hi"},
        ),
        patch(
            "src.tools.search.replies.transcribe_voice_messages",
            new_callable=AsyncMock,
        ),
        patch(
            "src.tools.search.replies._collect_forum_anchor_replies",
            new_callable=AsyncMock,
        ) as collect_mock,
    ):
        collected, _ = await _fetch_replies(
            client, entity, reply_to_id=52, limit=20, thread_scope="auto"
        )

    collect_mock.assert_not_called()
    assert len(collected) == 1


@pytest.mark.asyncio
async def test_thread_scope_auto_forum_non_topic_uses_topic_search():
    entity = make_forum_channel(100, "Forum", forum=True)
    anchor = _forum_in_topic_anchor(999, 52)

    msg12 = _mock_message(12)
    msg12.reply_to_msg_id = 999

    client = AsyncMock()
    client.get_messages = AsyncMock(return_value=anchor)
    client.side_effect = [
        MagicMock(topics=[]),  # _forum_topic_id_from_anchor checks topic 52
        MagicMock(topics=[]),  # routing GetForumTopicsByID for anchor 999
        _search_result_with(msg12),
        MagicMock(messages=[]),
    ]

    with (
        patch(
            "src.tools.search.results._build_result_for_message",
            new_callable=AsyncMock,
            return_value={"id": 12, "text": "reply"},
        ),
        patch(
            "src.tools.search.replies.transcribe_voice_messages",
            new_callable=AsyncMock,
        ),
    ):
        collected, _ = await _fetch_replies(
            client, entity, reply_to_id=999, limit=20, thread_scope="auto"
        )

    assert len(collected) == 1
    search_calls = [
        c[0][0] for c in client.call_args_list if isinstance(c[0][0], SearchRequest)
    ]
    assert search_calls
    assert search_calls[0].top_msg_id == 52
    assert search_calls[0].offset_id == 999 + 100


@pytest.mark.asyncio
async def test_forum_in_topic_replies_hydrate_search_stubs():
    """messages.search stubs lack text; full messages are loaded before building."""
    entity = make_forum_channel(100, "Forum", forum=True)
    anchor = _forum_in_topic_anchor(67596, 52, reply_to_top_id=52)

    stub = _mock_message(99)
    stub.reply_to_msg_id = 67596
    stub.text = None
    stub.message = None
    stub.caption = None
    stub.media = None
    stub.action = None

    full = _mock_message(99)
    full.reply_to_msg_id = 67596
    full.text = "hydrated reply"
    full.media = None

    client = AsyncMock()
    client.get_messages = AsyncMock(side_effect=[anchor, [full]])
    client.side_effect = [
        MagicMock(topics=[]),
        _search_result_with(stub),
        MagicMock(messages=[]),
    ]

    with (
        patch(
            "src.tools.search.results._build_result_for_message",
            new_callable=AsyncMock,
            return_value={"id": 99, "text": "hydrated reply"},
        ) as build_mock,
        patch(
            "src.tools.search.replies.transcribe_voice_messages",
            new_callable=AsyncMock,
        ),
    ):
        collected, _ = await _fetch_replies(
            client, entity, reply_to_id=67596, limit=20, thread_scope="auto"
        )

    assert len(collected) == 1
    args, _ = build_mock.call_args_list[0]
    hydrated_call = args[0]
    assert hydrated_call.text == "hydrated reply"


@pytest.mark.asyncio
async def test_forum_stub_topic_id_not_treated_as_topic_anchor():
    """GetForumTopicsByID can return a stub; only real topics use getReplies."""
    entity = make_forum_channel(100, "Forum", forum=True)
    stub_topic = MagicMock(id=67596, title=None, top_message=None)
    anchor = _forum_in_topic_anchor(67596, 52)

    msg99 = _mock_message(99)
    msg99.reply_to_msg_id = 67596

    client = AsyncMock()
    client.get_messages = AsyncMock(return_value=anchor)
    client.side_effect = [
        MagicMock(topics=[]),  # _forum_topic_id_from_anchor checks topic 52
        MagicMock(topics=[stub_topic]),  # routing GetForumTopicsByID for 67596
        _search_result_with(msg99),
        MagicMock(messages=[]),
    ]

    with (
        patch(
            "src.tools.search.results._build_result_for_message",
            new_callable=AsyncMock,
            return_value={"id": 99, "text": "reply"},
        ),
        patch(
            "src.tools.search.replies.transcribe_voice_messages",
            new_callable=AsyncMock,
        ),
    ):
        collected, _ = await _fetch_replies(
            client, entity, reply_to_id=67596, limit=20, thread_scope="full"
        )

    assert len(collected) == 1
    assert not any(
        isinstance(c[0][0], SearchRequest) and c[0][0].top_msg_id == 67596
        for c in client.call_args_list
    )


@pytest.mark.asyncio
async def test_forum_in_topic_reply_to_top_id_skips_topic_check():
    entity = make_forum_channel(100, "Forum", forum=True)
    anchor = _forum_in_topic_anchor(67599, 14194, reply_to_top_id=14194)

    reply = _mock_message(67616)
    reply.reply_to_msg_id = 67599

    client = AsyncMock()
    client.get_messages = AsyncMock(return_value=anchor)
    client.side_effect = [_search_result_with(reply), MagicMock(messages=[])]

    with (
        patch(
            "src.tools.search.results._build_result_for_message",
            new_callable=AsyncMock,
            return_value={"id": 67616, "text": "reply"},
        ),
        patch(
            "src.tools.search.replies.transcribe_voice_messages",
            new_callable=AsyncMock,
        ),
    ):
        collected, _ = await _fetch_replies(
            client, entity, reply_to_id=67599, limit=20, thread_scope="auto"
        )

    assert len(collected) == 1
    assert not any(
        isinstance(c[0][0], GetForumTopicsByIDRequest) for c in client.call_args_list
    )
    search_calls = [
        c[0][0] for c in client.call_args_list if isinstance(c[0][0], SearchRequest)
    ]
    assert search_calls[0].offset_id == 67599 + 100
    assert search_calls[0].top_msg_id == 14194


@pytest.mark.asyncio
async def test_forum_in_topic_widen_ladder_on_miss():
    entity = make_forum_channel(100, "Forum", forum=True)
    anchor = _forum_in_topic_anchor(67599, 14194, reply_to_top_id=14194)

    reply = _mock_message(67616)
    reply.reply_to_msg_id = 67599

    client = AsyncMock()
    client.get_messages = AsyncMock(return_value=anchor)
    client.side_effect = [
        MagicMock(messages=[]),
        _search_result_with(reply),
        MagicMock(messages=[]),
    ]

    with (
        patch(
            "src.tools.search.results._build_result_for_message",
            new_callable=AsyncMock,
            return_value={"id": 67616, "text": "reply"},
        ),
        patch(
            "src.tools.search.replies.transcribe_voice_messages",
            new_callable=AsyncMock,
        ),
    ):
        collected, _ = await _fetch_replies(
            client, entity, reply_to_id=67599, limit=20, thread_scope="auto"
        )

    assert len(collected) == 1
    search_calls = [
        c[0][0] for c in client.call_args_list if isinstance(c[0][0], SearchRequest)
    ]
    assert search_calls[0].offset_id == 67599 + 100
    assert search_calls[1].offset_id == 67599 + 200


@pytest.mark.asyncio
async def test_forum_search_stub_without_reply_to_enriched_before_filter():
    """Search stubs may omit reply_to; reload before reply_to_msg_id filter."""
    entity = make_forum_channel(100, "Forum", forum=True)
    anchor = _forum_in_topic_anchor(67599, 14194, reply_to_top_id=14194)

    stub = _mock_message(67616)
    stub.reply_to_msg_id = None
    stub.reply_to = None
    stub.text = "reply text"
    stub.action = None

    full = _mock_message(67616)
    full.reply_to_msg_id = 67599
    full.text = "reply text"

    client = AsyncMock()
    client.get_messages = AsyncMock(side_effect=[anchor, [full]])
    client.side_effect = [_search_result_with(stub), MagicMock(messages=[])]

    with (
        patch(
            "src.tools.search.results._build_result_for_message",
            new_callable=AsyncMock,
            return_value={"id": 67616, "text": "reply text"},
        ),
        patch(
            "src.tools.search.replies.transcribe_voice_messages",
            new_callable=AsyncMock,
        ),
    ):
        collected, _ = await _fetch_replies(
            client, entity, reply_to_id=67599, limit=20, thread_scope="auto"
        )

    assert len(collected) == 1
    assert client.get_messages.await_count == 2


@pytest.mark.asyncio
async def test_forum_id_window_fallback_when_search_filter_misses():
    """Direct id window load when search batch lacks usable reply_to metadata."""
    entity = make_forum_channel(100, "Forum", forum=True)
    anchor = _forum_in_topic_anchor(67599, 14194, reply_to_top_id=14194)

    stub = _mock_message(67616)
    stub.reply_to_msg_id = 14194
    stub.text = "reply text"
    stub.action = None

    full = _mock_message(67616)
    full.reply_to_msg_id = 67599
    full.text = "reply text"

    client = AsyncMock()
    client.get_messages = AsyncMock(side_effect=[anchor, [full]])
    client.side_effect = [_search_result_with(stub), MagicMock(messages=[])]

    with (
        patch(
            "src.tools.search.results._build_result_for_message",
            new_callable=AsyncMock,
            return_value={"id": 67616, "text": "reply text"},
        ),
        patch(
            "src.tools.search.replies.transcribe_voice_messages",
            new_callable=AsyncMock,
        ),
    ):
        collected, _ = await _fetch_replies(
            client, entity, reply_to_id=67599, limit=20, thread_scope="auto"
        )

    assert len(collected) == 1
    assert client.get_messages.await_count >= 2
    reloaded_ids = client.get_messages.await_args_list[-1].kwargs.get("ids") or []
    assert 67616 in reloaded_ids


@pytest.mark.asyncio
async def test_forum_search_stub_topic_parent_reloaded_before_filter():
    """Search stubs may set reply_to_msg_id to topic root instead of the anchor."""
    entity = make_forum_channel(100, "Forum", forum=True)
    anchor = _forum_in_topic_anchor(67599, 14194, reply_to_top_id=14194)

    stub = _mock_message(67616)
    stub.reply_to_msg_id = 14194
    stub.reply_to = None
    stub.text = "reply text"
    stub.action = None

    full = _mock_message(67616)
    full.reply_to_msg_id = 67599
    full.text = "reply text"

    client = AsyncMock()
    client.get_messages = AsyncMock(side_effect=[anchor, [full]])
    client.side_effect = [_search_result_with(stub), MagicMock(messages=[])]

    with (
        patch(
            "src.tools.search.results._build_result_for_message",
            new_callable=AsyncMock,
            return_value={"id": 67616, "text": "reply text"},
        ),
        patch(
            "src.tools.search.replies.transcribe_voice_messages",
            new_callable=AsyncMock,
        ),
    ):
        collected, _ = await _fetch_replies(
            client, entity, reply_to_id=67599, limit=20, thread_scope="auto"
        )

    assert len(collected) == 1
    assert client.get_messages.await_count == 2


def test_message_has_displayable_content_from_message_field():
    stub = _mock_message(1)
    stub.text = None
    stub.message = "body in message field"
    stub.media = None
    stub.action = None
    assert message_has_displayable_content(stub)


@pytest.mark.asyncio
async def test_forum_conditional_reload_skipped_when_stub_has_text():
    entity = make_forum_channel(100, "Forum", forum=True)
    anchor = _forum_in_topic_anchor(67599, 14194, reply_to_top_id=14194)

    reply = _mock_message(67616, text="already has text")
    reply.reply_to_msg_id = 67599

    client = AsyncMock()
    client.get_messages = AsyncMock(return_value=anchor)
    client.side_effect = [_search_result_with(reply), MagicMock(messages=[])]

    with (
        patch(
            "src.tools.search.results._build_result_for_message",
            new_callable=AsyncMock,
            return_value={"id": 67616, "text": "already has text"},
        ),
        patch(
            "src.tools.search.replies.transcribe_voice_messages",
            new_callable=AsyncMock,
        ),
    ):
        await _fetch_replies(
            client, entity, reply_to_id=67599, limit=20, thread_scope="auto"
        )

    assert client.get_messages.await_count == 1


@pytest.mark.asyncio
async def test_thread_scope_auto_forum_general_topic_no_search():
    entity = make_forum_channel(100, "Forum", forum=True)

    async def fake_iter(*_args, **_kwargs):
        yield _mock_message(2)

    client = MagicMock()
    client.iter_messages = fake_iter

    with (
        patch(
            "src.tools.search.results._build_result_for_message",
            new_callable=AsyncMock,
            return_value={"id": 2, "text": "reply"},
        ),
        patch(
            "src.tools.search.replies.transcribe_voice_messages",
            new_callable=AsyncMock,
        ),
    ):
        collected, _ = await _fetch_replies(
            client, entity, reply_to_id=1, limit=20, thread_scope="auto"
        )

    assert len(collected) == 1
    assert client.call_count == 0


@pytest.mark.asyncio
async def test_thread_scope_auto_channel_discussion_uses_get_replies():
    channel = MagicMock()
    channel.broadcast = True
    channel.forum = False
    discussion_entity = MagicMock()
    discussion_entity.forum = False
    discussion_entity.broadcast = False

    client = MagicMock()

    async def fake_iter(*_args, **_kwargs):
        yield _mock_message(20)

    client.iter_messages = fake_iter

    discussion_info = {
        "discussion_peer": discussion_entity,
        "discussion_msg_id": 77,
        "discussion_chat_id": "-100222",
        "discussion_total_count": 5,
    }

    with (
        patch(
            "src.tools.search.replies.get_post_discussion_info",
            new_callable=AsyncMock,
            return_value=discussion_info,
        ),
        patch(
            "src.tools.search.results._build_result_for_message",
            new_callable=AsyncMock,
            return_value={"id": 20, "text": "comment"},
        ),
        patch(
            "src.tools.search.replies.transcribe_voice_messages",
            new_callable=AsyncMock,
        ),
    ):
        collected, meta = await _fetch_replies(
            client, channel, reply_to_id=42, limit=20, thread_scope="auto"
        )

    assert len(collected) == 1
    assert meta is not None
    assert client.call_count == 0


@pytest.mark.asyncio
async def test_thread_scope_full_without_reply_to_id_errors():
    result = await search_messages_impl(chat_id="-1001", thread_scope="full")
    assert "error" in result
    assert "thread_scope requires reply_to_id" in result["error"]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_forum_topic_auto_includes_nested_reply_live():
    from tests.test_forum_topics_minimal import _get_live_forum_entity_or_skip

    entity = await _get_live_forum_entity_or_skip()
    from telethon.tl.functions.messages import GetForumTopicsRequest

    from src.client.connection import get_connected_client
    from src.utils.entity import compute_entity_identifier

    client = await get_connected_client()
    chat_id = compute_entity_identifier(entity)
    assert chat_id is not None

    topics_result = await client(
        GetForumTopicsRequest(
            peer=entity, offset_date=None, offset_id=0, offset_topic=0, limit=5, q=""
        )
    )
    topics = getattr(topics_result, "topics", None) or []
    topic_id = next(
        (t.id for t in topics if getattr(t, "id", None) not in (None, 1)), None
    )
    if topic_id is None:
        pytest.skip("no non-general forum topic available")

    preview = await search_messages_impl(
        chat_id=chat_id,
        reply_to_id=topic_id,
        limit=50,
        thread_scope="auto",
    )
    if "error" in preview:
        pytest.skip(f"could not load topic: {preview.get('error')}")

    messages = preview.get("messages") or []
    nested = [
        m
        for m in messages
        if m.get("reply_to_msg_id") and m.get("reply_to_msg_id") != topic_id
    ]
    if not nested:
        pytest.skip("no nested replies in sampled topic for integration check")

    assert any(m.get("id") for m in nested)

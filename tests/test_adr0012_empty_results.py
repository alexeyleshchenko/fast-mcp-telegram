"""Integration-style tests for ADR 0012 empty-result success + note responses."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.client.connection import SessionNotAuthorizedError
from src.tools.search.replies import _handle_reply_mode
from src.tools.search.search_mode import _gather_global_batch, _handle_query_mode


@pytest.mark.asyncio
@patch("src.tools.search.search_mode._collect_messages_in_chat", new_callable=AsyncMock)
@patch("src.tools.search.search_mode.get_connected_client", new_callable=AsyncMock)
async def test_handle_query_mode_empty_search_returns_note(mock_client, mock_collect):
    """Real _handle_query_mode path: empty collection → success + note."""
    mock_collect.return_value = None
    mock_client.return_value = MagicMock()

    result = await _handle_query_mode(
        query="missing-term",
        chat_id="me",
        limit=10,
        min_date=None,
        max_date=None,
        chat_type=None,
        public=None,
        auto_expand_batches=0,
        include_total_count=False,
        params={"operation": "get_messages"},
    )

    assert result["messages"] == []
    assert result["has_more"] is False
    assert "note" in result
    assert "missing-term" in result["note"]
    assert "error" not in result
    assert result.get("ok") is not False


@pytest.mark.asyncio
async def test_gather_global_batch_raises_on_session_not_authorized():
    """Global search batch must not return empty messages when all terms are unauthorized."""
    mock_client = AsyncMock(side_effect=SessionNotAuthorizedError("f9NdKOLR..."))
    terms = [{"query": "telegram", "offset_id": 0, "has_more": True}]

    with pytest.raises(SessionNotAuthorizedError):
        await _gather_global_batch(mock_client, terms, 10, None, None)


@pytest.mark.asyncio
@patch("src.tools.search.search_mode._collect_messages_global", new_callable=AsyncMock)
@patch("src.tools.search.search_mode.get_connected_client", new_callable=AsyncMock)
async def test_handle_query_mode_global_auth_error_from_collect(
    mock_client, mock_collect
):
    """Global search propagates session auth failures instead of empty success."""
    mock_client.return_value = MagicMock()
    mock_collect.side_effect = SessionNotAuthorizedError("f9NdKOLR...")

    result = await _handle_query_mode(
        query="telegram",
        chat_id=None,
        limit=10,
        min_date=None,
        max_date=None,
        chat_type=None,
        public=None,
        auto_expand_batches=0,
        include_total_count=False,
        params={"operation": "get_messages"},
    )

    assert result["ok"] is False
    assert result["code"] == -32002
    assert result["action"] == "AUTHENTICATE_SESSION"


@pytest.mark.asyncio
@patch("src.tools.search.replies._fetch_replies", new_callable=AsyncMock)
@patch("src.tools.search.replies.get_entity_by_id", new_callable=AsyncMock)
@patch("src.tools.search.replies.get_connected_client", new_callable=AsyncMock)
async def test_handle_reply_mode_empty_returns_note(
    mock_client, mock_entity, mock_fetch
):
    """Real _handle_reply_mode path: no replies → success + note."""
    mock_client.return_value = MagicMock()
    mock_entity.return_value = MagicMock(id=-100123)
    mock_fetch.return_value = ([], None)

    result = await _handle_reply_mode(
        chat_id="-1001234567890",
        reply_to_id=42,
        limit=20,
        query=None,
        params={"operation": "get_messages"},
    )

    assert result["messages"] == []
    assert result["has_more"] is False
    assert result["reply_to_id"] == 42
    assert "note" in result
    assert "42" in result["note"]
    assert "error" not in result
    assert result.get("ok") is not False

"""Tests for account-prefixed MCP tool names middleware."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import mcp.types as mt
import pytest
from fastmcp.exceptions import ToolError
from fastmcp.tools.base import Tool, ToolResult

from src.client.connection import SessionNotAuthorizedError
from src.config.server_config import ServerConfig, ServerMode
from src.server_components.account_prefix_cache import (
    _account_prefix_cache,
    clear_account_prefix_cache,
)
from src.server_components.account_tool_prefix_middleware import (
    AccountPrefixedToolsMiddleware,
    _prefixed_tool_name,
    _resolve_account_prefix,
    _strip_account_prefix,
)
from src.server_components.middleware_register import register_mcp_middleware


@pytest.fixture(autouse=True)
def _clear_prefix_cache():
    clear_account_prefix_cache()
    yield
    clear_account_prefix_cache()


class TestPrefixHelpers:
    def test_round_trip(self):
        assert _strip_account_prefix(
            "alice", _prefixed_tool_name("alice", "send_message")
        ) == ("send_message")

    def test_wrong_prefix_returns_none(self):
        assert _strip_account_prefix("alice", "bob_send_message") is None


class TestAccountPrefixCache:
    @pytest.mark.asyncio
    async def test_lru_eviction(self, monkeypatch):
        monkeypatch.setattr(
            "src.server_components.account_prefix_cache.cfg",
            lambda: SimpleNamespace(max_active_sessions=2),
        )
        _account_prefix_cache.put("token-a", "alice")
        _account_prefix_cache.put("token-b", "bob")
        _account_prefix_cache.put("token-c", "carol")

        mock_client = AsyncMock()
        mock_client.get_me.return_value = SimpleNamespace(username="alice", id=1)

        with patch(
            "src.server_components.account_tool_prefix_middleware.get_connected_client",
            return_value=mock_client,
        ):
            label = await _resolve_account_prefix("token-a")

        assert label == "alice"
        mock_client.assert_not_called()


class TestResolveAccountPrefix:
    @pytest.mark.asyncio
    async def test_uses_username_when_set(self):
        mock_client = AsyncMock()
        mock_client.get_me.return_value = SimpleNamespace(username="alice", id=123)

        with patch(
            "src.server_components.account_tool_prefix_middleware.get_connected_client",
            return_value=mock_client,
        ):
            assert await _resolve_account_prefix("tok123") == "alice"

    @pytest.mark.asyncio
    async def test_falls_back_to_user_id(self):
        mock_client = AsyncMock()
        mock_client.get_me.return_value = SimpleNamespace(username=None, id=123456789)

        with patch(
            "src.server_components.account_tool_prefix_middleware.get_connected_client",
            return_value=mock_client,
        ):
            assert await _resolve_account_prefix("tok123") == "123456789"

    @pytest.mark.asyncio
    async def test_propagates_session_not_authorized(self):
        with (
            patch(
                "src.server_components.account_tool_prefix_middleware.get_connected_client",
                side_effect=SessionNotAuthorizedError("not authorized"),
            ),
            pytest.raises(SessionNotAuthorizedError),
        ):
            await _resolve_account_prefix("tok123")

    @pytest.mark.asyncio
    async def test_resolve_skips_api_when_me_none_cached(self):
        mock_client = AsyncMock()
        mock_client.get_me.return_value = None

        with patch(
            "src.server_components.account_tool_prefix_middleware.get_connected_client",
            return_value=mock_client,
        ):
            assert await _resolve_account_prefix("tok123") is None
            assert await _resolve_account_prefix("tok123") is None

        assert mock_client.get_me.await_count == 1

    @pytest.mark.asyncio
    async def test_resolve_retries_after_unresolved_ttl(self, monkeypatch):
        import time

        monkeypatch.setattr(
            "src.server_components.account_prefix_cache._UNRESOLVED_TTL_SECONDS",
            0.01,
        )
        mock_client = AsyncMock()
        mock_client.get_me.side_effect = [
            None,
            SimpleNamespace(username="alice", id=1),
        ]

        with patch(
            "src.server_components.account_tool_prefix_middleware.get_connected_client",
            return_value=mock_client,
        ):
            assert await _resolve_account_prefix("tok123") is None
            time.sleep(0.02)
            assert await _resolve_account_prefix("tok123") == "alice"

        assert mock_client.get_me.await_count == 2


class TestAccountPrefixCacheMaxSize:
    def test_handles_max_sessions_one(self, monkeypatch):
        monkeypatch.setattr(
            "src.server_components.account_prefix_cache.cfg",
            lambda: SimpleNamespace(max_active_sessions=1),
        )
        _account_prefix_cache.put("token-a", "alice")
        _account_prefix_cache.put("token-b", "bob")

        assert "token-a" not in _account_prefix_cache._cache
        assert _account_prefix_cache._cache["token-b"] == "bob"


def _sample_tools() -> list[Tool]:
    return [Tool.from_function(lambda: None, name="send_message")]


def _list_context() -> MagicMock:
    return MagicMock()


def _call_context(name: str) -> MagicMock:
    ctx = MagicMock()
    ctx.message = mt.CallToolRequestParams(name=name, arguments={})
    ctx.copy.return_value = MagicMock()
    return ctx


class TestAccountPrefixedToolsMiddleware:
    @pytest.mark.asyncio
    async def test_list_tools_no_token(self):
        middleware = AccountPrefixedToolsMiddleware()
        tools = _sample_tools()
        call_next = AsyncMock(return_value=tools)

        with patch(
            "src.server_components.account_tool_prefix_middleware.get_access_token",
            return_value=None,
        ):
            result = await middleware.on_list_tools(_list_context(), call_next)

        assert result == tools
        assert result[0].name == "send_message"

    @pytest.mark.asyncio
    async def test_list_tools_with_username(self):
        middleware = AccountPrefixedToolsMiddleware()
        call_next = AsyncMock(return_value=_sample_tools())
        token = SimpleNamespace(token="tok123")

        with (
            patch(
                "src.server_components.account_tool_prefix_middleware.get_access_token",
                return_value=token,
            ),
            patch(
                "src.server_components.account_tool_prefix_middleware._resolve_account_prefix",
                new_callable=AsyncMock,
                return_value="alice",
            ),
        ):
            result = await middleware.on_list_tools(_list_context(), call_next)

        assert result[0].name == "alice_send_message"

    @pytest.mark.asyncio
    async def test_call_tool_strips_prefix(self):
        middleware = AccountPrefixedToolsMiddleware()
        ctx = _call_context("alice_send_message")
        modified_ctx = MagicMock()
        ctx.copy.return_value = modified_ctx
        call_next = AsyncMock(return_value=ToolResult(content=[]))
        token = SimpleNamespace(token="tok123")

        with (
            patch(
                "src.server_components.account_tool_prefix_middleware.get_access_token",
                return_value=token,
            ),
            patch(
                "src.server_components.account_tool_prefix_middleware._resolve_account_prefix",
                new_callable=AsyncMock,
                return_value="alice",
            ),
        ):
            await middleware.on_call_tool(ctx, call_next)

        ctx.copy.assert_called_once()
        assert ctx.copy.call_args.kwargs["message"].name == "send_message"
        call_next.assert_awaited_once_with(modified_ctx)

    @pytest.mark.asyncio
    async def test_call_tool_wrong_prefix_raises(self):
        middleware = AccountPrefixedToolsMiddleware()
        ctx = _call_context("bob_send_message")
        call_next = AsyncMock(return_value=ToolResult(content=[]))
        token = SimpleNamespace(token="tok123")

        with (
            patch(
                "src.server_components.account_tool_prefix_middleware.get_access_token",
                return_value=token,
            ),
            patch(
                "src.server_components.account_tool_prefix_middleware._resolve_account_prefix",
                new_callable=AsyncMock,
                return_value="alice",
            ),
            pytest.raises(ToolError, match="account prefix 'alice_'"),
        ):
            await middleware.on_call_tool(ctx, call_next)

        call_next.assert_not_called()

    @pytest.mark.asyncio
    async def test_call_tool_unprefixed_raises(self):
        middleware = AccountPrefixedToolsMiddleware()
        ctx = _call_context("send_message")
        call_next = AsyncMock(return_value=ToolResult(content=[]))
        token = SimpleNamespace(token="tok123")

        with (
            patch(
                "src.server_components.account_tool_prefix_middleware.get_access_token",
                return_value=token,
            ),
            patch(
                "src.server_components.account_tool_prefix_middleware._resolve_account_prefix",
                new_callable=AsyncMock,
                return_value="alice",
            ),
            pytest.raises(ToolError, match="account prefix 'alice_'"),
        ):
            await middleware.on_call_tool(ctx, call_next)

        call_next.assert_not_called()


class TestRegisterMcpMiddleware:
    def test_skips_when_flag_off(self):
        mcp = MagicMock()
        config = ServerConfig(
            _cli_parse_args=[],
            server_mode=ServerMode.HTTP_AUTH,
            prefix_mcp_tools_with_account=False,
        )
        register_mcp_middleware(mcp, config)
        mcp.add_middleware.assert_not_called()

    def test_registers_when_flag_on(self):
        mcp = MagicMock()
        config = ServerConfig(
            _cli_parse_args=[],
            server_mode=ServerMode.HTTP_AUTH,
            prefix_mcp_tools_with_account=True,
        )
        register_mcp_middleware(mcp, config)
        mcp.add_middleware.assert_called_once()
        assert mcp.add_middleware.call_args.args[0].__class__.__name__ == (
            "AccountPrefixedToolsMiddleware"
        )


class TestPrefixConfigParsing:
    def test_env_flag_parses_true(self, monkeypatch):
        monkeypatch.setenv("PREFIX_MCP_TOOLS_WITH_ACCOUNT", "true")
        config = ServerConfig(_cli_parse_args=[])
        assert config.prefix_mcp_tools_with_account is True

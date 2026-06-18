"""Tests for /.well-known/mcp/ discovery endpoints (SEP-1649 + SEP-1960)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from starlette.requests import Request

from src.server_components.server_card import (
    _compute_etag,
    register_server_card_route,
)


class _FakeTool:
    """Lightweight stand-in for MCP tool objects."""

    def __init__(self, name: str, description: str, schema: dict):
        self.name = name
        self.description = description
        self.parameters = schema


@pytest.fixture()
def mock_mcp():
    """Minimal mock FastMCP instance with custom_route and list_tools."""
    mcp = MagicMock()
    mcp.list_tools = AsyncMock(
        return_value=[
            _FakeTool(
                name="send_message",
                description="Send a Telegram message",
                schema={"type": "object", "properties": {}},
            ),
            _FakeTool(
                name="read_messages",
                description="Read Telegram messages",
                schema={"type": "object", "properties": {}},
            ),
        ]
    )
    # Capture registered routes
    _routes: dict[str, dict[str, object]] = {}

    def _custom_route(path: str, *, methods: list[str]):
        def decorator(func):
            for method in methods:
                _routes.setdefault(path, {})[method] = func
            return func

        return decorator

    mcp.custom_route = _custom_route
    mcp._routes = _routes
    return mcp


# ── SEP-1649: server-card.json ────────────────────────────────────────


class TestServerCard:
    """Tests for /.well-known/mcp/server-card.json endpoint."""

    def _get_handler(self, mock_mcp):
        """Register routes and return the GET handler for server-card.json."""
        register_server_card_route(mock_mcp)
        return mock_mcp._routes["/.well-known/mcp/server-card.json"]["GET"]

    def _make_request(self, headers=None):
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/.well-known/mcp/server-card.json",
            "headers": [],
        }
        if headers:
            scope["headers"] = [
                (k.lower().encode(), v.encode()) for k, v in headers.items()
            ]
        return Request(scope)

    @pytest.mark.asyncio
    async def test_returns_valid_card(self, mock_mcp):
        handler = self._get_handler(mock_mcp)
        resp = await handler(self._make_request())
        body = resp.body if hasattr(resp, "body") else resp.content
        import json

        card = json.loads(body)
        assert card["name"] == "fast-mcp-telegram"
        assert card["version"]
        assert card["title"] == "Telegram MCP Server"
        assert "description" in card
        assert "websiteUrl" in card
        assert "repository" in card
        assert "remotes" in card
        assert "capabilities" in card
        assert "authentication" in card
        assert len(card["tools"]) == 2
        assert card["tools"][0]["name"] == "send_message"

    @pytest.mark.asyncio
    async def test_has_spec_headers(self, mock_mcp):
        handler = self._get_handler(mock_mcp)
        resp = await handler(self._make_request())
        assert resp.headers.get("X-Content-Type-Options") == "nosniff"
        assert resp.headers.get("Access-Control-Allow-Origin") == "*"
        assert "Cache-Control" in resp.headers
        assert "ETag" in resp.headers

    @pytest.mark.asyncio
    async def test_etag_304(self, mock_mcp):
        handler = self._get_handler(mock_mcp)
        first = await handler(self._make_request())
        etag = first.headers.get("ETag")
        assert etag
        second = await handler(self._make_request(headers={"If-None-Match": etag}))
        assert second.status_code == 304

    @pytest.mark.asyncio
    async def test_has_schema_url(self, mock_mcp):
        handler = self._get_handler(mock_mcp)
        resp = await handler(self._make_request())
        import json

        card = json.loads(resp.body)
        assert card["$schema"].startswith("https://static.modelcontextprotocol.io/")

    @pytest.mark.asyncio
    async def test_remotes_include_streamable_http_and_stdio(self, mock_mcp):
        handler = self._get_handler(mock_mcp)
        resp = await handler(self._make_request())
        import json

        card = json.loads(resp.body)
        transport_types = [
            r["transport"]["type"] for r in card["remotes"]
        ]
        assert "streamable-http" in transport_types
        assert "stdio" in transport_types

    @pytest.mark.asyncio
    async def test_capabilities_flags(self, mock_mcp):
        handler = self._get_handler(mock_mcp)
        resp = await handler(self._make_request())
        import json

        card = json.loads(resp.body)
        assert card["capabilities"]["tools"]["listChanged"] is True
        assert card["capabilities"]["resources"]["listChanged"] is False
        assert card["capabilities"]["prompts"]["listChanged"] is False


# ── SEP-1960: /.well-known/mcp ───────────────────────────────────────


class TestMcpManifest:
    """Tests for /.well-known/mcp endpoint."""

    def _get_handler(self, mock_mcp):
        register_server_card_route(mock_mcp)
        return mock_mcp._routes["/.well-known/mcp"]["GET"]

    def _make_request(self, headers=None):
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/.well-known/mcp",
            "headers": [],
        }
        if headers:
            scope["headers"] = [
                (k.lower().encode(), v.encode()) for k, v in headers.items()
            ]
        return Request(scope)

    @pytest.mark.asyncio
    async def test_returns_valid_manifest(self, mock_mcp):
        handler = self._get_handler(mock_mcp)
        resp = await handler(self._make_request())
        import json

        manifest = json.loads(resp.body)
        assert manifest["name"] == "fast-mcp-telegram"
        assert manifest["version"]
        assert "remotes" in manifest
        assert "capabilities" in manifest
        assert manifest["toolCount"] == 2
        assert "2025-03-26" in manifest["protocolVersions"]

    @pytest.mark.asyncio
    async def test_capabilities_are_booleans(self, mock_mcp):
        handler = self._get_handler(mock_mcp)
        resp = await handler(self._make_request())
        import json

        manifest = json.loads(resp.body)
        assert manifest["capabilities"]["tools"] is True
        assert manifest["capabilities"]["resources"] is False
        assert manifest["capabilities"]["prompts"] is False

    @pytest.mark.asyncio
    async def test_has_spec_headers(self, mock_mcp):
        handler = self._get_handler(mock_mcp)
        resp = await handler(self._make_request())
        assert resp.headers.get("X-Content-Type-Options") == "nosniff"
        assert resp.headers.get("Access-Control-Allow-Origin") == "*"

    @pytest.mark.asyncio
    async def test_etag_304(self, mock_mcp):
        handler = self._get_handler(mock_mcp)
        first = await handler(self._make_request())
        etag = first.headers.get("ETag")
        assert etag
        second = await handler(self._make_request(headers={"If-None-Match": etag}))
        assert second.status_code == 304


# ── CORS preflight ───────────────────────────────────────────────────


class TestCorsPreflight:
    """Tests for OPTIONS handlers on both discovery endpoints."""

    @pytest.mark.asyncio
    async def test_options_returns_spec_headers(self, mock_mcp):
        register_server_card_route(mock_mcp)
        handler = mock_mcp._routes["/.well-known/mcp"]["OPTIONS"]
        scope = {
            "type": "http",
            "method": "OPTIONS",
            "path": "/.well-known/mcp",
            "headers": [],
        }
        resp = await handler(Request(scope))
        assert resp.status_code == 204
        assert resp.headers.get("Access-Control-Allow-Origin") == "*"
        assert resp.headers.get("X-Content-Type-Options") == "nosniff"


# ── ETag utility ─────────────────────────────────────────────────────


class TestComputeEtag:
    def test_deterministic(self):
        d = {"b": 2, "a": 1}
        assert _compute_etag(d) == _compute_etag(d)

    def test_changes_on_content(self):
        d1 = {"a": 1}
        d2 = {"a": 2}
        assert _compute_etag(d1) != _compute_etag(d2)

    def test_format(self):
        etag = _compute_etag({"x": 1})
        assert etag.startswith('"')
        assert etag.endswith('"')
        assert len(etag) == 34  # " + 32 hex chars + "

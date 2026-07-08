"""HTTP /mtproto-api route handler tests for session ACL gates."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from starlette.responses import JSONResponse

from src.config.server_config import ServerConfig, ServerMode, set_config
from src.server_components import mtproto_api
from src.server_components.session_acl import (
    INVALID_MTPROTO_JSON_DENY_MSG,
    clear_acl_cache,
)
from tests.conftest import VALID_TEST_BEARER_TOKEN

BOTFATHER_ID = 93372553


class _FakeMcpApp:
    def __init__(self) -> None:
        self.routes: dict[tuple[str, tuple[str, ...]], object] = {}

    def custom_route(self, path: str, methods: list[str] | tuple[str, ...]):
        def decorator(func):
            self.routes[(path, tuple(methods))] = func
            return func

        return decorator


class _FakeRequest:
    def __init__(
        self,
        *,
        method: str = "messages.getHistory",
        body: dict | None = None,
        bearer: str | None = VALID_TEST_BEARER_TOKEN,
    ) -> None:
        self.path_params = {"method": method}
        self._body = body or {}
        self.headers = {}
        if bearer is not None:
            self.headers["authorization"] = f"Bearer {bearer}"

    async def json(self) -> dict:
        return self._body


def _write_acl_config(tmp_path: Path, body: str) -> Path:
    acl_file = tmp_path / "acl.yaml"
    acl_file.write_text(body, encoding="utf-8")
    config = ServerConfig(_cli_parse_args=[])
    config.server_mode = ServerMode.HTTP_AUTH
    config.acl_enabled = True
    config.acl_config_path = str(acl_file)
    set_config(config)
    clear_acl_cache()
    return acl_file


def _mtproto_handler(app: _FakeMcpApp):
    return app.routes[("/mtproto-api/{method}", ("POST",))]


def _response_json(response: JSONResponse) -> dict:
    return json.loads(response.body.decode())


@pytest.fixture(autouse=True)
def _reset_acl():
    clear_acl_cache()
    yield
    clear_acl_cache()


@pytest.fixture
def mtproto_route():
    app = _FakeMcpApp()
    mtproto_api.register_mtproto_api_routes(app)
    return _mtproto_handler(app)


@pytest.mark.asyncio
async def test_mtproto_api_acl_denies_when_allow_mtproto_false(
    tmp_path: Path, mtproto_route, monkeypatch
):
    _write_acl_config(
        tmp_path,
        f"""
principals:
  {VALID_TEST_BEARER_TOKEN}:
    chats:
      - me
    read_only: false
    allow_mtproto: false
""",
    )

    async def _noop_invoke(**_kwargs):
        return {"ok": True}

    monkeypatch.setattr(mtproto_api, "invoke_mtproto_impl", _noop_invoke)

    response = await mtproto_route(_FakeRequest(body={"params": {}, "params_json": ""}))
    assert response.status_code == 403
    payload = _response_json(response)
    assert payload["ok"] is False
    assert "allow_mtproto" in payload["error"].lower()


@pytest.mark.asyncio
async def test_mtproto_api_acl_invalid_params_json_when_blocked_peers(
    tmp_path: Path, mtproto_route, monkeypatch
):
    _write_acl_config(
        tmp_path,
        f"""
blocked_peers:
  - {BOTFATHER_ID}
principals:
  {VALID_TEST_BEARER_TOKEN}:
    chats:
      - me
    allow_mtproto: true
""",
    )

    async def _noop_invoke(**_kwargs):
        return {"ok": True}

    monkeypatch.setattr(mtproto_api, "invoke_mtproto_impl", _noop_invoke)

    response = await mtproto_route(
        _FakeRequest(
            body={"params": {}, "params_json": "not-json"},
        )
    )
    assert response.status_code == 403
    payload = _response_json(response)
    assert payload["ok"] is False
    assert payload["error"] == INVALID_MTPROTO_JSON_DENY_MSG


@pytest.mark.asyncio
async def test_mtproto_api_acl_denies_blocked_peer_in_params_json(
    tmp_path: Path, mtproto_route, monkeypatch
):
    _write_acl_config(
        tmp_path,
        f"""
blocked_peers:
  - {BOTFATHER_ID}
principals:
  {VALID_TEST_BEARER_TOKEN}:
    chats:
      - me
    allow_mtproto: true
""",
    )

    async def _noop_invoke(**_kwargs):
        return {"ok": True}

    monkeypatch.setattr(mtproto_api, "invoke_mtproto_impl", _noop_invoke)

    response = await mtproto_route(
        _FakeRequest(
            body={
                "params": {},
                "params_json": json.dumps({"user_id": BOTFATHER_ID}),
            },
        )
    )
    assert response.status_code == 403
    payload = _response_json(response)
    assert payload["ok"] is False
    assert "blocked peer" in payload["error"].lower()

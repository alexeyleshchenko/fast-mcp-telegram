"""
Opt-in per-principal session ACL for http-auth deployments.

When ACL is enabled, principals listed in the config file get chat and operation
restrictions. Principals not listed keep full account access (backward compatible).
"""

from __future__ import annotations

import inspect
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import wraps
from pathlib import Path
from typing import Any

import yaml

from src.config.server_config import cfg
from src.server_components.attachment_tickets import (
    revoke_attachment_tickets,
    track_minted_attachment_tickets,
)
from src.utils.error_handling import log_and_build_error

logger = logging.getLogger(__name__)

_WRITE_OPERATIONS = frozenset(
    {
        "send_message",
        "edit_message",
        "send_message_to_phone",
    }
)
_LIST_RESULT_OPERATIONS = frozenset({"find_chats", "search_messages_globally"})
_EMPTY_LANE_CHAT_SCOPED_OPERATIONS = frozenset({"get_messages", "get_chat_info"})
_EMPTY_LANE_PRE_DENY_OPERATIONS = (
    _LIST_RESULT_OPERATIONS
    | _EMPTY_LANE_CHAT_SCOPED_OPERATIONS
    | frozenset({"send_message_to_phone"})
)
_EMPTY_LANE_POST_DENY_OPERATIONS = (
    _LIST_RESULT_OPERATIONS | _EMPTY_LANE_CHAT_SCOPED_OPERATIONS
)

_EMPTY_LANE_DENY_MSG = (
    "Session ACL: this principal has an empty chat lane (chats: [] or chats omitted). "
    "Add at least one chat id, @username, or me to the principal entry under principals:."
)
_UNLISTED_PRINCIPAL_DENY_MSG = (
    "Session ACL: this principal is not listed in the ACL config and "
    "ACL_DENY_UNLISTED_PRINCIPALS=true. Add a principals: entry for this session's "
    "principal identifier, or set ACL_DENY_UNLISTED_PRINCIPALS=false."
)
_MTPROTO_READ_ONLY_DENY_MSG = (
    "Session ACL is read-only: raw MTProto access is not permitted for this principal."
)
_MTPROTO_GLOBAL_SEARCH_DENY_MSG = (
    "Session ACL: raw MTProto is not permitted when allow_global_search is false. "
    "Set allow_global_search: true or use in-lane tools instead."
)
_MTPROTO_NOT_ALLOWED_DENY_MSG = (
    "Session ACL: raw MTProto is not permitted for this principal (allow_mtproto is false). "
    "Set allow_mtproto: true in the principal entry to opt in."
)
_KNOWN_TOKEN_ACL_KEYS = frozenset(
    {"chats", "read_only", "allow_global_search", "allow_mtproto"}
)
_BLOCKED_PEER_DENY_MSG = (
    "Session ACL: blocked peer ({ref}) is denied for this deployment. See SECURITY.md."
)
_INVALID_MTPROTO_JSON_DENY_MSG = (
    "Session ACL: invalid params_json when blocked_peers is configured. "
    "Provide valid JSON or omit params_json. See SECURITY.md."
)
INVALID_MTPROTO_JSON_DENY_MSG = _INVALID_MTPROTO_JSON_DENY_MSG
_MTPROTO_PEER_ID_KEYS = frozenset({"user_id", "chat_id", "channel_id", "peer_id", "id"})
_CHAT_SCOPED_OPERATIONS = frozenset(
    {"get_messages", "get_chat_info", "send_message", "edit_message"}
)

_acl_cache: dict[str, Any] | None = None
_acl_cache_path: Path | None = None
_blocked_peers_cache: frozenset[str | int] | None = None


@dataclass(frozen=True)
class PrincipalAclRule:
    chats: frozenset[str | int] = field(default_factory=frozenset)
    read_only: bool = False
    allow_global_search: bool = True
    allow_mtproto: bool = False
    unlisted_deny: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PrincipalAclRule:
        raw_chats = data.get("chats") or []
        chats: set[str | int] = set()
        for item in raw_chats:
            chats.add(_normalize_chat_ref(item))
        return cls(
            chats=frozenset(chats),
            read_only=bool(data.get("read_only", False)),
            allow_global_search=bool(data.get("allow_global_search", True)),
            allow_mtproto=bool(data.get("allow_mtproto", False)),
        )


def clear_acl_cache() -> None:
    """Reset loaded ACL config (for tests)."""
    global _acl_cache, _acl_cache_path, _blocked_peers_cache
    _acl_cache = None
    _acl_cache_path = None
    _blocked_peers_cache = None


def _get_principals_dict() -> dict:
    """Return the principals mapping from ACL config, or empty dict on failure.

    Handles disabled ACL, missing/invalid config, and malformed structure
    so callers can assume a safe dict return.  Never raises.
    """
    config = cfg()
    if not config.acl_enabled:
        return {}
    try:
        doc = _load_acl_document()
    except AclConfigError:
        return {}
    principals = doc.get("principals") or {}
    return principals if isinstance(principals, dict) else {}


def principal_count() -> int:
    """Number of principals in the ACL config.

    Returns 0 when ACL is disabled, the config file is missing, or no
    principals are defined — never raises.
    """
    return len(_get_principals_dict())


def read_only_count() -> int:
    """Number of ACL principals with ``read_only: true``.

    Returns 0 when ACL is disabled, the config file is missing, or no
    principals have the read_only flag — never raises.
    """
    return sum(
        1
        for p in _get_principals_dict().values()
        if isinstance(p, dict) and p.get("read_only", False) is True
    )


def _configured_acl_path() -> Path | None:
    config = cfg()
    if not config.acl_enabled:
        return None
    return config.acl_config_file


class AclConfigError(ValueError):
    """Raised when ACL is enabled but the config file is missing or invalid."""


def _validate_acl_document(doc: dict[str, Any], path: Path) -> None:
    """Fail-closed startup validation for principal entries."""
    legacy = doc.get("tokens")
    if legacy:
        raise AclConfigError(
            "ACL config key 'tokens:' was renamed to 'principals:' in 0.23.0. "
            f"Rename the top-level key in your acl.yaml; see SECURITY.md. ({path})"
        )

    principals = doc.get("principals") or {}
    if not isinstance(principals, dict):
        raise AclConfigError(f"ACL config 'principals' must be a mapping: {path}")

    for principal_key, raw in principals.items():
        prefix = str(principal_key)[:8]
        if not isinstance(raw, dict):
            raise AclConfigError(
                f"ACL config entry for principal identifier prefix {prefix}... must be "
                f"a mapping, not {type(raw).__name__}: {path}"
            )
        rule = PrincipalAclRule.from_dict(raw)
        if rule.read_only and not rule.chats:
            raise AclConfigError(
                f"ACL principal identifier prefix {prefix}... has read_only: true but "
                f"empty or missing chats. Analyst profile requires a non-empty chat "
                f"lane: {path}"
            )
        _warn_principal_acl_entry(principal_key, raw, rule, path)

    blocked = doc.get("blocked_peers")
    if blocked is None:
        return
    if not isinstance(blocked, list):
        raise AclConfigError(
            f"ACL config 'blocked_peers' must be a list when present: {path}"
        )
    for index, item in enumerate(blocked):
        if not _is_valid_blocked_peer_entry(item):
            raise AclConfigError(
                f"ACL config blocked_peers[{index}] is invalid (expected int, numeric "
                f"string, or @username): {path}"
            )


def _warn_principal_acl_entry(
    principal_key: str,
    raw: dict[str, Any],
    rule: PrincipalAclRule,
    path: Path,
) -> None:
    """Log operator hygiene warnings (non-fatal)."""
    prefix = str(principal_key)[:8]
    unknown = {
        key
        for key in raw
        if key not in _KNOWN_TOKEN_ACL_KEYS and not str(key).startswith("x_")
    }
    if unknown:
        logger.warning(
            "ACL config principal identifier prefix %s... has unknown key(s) %s (ignored): %s",
            prefix,
            sorted(unknown),
            path,
        )
    if not rule.chats and "chats" not in raw:
        logger.warning(
            "ACL config principal identifier prefix %s... has missing chats key (empty lane): %s",
            prefix,
            path,
        )
    if rule.allow_mtproto and rule.read_only:
        logger.warning(
            "ACL config principal identifier prefix %s... has allow_mtproto: true with "
            "read_only: true (read_only blocks MTProto at runtime): %s",
            prefix,
            path,
        )
    if rule.allow_mtproto and not rule.allow_global_search:
        logger.warning(
            "ACL config principal identifier prefix %s... has allow_mtproto: true with "
            "allow_global_search: false (global search off blocks MTProto at runtime): %s",
            prefix,
            path,
        )


def _is_valid_blocked_peer_entry(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    if not isinstance(value, str):
        return False
    normalized = _normalize_chat_ref(value)
    return normalized != ""


def validate_acl_config() -> None:
    """Fail-closed: refuse startup when ACL is enabled but config is absent or invalid."""
    config = cfg()
    if not config.acl_enabled or config.disable_auth:
        return
    path = config.acl_config_file
    if not path.is_file():
        raise AclConfigError(
            f"ACL is enabled (ACL_ENABLED=true) but ACL config file not found: {path}. "
            "Create the file or set ACL_CONFIG_PATH to a valid path."
        )
    _load_acl_document()


def _blocked_peers_from_doc(doc: dict[str, Any]) -> frozenset[str | int]:
    raw = doc.get("blocked_peers")
    if not isinstance(raw, list):
        return frozenset()
    peers: set[str | int] = set()
    for item in raw:
        peers.add(_normalize_chat_ref(item))
    return frozenset(peers)


def _load_acl_document() -> dict[str, Any]:
    global _acl_cache, _acl_cache_path, _blocked_peers_cache
    path = _configured_acl_path()
    if path is None:
        _acl_cache = {}
        _acl_cache_path = None
        _blocked_peers_cache = frozenset()
        return _acl_cache

    if not path.is_file():
        raise AclConfigError(f"ACL config file not found: {path}")

    if _acl_cache is not None and _acl_cache_path == path:
        return _acl_cache

    text = path.read_text(encoding="utf-8")
    try:
        if path.suffix.lower() == ".json":
            doc = json.loads(text)
        else:
            doc = yaml.safe_load(text) or {}
    except json.JSONDecodeError as exc:
        raise AclConfigError(
            f"ACL config is not valid JSON: {path} ({exc.msg} at line {exc.lineno}, "
            f"column {exc.colno})"
        ) from exc
    except yaml.YAMLError as exc:
        raise AclConfigError(f"ACL config is not valid YAML: {path} ({exc})") from exc

    if not isinstance(doc, dict):
        raise AclConfigError(f"ACL config must be a mapping: {path}")

    _validate_acl_document(doc, path)

    config = cfg()
    if config.acl_deny_unlisted_principals:
        logger.info(
            "Session ACL: unlisted principals denied (ACL_DENY_UNLISTED_PRINCIPALS=true)"
        )

    _acl_cache = doc
    _acl_cache_path = path
    _blocked_peers_cache = _blocked_peers_from_doc(doc)
    logger.info("Loaded session ACL config from %s", path)
    return _acl_cache


def _rules_for_principal(principal_id: str | None) -> PrincipalAclRule | None:
    if not principal_id:
        return None
    config = cfg()
    if not config.acl_enabled or config.disable_auth:
        return None

    doc = _load_acl_document()
    principals = doc.get("principals") or {}
    if not isinstance(principals, dict):
        return None

    raw = principals.get(principal_id)
    if raw is None:
        if config.acl_deny_unlisted_principals:
            return PrincipalAclRule(unlisted_deny=True)
        return None
    if not isinstance(raw, dict):
        logger.error(
            "Invalid ACL entry for principal identifier prefix %s... (expected mapping); "
            "denying all tool access",
            principal_id[:8],
        )
        return PrincipalAclRule()
    return PrincipalAclRule.from_dict(raw)


def _normalize_chat_ref(value: Any) -> str | int:
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if not text:
        return ""
    lowered = text.lower()
    if lowered in {"me", "saved", "saved messages"}:
        return "me"
    if text.startswith("@"):
        return text[1:].lower()
    try:
        return int(text)
    except ValueError:
        return lowered


def _chat_ref_matches(allowed: str | int, candidate: str | int) -> bool:
    if allowed == candidate:
        return True
    if isinstance(allowed, str) and isinstance(candidate, str):
        return allowed.lower() == candidate.lower()
    return False


def _load_blocked_peers() -> frozenset[str | int]:
    config = cfg()
    if not config.acl_enabled or config.disable_auth:
        return frozenset()
    global _blocked_peers_cache
    if _blocked_peers_cache is not None:
        return _blocked_peers_cache
    _load_acl_document()
    return _blocked_peers_cache if _blocked_peers_cache is not None else frozenset()


def blocked_peers_configured() -> bool:
    """True when ACL is on and blocked_peers is a non-empty deployment list."""
    return bool(_load_blocked_peers())


def _is_blocked_peer(ref: Any) -> bool:
    normalized = _normalize_chat_ref(ref)
    if normalized == "":
        return False
    return any(
        _chat_ref_matches(blocked, normalized) for blocked in _load_blocked_peers()
    )


def _deny_blocked_peer(
    operation: str, ref: str | int, params: dict[str, Any] | None = None
) -> dict[str, Any]:
    return _deny(
        operation,
        _BLOCKED_PEER_DENY_MSG.format(ref=ref),
        params=params,
    )


def _first_blocked_ref(refs: list[str | int]) -> str | int | None:
    for ref in refs:
        if _is_blocked_peer(ref):
            return ref
    return None


def _peer_refs_from_result(operation: str, result: dict[str, Any]) -> list[str | int]:
    refs: list[str | int] = []

    def _append_ref(value: Any) -> None:
        if value is None:
            return
        normalized = _normalize_chat_ref(value)
        if normalized != "":
            refs.append(normalized)

    if operation == "get_chat_info":
        _append_ref(result.get("id"))
        _append_ref(result.get("username"))
        return refs

    if operation == "get_messages":
        _append_ref(result.get("chat_id"))
        chat = result.get("chat")
        if isinstance(chat, dict):
            _append_ref(chat.get("id"))
            _append_ref(chat.get("username"))
        messages = result.get("messages")
        if isinstance(messages, list):
            for message in messages:
                if not isinstance(message, dict):
                    continue
                msg_chat = message.get("chat")
                if isinstance(msg_chat, dict):
                    _append_ref(msg_chat.get("id"))
                    _append_ref(msg_chat.get("username"))
        return refs

    return refs


def merge_mtproto_request_params(
    params: dict[str, Any], params_json: str
) -> dict[str, Any]:
    """Merge HTTP/tool MTProto params dict with optional params_json string."""
    merged = dict(params) if isinstance(params, dict) else {}
    if not params_json:
        return merged
    parsed = json.loads(params_json)
    if not isinstance(parsed, dict):
        raise json.JSONDecodeError(
            "params_json must decode to a JSON object", params_json, 0
        )
    merged.update(parsed)
    return merged


def _extract_peer_ids_from_mtproto_params(params: dict[str, Any]) -> set[int]:
    ids: set[int] = set()
    if not isinstance(params, dict):
        return ids
    for key, value in params.items():
        if (
            key in _MTPROTO_PEER_ID_KEYS
            and isinstance(value, int)
            and not isinstance(value, bool)
        ):
            ids.add(value)
        elif isinstance(value, dict):
            for nested_key, nested_value in value.items():
                if (
                    nested_key in _MTPROTO_PEER_ID_KEYS
                    and isinstance(nested_value, int)
                    and not isinstance(nested_value, bool)
                ):
                    ids.add(nested_value)
    return ids


def check_blocked_peer_mtproto_params(
    params: dict[str, Any], *, operation: str = "invoke_mtproto"
) -> dict[str, Any] | None:
    """Return denial when merged MTProto params reference a numeric blocked peer id."""
    blocked = _load_blocked_peers()
    if not blocked:
        return None
    numeric_blocked = {peer for peer in blocked if isinstance(peer, int)}
    if not numeric_blocked:
        return None
    for peer_id in _extract_peer_ids_from_mtproto_params(params):
        if peer_id in numeric_blocked:
            return _deny_blocked_peer(operation, peer_id, params=params)
    return None


def _check_blocked_peer_pre(
    operation: str, kwargs: dict[str, Any]
) -> dict[str, Any] | None:
    if operation == "invoke_mtproto":
        params_json = kwargs.get("params_json") or ""
        raw_params = kwargs.get("params")
        params = raw_params if isinstance(raw_params, dict) else {}
        if params_json:
            try:
                merged = merge_mtproto_request_params(params, params_json)
            except json.JSONDecodeError:
                return _deny(
                    operation,
                    INVALID_MTPROTO_JSON_DENY_MSG,
                    params=kwargs,
                )
        else:
            merged = params
        return check_blocked_peer_mtproto_params(merged)

    chat_id = kwargs.get("chat_id")
    if (
        chat_id is not None
        and operation in _CHAT_SCOPED_OPERATIONS
        and _is_blocked_peer(chat_id)
    ):
        return _deny_blocked_peer(
            operation, _normalize_chat_ref(chat_id), {"chat_id": chat_id}
        )
    return None


def _filter_blocked_peers_from_result(operation: str, result: dict[str, Any]) -> Any:
    if operation in {"get_chat_info", "get_messages"}:
        blocked_ref = _first_blocked_ref(_peer_refs_from_result(operation, result))
        if blocked_ref is not None:
            return _deny_blocked_peer(operation, blocked_ref)

    if operation == "find_chats" and isinstance(result.get("chats"), list):
        filtered = [
            chat
            for chat in result["chats"]
            if isinstance(chat, dict)
            and _first_blocked_ref(
                [
                    _normalize_chat_ref(chat.get("id")),
                    _normalize_chat_ref(chat.get("chat_id")),
                    _normalize_chat_ref(chat.get("username")),
                ]
            )
            is None
        ]
        return {**result, "chats": filtered}

    if operation == "search_messages_globally" and isinstance(
        result.get("messages"), list
    ):
        filtered = [
            msg
            for msg in result["messages"]
            if isinstance(msg, dict)
            and (cid := _message_chat_id(msg)) is not None
            and not _is_blocked_peer(cid)
        ]
        return {**result, "messages": filtered}

    return result


def _is_empty_lane(rule: PrincipalAclRule) -> bool:
    return not rule.chats


def _empty_lane_deny_msg(rule: PrincipalAclRule) -> str:
    if rule.unlisted_deny:
        return _UNLISTED_PRINCIPAL_DENY_MSG
    return _EMPTY_LANE_DENY_MSG


def _is_chat_allowed(chat_ref: Any, rule: PrincipalAclRule) -> bool:
    if _is_empty_lane(rule):
        return False
    normalized = _normalize_chat_ref(chat_ref)
    return any(_chat_ref_matches(a, normalized) for a in rule.chats)


def _is_chat_dict_lane_allowed(chat: dict[str, Any], rule: PrincipalAclRule) -> bool:
    """True when chat row id, chat_id, or username matches the principal lane."""
    peer_ref = chat.get("id") if chat.get("id") is not None else chat.get("chat_id")
    if peer_ref is not None and _is_chat_allowed(peer_ref, rule):
        return True
    username = chat.get("username")
    return username is not None and _is_chat_allowed(username, rule)


def _is_unlisted_synthetic_rule(rule: PrincipalAclRule) -> bool:
    """True when rule was synthesized for ACL_DENY_UNLISTED_PRINCIPALS (not from yaml)."""
    return rule.unlisted_deny


def _mtproto_denial_for_rule(
    rule: PrincipalAclRule,
    operation: str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Return denial when raw MTProto is blocked for this principal rule."""
    if _is_unlisted_synthetic_rule(rule):
        return _deny(operation, _UNLISTED_PRINCIPAL_DENY_MSG, params=params)
    if rule.read_only:
        return _deny(operation, _MTPROTO_READ_ONLY_DENY_MSG, params=params)
    if not rule.allow_global_search:
        return _deny(operation, _MTPROTO_GLOBAL_SEARCH_DENY_MSG, params=params)
    if not rule.allow_mtproto:
        return _deny(operation, _MTPROTO_NOT_ALLOWED_DENY_MSG, params=params)
    return None


def _deny(
    operation: str, message: str, params: dict[str, Any] | None = None
) -> dict[str, Any]:
    return log_and_build_error(
        operation=operation,
        error_message=message,
        params=params,
        error_code=-32007,
    )


def _bind_tool_kwargs(
    func: Callable, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> dict[str, Any]:
    """Merge positional and keyword tool arguments for ACL pre-checks."""
    bound = inspect.signature(func).bind(*args, **kwargs)
    bound.apply_defaults()
    return dict(bound.arguments)


def check_pre_tool_access(
    operation_name: str, kwargs: dict[str, Any]
) -> dict[str, Any] | None:
    """Return an error dict when the tool call must be blocked, else None."""
    from src.client.connection import get_request_token

    config = cfg()
    if not config.acl_enabled or config.disable_auth:
        return None

    if blocked_peers_configured() and (
        blocked_denial := _check_blocked_peer_pre(operation_name, kwargs)
    ):
        return blocked_denial

    rule = _rules_for_principal(get_request_token())
    if rule is None:
        return None

    if rule.read_only and operation_name in _WRITE_OPERATIONS:
        return _deny(
            operation_name,
            "Session ACL is read-only: send and edit operations are not permitted.",
            params=kwargs,
        )

    if _is_empty_lane(rule):
        lane_msg = _empty_lane_deny_msg(rule)
        if operation_name in _EMPTY_LANE_PRE_DENY_OPERATIONS:
            return _deny(operation_name, lane_msg, params=kwargs)

    if operation_name == "invoke_mtproto" and (
        denial := _mtproto_denial_for_rule(rule, operation_name, kwargs)
    ):
        return denial

    if operation_name == "search_messages_globally" and not rule.allow_global_search:
        return _deny(
            operation_name,
            "Session ACL disallows global message search for this principal.",
            params=kwargs,
        )

    chat_id = kwargs.get("chat_id")
    if (
        chat_id is not None
        and operation_name
        in _WRITE_OPERATIONS
        | {
            "get_messages",
            "get_chat_info",
        }
        and not _is_chat_allowed(chat_id, rule)
    ):
        return _deny(
            operation_name,
            "Session ACL: chat is not in the allowed list for this principal.",
            params={"chat_id": chat_id},
        )

    if operation_name == "send_message_to_phone" and not _is_empty_lane(rule):
        return _deny(
            operation_name,
            "Session ACL: send_message_to_phone is blocked when a chat whitelist is configured.",
            params=kwargs,
        )

    return None


def _message_chat_id(message: dict[str, Any]) -> str | int | None:
    for key in ("chat_id", "peer_id"):
        if key in message:
            return _normalize_chat_ref(message[key])
    chat = message.get("chat")
    if isinstance(chat, dict) and "id" in chat:
        return _normalize_chat_ref(chat["id"])
    return None


def filter_tool_result(operation_name: str, result: Any) -> Any:
    """Post-filter tool results to enforce chat whitelist on list payloads."""
    from src.client.connection import get_request_token

    if not isinstance(result, dict):
        return result
    if result.get("ok") is False:
        return result

    config = cfg()
    if config.acl_enabled and not config.disable_auth and blocked_peers_configured():
        result = _filter_blocked_peers_from_result(operation_name, result)
        if isinstance(result, dict) and result.get("ok") is False:
            return result

    rule = _rules_for_principal(get_request_token())
    if rule is None:
        return result

    if _is_empty_lane(rule):
        if operation_name in _EMPTY_LANE_POST_DENY_OPERATIONS:
            return _deny(operation_name, _empty_lane_deny_msg(rule))
        return result

    if operation_name == "find_chats" and isinstance(result.get("chats"), list):
        filtered = [
            chat
            for chat in result["chats"]
            if isinstance(chat, dict) and _is_chat_dict_lane_allowed(chat, rule)
        ]
        return {**result, "chats": filtered}

    if operation_name == "search_messages_globally" and isinstance(
        result.get("messages"), list
    ):
        filtered = [
            msg
            for msg in result["messages"]
            if isinstance(msg, dict)
            and (cid := _message_chat_id(msg)) is not None
            and _is_chat_allowed(cid, rule)
        ]
        return {**result, "messages": filtered}

    if operation_name == "get_messages" and isinstance(result.get("messages"), list):
        chat_id = result.get("chat_id")
        if chat_id is not None and not _is_chat_allowed(chat_id, rule):
            return _deny(
                operation_name,
                "Session ACL: chat is not in the allowed list for this principal.",
                params={"chat_id": chat_id},
            )

    return result


def enforce_session_acl(operation_name: str):
    """Decorator: pre-check ACL and post-filter list results."""

    def decorator(func: Callable):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            bound_kwargs = _bind_tool_kwargs(func, args, kwargs)
            denial = check_pre_tool_access(operation_name, bound_kwargs)
            if denial is not None:
                return denial

            track_tickets = operation_name == "get_messages"
            if track_tickets:
                with track_minted_attachment_tickets() as minted_ids:
                    result = await func(*args, **kwargs)
                    return await _finalize_acl_result(
                        operation_name, result, minted_ids
                    )

            result = await func(*args, **kwargs)
            return await _finalize_acl_result(operation_name, result, [])

        return wrapper

    return decorator


async def _finalize_acl_result(
    operation_name: str,
    result: Any,
    minted_ids: list[str],
) -> Any:
    if isinstance(result, dict) and result.get("ok") is False:
        return result
    was_ok = isinstance(result, dict) and result.get("ok") is not False
    filtered = filter_tool_result(operation_name, result)
    if (
        operation_name == "get_messages"
        and was_ok
        and isinstance(filtered, dict)
        and filtered.get("ok") is False
        and minted_ids
    ):
        removed = await revoke_attachment_tickets(minted_ids)
        if removed:
            logger.info(
                "Revoked %d attachment ticket(s) after get_messages ACL denial",
                removed,
            )
    return filtered


def check_mtproto_api_access(
    token: str | None, *, allow_dangerous: bool
) -> dict[str, Any] | None:
    """ACL gate for HTTP MTProto bridge (http-auth only)."""
    rule = _rules_for_principal(token)
    if rule is None:
        return None
    params = {"allow_dangerous": allow_dangerous}
    return _mtproto_denial_for_rule(rule, "mtproto_api", params)

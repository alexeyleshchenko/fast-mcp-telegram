import base64
import contextlib
import datetime
import inspect
import json
import logging
import re
from importlib import import_module
from typing import Any

from telethon.errors import RPCError
from telethon.errors.rpcerrorlist import rpc_errors_dict, rpc_errors_re

from src.client.connection import get_connected_client
from src.utils.entity import get_entity_by_id, is_ambiguous_peer_scalar
from src.utils.error_handling import log_and_build_error, log_connection_error_response
from src.utils.helpers import normalize_method_name
from src.utils.json_ids import stringify_int64

logger = logging.getLogger(__name__)

# ============================================================================
# CONSTANTS
# ============================================================================

# Dangerous methods that require explicit permission
DANGEROUS_METHODS = {
    "account.DeleteAccount",
    "messages.DeleteHistory",
    "messages.DeleteUserHistory",
    "messages.DeleteChatUser",
    "messages.DeleteMessages",
    "channels.DeleteHistory",
    "channels.DeleteMessages",
}

# Reverse mapping: Telethon exception class -> Telegram RPC error code (from Telethon guts)
_RPC_CLASS_TO_CODE: dict[type, str] = {
    cls: code for code, cls in rpc_errors_dict.items()
}
for pattern, cls in rpc_errors_re:
    base = re.sub(r"\([^)]+\)", "", pattern).strip("_").replace("__", "_")
    _RPC_CLASS_TO_CODE[cls] = base

# Regex for Telegram raw error codes (UPPER_SNAKE_CASE)
_UPPER_SNAKE_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


def _normalize_rpc_error_code(exception: Exception) -> str | None:
    """
    Map a Telethon RPCError to a stable, machine-readable Telegram error code.

    Uses Telethon's rpc_errors_dict and rpc_errors_re for mapping.
    Fallback: use exception.message if it matches UPPER_SNAKE_CASE.

    Returns:
        Error code string (e.g., "INVITE_HASH_EXPIRED") or None
    """
    if code := _RPC_CLASS_TO_CODE.get(type(exception)):
        return code
    if isinstance(exception, RPCError):
        msg = getattr(exception, "message", str(exception))
        if isinstance(msg, str) and _UPPER_SNAKE_RE.match(msg.strip()):
            return msg.strip()
    return None


# ============================================================================
# UTILITY FUNCTIONS
# ============================================================================


def _construct_tl_object_from_dict(data: Any) -> Any:
    """
    Recursively construct Telethon TL objects from dictionaries.

    Handles dictionaries with '_' key containing the TL object type name.
    Supports case-insensitive type name lookup.
    Recursively processes nested dictionaries and lists.
    """
    if not isinstance(data, dict) or "_" not in data:
        return data

    requested_name = data["_"]
    # Import types dynamically to avoid circular imports
    from telethon.tl import types

    # Case-insensitive lookup: build mapping if not already built (module-level cache)
    _name_mapping: dict[str, str] = getattr(
        _construct_tl_object_from_dict,
        "_name_mapping",
        _construct_tl_object_from_dict.__dict__.setdefault("_name_mapping", {}),
    )
    if not _name_mapping:
        for name in dir(types):
            cls = getattr(types, name)
            # Only include TL object classes (they have CONSTRUCTOR_ID)
            if hasattr(cls, "CONSTRUCTOR_ID"):
                _name_mapping[name.lower()] = name

    # Try case-insensitive lookup first
    name_mapping = _name_mapping
    if requested_name.lower() in name_mapping:
        class_name = name_mapping[requested_name.lower()]
        logger.debug(f"Resolved TL type '{requested_name}' to '{class_name}'")
    else:
        class_name = requested_name

    if not hasattr(types, class_name):
        logger.warning(f"Unknown TL type: {requested_name} (resolved to: {class_name})")
        return data

    cls = getattr(types, class_name)
    if not hasattr(cls, "__init__"):
        return data

    try:
        # Get the constructor signature
        sig = inspect.signature(cls.__init__)
        params = {}

        for param_name in sig.parameters:
            if param_name == "self":
                continue
            if param_name in data:
                value = data[param_name]
                # Recursively construct nested objects
                if isinstance(value, dict) and "_" in value:
                    params[param_name] = _construct_tl_object_from_dict(value)
                elif isinstance(value, list):
                    params[param_name] = [
                        _construct_tl_object_from_dict(item) for item in value
                    ]
                else:
                    # Coerce numeric strings back to int.  After the int64
                    # stringification fix, access_hash and large ids are
                    # serialized as strings in JSON results.  When clients
                    # pass these back, json.loads produces strings where TL
                    # constructors need ints.
                    if isinstance(value, str):
                        try:
                            value = int(value)
                        except (ValueError, TypeError):
                            pass
                    params[param_name] = value
            else:
                # Fill missing required int params with 0 so callers that
                # omit offset_id / offset_date / add_offset / hash etc.
                # don't crash the constructor.
                annotation = sig.parameters[param_name].annotation
                if annotation is int:
                    params[param_name] = 0

        return cls(**params)
    except Exception as e:
        logger.warning(f"Failed to construct TL object {class_name}: {e}")
        return data


def _json_safe(value: Any) -> Any:
    """Recursively convert value into a JSON- and UTF-8-safe structure.

    - bytes -> base64 ascii string
    - set/tuple -> list
    - objects with to_dict -> recurse into to_dict()
    - other non-serializable -> str(value)
    - ensure all strings are UTF-8 encodable (replace errors if needed)
    """
    try:
        if value is None or isinstance(value, bool | float):
            return value
        if isinstance(value, int):
            # 64-bit Telegram ids (document_id, access_hash, peer ids, ...) lose
            # precision when JSON-decoded as doubles; emit out-of-range ints as
            # strings. Small ints (offsets, flags, lengths) stay numeric.
            return stringify_int64(value)
        if isinstance(value, bytes):
            return base64.b64encode(value).decode("ascii")
        if isinstance(value, datetime.datetime):
            # Datetime objects (e.g. Updates.date) must be int Unix timestamps,
            # not strings. MCP output schema validates date as integer.
            return int(value.timestamp())
        if isinstance(value, str):
            try:
                value.encode("utf-8", "strict")
                return value
            except Exception:
                return value.encode("utf-8", "replace").decode("utf-8")
        if isinstance(value, dict):
            return {str(k): _json_safe(v) for k, v in value.items()}
        if isinstance(value, list | tuple | set):
            return [_json_safe(v) for v in value]
        if hasattr(value, "to_dict") and callable(value.to_dict):
            try:
                return _json_safe(value.to_dict())
            except Exception:
                return str(value)
        return str(value)
    except Exception:
        return str(value)


def _construct_tl_params(params: dict[str, Any]) -> dict[str, Any]:
    """Construct Telethon TL objects from all nested dicts in *params*.

    Recursively converts dictionaries with ``_`` key
    (e.g. ``{"_": "inputChat", "chat_id": 123, "access_hash": "456"}``)
    into proper Telethon TL objects (``InputChat``).  Handles lists, nested
    dicts, and mixed structures.  Already-constructed TL objects pass through
    unchanged.

    This runs unconditionally (before the ``resolve`` check) so that TL dicts
    work even when entity resolution is disabled.
    """
    if not params:
        return {}

    def _process_value(value: Any) -> Any:
        if isinstance(value, dict):
            constructed = _construct_tl_object_from_dict(value)
            if constructed is not value:  # Construction succeeded
                return constructed
            return {k: _process_value(v) for k, v in value.items()}
        if isinstance(value, list | tuple):
            return [_process_value(item) for item in value]
        return value

    return {k: _process_value(v) for k, v in params.items()}


async def _resolve_params(params: dict[str, Any]) -> dict[str, Any]:
    """Best-effort resolution of entity-like parameters and TL object construction using Telethon.

    Handles entity resolution for: peer, from_peer, to_peer, user, user_id,
    channel, chat, chat_id, users, chats, peers.

    .. note::
       TL object construction (dict → Telethon type) is done by
       :func:`_construct_tl_params` which is called before this function
       in :func:`invoke_mtproto_impl`.  Direct callers should also run
       ``_construct_tl_params`` first.
    """
    if not params:
        return {}

    client = await get_connected_client()

    async def _resolve_one(value: Any) -> Any:
        # Pass-through for already-resolved TL objects
        with contextlib.suppress(Exception):
            # Telethon TL objects usually have to_dict
            if hasattr(value, "to_dict") or getattr(value, "_", None):
                return value
        # Parse Telegram URLs (https://t.me/…, tg://…) to peer identifiers
        # before falling through to get_input_entity, which cannot resolve URLs.
        if isinstance(value, str):
            from src.utils.entity import _parse_telegram_url

            parsed = _parse_telegram_url(value)
            if parsed is not None:
                value = parsed
        if is_ambiguous_peer_scalar(value):
            entity = await get_entity_by_id(value, client=client)
            if entity is not None:
                return await client.get_input_entity(entity)
        return await client.get_input_entity(value)

    keys_to_resolve = {
        "peer",
        "from_peer",
        "to_peer",
        "user",
        "user_id",
        "channel",
        "chat",
        "chat_id",
        "users",
        "chats",
        "peers",
    }

    resolved: dict[str, Any] = dict(params)

    # TL object construction from nested dicts is done by _construct_tl_params()
    # before this function is called.  We run it again as a safety net for
    # callers that skip that step — it's a no-op on already-converted values.
    resolved = _construct_tl_params(resolved)

    # Resolve entity-like parameters
    for key in list(resolved.keys()):
        if key in keys_to_resolve:
            value = resolved[key]
            if isinstance(value, list | tuple):
                resolved[key] = [await _resolve_one(v) for v in value]
            else:
                resolved[key] = await _resolve_one(value)

    return resolved


async def _convert_peer_types(
    client,
    params: dict[str, Any],
    method_cls,
) -> dict[str, Any]:
    """Fix resolved peer types to match the method's expected parameter types.

    Two cases:

    1. **Method expects ``int``** but the value is a peer/input object
       (``InputPeerChat``, ``InputChat``, etc.) → extract the raw ID
       (``.chat_id``, ``.user_id``, ``.channel_id``).

       This happens when ``_resolve_params()`` converts an int (e.g.
       ``chat_id=5417489797``) to ``InputPeerChat``, but the method's
       signature (e.g. ``AddChatUserRequest.__init__(chat_id: int, ...)``)
       expects the raw integer, not an ``InputPeer*``.

    2. **Method expects ``Input*``** (string annotation like ``'InputChat'``
       or ``'TypeInputUser'``) but the value is ``InputPeer*`` → fetch the
       full entity from cache to construct ``Input*`` with the ``access_hash``.
    """
    # Import Telethon TL types — InputChat / InputChannel may not exist
    # in older Telethon builds, so we handle them conditionally.
    from telethon.tl.types import (
        InputPeerChat,
        InputPeerUser,
        InputPeerChannel,
    )
    try:
        from telethon.tl.types import InputChat
    except ImportError:
        InputChat = None  # not available in this Telethon version
    try:
        from telethon.tl.types import InputUser
    except ImportError:
        InputUser = None
    try:
        from telethon.tl.types import InputChannel
    except ImportError:
        InputChannel = None

    # Types from which we can extract a raw peer/user/chat ID.
    # Key = class, value = attribute name holding the int ID.
    PEER_TYPES_WITH_ID: dict[type, str] = {
        InputPeerChat: "chat_id",
        InputPeerUser: "user_id",
        InputPeerChannel: "channel_id",
    }
    # Add Input* types if they exist in this Telethon build
    if InputChat is not None:
        PEER_TYPES_WITH_ID[InputChat] = "chat_id"
    if InputUser is not None:
        PEER_TYPES_WITH_ID[InputUser] = "user_id"
    if InputChannel is not None:
        PEER_TYPES_WITH_ID[InputChannel] = "channel_id"

    sig = inspect.signature(method_cls.__init__)
    result = dict(params)

    for name, param in sig.parameters.items():
        if name == "self" or name not in result:
            continue
        value = result[name]
        expected = param.annotation

        # --- Case 1: method expects int → extract raw ID from peer object ---
        if expected is int:
            value_cls = type(value)
            if value_cls in PEER_TYPES_WITH_ID:
                id_field = PEER_TYPES_WITH_ID[value_cls]
                result[name] = getattr(value, id_field)
                logger.debug(
                    "Extracted %s.%s = %s for parameter '%s' (method expects int)",
                    value_cls.__name__,
                    id_field,
                    result[name],
                    name,
                )
            continue

        # --- Case 2: method expects Input* (string annotation) but we have InputPeer* ---
        if not isinstance(expected, str):
            continue

        expected_name = expected
        # Strip 'Type' prefix from union annotations: TypeInputUser → InputUser
        if expected_name.startswith("Type"):
            expected_stripped = expected_name[4:]
        else:
            expected_stripped = expected_name

        # Map expected stripped name → (peer_cls, constructor for full entity).
        # Only include types that exist in this Telethon build.
        INPUT_BUILDERS: dict[str, tuple] = {}
        if InputChat is not None:
            INPUT_BUILDERS["InputChat"] = (
                InputPeerChat,
                lambda e: InputChat(
                    chat_id=e.id, access_hash=e.access_hash
                ),
            )
        if InputUser is not None:
            INPUT_BUILDERS["InputUser"] = (
                InputPeerUser,
                lambda e: InputUser(
                    user_id=e.id, access_hash=e.access_hash
                ),
            )
        if InputChannel is not None:
            INPUT_BUILDERS["InputChannel"] = (
                InputPeerChannel,
                lambda e: InputChannel(
                    channel_id=e.id, access_hash=e.access_hash
                ),
            )

        if expected_stripped not in INPUT_BUILDERS:
            continue

        peer_cls, builder = INPUT_BUILDERS[expected_stripped]
        if not isinstance(value, peer_cls):
            continue

        # Fetch the full entity to get the access_hash from cache
        try:
            entity = await client.get_entity(value)
            result[name] = builder(entity)
            logger.debug(
                "Converted %s → %s for parameter '%s'",
                peer_cls.__name__,
                expected_stripped,
                name,
            )
        except Exception as e:
            logger.warning(
                "Could not convert %s → %s for '%s': %s",
                peer_cls.__name__,
                expected_stripped,
                name,
                e,
            )

    return result


def _resolve_method_class(method_full_name: str):
    """Resolve MTProto method name to Telethon class.

    Args:
        method_full_name: Full class name of the MTProto method, e.g., 'messages.GetHistory'

    Returns:
        Tuple of (method_cls, normalized_name)

    Raises:
        ValueError: If method name format is invalid
        ImportError: If method class cannot be found
    """
    if "." not in method_full_name:
        raise ValueError(
            "method_full_name must be in the form 'module.ClassName', e.g., 'messages.GetHistory'"
        )

    module_name, class_name = method_full_name.rsplit(".", 1)

    # Telethon uses e.g. GetHistoryRequest, not GetHistory
    if not class_name.endswith("Request"):
        class_name += "Request"

    tl_module = import_module(f"telethon.tl.functions.{module_name}")
    method_cls = getattr(tl_module, class_name)

    return method_cls, method_full_name


# ============================================================================
# PARAMETER SANITIZATION
# ============================================================================


def _fill_missing_int_defaults(cls, params: dict[str, Any]) -> None:
    """Fill missing required int/nullable params with sensible defaults in-place.

    After the int64 stringification fix, callers may omit params like
    offset_id / add_offset / hash that Telegram expects.  Rather than a
    cryptic "missing N required" TypeError, fill int-typed params with 0
    and nullable-typed params with None.
    """
    try:
        sig = inspect.signature(cls.__init__)
    except (ValueError, TypeError):
        return
    for name, param in sig.parameters.items():
        if name == "self" or name in params:
            continue
        if param.default is not inspect.Parameter.empty:
            continue  # has a default already
        if param.annotation is int:
            params[name] = 0
        else:
            # Check for Union types containing None (e.g. datetime | None)
            try:
                if type(None) in param.annotation.__args__:
                    params[name] = None
            except AttributeError:
                pass


def _sanitize_mtproto_params(params: dict[str, Any]) -> dict[str, Any]:
    """
    Sanitize and validate MTProto method parameters for security.

    Args:
        params: Raw parameters dictionary
    Returns:
        Sanitized parameters dictionary
    """
    sanitized = params.copy()

    # Security: Handle hash parameter correctly
    # 'hash' can be: (a) string invite hash for messages.ImportChatInvite, or
    # (b) integer for state/difference methods like messages.GetState
    if "hash" in sanitized:
        hash_value = sanitized["hash"]
        if isinstance(hash_value, str):
            if trimmed := hash_value.strip():
                sanitized["hash"] = trimmed
            else:
                del sanitized["hash"]
        elif isinstance(hash_value, int):
            if not 0 <= hash_value <= 0xFFFFFFFF:
                logger.warning(f"Hash out of bounds: {hash_value}, removing")
                del sanitized["hash"]
        else:
            logger.warning(f"Invalid hash type: {type(hash_value)}, removing")
            del sanitized["hash"]

    # Security: Validate other critical parameters
    for key, value in list(sanitized.items()):
        # Prevent injection of potentially dangerous parameters
        if key.startswith("_") or key in ["__class__", "__dict__", "__module__"]:
            logger.warning(f"Removing potentially dangerous parameter: {key}")
            del sanitized[key]
            continue

        # Validate string parameters for reasonable length
        if isinstance(value, str) and len(value) > 10000:
            logger.warning(
                f"String parameter {key} too long ({len(value)} chars), truncating"
            )
            sanitized[key] = value[:10000]

    return sanitized


# ============================================================================
# HIGH-LEVEL API FUNCTIONS
# ============================================================================


async def invoke_mtproto_impl(
    method_full_name: str,
    params_json: str,
    allow_dangerous: bool = False,
    resolve: bool = True,
) -> dict[str, Any]:
    """
    Invoke MTProto methods with enhanced features.

    This function provides comprehensive MTProto method invocation with:
    - Method name normalization
    - Dangerous method protection
    - Entity resolution
    - Parameter sanitization
    - Telethon client interaction
    - Result processing

    Args:
        method_full_name: Telegram API method name (e.g., "messages.GetHistory")
        params_json: Method parameters as JSON string
        allow_dangerous: Allow dangerous methods like delete operations (default: False)
        resolve: Automatically resolve entity-like parameters (default: True)

    Returns:
        API response as dict, or error details if failed
    """
    try:
        # Normalize method name for consistency
        try:
            normalized_method = normalize_method_name(method_full_name)
        except Exception as e:
            return log_and_build_error(
                operation="invoke_mtproto",
                error_message=f"Invalid method name format: {e}",
                params={
                    "method_full_name": method_full_name,
                    "params_json": params_json,
                },
                exception=e,
            )

        # Check for dangerous methods unless explicitly allowed
        if normalized_method in DANGEROUS_METHODS and not allow_dangerous:
            return log_and_build_error(
                operation="invoke_mtproto",
                error_message=(
                    f"Method '{normalized_method}' is blocked by default. "
                    "Pass allow_dangerous=true to override."
                ),
                params={
                    "method_full_name": method_full_name,
                    "normalized_method": normalized_method,
                    "params_json": params_json,
                },
            )

        # Parse parameters
        try:
            params = json.loads(params_json)
        except Exception as e:
            return log_and_build_error(
                operation="invoke_mtproto",
                error_message=f"Invalid JSON in params_json: {e}",
                params={
                    "method_full_name": method_full_name,
                    "normalized_method": normalized_method,
                    "params_json": params_json,
                },
                exception=e,
            )

        # Construct TL objects from dicts + optional entity resolution
        try:
            final_params = params
            if isinstance(params, dict):
                # Always construct TL objects from nested dicts,
                # regardless of resolve flag.  Fixes "Cannot cast dict
                # to Peer" when resolve=false.
                final_params = _construct_tl_params(params)
                if resolve:
                    final_params = await _resolve_params(final_params)
        except Exception as e:
            return log_and_build_error(
                operation="invoke_mtproto",
                error_message=f"Failed to resolve parameters: {e}",
                params={
                    "method_full_name": method_full_name,
                    "normalized_method": normalized_method,
                    "params_json": params_json,
                },
                exception=e,
            )

        # Now invoke the actual MTProto method
        logger.debug(
            f"Invoking MTProto method: {normalized_method} with params: {_json_safe(final_params)}"
        )

        try:
            # Resolve method class
            method_cls, _ = _resolve_method_class(normalized_method)

            # Security: Validate and sanitize parameters
            sanitized_params = _sanitize_mtproto_params(final_params)

            # Fill missing required int params with 0 so callers that omit
            # offset_id / offset_date / add_offset / hash etc. get defaults
            # instead of a cryptic "missing N required" TypeError.
            _fill_missing_int_defaults(method_cls, sanitized_params)

            # Fix resolved peer types to match method's expected parameter types.
            # Case 1: method expects int → extract raw ID from InputPeer* / Input*
            #   (e.g. AddChatUserRequest.__init__(chat_id: int) gets 5417489797
            #    from InputPeerChat(chat_id=5417489797)).
            # Case 2: method expects Input* (string annotation) → fetch entity
            #   and construct Input* with access_hash (e.g. InputPeerChat → InputChat).
            _client = await get_connected_client()
            sanitized_params = await _convert_peer_types(
                _client, sanitized_params, method_cls
            )

            # Create method object and invoke via Telethon
            method_obj = method_cls(**sanitized_params)
            client = await get_connected_client()
            result = await client(method_obj)

            # Process result to JSON-safe format
            result_dict = (
                result.to_dict() if hasattr(result, "to_dict") else str(result)
            )
            safe_result = _json_safe(result_dict)

            logger.info(f"MTProto method {normalized_method} invoked successfully")
            return safe_result

        except RPCError as e:
            return log_and_build_error(
                operation="invoke_mtproto",
                error_message=f"Failed to invoke MTProto method '{normalized_method}': {e!s}",
                params={
                    "method_full_name": method_full_name,
                    "normalized_method": normalized_method,
                    "params": _json_safe(final_params),
                },
                exception=e,
                error_code=_normalize_rpc_error_code(e),
            )
        except Exception as e:
            invoke_err_params = {
                "method_full_name": method_full_name,
                "normalized_method": normalized_method,
                "params": _json_safe(final_params),
            }
            if (
                r := log_connection_error_response(
                    "invoke_mtproto", invoke_err_params, e
                )
            ) is not None:
                return r
            return log_and_build_error(
                operation="invoke_mtproto",
                error_message=f"Failed to invoke MTProto method '{normalized_method}': {e!s}",
                params=invoke_err_params,
                exception=e,
            )

    except Exception as e:
        outer_params = {
            "method_full_name": method_full_name,
            "params_json": params_json,
        }
        if (
            r := log_connection_error_response("invoke_mtproto", outer_params, e)
        ) is not None:
            return r
        return log_and_build_error(
            operation="invoke_mtproto",
            error_message=f"Error in invoke_mtproto: {e!s}",
            params=outer_params,
            exception=e,
        )

## Technologies Used

### Core Framework
- **FastMCP**: MCP (Modular Control Platform) server framework
- **Telethon**: Python library for Telegram's MTProto API
- **Python 3.x**: Primary development language
- **asyncio**: For parallel query execution and async operations

### Key Dependencies
```python
# Core dependencies from pyproject.toml (managed by setuptools)
fastmcp-slim[server]  # FastMCP 3.3+ server stack; import namespace remains fastmcp
telethon         # Telegram API client (iter_download for attachment streaming)
# Logging handled by Python stdlib
asyncio          # Async/await support (built-in)
httpx            # HTTP client for file downloads and MCP transport
python-dotenv    # Environment variable management
```

### Attachment streaming (HTTP)
- **Routes**: `register_attachment_routes` in `src/server_components/attachment_routes.py` — `GET /v1/attachments/{ticket_id}` via FastMCP `custom_route`; **Starlette `StreamingResponse`**
- **Tickets**: `src/server_components/attachment_tickets.py` — asyncio-locked in-memory map
- **URLs in JSON**: `src/utils/message_format/attachments.py` — `_maybe_set_attachment_download_url` after `_build_media_placeholder`
- **Config**: `domain` / `DOMAIN`, `public_base_url_normalized` (derived for attachments), `attachment_ticket_ttl_seconds`

**Dependency Management**: setuptools with pyproject.toml for package management; MCP framework pinned as `fastmcp-slim[server]>=3.3` (same `from fastmcp import ...` imports)

**GHCR image size (linux/amd64 compressed manifest, 2026-05-19)**: before 0.18.1 `main` — 41,228,888 bytes (~39.3 MiB); after `sha-8703410` — 41,188,795 bytes (~39.28 MiB); delta −40,093 bytes (−0.10%)
**Version Management**: Single source of truth in `src/_version.py` with direct import approach
**Session Management**: Session files stored in persistent user config directory (~/.config/fast-mcp-telegram/)
**Cross-Platform Support**: Automatic handling of macOS resource forks and permission differences

### Development Tools
- **Cursor IDE**: Primary development environment
- **Git**: Version control
- **Ruff**: Code formatting and linting
- **pytest**: Comprehensive testing framework with async support

## Development Setup

### Environment Configuration
```bash
# Session cleanup (0.27.0)
TELEGRAM_INACTIVE_SESSION_DAYS=30      # Auto-delete .session files unused >N days (0=disable)

# Project structure
tg_mcp/
├── src/                   # Source code
│   ├── server.py         # MCP server entry point with authentication middleware
│   ├── _version.py       # Version information (single source of truth)
│   ├── tools/            # Tool implementations (all with @with_auth_context)
│   ├── client/           # Telegram client management with token-based sessions
│   │   └── connection.py # Token management, LRU cache, session isolation
│   ├── config/           # Configuration and logging
│   └── utils/            # Utility functions
├── scripts/               # Deployment and utility scripts
│   ├── sync-remote-config.sh  # Remote config synchronization script
│   └── check-status.sh    # Health status check script
```

### Testing Infrastructure
- **Comprehensive Test Suite**: 140+ tests covering all functionality
- **Async Test Support**: pytest-asyncio for coroutine testing
- **Coverage Reporting**: pytest-cov for test coverage analysis
- **Parallel Execution**: pytest-xdist for faster test runs
- **Test Organization**: Separate test files for each module with clear naming

## Tool Usage Patterns

### Authentication Pattern
```python
# All tools use this exact pattern for consistency
@mcp_tool_with_restrictions("tool_name")  # outermost: restriction + auth context
@with_error_handling("tool_name")  # raises ToolError for proper isError=True
async def tool_function(token: str, ...) -> dict:
    # Function body
```

### Error Handling Pattern
```python
# Consistent error response format across all tools
# Error dicts are caught by @with_error_handling and re-raised as ToolError
return log_and_build_error(
    operation="tool_name",
    error_message="Human readable message",
    params=params,
    exception=original_exception,
    action=ErrorAction.RETRY,  # typed enum, serialized as string
    code=MCPErrorCode.CONNECTION_ERROR,  # typed enum
)
```

### Session Management
- **Token-Based Sessions**: Each bearer token maps to isolated session file
- **LRU Cache**: Recently used sessions cached in memory for performance
- **Automatic Cleanup**: Failed sessions automatically removed and recreated
- **Cross-Server Isolation**: HTTP_AUTH mode uses random tokens; STDIO uses configured names

## Server Modes

### Three Server Modes
1. **stdio**: Local development, direct process communication
2. **http-no-auth**: HTTP transport without authentication (development only)
3. **http-auth**: Production HTTP transport with bearer token authentication

### Configuration System
- **Pydantic Settings**: Modern configuration with validation and defaults
- **Multiple Sources**: CLI args, environment variables, .env files, config files
- **Smart Defaults**: Mode-appropriate behavior and validation
- **Runtime Overrides**: DOMAIN and other settings configurable at runtime

## Message Processing

### Message Format
- **Consistent Structure**: All message results follow same schema
- **Media Placeholders**: LLM-friendly media representations instead of raw objects
- **Reply Markup**: Automatic extraction of keyboard and inline buttons
- **Forward Information**: Structured forwarded message metadata

### Search Architecture
- **Parallel Execution**: Multiple queries run concurrently for performance
- **Deduplication**: Smart deduplication by message ID across queries
- **Pagination**: Conservative has_more logic prevents missed messages
- **Filtering**: Chat type, public visibility, and date range filtering

## Deployment

### Development
- **Local Development**: Using stdio transport
- **HTTP Server**: For testing with HTTP transport

### Production
- **VDS Deployment**: Containerized with Traefik and TLS
- **Session Persistence**: Zero-downtime deployments with automatic backup/restore
- **Health Monitoring**: HTTP `/health` endpoint for session statistics

## Technical Constraints

### Telegram API Limitations
- **Rate Limiting**: API calls are subject to Telegram's rate limits
- **Search Limitations**: Global search has different capabilities than per-chat search
- **Entity Resolution**: Chat IDs can be in multiple formats (username, numeric ID, channel ID)
- **Session Management**: Requires proper session handling and authentication

### Telethon-specific Constraints
- **asyncio.wait_for is DANGEROUS**: Cancelling a Telethon coroutine with `asyncio.wait_for` leaves the MTProto sender in an inconsistent state. The delayed response gets read by the next request's handler, causing `msg_id` desync → server disconnect → reconnect overhead. Never use `asyncio.wait_for` on Telethon client calls.
- **get_entity(InputPeer) ALWAYS makes API calls**: Telethon does not check the session cache before making a `users.GetUsersRequest` for `InputPeerUser`. Even if the entity is cached, the API call is made.
- **Batch get_entity triggers flood wait**: `client.get_entity([71 users])` makes one `users.GetUsersRequest([71])`, which triggers `FLOOD_WAIT_178` for large lists. Individual `get_entity` calls are safe but some users take 41-42s (Telegram cross-DC internal lookup).
- **Stale access_hash causes ~30s hang**: Entities from `iter_dialogs` can have stale `access_hash` (deleted accounts, cross-DC migration). GetPeerDialogsRequest with stale hash hangs ~30s. No built-in fix in Telethon.
- **flood_sleep_threshold blocks by CONSTRUCTOR_ID**: Telethon's `_flood_waited_requests` dict keys on the request's `CONSTRUCTOR_ID`. One FLOOD_WAIT for GetUsersRequest blocks ALL subsequent GetUsersRequest calls.
- **session.process_entities**: Entities are saved to session DB only AFTER a successful API call. No mechanism to pre-populate or validate cached entity hashes.

### MCP Protocol Constraints
- **Tool Registration**: All tools must be properly registered with FastMCP
- **Async Operations**: All Telegram operations must be async
- **Error Handling**: All tools return structured error responses instead of raising exceptions
- **Documentation**: Tool descriptions must be clear for AI model consumption

### FastMCP Authentication Constraints
- **Auth Provider (HTTP_AUTH mode)**: Uses `auth=SessionFileTokenVerifier` – FastMCP's official TokenVerifier protocol. Token extraction runs in middleware before the Mount, bypassing get_http_headers() bug (PrefectHQ/fastmcp#596)
- **Token in Tools**: `with_auth_context` uses `get_access_token()` from fastmcp.server.dependencies, not `get_http_headers()`. Access token flows from auth middleware to tools via dependency injection
- **Custom Routes**: `extract_bearer_token_from_request(request)` remains for MTProto API and web setup – direct request access works
- **Critical Parameter**: `stateless_http=True` is required for HTTP transport
- **Decorator Order**: `@with_auth_context` must be the innermost decorator on all tool functions
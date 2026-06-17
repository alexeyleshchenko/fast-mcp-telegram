"""
Main server module for the Telegram MCP server functionality.
Provides API endpoints and core bot features.
"""

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager
from typing import Literal

from fastmcp import FastMCP

from src.client.connection import (
    _cleanup_inactive_sessions,
    cleanup_idle_sessions,
    cleanup_session_cache,
    is_s3_enabled,
    validate_api_credentials,
)
from src.config.logging import setup_logging
from src.config.server_config import cfg
from src.server_components.attachment_routes import register_attachment_routes
from src.server_components.auth_middleware import UrlTokenMiddleware
from src.server_components.health import register_health_routes
from src.server_components.middleware_register import register_mcp_middleware
from src.server_components.mtproto_api import register_mtproto_api_routes
from src.server_components.server_card import register_server_card_route
from src.server_components.tools_register import register_tools
from src.server_components.web_setup import register_web_setup_routes
from src.telemetry import telemetry_task

logger = logging.getLogger(__name__)

# Get configuration
config = cfg()

# Background cleanup task
_cleanup_task = None
_telemetry_task = None


async def cleanup_loop():
    """Background task: inactivity and idle session cleanup every 60 seconds."""
    logger.info("Starting background cleanup task")

    # On startup, run an immediate inactivity cleanup
    try:
        await _run_inactivity_cleanup()
    except Exception as e:
        logger.error("Error in startup inactivity cleanup: %s", e)

    cleanup_cycle = 0
    inactivity_check_interval = 1440  # every 24 hours (1440 iterations * 60s)

    # Periodic cleanup loop
    while True:
        try:
            await asyncio.sleep(60)  # Check every minute
            cleanup_cycle += 1

            # Run daily inactivity cleanup
            if cleanup_cycle % inactivity_check_interval == 0:
                try:
                    await _run_inactivity_cleanup()
                except Exception as e:
                    logger.error("Error in periodic inactivity cleanup: %s", e)

            # Disconnect idle cached sessions
            await cleanup_idle_sessions()
        except asyncio.CancelledError:
            logger.info("Background cleanup task cancelled")
            break
        except Exception as e:
            logger.error("Error in cleanup task: %s", e)
            await asyncio.sleep(60)  # Wait before retrying


async def _run_inactivity_cleanup():
    """Run inactivity-based session file cleanup and log results."""
    deleted = await _cleanup_inactive_sessions()
    if deleted:
        logger.info("Inactivity cleanup: removed %s session(s)", deleted)


@asynccontextmanager
async def lifespan(app: FastMCP):
    """Lifecycle manager for the MCP server."""
    # Startup: S3 session storage health check
    if is_s3_enabled():
        try:
            import aiobotocore  # noqa: F401
        except ImportError:
            logger.critical(
                "S3_SESSION_STORAGE=true but aiobotocore not installed. "
                "Install with: pip install fast-mcp-telegram[s3]"
            )
            raise SystemExit(1) from None
        from src import s3_session
        try:
            s3_session.configure(cfg().s3_bucket)
            await s3_session.health_check()
            logger.info("✓ S3 session storage: bucket '%s' verified", cfg().s3_bucket)
        except Exception as e:
            logger.error("❌ S3 session storage startup check failed: %s", e)
            raise

    # Startup: validate credentials early
    try:
        validate_api_credentials()
    except ValueError as e:
        logger.error("❌ Configuration error: %s", e)
        raise

    # Startup: remove orphaned .session.tmp files from previous crashes
    session_dir = cfg().session_directory
    if session_dir.exists():
        for tmp_file in session_dir.glob("*.session.tmp"):
            logger.info("Removing orphaned temp file: %s", tmp_file.name)
            tmp_file.unlink(missing_ok=True)

    # Startup: background cleanup
    global _cleanup_task, _telemetry_task
    _cleanup_task = asyncio.create_task(cleanup_loop())

    # Startup: anonymous telemetry (fire-and-forget heartbeat loop)
    _telemetry_task = asyncio.create_task(telemetry_task())

    yield

    # Shutdown
    if _telemetry_task:
        _telemetry_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _telemetry_task

    # Graceful S3 shutdown: mark shutting down → evict all sessions → close S3 client
    if is_s3_enabled():
        from src import s3_session

        s3_session.mark_shutting_down()

    # Cancel background cleanup task
    if _cleanup_task:
        _cleanup_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _cleanup_task

    # Evict all cached sessions (handles S3 upload when enabled)
    # Dynamic timeout: 10s per session, min 25s, max 120s
    await cleanup_session_cache()

    # Close S3 client after all evictions are done
    if is_s3_enabled():
        try:
            await asyncio.wait_for(s3_session.close_s3_client(), timeout=5)
        except TimeoutError:
            logger.warning("Shutdown: S3 client close timed out")


setup_logging()

# Initialize MCP server
# Note: auth is handled by the @require_auth decorator on each tool,
# not by FastMCP transport-level auth. This lets unauthenticated calls
# reach the decorator which returns structured guidance instead of 401.
mcp = FastMCP("Telegram MCP Server", lifespan=lifespan)

# Register routes and tools immediately (no on_startup hook available)
register_health_routes(mcp)
register_web_setup_routes(mcp)
register_mtproto_api_routes(mcp)
register_attachment_routes(mcp)
register_tools(mcp)
register_server_card_route(mcp)
register_mcp_middleware(mcp, config)


def main():
    """Entry point for console script; runs the MCP server."""
    transport: Literal["stdio", "http"] = config.transport
    if transport == "http":
        # Use http_app() to get the Starlette application so we can add middleware
        app = mcp.http_app(
            path="/v1/mcp",
            stateless_http=True,
        )

        # Add URL token middleware for clients that can't set headers
        if config.require_auth:
            app = UrlTokenMiddleware(app, config)

        # Run with uvicorn
        import uvicorn

        uvicorn.run(
            app,
            host=config.host,
            port=config.port,
            log_level="info",
        )
    else:
        mcp.run(transport=transport)


# Run the server if this file is executed directly
if __name__ == "__main__":
    main()

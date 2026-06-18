"""Synchronous HTTP server for the telemetry collector.

Serves the /v1/event endpoint and /health check using stdlib
``http.server.ThreadingHTTPServer`` — no ASGI framework needed.
"""

from __future__ import annotations

import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from app.services import (
    RateLimitError,
    StorageBackend,
    ValidationError,
    process_event,
)


def create_handler(
    storage: StorageBackend,
) -> type[BaseHTTPRequestHandler]:
    """Build a ``BaseHTTPRequestHandler`` subclass wired to *storage*.

    In tests, pass an in-memory ``storage`` backend.
    In production, ``main()`` builds a ``PgStorage`` and passes it here.
    """

    class TelemetryHandler(BaseHTTPRequestHandler):
        """Minimal HTTP handler for telemetry ingestion."""

        server_version = "TelemetryCollector/0.1.0"
        sys_version = ""

        # ---- helpers ----

        def _json_response(self, data: object, status: int = 200) -> None:
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_body(self) -> bytes:
            length = int(self.headers.get("Content-Length", 0))
            return self.rfile.read(length)

        # ---- routes ----

        def do_GET(self) -> None:
            if self.path == "/health":
                now = time.time()
                try:
                    last = storage.last_event_at()
                except Exception:
                    last = None

                if last is not None:
                    last_ts = last.timestamp() if hasattr(last, "timestamp") else float(last)
                    seconds_ago = round(now - last_ts)
                    last_iso = (
                        last.isoformat()
                        if hasattr(last, "isoformat")
                        else str(last)
                    )
                else:
                    seconds_ago = None
                    last_iso = None

                return self._json_response({
                    "status": "ok",
                    "service": "telemetry-collector",
                    "last_heartbeat_at": last_iso,
                    "seconds_since_last_heartbeat": seconds_ago,
                })
            self._json_response({"error": "not found"}, 404)

        def do_POST(self) -> None:
            if self.path != "/v1/event":
                return self._json_response({"error": "not found"}, 404)

            raw = self._read_body()

            try:
                data = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                return self._json_response({"detail": "Invalid JSON body"}, 422)

            if not isinstance(data, dict):
                return self._json_response(
                    {"detail": "Body must be a JSON object"}, 422
                )

            client_ip = self.client_address[0]

            try:
                process_event(data, client_ip, storage)
                self.send_response(204)
                self.end_headers()
            except ValidationError as exc:
                self._json_response({"detail": str(exc)}, 422)
            except RateLimitError as exc:
                self._json_response({"detail": str(exc)}, 429)

        def log_message(self, fmt: str, *args: object) -> None:
            """Quiet — container logs via stdout instead."""
            pass

    return TelemetryHandler


# ---- Production entry point (used by Docker CMD) ----


def main() -> None:
    """Connect to PostgreSQL and start the HTTP server.

    Called by ``python3 -m app.main`` in the Docker container.
    """
    from app.database import PgStorage
    from app.settings import DSN

    print(f"Connecting to database…", file=sys.stderr)
    storage = PgStorage(DSN)
    try:
        storage.connect()
    except Exception as exc:
        print(f"Database connection failed: {exc}", file=sys.stderr)
        sys.exit(1)

    print("Connected. Starting HTTP server on http://0.0.0.0:8000", file=sys.stderr)
    handler = create_handler(storage)
    server = ThreadingHTTPServer(("0.0.0.0", 8000), handler)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down…", file=sys.stderr)
    finally:
        server.server_close()
        storage.close()
        print("Stopped.", file=sys.stderr)


if __name__ == "__main__":
    main()

"""End-to-end tests for the telemetry collector against real PostgreSQL.

Prerequisites (local):
    docker compose -f collector/docker-compose.dev.yml up -d postgres

In CI these tests run against a GitHub Actions postgres service container.

All tests in this file are marked ``e2e`` — skip them with ``-m "not e2e"``
when PostgreSQL is not available.
"""

from __future__ import annotations

import json as json_module
import os
import threading
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer

import pytest

pytestmark = [
    pytest.mark.e2e,
]

# -- Helpers -----------------------------------------------------------------


def _pg_dsn() -> str:
    """Build a PostgreSQL DSN from environment variables with defaults.

    Local dev: docker compose remaps PG to port 5433, uses telemetry/telemetry.
    CI:       postgres service container on port 5432, same credentials.
    """
    pg_host = os.environ.get("TELEMETRY_PG_HOST", "localhost")
    pg_port = os.environ.get("TELEMETRY_PG_PORT", "5433")
    pg_user = os.environ.get("TELEMETRY_PG_USER", "telemetry")
    pg_pass = os.environ.get("TELEMETRY_PG_PASSWORD", "telemetry")
    pg_db = os.environ.get("TELEMETRY_PG_DB", "telemetry")
    return f"postgres://{pg_user}:{pg_pass}@{pg_host}:{pg_port}/{pg_db}"


def _pg_is_available() -> bool:
    """Quick check whether PostgreSQL is reachable.

    Skips the test file entirely when PG is not running (local dev
    without ``docker compose up``).
    """
    try:
        import psycopg2

        dsn = _pg_dsn()
        conn = psycopg2.connect(dsn, connect_timeout=3)
        conn.close()
        return True
    except (ImportError, Exception):
        return False


if not _pg_is_available():
    pytest.skip(
        "PostgreSQL not reachable — set TELEMETRY_PG_HOST/PORT or start docker compose",
        allow_module_level=True,
    )


# -- Fixtures -----------------------------------------------------------------


@pytest.fixture
def pg_storage():
    """PgStorage connected to a real PostgreSQL instance.

    Cleans up the telemetry table after all tests in the module.
    """
    from app.database import PgStorage

    dsn = _pg_dsn()
    storage = PgStorage(dsn)
    storage.connect()

    # Wipe any leftover data from previous runs
    with storage._conn.cursor() as cur:
        cur.execute("DELETE FROM telemetry")
    storage._conn.commit()

    yield storage

    # Cleanup
    with storage._conn.cursor() as cur:
        cur.execute("DELETE FROM telemetry")
    storage._conn.commit()
    storage.close()


@pytest.fixture
def e2e_server(pg_storage):
    """ThreadingHTTPServer wired to real PostgreSQL storage on a random port."""
    from app.main import create_handler

    handler = create_handler(pg_storage)
    server = ThreadingHTTPServer(("localhost", 0), handler)
    port = server.server_address[1]

    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()

    yield port

    server.shutdown()


class _E2EClient:
    """Minimal HTTP client for e2e tests."""

    def __init__(self, port: int):
        self._port = port

    def _request(self, method: str, path: str, body: bytes | None = None):
        conn = HTTPConnection("localhost", self._port, timeout=10)
        try:
            headers = {"Content-Type": "application/json"} if body else {}
            conn.request(method, path, body=body, headers=headers)
            resp = conn.getresponse()
            data = resp.read()
            resp.status_code = resp.status
            resp.json = lambda _data=data: json_module.loads(_data)
            return resp
        finally:
            conn.close()

    def get(self, path: str):
        return self._request("GET", path)

    def post(self, path: str, json: dict):
        body = json_module.dumps(json).encode()
        return self._request("POST", path, body)


@pytest.fixture
def client(e2e_server):
    """HTTP client connected to the e2e test server."""
    return _E2EClient(e2e_server)


@pytest.fixture
def valid_payload():
    """A valid telemetry payload dict."""
    from collector.tests._helpers import make_nested_payload

    return make_nested_payload()


def _count_rows(storage) -> int:
    """Count rows in the telemetry table."""
    with storage._conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM telemetry")
        return cur.fetchone()[0]


def _fetch_all(storage) -> list[dict]:
    """Fetch all rows for inspection."""
    with storage._conn.cursor() as cur:
        cur.execute("SELECT * FROM telemetry ORDER BY id")
        columns = [desc[0] for desc in cur.description]
        rows = cur.fetchall()
        return [dict(zip(columns, row, strict=False)) for row in rows]


# -- Tests --------------------------------------------------------------------


class TestE2EHealth:
    """Health endpoint works with real storage attached."""

    def test_health(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"


class TestE2ECollect:
    """Collecting events against real PostgreSQL."""

    def test_store_and_retrieve(self, client, pg_storage, valid_payload):
        """A valid payload is stored and queryable via PostgreSQL."""
        resp = client.post("/v1/event", json=valid_payload)
        assert resp.status_code == 204

        count = _count_rows(pg_storage)
        assert count == 1

        rows = _fetch_all(pg_storage)
        row = rows[0]
        # Check extracted instance_id column
        assert row["instance_id"] == valid_payload["iid"]
        # Check JSONB payload
        stored = row["payload"]  # psycopg2 returns JSONB as dict natively
        assert stored["iid"] == valid_payload["iid"]
        assert stored["v"] == 1
        # Verify indexes exist and hash columns are populated
        assert len(row["payload_hash"]) == 64  # SHA-256 hex
        assert len(row["source_ip_hash"]) == 64  # SHA-256 hex

    def test_multiple_events(self, client, pg_storage):
        """Multiple valid events are all stored."""
        from collector.tests._helpers import make_nested_payload

        n = 5
        for i in range(n):
            data = make_nested_payload(
                iid=f"550e8400-e29b-41d4-a716-4466554400{i:02d}",
                counters={"total_calls": i, "errors": 0},
            )
            resp = client.post("/v1/event", json=data)
            assert resp.status_code == 204

        count = _count_rows(pg_storage)
        assert count == n

    def test_invalid_payload_rejected(self, client, pg_storage, valid_payload):
        """Invalid payload returns 422 and nothing is stored."""
        del valid_payload["iid"]
        resp = client.post("/v1/event", json=valid_payload)
        assert resp.status_code == 422

        count = _count_rows(pg_storage)
        assert count == 0

    def test_duplicate_dedup_postgres(self, client, pg_storage, valid_payload):
        """Same payload sent twice within dedup window is deduped (single row)."""
        resp1 = client.post("/v1/event", json=valid_payload)
        assert resp1.status_code == 204

        resp2 = client.post("/v1/event", json=valid_payload)
        assert resp2.status_code == 204  # Silent dedup

        count = _count_rows(pg_storage)
        assert count == 1, f"Expected 1 row after dedup, got {count}"

    def test_rate_limit_postgres(self, client, pg_storage):
        """Exceeding per-instance rate limit returns 429."""
        from app.services import INSTANCE_RATE_LIMIT

        from collector.tests._helpers import make_nested_payload

        # Send up to the limit
        for i in range(INSTANCE_RATE_LIMIT):
            data = make_nested_payload(
                iid="rate-limited-test-uuid",
                counters={"total_calls": i, "errors": 0},
            )
            resp = client.post("/v1/event", json=data)
            assert resp.status_code == 204

        # One more should be rate-limited
        data = make_nested_payload(
            iid="rate-limited-test-uuid",
            counters={"total_calls": 9999, "errors": 0},
        )
        resp = client.post("/v1/event", json=data)
        assert resp.status_code == 429

        # Verify only the limit number of rows exists
        count = _count_rows(pg_storage)
        assert count == INSTANCE_RATE_LIMIT

    def test_different_instance_not_rate_limited(self, client, pg_storage):
        """Events from different iids don't interfere with rate limiting."""
        from app.services import INSTANCE_RATE_LIMIT

        from collector.tests._helpers import make_nested_payload

        # Saturate one iid
        for i in range(INSTANCE_RATE_LIMIT):
            data = make_nested_payload(
                iid="saturated-instance",
                counters={"total_calls": i, "errors": 0},
            )
            resp = client.post("/v1/event", json=data)
            assert resp.status_code == 204

        # A different iid should still be allowed
        fresh = make_nested_payload(
            iid="fresh-instance",
            counters={"total_calls": 0, "errors": 0},
        )
        resp = client.post("/v1/event", json=fresh)
        assert resp.status_code == 204

    def test_column_types_and_indexes(self, pg_storage):
        """Verify table schema and indexes exist."""
        with pg_storage._conn.cursor() as cur:
            # Check columns exist with expected types
            cur.execute(
                """
                SELECT column_name, data_type, is_nullable
                FROM information_schema.columns
                WHERE table_name = 'telemetry'
                ORDER BY ordinal_position
                """
            )
            cols = cur.fetchall()
            col_names = [c[0] for c in cols]
            assert "id" in col_names
            assert "instance_id" in col_names
            assert "payload" in col_names
            assert "payload_hash" in col_names
            assert "source_ip_hash" in col_names
            assert "received_at" in col_names

            # Check key indexes exist
            cur.execute(
                """
                SELECT indexname
                FROM pg_indexes
                WHERE tablename = 'telemetry'
                """
            )
            index_names = {r[0] for r in cur.fetchall()}
            assert "telemetry_pkey" in index_names
            assert "idx_telemetry_instance_id" in index_names
            assert "idx_telemetry_received_at" in index_names
            assert "idx_telemetry_payload_hash" in index_names

    def test_row_cap_postgres(self, pg_storage):
        """enforce_row_cap correctly removes oldest rows."""
        from collector.tests._helpers import make_nested_payload

        cap = 5
        # Insert more than cap
        for i in range(cap + 3):
            data = make_nested_payload(
                iid=f"cap-test-{i:02d}",
                counters={"total_calls": i, "errors": 0},
            )
            # Bypass the HTTP layer — store directly
            from app.models import TelemetryPayload
            from app.services import compute_payload_hash, hash_source_ip

            payload = TelemetryPayload.from_dict(data)
            pg_storage.store(
                payload, hash_source_ip("10.0.0.1"), compute_payload_hash(payload)
            )

        count_before = _count_rows(pg_storage)
        assert count_before == cap + 3  # all rows present before cap

        removed = pg_storage.enforce_row_cap(max_rows=cap)
        assert removed == 3

        count_after = _count_rows(pg_storage)
        assert count_after == cap

    def test_ttl_cleanup_postgres(self, pg_storage):
        """cleanup_ttl removes rows older than retention window."""
        # Insert a row with an old received_at
        with pg_storage._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO telemetry (
                    instance_id, payload, payload_hash, source_ip_hash, received_at
                )
                VALUES (
                    'old-test', '{}'::jsonb, 'oldhash', 'oldip',
                    NOW() - INTERVAL '200 days'
                )
                """
            )
        pg_storage._conn.commit()

        removed = pg_storage.cleanup_ttl(retention_days=90)
        assert removed >= 1

        # Verify it's gone
        with pg_storage._conn.cursor() as cur:
            cur.execute("SELECT 1 FROM telemetry WHERE instance_id = 'old-test'")
            assert cur.fetchone() is None

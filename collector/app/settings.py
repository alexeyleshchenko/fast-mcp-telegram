"""Application settings loaded from environment variables."""

import os

DSN: str = os.environ.get(
    "TELEMETRY_DSN",
    "postgres://telemetry:telemetry@localhost:5432/telemetry",
)

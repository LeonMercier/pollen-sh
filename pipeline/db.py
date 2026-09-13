"""Database connection helpers for the pipeline."""

import logging
import time
from pathlib import Path

import psycopg

from . import config

log = logging.getLogger(__name__)

SCHEMA_FILE = Path(__file__).resolve().parent.parent / "db" / "init" / "01_schema.sql"


def connect(retries: int = 10, delay: float = 2.0) -> psycopg.Connection:
    """Connect to PostgreSQL, waiting for it to accept connections.

    The pipeline container can start before postgres is ready when it is run by
    hand right after `compose up`, so a short retry loop is worth the six lines.
    """
    url = config.database_url()
    last: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            return psycopg.connect(url, autocommit=True)
        except psycopg.OperationalError as exc:
            last = exc
            log.info("Database not ready (attempt %d/%d), waiting", attempt, retries)
            time.sleep(delay)
    raise RuntimeError(f"Could not connect to the database: {last}")


def apply_schema(conn: psycopg.Connection) -> None:
    """Apply the idempotent schema file.

    The postgres image only runs db/init on a fresh volume, so applying it here
    too means a schema change ships with the code instead of needing a reset.
    """
    log.info("Applying schema from %s", SCHEMA_FILE)
    conn.execute(SCHEMA_FILE.read_text())

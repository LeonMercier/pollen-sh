"""Environment configuration for the API."""

import os


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Required environment variable {name} is not set")
    return value


def database_url() -> str:
    host = os.environ.get("POSTGRES_HOST", "postgres").strip()
    port = os.environ.get("POSTGRES_PORT", "5432").strip()
    name = os.environ.get("POSTGRES_DB", "pollen").strip()
    user = os.environ.get("POSTGRES_USER", "pollen").strip()
    password = _require("POSTGRES_PASSWORD")
    return f"postgresql://{user}:{password}@{host}:{port}/{name}"


# A forecast older than this means the daily pipeline has stopped running.
# /health reports unhealthy past this point, which is what monitoring watches.
MAX_FORECAST_AGE_HOURS = int(os.environ.get("MAX_FORECAST_AGE_HOURS", "36"))

# Small pool: this is a low-traffic site and each request makes one or two
# short queries.
POOL_MIN_SIZE = int(os.environ.get("POOL_MIN_SIZE", "1"))
POOL_MAX_SIZE = int(os.environ.get("POOL_MAX_SIZE", "8"))

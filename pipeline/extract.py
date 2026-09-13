"""Bronze layer: fetch the daily CAMS forecast as GRIB."""

import logging
import os
import time
from datetime import date
from pathlib import Path

import cdsapi

from . import config

log = logging.getLogger(__name__)

# CDS queues requests server-side, and the queue is occasionally slow or the
# endpoint briefly unavailable. Retry with backoff, but keep the total bounded
# so a stuck run cannot overlap the next day's.
MAX_ATTEMPTS = 5
BACKOFF_SECONDS = [60, 300, 900, 1800]


def _request_body(day: date) -> dict:
    iso = day.isoformat()
    return {
        "variable": sorted(config.CDS_VARIABLES),
        "model": ["ensemble"],
        "level": ["0"],
        "date": [f"{iso}/{iso}"],
        "type": ["forecast"],
        "time": [config.FORECAST_BASE_TIME],
        "leadtime_hour": config.LEADTIME_HOURS,
        "data_format": "grib",
        "area": config.cdsapi_area(),
    }


def prune_bronze(keep_days: int = config.BRONZE_RETENTION_DAYS) -> None:
    """Drop GRIB files older than the retention window."""
    files = sorted(config.bronze_dir().glob("pollen_*.grib"))
    for stale in files[:-keep_days] if keep_days > 0 else files:
        log.info("Pruning old bronze file %s", stale.name)
        stale.unlink(missing_ok=True)


def extract(day: date | None = None, force: bool = False) -> Path:
    """Download the forecast for `day` and return the local GRIB path.

    An existing download for the same day is reused unless `force` is set. This
    keeps re-runs and debugging from hammering an API that is rate-limited and
    queued.
    """
    day = day or date.today()
    target = config.bronze_dir() / f"pollen_{day.isoformat()}.grib"

    if target.exists() and target.stat().st_size > 0 and not force:
        log.info(
            "Reusing existing bronze file %s (%.1f MB); pass --force to re-download",
            target.name,
            target.stat().st_size / 1024**2,
        )
        return target

    url, key = config.cdsapi_credentials()
    client = cdsapi.Client(url=url, key=key, quiet=True, progress=False)
    request = _request_body(day)

    # Download to a temporary name so an interrupted transfer can never be
    # mistaken for a complete cached file on the next run.
    partial = target.with_suffix(".grib.partial")

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            log.info(
                "Requesting %s for %s (attempt %d/%d)",
                config.DATASET,
                day.isoformat(),
                attempt,
                MAX_ATTEMPTS,
            )
            started = time.monotonic()
            client.retrieve(config.DATASET, request, str(partial))
            elapsed = time.monotonic() - started

            size = partial.stat().st_size
            if size == 0:
                raise RuntimeError("CDS returned an empty file")

            partial.replace(target)
            log.info(
                "Downloaded %.1f MB in %.0f s -> %s",
                size / 1024**2,
                elapsed,
                target.name,
            )
            return target

        except Exception as exc:  # noqa: BLE001 - retry on anything transient
            partial.unlink(missing_ok=True)
            if attempt == MAX_ATTEMPTS:
                log.error("CDS request failed after %d attempts", MAX_ATTEMPTS)
                raise
            delay = BACKOFF_SECONDS[attempt - 1]
            log.warning("CDS request failed (%s); retrying in %d s", exc, delay)
            time.sleep(delay)

    raise AssertionError("unreachable")


def main() -> None:
    """Standalone entrypoint, mainly for verifying credentials and grid shape."""
    import argparse

    parser = argparse.ArgumentParser(description="Download one day of CAMS forecast")
    parser.add_argument("--date", type=date.fromisoformat, default=None)
    parser.add_argument("--force", action="store_true", help="ignore any cached file")
    args = parser.parse_args()

    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    print(extract(args.date, force=args.force))


if __name__ == "__main__":
    main()

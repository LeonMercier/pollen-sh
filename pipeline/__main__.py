"""Daily entrypoint: extract -> transform -> load.

Run as `python -m pipeline`. Exits non-zero on any failure, in which case the
blue-green swap never happened and the previous forecast is still live.
"""

import argparse
import logging
import os
import sys
import time
import urllib.request
from datetime import date

from . import config, db
from .extract import extract, prune_bronze
from .load import load
from .transform import transform

log = logging.getLogger("pipeline")


def _ping_healthcheck(suffix: str = "") -> None:
    """Signal liveness to healthchecks.io, if configured.

    A daily job that silently stops is the realistic failure mode for this
    project, so the absence of a ping is what raises the alarm.
    """
    if not config.HEALTHCHECK_URL:
        return
    url = config.HEALTHCHECK_URL.rstrip("/") + suffix
    try:
        urllib.request.urlopen(url, timeout=10).close()
        log.info("Pinged healthcheck%s", suffix or "")
    except Exception as exc:  # noqa: BLE001 - monitoring must never fail the run
        log.warning("Healthcheck ping failed: %s", exc)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the daily pollen ETL")
    parser.add_argument(
        "--date", type=date.fromisoformat, default=None, help="forecast day (default: today UTC)"
    )
    parser.add_argument("--force", action="store_true", help="re-download even if cached")
    parser.add_argument(
        "--skip-extract",
        action="store_true",
        help="reprocess the newest cached GRIB without contacting CDS",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    started = time.monotonic()
    _ping_healthcheck("/start")

    try:
        if args.skip_extract:
            cached = sorted(config.bronze_dir().glob("pollen_*.grib"))
            if not cached:
                raise RuntimeError("--skip-extract given but the bronze layer is empty")
            grib_path = cached[-1]
            log.info("Reprocessing cached %s", grib_path.name)
        else:
            grib_path = extract(args.date, force=args.force)

        cube = transform(grib_path)

        conn = db.connect()
        try:
            db.apply_schema(conn)
            rows = load(conn, cube)
        finally:
            conn.close()

        prune_bronze()

        log.info("Done: %d rows in %.0f s", rows, time.monotonic() - started)
        _ping_healthcheck()
        return 0

    except Exception:
        log.exception("Pipeline run failed; the previous forecast remains live")
        _ping_healthcheck("/fail")
        return 1


if __name__ == "__main__":
    sys.exit(main())

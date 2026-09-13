"""Populate the `cities` table from the GeoNames cities500 dump.

Run occasionally (`python -m pipeline.geocode`), not daily -- GeoNames changes
slowly and the grid it snaps to is fixed.

Snapping a city to its forecast cell is two `round()` calls (see grid.py), so
this replaces a Spark cross join of ~10k cities against ~36k grid points.
"""

import csv
import io
import logging
import os
import sys
import urllib.request
import zipfile

import psycopg

from . import config, db
from .grid import Grid

log = logging.getLogger("geocode")

# Column positions in the tab-separated GeoNames dump.
# See https://download.geonames.org/export/dump/readme.txt
GEONAME_ID, NAME, ASCII_NAME = 0, 1, 2
LATITUDE, LONGITUDE = 4, 5
COUNTRY_CODE, ADMIN1 = 8, 10
POPULATION, TIMEZONE = 14, 17


def _read_grid(conn: psycopg.Connection) -> Grid:
    row = conn.execute("SELECT lat0, lon0, step, n_lat, n_lon FROM grid WHERE id = 1").fetchone()
    if row is None:
        raise RuntimeError(
            "The grid table is empty. Run the forecast pipeline once "
            "(`python -m pipeline`) before geocoding."
        )
    return Grid(lat0=row[0], lon0=row[1], step=row[2], n_lat=row[3], n_lon=row[4])


def _download_cities() -> list[list[str]]:
    log.info("Downloading %s", config.GEONAMES_URL)
    with urllib.request.urlopen(config.GEONAMES_URL, timeout=120) as response:
        payload = response.read()
    log.info("Downloaded %.1f MB", len(payload) / 1024**2)

    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        with archive.open("cities500.txt") as handle:
            text = io.TextIOWrapper(handle, encoding="utf-8", newline="")
            rows = list(csv.reader(text, delimiter="\t", quoting=csv.QUOTE_NONE))

    log.info("Parsed %d cities", len(rows))
    return rows


def _prepare(rows: list[list[str]], grid: Grid) -> list[tuple]:
    """Filter to cities inside the forecast grid and attach their cell indices."""
    prepared: list[tuple] = []
    skipped_country = skipped_outside = 0

    for row in rows:
        if row[COUNTRY_CODE] not in config.EUROPE_COUNTRIES:
            skipped_country += 1
            continue

        lat, lon = float(row[LATITUDE]), float(row[LONGITUDE])
        cell = grid.index_of(lat, lon)
        if cell is None:
            skipped_outside += 1
            continue
        lat_idx, lon_idx = cell

        prepared.append(
            (
                int(row[GEONAME_ID]),
                row[NAME],
                row[ASCII_NAME],
                row[COUNTRY_CODE],
                row[ADMIN1] or None,
                int(row[POPULATION]) if row[POPULATION] else None,
                row[TIMEZONE] or None,
                lat,
                lon,
                lat_idx,
                lon_idx,
            )
        )

    log.info(
        "Kept %d cities (%d outside Europe, %d outside the forecast grid)",
        len(prepared),
        skipped_country,
        skipped_outside,
    )
    return prepared


def geocode() -> int:
    conn = db.connect()
    try:
        db.apply_schema(conn)
        grid = _read_grid(conn)
        log.info(
            "Snapping to a %dx%d grid, origin (%.2f, %.2f), step %.4f deg",
            grid.n_lat, grid.n_lon, grid.lat0, grid.lon0, grid.step,
        )

        prepared = _prepare(_download_cities(), grid)
        if not prepared:
            raise RuntimeError("No cities fell inside the forecast grid; refusing to truncate")

        # Only ~10k rows, so a truncate-and-reload inside one transaction is
        # sub-second. No blue-green machinery needed here.
        with conn.transaction():
            conn.execute("TRUNCATE cities")
            statement = """
                COPY cities (geoname_id, name, ascii_name, country_code, admin1_code,
                             population, timezone, latitude, longitude, lat_idx, lon_idx)
                FROM STDIN
            """
            with conn.cursor() as cur, cur.copy(statement) as copy:
                for record in prepared:
                    copy.write_row(record)

        conn.execute("ANALYZE cities")
        count = conn.execute("SELECT count(*) FROM cities").fetchone()[0]
        log.info("Loaded %d cities", count)
        return count
    finally:
        conn.close()


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    try:
        geocode()
        return 0
    except Exception:
        log.exception("Geocode failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())

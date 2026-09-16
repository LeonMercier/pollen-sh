"""Populate the `cities` table from the GeoNames cities500 dump.

Run occasionally (`python -m pipeline.geocode`), not daily -- GeoNames changes
slowly and the grid it snaps to is fixed.

Snapping a city to its forecast cell is two `round()` calls (see grid.py), so
this replaces a Spark cross join of ~10k cities against ~36k grid points.
"""

import csv
import io
import logging
import re
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
GEONAME_ID, NAME, ASCII_NAME, ALTERNATE_NAMES = 0, 1, 2, 3
LATITUDE, LONGITUDE = 4, 5
COUNTRY_CODE, ADMIN1 = 8, 10
POPULATION, TIMEZONE = 14, 17


# Aliases we keep are Latin-script only: ASCII letters plus the Latin-1,
# Latin Extended-A/B and Latin Extended Additional ranges, so Finnish, Swedish,
# Estonian, Latvian, Polish and German forms all survive while the Cyrillic,
# Greek, Arabic and CJK transliterations GeoNames also carries are dropped.
# Those would only add noise to an autocomplete aimed at Northern Europe.
_LATIN_NAME = re.compile(r"^[A-Za-z0-9\u00C0-\u024F\u1E00-\u1EFF .'\u2019-]+$")

MIN_ALIAS_LEN = 2
MAX_ALIAS_LEN = 100


def _aliases(row: list[str]) -> list[str]:
    """Every name this city should be findable by.

    The localised name and the ASCII form come first, then the Latin-script
    entries from the alternatenames column -- which is where the other language
    form of a bilingual place lives (GeoNames stores only one of Loviisa and
    Lovisa in `name`).

    Deduplicated case- and accent-insensitively, keeping the first spelling
    seen, so "Helsinki" and "helsinki" do not both become rows.
    """
    candidates = [row[NAME], row[ASCII_NAME]]
    if len(row) > ALTERNATE_NAMES and row[ALTERNATE_NAMES]:
        candidates += row[ALTERNATE_NAMES].split(",")

    seen: set[str] = set()
    out: list[str] = []
    for raw in candidates:
        alias = raw.strip()
        if not (MIN_ALIAS_LEN <= len(alias) <= MAX_ALIAS_LEN):
            continue
        if not _LATIN_NAME.match(alias):
            continue
        # Cheap ASCII-fold for dedupe only; the database's city_norm() is the
        # authority for matching, this just avoids obvious duplicate rows.
        key = alias.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(alias)
    return out


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


def _prepare(rows: list[list[str]], grid: Grid) -> tuple[list[tuple], list[tuple]]:
    """Filter to cities inside the forecast grid and attach cell indices.

    Returns (city rows, alias rows).
    """
    prepared: list[tuple] = []
    aliases: list[tuple] = []
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

        geoname_id = int(row[GEONAME_ID])
        aliases.extend((geoname_id, alias) for alias in _aliases(row))

    log.info(
        "Kept %d cities (%d outside Europe, %d outside the forecast grid)",
        len(prepared),
        skipped_country,
        skipped_outside,
    )
    log.info(
        "Collected %d searchable names (%.1f per city)",
        len(aliases),
        len(aliases) / len(prepared) if prepared else 0,
    )
    return prepared, aliases


def geocode() -> int:
    conn = db.connect()
    try:
        db.apply_schema(conn)
        grid = _read_grid(conn)
        log.info(
            "Snapping to a %dx%d grid, origin (%.2f, %.2f), step %.4f deg",
            grid.n_lat, grid.n_lon, grid.lat0, grid.lon0, grid.step,
        )

        prepared, aliases = _prepare(_download_cities(), grid)
        if not prepared:
            raise RuntimeError("No cities fell inside the forecast grid; refusing to truncate")

        # Only ~10k cities and ~10x that in aliases, so a truncate-and-reload
        # inside one transaction is sub-second. No blue-green machinery needed.
        # city_alias is truncated alongside cities because it references it.
        with conn.transaction():
            conn.execute("TRUNCATE cities, city_alias")
            statement = """
                COPY cities (geoname_id, name, ascii_name, country_code, admin1_code,
                             population, timezone, latitude, longitude, lat_idx, lon_idx)
                FROM STDIN
            """
            with conn.cursor() as cur, cur.copy(statement) as copy:
                for record in prepared:
                    copy.write_row(record)

            with conn.cursor() as cur, cur.copy(
                "COPY city_alias (geoname_id, alias) FROM STDIN"
            ) as copy:
                for record in aliases:
                    copy.write_row(record)

        conn.execute("ANALYZE cities")
        conn.execute("ANALYZE city_alias")
        count = conn.execute("SELECT count(*) FROM cities").fetchone()[0]
        alias_count = conn.execute("SELECT count(*) FROM city_alias").fetchone()[0]
        log.info("Loaded %d cities and %d searchable names", count, alias_count)
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

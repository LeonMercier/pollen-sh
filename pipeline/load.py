"""Gold layer: forecast cube -> PostgreSQL, with a blue-green swap.

New data goes into a staging table, is indexed and validated there, and is then
promoted in a single transaction. The live table is never partially written, so
the API keeps serving yesterday's forecast until the moment a complete new one
is ready -- and keeps serving it if the run fails.
"""

import logging
from typing import Iterator

import numpy as np
import psycopg

from . import config
from .transform import Cube

log = logging.getLogger(__name__)

STAGING = "pollen_forecast_staging"

# Fewer rows than this means something went wrong upstream regardless of season.
MIN_ROWS = 100_000


class ValidationError(RuntimeError):
    """Raised when staged data fails its checks; the swap is then skipped."""


def _rows(cube: Cube) -> Iterator[tuple[str, int, int, list[float]]]:
    """Yield one row per (species, cell), each carrying the full hourly series."""
    _, _, n_lat, n_lon = cube.data.shape
    for s, species in enumerate(cube.species):
        for lat_idx in range(n_lat):
            # (n_hours, n_lon) -> (n_lon, n_hours) so each row's series is
            # contiguous when we slice it.
            series = np.ascontiguousarray(cube.data[s, :, lat_idx, :].T)
            for lon_idx in range(n_lon):
                yield species, lat_idx, lon_idx, series[lon_idx].tolist()
        log.info("Staged %s (%d/%d species)", species, s + 1, len(cube.species))


def _create_staging(conn: psycopg.Connection) -> None:
    conn.execute(f"DROP TABLE IF EXISTS {STAGING}")
    # Without indexes: the primary key is added after COPY, which is faster.
    conn.execute(f"CREATE TABLE {STAGING} (LIKE pollen_forecast INCLUDING DEFAULTS)")


def _copy(conn: psycopg.Connection, cube: Cube) -> None:
    statement = f"COPY {STAGING} (species, lat_idx, lon_idx, hourly) FROM STDIN (FORMAT BINARY)"
    with conn.cursor() as cur, cur.copy(statement) as copy:
        copy.set_types(["text", "int2", "int2", "float4[]"])
        for row in _rows(cube):
            copy.write_row(row)


def _validate(conn: psycopg.Connection, cube: Cube) -> int:
    expected = len(cube.species) * cube.grid.n_cells

    rows = conn.execute(f"SELECT count(*) FROM {STAGING}").fetchone()[0]
    if rows != expected:
        raise ValidationError(f"staged {rows} rows, expected exactly {expected}")
    if rows < MIN_ROWS:
        raise ValidationError(f"staged only {rows} rows, below the floor of {MIN_ROWS}")

    species = conn.execute(f"SELECT count(DISTINCT species) FROM {STAGING}").fetchone()[0]
    if species != len(cube.species):
        raise ValidationError(f"staged {species} species, expected {len(cube.species)}")

    bad_length = conn.execute(
        f"SELECT count(*) FROM {STAGING} WHERE array_length(hourly, 1) IS DISTINCT FROM %s",
        (cube.n_hours,),
    ).fetchone()[0]
    if bad_length:
        raise ValidationError(f"{bad_length} rows have an hourly array that is not {cube.n_hours} long")

    # An entirely flat forecast is suspicious but not impossible in midwinter,
    # so this warns rather than aborting.
    peak = float(cube.data.max())
    if peak <= 0.0:
        log.warning("Every value in this forecast is zero -- check the source data")
    else:
        log.info("Peak concentration in this run: %.1f grains/m3", peak)

    return rows


def _swap(conn: psycopg.Connection, cube: Cube) -> None:
    """Promote staging to live, together with its metadata, atomically."""
    with conn.transaction():
        conn.execute("DROP TABLE IF EXISTS pollen_forecast_old")
        conn.execute("ALTER TABLE IF EXISTS pollen_forecast RENAME TO pollen_forecast_old")
        conn.execute(
            "ALTER TABLE IF EXISTS pollen_forecast_old "
            "RENAME CONSTRAINT pollen_forecast_pkey TO pollen_forecast_old_pkey"
        )
        conn.execute(f"ALTER TABLE {STAGING} RENAME TO pollen_forecast")
        conn.execute(
            "ALTER TABLE pollen_forecast "
            f"RENAME CONSTRAINT {STAGING}_pkey TO pollen_forecast_pkey"
        )

        # Grid and run metadata must land in the same transaction: the API reads
        # start_time to turn array positions into timestamps.
        conn.execute(
            """
            INSERT INTO grid (id, lat0, lon0, step, n_lat, n_lon)
            VALUES (1, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET
                lat0 = EXCLUDED.lat0, lon0 = EXCLUDED.lon0, step = EXCLUDED.step,
                n_lat = EXCLUDED.n_lat, n_lon = EXCLUDED.n_lon
            """,
            (
                cube.grid.lat0,
                cube.grid.lon0,
                cube.grid.step,
                cube.grid.n_lat,
                cube.grid.n_lon,
            ),
        )
        conn.execute(
            """
            INSERT INTO forecast_run (id, start_time, n_hours, loaded_at)
            VALUES (1, %s, %s, now())
            ON CONFLICT (id) DO UPDATE SET
                start_time = EXCLUDED.start_time,
                n_hours    = EXCLUDED.n_hours,
                loaded_at  = EXCLUDED.loaded_at
            """,
            (cube.start_time, cube.n_hours),
        )

    # Outside the transaction so the swap commits as fast as possible.
    conn.execute("DROP TABLE IF EXISTS pollen_forecast_old")


def load(conn: psycopg.Connection, cube: Cube) -> int:
    """Stage, validate and promote a forecast cube. Returns the row count."""
    expected = len(cube.species) * cube.grid.n_cells
    log.info("Loading %d rows (%d species x %d cells)", expected, len(cube.species), cube.grid.n_cells)

    _create_staging(conn)
    _copy(conn, cube)

    log.info("Building primary key on staging table")
    conn.execute(
        f"ALTER TABLE {STAGING} ADD CONSTRAINT {STAGING}_pkey "
        "PRIMARY KEY (species, lat_idx, lon_idx)"
    )
    conn.execute(f"ANALYZE {STAGING}")

    rows = _validate(conn, cube)
    log.info("Validation passed: %d rows", rows)

    _swap(conn, cube)
    log.info("Swap complete -- the new forecast is live")
    return rows

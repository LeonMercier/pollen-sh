"""Database access for the API.

One pooled connection per query, opened lazily. Every statement is
parameterised; nothing here interpolates user input into SQL.
"""

import logging
from dataclasses import dataclass
from datetime import datetime

from psycopg_pool import ConnectionPool

import config

log = logging.getLogger(__name__)

_pool: ConnectionPool | None = None


def open_pool() -> ConnectionPool:
    """Create the pool. Does not connect -- startup must not depend on the DB."""
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            config.database_url(),
            min_size=config.POOL_MIN_SIZE,
            max_size=config.POOL_MAX_SIZE,
            open=False,
        )
        _pool.open(wait=False)
    return _pool


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


def pool() -> ConnectionPool:
    if _pool is None:
        return open_pool()
    return _pool


# ----------------------------------------------------------------------------
# Grid
# ----------------------------------------------------------------------------
@dataclass(frozen=True)
class Grid:
    lat0: float
    lon0: float
    step: float
    n_lat: int
    n_lon: int

    def coords_of(self, lat_idx: int, lon_idx: int) -> tuple[float, float]:
        return (
            round(self.lat0 + lat_idx * self.step, 6),
            round(self.lon0 + lon_idx * self.step, 6),
        )


def get_grid() -> Grid | None:
    with pool().connection() as conn:
        row = conn.execute(
            "SELECT lat0, lon0, step, n_lat, n_lon FROM grid WHERE id = 1"
        ).fetchone()
    return Grid(*row) if row else None


# ----------------------------------------------------------------------------
# Cities
# ----------------------------------------------------------------------------
def _like_prefix(query: str) -> str:
    """Turn user input into a LIKE prefix pattern, escaping its wildcards.

    Without this, typing '_' or '%' silently becomes a wildcard: "H_lsinki"
    would match Helsinki, and "%" would match every city.
    """
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"{escaped}%"


def search_cities(query: str, limit: int = 10) -> list[dict]:
    """Cities findable by a name starting with `query`, most populous first.

    Matching is accent-insensitive and runs over every name form a city has --
    its localised name, its ASCII form and its other-language names -- so "aht"
    finds Ähtäri and "Loviisa" finds the town GeoNames calls Lovisa.

    city_norm() is applied to the query only; the stored side is normalised at
    write time by a generated column.
    """
    if len(query) < 2:
        return []

    with pool().connection() as conn:
        rows = conn.execute(
            """
            SELECT s.alias, s.canonical, s.country_code
            FROM (
                SELECT DISTINCT ON (c.geoname_id)
                       a.alias,
                       c.name         AS canonical,
                       c.country_code,
                       c.population
                FROM city_alias a
                JOIN cities c USING (geoname_id)
                WHERE a.alias_norm LIKE city_norm(%(pattern)s)
                -- Among the names of one city that match, prefer the one it is
                -- actually called, then its ASCII form, then the spelling the
                -- user literally typed, then the shortest. Without the third
                -- rule, searching "Åbo" answers "Abo": both are length 3 and
                -- plain A sorts before Å. Without the first two, abbreviations
                -- in the alternatenames column outrank the real name.
                ORDER BY c.geoname_id,
                         (a.alias_norm = city_norm(c.name)) DESC,
                         (a.alias_norm = city_norm(c.ascii_name)) DESC,
                         (a.alias ILIKE %(pattern)s) DESC,
                         length(a.alias), a.alias
            ) s
            ORDER BY s.population DESC NULLS LAST, s.alias
            LIMIT %(limit)s
            """,
            {"pattern": _like_prefix(query), "limit": limit},
        ).fetchall()

    return [
        # `canonical` is surfaced so the UI can disambiguate a bilingual place:
        # "Loviisa (Lovisa)". It is omitted when it adds nothing.
        {
            "name": r[0],
            "canonical": r[1] if r[1] != r[0] else None,
            "country_code": r[2],
        }
        for r in rows
    ]


@dataclass(frozen=True)
class City:
    name: str
    timezone: str
    lat_idx: int
    lon_idx: int


def lookup_city(city_name: str) -> City | None:
    """Resolve a name to a grid cell. Accent-insensitive, alias-aware.

    The returned name is what to title the page with: the city's own name when
    that is what was asked for (so "ahtari" comes back as "Ähtäri"), otherwise
    the matched alias (so "Loviisa" stays "Loviisa" rather than flipping to the
    Swedish "Lovisa").
    """
    with pool().connection() as conn:
        row = conn.execute(
            """
            SELECT CASE WHEN city_norm(c.name) = city_norm(%(q)s)
                        THEN c.name ELSE a.alias END,
                   c.timezone, c.lat_idx, c.lon_idx
            FROM city_alias a
            JOIN cities c USING (geoname_id)
            WHERE a.alias_norm = city_norm(%(q)s)
            ORDER BY c.population DESC NULLS LAST,
                     (a.alias_norm = city_norm(c.name)) DESC,
                     -- Same reason as in search_cities: keep the accented
                     -- spelling when that is what was asked for.
                     (lower(a.alias) = lower(%(q)s)) DESC,
                     length(a.alias), a.alias
            LIMIT 1
            """,
            {"q": city_name},
        ).fetchone()

    if row is None:
        return None
    return City(name=row[0], timezone=row[1] or "UTC", lat_idx=row[2], lon_idx=row[3])


# ----------------------------------------------------------------------------
# Forecast
# ----------------------------------------------------------------------------
@dataclass(frozen=True)
class Forecast:
    start_time: datetime  # UTC
    n_hours: int
    series: dict[str, list[float]]  # species -> hourly values


def get_forecast(lat_idx: int, lon_idx: int) -> Forecast | None:
    """All species' hourly series for one grid cell.

    Reads the run metadata and the rows in one transaction so a concurrent
    blue-green swap cannot yield a start_time from a different forecast than
    the values.
    """
    with pool().connection() as conn, conn.transaction():
        run = conn.execute(
            "SELECT start_time, n_hours FROM forecast_run WHERE id = 1"
        ).fetchone()
        if run is None:
            return None

        rows = conn.execute(
            "SELECT species, hourly FROM pollen_forecast WHERE lat_idx = %s AND lon_idx = %s",
            (lat_idx, lon_idx),
        ).fetchall()

    if not rows:
        return None
    return Forecast(start_time=run[0], n_hours=run[1], series={r[0]: r[1] for r in rows})


def forecast_age_hours() -> float | None:
    """Hours since the live forecast was loaded, or None if none has been."""
    with pool().connection() as conn:
        row = conn.execute(
            "SELECT extract(epoch FROM now() - loaded_at) / 3600 FROM forecast_run WHERE id = 1"
        ).fetchone()
    return float(row[0]) if row else None

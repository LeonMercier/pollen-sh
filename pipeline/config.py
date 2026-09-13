"""Static configuration and environment parsing for the pipeline."""

import os
from pathlib import Path

# ----------------------------------------------------------------------------
# Geographic area
# ----------------------------------------------------------------------------
# CAMS data covers all of Europe excluding the Canaries and Azores but including
# Iceland. We limit to a box covering Finland, Sweden and the Baltic countries.
#
# NOTE: these bounds are only used to *request* data from CDS. The authoritative
# grid geometry (origin and step) is read back out of the GRIB file itself and
# stored in the `grid` table, so nothing downstream hardcodes an assumption
# about where the CAMS grid points actually sit.
GRID_NORTH = 70.05  # northernmost point of Finland
GRID_SOUTH = 53.0  # southernmost point of Lithuania
GRID_WEST = 10.0  # westernmost point of Sweden
GRID_EAST = 31.4  # easternmost point of Finland


def cdsapi_area() -> list[float]:
    """Bounding box in the [north, west, south, east] order CDS expects."""
    return [GRID_NORTH, GRID_WEST, GRID_SOUTH, GRID_EAST]


# ----------------------------------------------------------------------------
# Forecast contents
# ----------------------------------------------------------------------------
DATASET = "cams-europe-air-quality-forecasts"

# CDS variable name -> canonical species key used everywhere downstream.
CDS_VARIABLES = {
    "alder_pollen": "Alnus",
    "birch_pollen": "Betula",
    "grass_pollen": "Poaceae",
    "mugwort_pollen": "Artemisia",
    "olive_pollen": "Olea",
    "ragweed_pollen": "Ambrosia",
}

# Canonical species keys, in a fixed order. The index into this list is the
# first axis of the forecast cube, so the order must stay stable within a run.
SPECIES = sorted(CDS_VARIABLES.values())

# GRIB reports the constituent type by name, except for olive which comes
# through as the bare numeric code 64002. Normalise it here so no magic number
# ever reaches the database.
GRIB_NAME_TO_SPECIES = {
    "Alnus": "Alnus",
    "Betula": "Betula",
    "Poaceae": "Poaceae",
    "Artemisia": "Artemisia",
    "Ambrosia": "Ambrosia",
    "Olea": "Olea",
    "64002": "Olea",
}

# Leadtime hours 0..96 inclusive: the forecast start plus four full days.
N_HOURS = 97
LEADTIME_HOURS = [str(h) for h in range(N_HOURS)]

# The full forecast is guaranteed available daily at 10:00 UTC. The request
# "time" is always 00:00 -- that is the forecast base time, not the run time.
FORECAST_BASE_TIME = "00:00"


# ----------------------------------------------------------------------------
# Environment
# ----------------------------------------------------------------------------
def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Required environment variable {name} is not set")
    return value


def database_url() -> str:
    """libpq connection URL, assembled from the compose environment."""
    host = os.environ.get("POSTGRES_HOST", "postgres").strip()
    port = os.environ.get("POSTGRES_PORT", "5432").strip()
    name = os.environ.get("POSTGRES_DB", "pollen").strip()
    user = os.environ.get("POSTGRES_USER", "pollen").strip()
    password = _require("POSTGRES_PASSWORD")
    return f"postgresql://{user}:{password}@{host}:{port}/{name}"


def cdsapi_credentials() -> tuple[str, str]:
    return _require("CDSAPI_URL"), _require("CDSAPI_KEY")


def bronze_dir() -> Path:
    """Where raw GRIB downloads are kept, for reprocessing without re-fetching."""
    path = Path(os.environ.get("BRONZE_DIR", "/data/bronze"))
    path.mkdir(parents=True, exist_ok=True)
    return path


# How many days of raw GRIB to keep in the bronze layer (~30 MB per day).
BRONZE_RETENTION_DAYS = int(os.environ.get("BRONZE_RETENTION_DAYS", "7"))

# Optional healthchecks.io (or compatible) ping URL, hit on a successful run.
HEALTHCHECK_URL = os.environ.get("HEALTHCHECK_URL", "").strip()

GEONAMES_URL = os.environ.get(
    "GEONAMES_URL", "https://download.geonames.org/export/dump/cities500.zip"
)

# European country codes kept from the GeoNames dump. Cities outside the
# forecast bounding box are dropped separately, so this is just a coarse
# pre-filter.
EUROPE_COUNTRIES = frozenset(
    """AD AL AT AX BA BE BG BY CH CY CZ DE DK EE ES FI FO FR GB GG GI GR HR HU
       IE IM IS IT JE LI LT LU LV MC MD ME MK MT NL NO PL PT RO RS SE SI SK SM
       UA VA XK""".split()
)

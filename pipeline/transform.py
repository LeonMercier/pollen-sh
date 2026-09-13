"""GRIB -> in-memory forecast cube.

The whole daily forecast is 6 species x 97 hours x 171 x 214 cells, which is
85 MB as float32. It fits in memory comfortably, so there is no batching, no
spill to a silver layer, and no Spark. Each GRIB message is one (species, hour)
slice of ~36k values; we read them straight into a preallocated array.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, time, timezone
from pathlib import Path

import numpy as np
import pygrib

from . import config
from .grid import Grid, grid_from_latlons

log = logging.getLogger(__name__)


class TransformError(RuntimeError):
    """Raised when the GRIB file does not match what the pipeline expects."""


@dataclass(frozen=True)
class Cube:
    """A complete forecast, ready to load."""

    grid: Grid
    start_time: datetime  # forecast t0, UTC
    species: list[str]  # canonical keys, indexing axis 0 of `data`
    data: np.ndarray  # (n_species, n_hours, n_lat, n_lon) float32

    @property
    def n_hours(self) -> int:
        return self.data.shape[1]


def _message_start_time(grb) -> datetime:
    """Forecast base time of a message, as an aware UTC datetime."""
    # grb.date is an int like 20260913; grb.hour is the base hour.
    day = datetime.strptime(str(grb.date), "%Y%m%d").date()
    return datetime.combine(day, time(grb.hour, 0), tzinfo=timezone.utc)


def transform(grib_path: Path) -> Cube:
    species_index = {name: i for i, name in enumerate(config.SPECIES)}
    n_species = len(config.SPECIES)
    n_hours = config.N_HOURS

    grid: Grid | None = None
    flip_lat = False
    data: np.ndarray | None = None
    start_time: datetime | None = None
    # Track which (species, hour) slices actually arrived, so a short or
    # duplicated file is caught rather than loaded with silent holes.
    seen = np.zeros((n_species, n_hours), dtype=bool)

    log.info("Reading %s (%.1f MB)", grib_path.name, grib_path.stat().st_size / 1024**2)

    with pygrib.open(str(grib_path)) as messages:
        for grb in messages:
            raw_name = str(grb.constituentTypeName).strip()
            species = config.GRIB_NAME_TO_SPECIES.get(raw_name)
            if species is None:
                raise TransformError(
                    f"Unknown GRIB constituent {raw_name!r}. Add it to "
                    "config.GRIB_NAME_TO_SPECIES if CAMS has changed its naming."
                )

            hour = int(grb.forecastTime)
            if not 0 <= hour < n_hours:
                raise TransformError(
                    f"Message has forecastTime {hour}, outside the expected 0..{n_hours - 1}"
                )

            if grid is None:
                lats, lons = grb.latlons()
                grid, flip_lat = grid_from_latlons(lats, lons)
                log.info(
                    "Grid: %d x %d cells, origin (%.2f, %.2f), step %.4f deg%s",
                    grid.n_lat,
                    grid.n_lon,
                    grid.lat0,
                    grid.lon0,
                    grid.step,
                    " (rows flipped to south-up)" if flip_lat else "",
                )
                data = np.zeros((n_species, n_hours, grid.n_lat, grid.n_lon), np.float32)
                start_time = _message_start_time(grb)
                log.info("Forecast start time: %s", start_time.isoformat())

            message_start = _message_start_time(grb)
            if message_start != start_time:
                raise TransformError(
                    f"Mixed forecast base times in one file: {start_time} and {message_start}"
                )

            s = species_index[species]
            if seen[s, hour]:
                raise TransformError(f"Duplicate message for {species} hour {hour}")

            # grb.values is a masked array over sea/missing points in some
            # products; fill rather than propagate a mask into the database.
            values = np.ma.filled(grb.values, 0.0).astype(np.float32, copy=False)
            if values.shape != grid.shape:
                raise TransformError(
                    f"{species} hour {hour} has shape {values.shape}, expected {grid.shape}"
                )

            data[s, hour] = values[::-1, :] if flip_lat else values
            seen[s, hour] = True

    if grid is None or data is None or start_time is None:
        raise TransformError(f"{grib_path.name} contains no GRIB messages")

    missing = int((~seen).sum())
    if missing:
        gaps = [
            f"{config.SPECIES[s]}@{h}" for s, h in zip(*np.where(~seen))  # noqa: B905
        ]
        raise TransformError(
            f"{missing} of {seen.size} (species, hour) slices are missing, "
            f"e.g. {', '.join(gaps[:5])}"
        )

    # Tiny negatives show up in the ensemble output; they are not physical.
    np.clip(data, 0.0, None, out=data)

    log.info(
        "Built cube: %s = %.0f MB, max value %.1f grains/m3",
        "x".join(str(d) for d in data.shape),
        data.nbytes / 1024**2,
        float(data.max()),
    )
    return Cube(grid=grid, start_time=start_time, species=list(config.SPECIES), data=data)

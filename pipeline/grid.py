"""Regular lat/lon grid geometry.

The CAMS European forecast is delivered on a regular 0.1-degree grid, so
snapping an arbitrary point to its nearest grid cell is arithmetic rather than a
spatial search:

    lat_idx = round((lat - lat0) / step)

Over a patch this small, great-circle distance is very nearly monotone in

    (dlat)^2 + (dlon * cos(lat))^2

which is separable, so rounding each axis independently minimises it. Measured
against a brute-force haversine search over all 5450 cities and 36594 cells,
this picks a different cell only for cities sitting within ~2 m of exactly
equidistant between two neighbours (10 of 5450), and the cell it picks is at
most 1.7 m farther than the true nearest -- against a mean city-to-cell
distance of 3.4 km and a cell size of ~11 km.

That is why there is no PostGIS in this project.

Grid geometry is never hardcoded: it is derived from the GRIB file at transform
time and persisted to the `grid` table, so a change on the CAMS side surfaces as
a loud failure rather than as silently mis-snapped cities.
"""

from dataclasses import dataclass

import numpy as np

# Grid axes must be uniform to within this many degrees to be accepted.
_TOLERANCE = 1e-6


class GridError(RuntimeError):
    """Raised when a GRIB message does not sit on a usable regular grid."""


@dataclass(frozen=True)
class Grid:
    """A regular lat/lon grid, indexed south-to-north and west-to-east."""

    lat0: float  # latitude of lat_idx == 0 (southernmost row)
    lon0: float  # longitude of lon_idx == 0 (westernmost column)
    step: float  # spacing in degrees, positive on both axes
    n_lat: int
    n_lon: int

    @property
    def shape(self) -> tuple[int, int]:
        return (self.n_lat, self.n_lon)

    @property
    def n_cells(self) -> int:
        return self.n_lat * self.n_lon

    def coords_of(self, lat_idx: int, lon_idx: int) -> tuple[float, float]:
        """Centre coordinates of a cell, rounded to kill float noise."""
        return (
            round(self.lat0 + lat_idx * self.step, 6),
            round(self.lon0 + lon_idx * self.step, 6),
        )

    def index_of(self, lat: float, lon: float) -> tuple[int, int] | None:
        """Nearest cell to a point, or None if it falls outside the grid."""
        lat_idx = int(round((lat - self.lat0) / self.step))
        lon_idx = int(round((lon - self.lon0) / self.step))
        if 0 <= lat_idx < self.n_lat and 0 <= lon_idx < self.n_lon:
            return lat_idx, lon_idx
        return None

    def index_arrays(
        self, lats: np.ndarray, lons: np.ndarray
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Vectorised `index_of` over arrays.

        Returns (lat_idx, lon_idx, inside) where `inside` is a boolean mask of
        the points that landed within the grid.
        """
        lat_idx = np.rint((lats - self.lat0) / self.step).astype(np.int32)
        lon_idx = np.rint((lons - self.lon0) / self.step).astype(np.int32)
        inside = (
            (lat_idx >= 0)
            & (lat_idx < self.n_lat)
            & (lon_idx >= 0)
            & (lon_idx < self.n_lon)
        )
        return lat_idx, lon_idx, inside


def _axis_step(axis: np.ndarray, name: str) -> float:
    """Validate that an axis is uniformly spaced and return its signed step."""
    if axis.size < 2:
        raise GridError(f"{name} axis has {axis.size} point(s), need at least 2")

    diffs = np.diff(axis)
    step = float(diffs[0])
    if step == 0:
        raise GridError(f"{name} axis has a zero step")
    if np.max(np.abs(diffs - step)) > _TOLERANCE:
        raise GridError(
            f"{name} axis is not uniformly spaced "
            f"(step {step}, max deviation {float(np.max(np.abs(diffs - step)))})"
        )
    return step


def grid_from_latlons(lats: np.ndarray, lons: np.ndarray) -> tuple[Grid, bool]:
    """Derive a `Grid` from the 2D lat/lon arrays of a GRIB message.

    GRIB commonly orders rows north-to-south. We normalise to south-to-north and
    report whether a flip is required, so the caller can orient value arrays the
    same way.

    Returns:
        (grid, flip_lat) -- apply ``values[::-1, :]`` when ``flip_lat`` is True.
    """
    if lats.ndim != 2 or lons.ndim != 2 or lats.shape != lons.shape:
        raise GridError(
            f"expected matching 2D lat/lon arrays, got {lats.shape} and {lons.shape}"
        )

    lat_axis = np.asarray(lats[:, 0], dtype=float)
    lon_axis = np.asarray(lons[0, :], dtype=float)

    # Longitudes are sometimes expressed on 0..360. Our area is entirely east of
    # Greenwich so a simple wrap is safe, but re-check monotonicity afterwards
    # in case the box straddles the seam.
    if np.any(lon_axis > 180.0):
        lon_axis = np.where(lon_axis > 180.0, lon_axis - 360.0, lon_axis)

    # Rows must be constant in latitude and columns constant in longitude,
    # otherwise this is a rotated or irregular grid and the arithmetic above
    # does not hold.
    if np.max(np.abs(lats - lats[:, :1])) > _TOLERANCE:
        raise GridError("latitude varies along a row; grid is not a regular lat/lon grid")
    if np.max(np.abs(lons - lons[:1, :])) > _TOLERANCE:
        raise GridError("longitude varies down a column; grid is not a regular lat/lon grid")

    lat_step = _axis_step(lat_axis, "latitude")
    lon_step = _axis_step(lon_axis, "longitude")

    if lon_step < 0:
        raise GridError("longitude axis runs east to west, which is not supported")
    if abs(abs(lat_step) - lon_step) > _TOLERANCE:
        raise GridError(
            f"latitude step {abs(lat_step)} differs from longitude step {lon_step}; "
            "a non-square grid is not supported"
        )

    flip_lat = lat_step < 0
    step = lon_step

    grid = Grid(
        lat0=round(float(lat_axis[-1] if flip_lat else lat_axis[0]), 6),
        lon0=round(float(lon_axis[0]), 6),
        step=round(step, 6),
        n_lat=int(lat_axis.size),
        n_lon=int(lon_axis.size),
    )
    return grid, flip_lat

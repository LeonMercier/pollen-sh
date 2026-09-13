"""Build one Plotly figure per pollen species for a location.

Figures are constructed from the hourly arrays returned by db.get_forecast; no
pandas is involved.

Time handling: x values are emitted as *naive local wall-clock* ISO strings
("2026-09-13T14:30:00"). Plotly.js ignores UTC offsets in date strings and plots
whatever wall time it is given, and the frontend's "Now" marker is built with
`toLocaleString("sv-SE", {timeZone})`, which is the same wall-clock form. Naive
local strings are the only representation that lines those two up unambiguously.
"""

import json
import math
from datetime import timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import plotly.graph_objects as go

from db import Forecast

CONSTITUENT_DISPLAY_NAMES = {
    "Alnus": "Alder / Leppä",
    "Betula": "Birch / Koivu",
    "Poaceae": "Grasses / Heinät",
    "Ambrosia": "Ragweed / Tuoksukki",
    "Artemisia": "Mugwort / Pujo",
    "Olea": "Olive / Oliivi",
}

# Per-species severity thresholds in grains/m3, as [low, medium, high]; above
# 'high' is Very High. Approximate -- TODO: calibrate against more sources.
# https://climate-adapt.eea.europa.eu/en/observatory/publications-data/analysis-data/cams-ground-level-pollen-forecast
SEVERITY_THRESHOLDS = {
    "Alnus": [10, 100, 200],
    "Betula": [10, 100, 200],
    "Poaceae": [3, 50, 100],
    "Ambrosia": [3, 50, 100],
    "Artemisia": [10, 100, 200],
    "Olea": [10, 100, 200],
}
DEFAULT_THRESHOLDS = [10, 100, 200]

# Semi-transparent bands drawn behind the trace: low, medium, high, very high.
_BAND_COLORS = [
    "rgba(0,   180,  0,   0.10)",
    "rgba(255, 210,  0,   0.15)",
    "rgba(255, 130,  0,   0.18)",
    "rgba(220,  30,  30,  0.20)",
]

# Zero is unplottable on a log axis, so the floor is 1 grain/m3. The frontend
# relies on this too: it treats "> 1" as activity.
_VALUE_FLOOR = 1.0


def _zone(timezone_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def _utc_offset_label(zone: ZoneInfo, at) -> str:
    """"UTC+3" style label. Sub-hour offsets are rounded to the hour."""
    offset = at.astimezone(zone).utcoffset()
    if offset is None:
        return "unknown"
    hours = int(offset.total_seconds() // 3600)
    return f"UTC+{hours}" if hours >= 0 else f"UTC{hours}"


def _local_timestamps(forecast: Forecast, zone: ZoneInfo) -> list[str]:
    """Naive local wall-clock ISO strings, one per forecast hour."""
    return [
        (forecast.start_time + timedelta(hours=h))
        .astimezone(zone)
        .strftime("%Y-%m-%dT%H:%M:%S")
        for h in range(forecast.n_hours)
    ]


def _severity_shapes(species: str) -> list[dict]:
    low, medium, high = SEVERITY_THRESHOLDS.get(species, DEFAULT_THRESHOLDS)
    bands = [
        (0.1, low, _BAND_COLORS[0]),
        (low, medium, _BAND_COLORS[1]),
        (medium, high, _BAND_COLORS[2]),
        # Very high stretches past the top; Plotly clips it to the axis range.
        (high, 1e9, _BAND_COLORS[3]),
    ]
    return [
        {
            "type": "rect",
            "xref": "paper",
            "x0": 0,
            "x1": 1,
            "yref": "y",  # raw data coordinates; Plotly handles the log scaling
            "y0": y0,
            "y1": y1,
            "fillcolor": color,
            "line": {"width": 0},
            "layer": "below",
        }
        for y0, y1, color in bands
    ]


def _log_y_max(species: str, values: list[float]) -> float:
    """Upper bound of the log axis: a per-species floor, raised to fit the data."""
    thresholds = SEVERITY_THRESHOLDS.get(species, DEFAULT_THRESHOLDS)
    # Arbitrarily the highest threshold times five, rounded up to a decade so
    # that species with the same thresholds share an axis.
    floor = math.ceil(math.log10(thresholds[-1] * 5))
    peak = max(values, default=0.0)
    return max(floor, math.ceil(math.log10(peak)) if peak > 1 else floor)


def build_figures(forecast: Forecast, timezone_name: str, city_name: str) -> dict:
    """One figure per species, keyed by display name, ordered by activity."""
    zone = _zone(timezone_name)
    timestamps = _local_timestamps(forecast, zone)
    x_label = f"{city_name} Local Time ({_utc_offset_label(zone, forecast.start_time)})"

    figures: dict[str, dict] = {}
    activity: dict[str, bool] = {}

    for species, raw in sorted(forecast.series.items()):
        display_name = CONSTITUENT_DISPLAY_NAMES.get(species, species)
        # Snap zeroes and sub-floor values up so the log axis can show them.
        values = [max(float(v), _VALUE_FLOOR) for v in raw[: forecast.n_hours]]

        figure = go.Figure(
            data=[
                go.Scatter(
                    x=timestamps,
                    y=values,
                    mode="lines",
                    line={"shape": "spline"},
                    name=display_name,
                    hovertemplate="%{x|%a %H:%M}<br>%{y:.1f} grains/m³<extra></extra>",
                )
            ],
            layout=go.Layout(
                title={"text": display_name},
                xaxis={"title": {"text": x_label}},
                yaxis={
                    "title": {"text": "Pollen grains / m³"},
                    "type": "log",
                    "range": [0, _log_y_max(species, values)],
                },
                shapes=_severity_shapes(species),
                margin={"t": 50, "r": 20, "b": 50, "l": 60},
            ),
        )

        # Round-trip through Plotly's own encoder so the payload is exactly what
        # Plotly.js expects.
        payload = json.loads(figure.to_json())
        figures[display_name] = {
            "data": payload["data"],
            "layout": payload["layout"],
            "config": {"responsive": True},
        }
        # "Active" means a sustained signal, not a single spike.
        activity[display_name] = sum(v > _VALUE_FLOOR for v in values) >= 10

    # Active species first, then alphabetical.
    return dict(
        sorted(figures.items(), key=lambda kv: (0 if activity[kv[0]] else 1, kv[0]))
    )

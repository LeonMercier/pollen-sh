"""Pollen forecast API.

Caddy serves the static frontend and reverse-proxies /api and /health to this
app, so the two are same-origin and there is no CORS configuration.
"""

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse

import config
import db
from plot import build_figures

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # The pool opens lazily: the API must start even when the database is
    # briefly unavailable, and report that through /health instead.
    db.open_pool()
    log.info("Connection pool created")
    yield
    db.close_pool()


app = FastAPI(
    title="Pollen forecast API",
    description="Hourly pollen forecasts for European cities, from CAMS data",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/api/cities")
async def api_cities(q: str = Query(min_length=2, max_length=100)):
    """City autocomplete: up to 10 suggestions for a name prefix."""
    try:
        return db.search_cities(q)
    except Exception:
        log.exception("City search failed for %r", q)
        return []


@app.get("/api/plots")
async def api_plots(city: str | None = Query(default=None, max_length=100)):
    """One Plotly figure per pollen species for the named city."""
    if not city:
        return {"success": False, "error": "City parameter is required"}

    try:
        match = db.lookup_city(city)
        if match is None:
            return {"success": False, "error": f"City '{city}' not found"}

        forecast = db.get_forecast(match.lat_idx, match.lon_idx)
        if forecast is None:
            return {"success": False, "error": "No forecast data is loaded yet"}

        grid = db.get_grid()
        lat, lon = (
            grid.coords_of(match.lat_idx, match.lon_idx) if grid else (None, None)
        )

        return {
            "success": True,
            "city": match.name,
            "lat": lat,
            "lon": lon,
            "timezone": match.timezone,
            "plots": build_figures(forecast, match.timezone, match.name),
        }
    except Exception:
        log.exception("Failed to build plots for %r", city)
        return {"success": False, "error": "Error generating plots"}


@app.get("/health")
async def health():
    """Liveness plus data freshness.

    A stale forecast is the realistic failure here -- the API can be perfectly
    healthy while the daily pipeline has been dead for a week.
    """
    try:
        age = db.forecast_age_hours()
    except Exception as exc:
        log.warning("Health check could not reach the database: %s", exc)
        return JSONResponse(
            status_code=503, content={"status": "unhealthy", "reason": "database unreachable"}
        )

    if age is None:
        return JSONResponse(
            status_code=503, content={"status": "unhealthy", "reason": "no forecast loaded"}
        )

    stale = age > config.MAX_FORECAST_AGE_HOURS
    body = {
        "status": "degraded" if stale else "healthy",
        "forecast_age_hours": round(age, 1),
        "max_age_hours": config.MAX_FORECAST_AGE_HOURS,
    }
    return JSONResponse(status_code=503 if stale else 200, content=body)

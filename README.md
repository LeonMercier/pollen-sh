# Pollen forecast

Hourly pollen forecasts for cities in Finland, Sweden and the Baltics, live at
[pollencast.eu](https://www.pollencast.eu/). Every morning a pipeline downloads the
[Copernicus CAMS](https://atmosphere.copernicus.eu/) European air quality forecast — six
pollen species, 97 hourly steps, on a 0.1° grid — flattens it into PostgreSQL, and a
FastAPI backend serves per-city Plotly charts to a static frontend. The whole thing runs
as four containers on a single VPS.

## Running it

You need Podman or Docker with Compose, [go-task](https://taskfile.dev/), and a
[CDS API key](https://ads.atmosphere.copernicus.eu/how-to-api).

```bash
cp .env.example .env
$EDITOR .env          # set POSTGRES_PASSWORD, CDSAPI_URL, CDSAPI_KEY

task up               # build and start; site on http://localhost:8080
task pipeline         # download and load today's forecast (~2 min)
task geocode          # load the city list (run once, after the first pipeline run)
```

`task smoke` checks it end to end. `task --list` shows everything else.

Use `COMPOSE="docker compose" task up` for Docker instead of Podman.

## Layout

```
api/       FastAPI app
pipeline/  daily ETL: extract, transform, load, plus geocoding
web/       static frontend
db/init/   schema
deploy/    Caddyfile, systemd timer, smoke test
```

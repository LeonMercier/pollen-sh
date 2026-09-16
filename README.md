# Pollen forecast

Daily pollen forecasts for Northern Europe, from
[EU Copernicus CAMS](https://atmosphere.copernicus.eu/) data. Live at
[pollencast.eu](http://www.pollencast.eu/).

A pipeline downloads the CAMS European air-quality forecast every morning,
flattens it into PostgreSQL, and a FastAPI backend renders per-city Plotly
charts for a static frontend. Everything runs in containers on a single VPS.

## Stack

| Layer | Choice |
| --- | --- |
| Reverse proxy, TLS, static files | Caddy |
| API | FastAPI + uvicorn |
| Database | PostgreSQL 17 |
| Pipeline | Python (`cdsapi`, `pygrib`, `numpy`, `psycopg`) |
| Scheduling | systemd timer |
| Containers | Podman / Docker via Compose |

## How it works

```
CDS API ──► pipeline ──► PostgreSQL ──► api ──► Caddy ──► browser
            (daily)                                  ▲
                                             web/ (static)
```

**Bronze → gold, no silver.** The daily forecast is 6 species × 97 hourly
leadtimes over a 171 × 214 grid: about 85 MB as float32. That fits in memory, so
the pipeline reads the raw GRIB straight into a numpy array and loads it in one
pass — no intermediate parquet, no batching, no Spark. Raw GRIB files are kept
for seven days so a run can be reprocessed without hitting a rate-limited API.

**The gold table is shaped for the query it serves.** The API only ever asks for
every hourly value at one grid cell, so `pollen_forecast` stores the whole
97-hour series for a (cell, species) pair in one `real[]` row. That is ~220k rows
and ~110 MB per run, against ~21.3M rows and ~2.6 GB for the same data in long
format, and it turns the daily load into a ten-second operation.

**Cities snap to grid cells by arithmetic.** On a regular 0.1° grid the nearest
cell to a point is `round((lat - lat0) / step)` — locally, great-circle distance
is near-monotone in a separable quantity, so rounding each axis independently
minimises it. Checked against a brute-force haversine search over every city and
every cell, this differs only for cities within ~2 m of exactly equidistant
between two neighbours, by at most 1.7 m — against a mean city-to-cell distance
of 3.4 km. Hence no PostGIS.

**Loads are blue-green.** New data is staged, indexed and validated in a side
table, then promoted in a single transaction along with its run metadata. A
failed run changes nothing and yesterday's forecast stays live.

**Grid geometry is discovered, not hardcoded.** It is read out of the GRIB file
each run and written to the `grid` table, so a change on the CAMS side fails
loudly instead of silently mis-snapping every city.

## Running it locally

Requires Podman (or Docker) with Compose, and a
[CDS API key](https://ads.atmosphere.copernicus.eu/how-to-api).

```bash
cp .env.example .env
$EDITOR .env          # set POSTGRES_PASSWORD, CDSAPI_URL, CDSAPI_KEY
```

Start the stack:

```bash
podman-compose up -d --build
```

The site is on <http://localhost:8080>. `/health` reports 503 until a forecast
has been loaded.

Load data — the forecast first, then the cities that snap to its grid:

```bash
podman-compose --profile manual run --rm --no-deps pipeline
podman-compose --profile manual run --rm --no-deps --entrypoint python pipeline -m pipeline.geocode
```

The first command downloads roughly 30 MB from CDS. Requests are queued
server-side and can take several minutes. Re-runs on the same day reuse the
cached GRIB; pass `--force` to download again.

Check it:

```bash
bash deploy/smoke_test.sh
```

With [go-task](https://taskfile.dev/) installed, the same steps are `task up`,
`task pipeline`, `task geocode`, `task smoke`. `task --list` shows the rest.
Set `COMPOSE="docker compose"` to use Docker instead of Podman.

### Useful commands

```bash
task psql                 # psql shell on the database
task logs                 # follow all container logs
task pipeline:reprocess   # rebuild from the cached GRIB, no CDS call
task health               # query /health
task clean                # delete all volumes (destroys the database)
```

### Two podman-compose quirks

- **Always pass `--no-deps` to `run`.** Without it, podman-compose restarts the
  dependency graph and takes `api` and `caddy` down with it — the site would go
  offline every time the pipeline ran. The Taskfile and the systemd unit both
  pass it.
- **Rebuild with a full `down` then `up`.** `podman-compose up -d --build api`
  reports success but silently keeps the old container running, because podman
  refuses to replace a container that `caddy` depends on. Use
  `podman-compose down && podman-compose up -d --build` (`task down && task up`).

Neither applies to Docker Compose.

## Deploying to a VPS

1. Install Podman and Compose, clone the repo to `/opt/pollen`.
2. Write `.env`:

   ```bash
   SITE_ADDRESS=pollencast.eu, www.pollencast.eu
   APEX_HOST=pollencast.eu
   WWW_HOST=www.pollencast.eu
   HTTP_PORT=80
   HTTPS_PORT=443
   ```

   Caddy obtains and renews a certificate for each name in `SITE_ADDRESS` on
   its own — there is no certbot to configure. `APEX_HOST`/`WWW_HOST` drive a
   301 from the bare apex to the www host, preserving the request scheme so an
   HTTPS visitor is never bounced to plain HTTP.
3. Point **both** names at the VPS with A records, and make sure port 80 is
   reachable — Caddy needs it for the ACME HTTP-01 challenge. See
   [DNS cutover](#dns-cutover).
4. `podman-compose up -d --build`
5. Run the pipeline once, then geocode (as above).
6. Install the timer:

   ```bash
   sudo cp deploy/systemd/pollen-pipeline.* /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now pollen-pipeline.timer
   ```

   It fires at 10:20 UTC daily — CAMS guarantees the full forecast at 10:00.
   `journalctl -u pollen-pipeline -f` shows a run.

Deploying a change is `git pull && podman-compose up -d --build`. The frontend is
bind-mounted, so `web/` changes need only a `git pull`.

Sizing: 2 GB RAM and 20 GB disk is comfortable. Pipeline peak RSS is under
200 MB.

### SELinux (Fedora, RHEL, Rocky)

On an enforcing SELinux system, bind-mounted host paths are not readable by the
container unless they carry a container label. Symptom:

```
SELinux is preventing ls from read access on the directory init
scontext=system_u:system_r:container_t:s0:c305,c600
tcontext=system_u:object_r:svirt_image_t:s0
```

Every host bind mount in `compose.yaml` therefore carries `,z`, which asks
podman to relabel the source as `container_file_t` at mount time. Nothing extra
to run — but two things are worth knowing:

- **`z` relabels the path on the host**, persistently. Running
  `restorecon -R .` over the repo reverts the labels and the denials come back.
  Re-running `podman-compose up` relabels them again.
- **Lowercase `z`, not uppercase `Z`.** `Z` stamps a private MCS category that
  locks the path to a single container, which breaks the moment a second
  container needs the same path.

To confirm SELinux is the cause of some other denial, temporarily add
`security_opt: ["label=disable"]` to the affected service. That is a diagnostic,
not a fix — remove it once the real labelling issue is found.

### DNS cutover

The zone starts out with the apex on the registrar's URL-forwarding service and
`www` pointing at Azure blob storage:

```
pollencast.eu       A     65.108.94.145 (TTL 14400)  → NordName forwarder → 301 → http://www
www.pollencast.eu   CNAME stwebpollenprod.z1.web.core.windows.net. (TTL 3600)
```

Both move onto the VPS. Lower the TTLs first and wait out the **old** values —
publishing a shorter TTL does nothing for resolvers that already cached the long
one:

```
T+0h     lower www 3600 → 300 and apex 14400 → 300 (change TTLs only)
T+4h     apex A    → <VPS IP>   (this retires the registrar forwarder)
         www CNAME → A record, <VPS IP>
T+4h05   Caddy completes ACME for both names; verify, then tear down Azure
T+2d     raise TTLs back to 3600 / 14400
```

Four hours, not one, because the apex A carries the longest old TTL. Expect a
thin tail of stragglers beyond that from resolvers that clamp TTLs, so leave the
Azure endpoint alive until it dies down.

**Repoint `www` before deleting the Azure storage account.** A CNAME left
pointing at `stwebpollenprod.z1.web.core.windows.net` after the account is gone
is a subdomain-takeover vector: whoever claims that storage account name then
serves content on `www.pollencast.eu`.

### Monitoring

Set `HEALTHCHECK_URL` in `.env` to a [healthchecks.io](https://healthchecks.io)
ping URL. The pipeline signals start, success and failure, so a run that never
happens raises an alert — the realistic failure mode for a daily job.

`/health` reports the age of the live forecast and returns 503 once it exceeds
`MAX_FORECAST_AGE_HOURS` (default 36).

### Backups

Deliberately minimal. `pollen_forecast` is fully regenerated every day and there
is no user data. `cities` is reproducible from GeoNames with one command. If you
want to skip that step after a disaster, dump the one table:

```bash
podman-compose exec postgres pg_dump -U pollen -t cities pollen > cities.sql
```

## Repository layout

```
api/            FastAPI app (config, db, plot, main)
pipeline/       ETL: extract → transform → load, plus geocode
db/init/        Schema, applied by postgres on first start and by every run
web/            Static frontend, served directly by Caddy
deploy/         Caddyfile, systemd units, smoke test
compose.yaml    All four services
```

## Data sources

- Pollen forecast: [CAMS European air quality forecasts](https://ads.atmosphere.copernicus.eu/datasets/cams-europe-air-quality-forecasts)
  — 0.1° grid, six species, 96-hour horizon, updated daily.
- Cities: [GeoNames](https://www.geonames.org) `cities500`.

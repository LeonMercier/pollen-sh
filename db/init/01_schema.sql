-- Pollen forecast schema.
--
-- Run once by the postgres image on first start of an empty data volume. The
-- pipeline also applies this file itself on every run (it is idempotent), so a
-- schema change ships with the code rather than needing a volume reset.
--
-- Storage shape: the API only ever asks one question -- "give me every hourly
-- value for one grid cell" -- so the gold table keeps the whole 97-hour series
-- for a (cell, species) pair in a single row. That is ~220k rows and ~110 MB
-- per run, against ~21.3M rows and ~2.6 GB for the same data in long format.

-- Grid geometry, read out of the GRIB file rather than hardcoded.
-- Single row, enforced by the CHECK on the primary key.
CREATE TABLE IF NOT EXISTS grid (
    id     smallint PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    lat0   double precision NOT NULL,  -- latitude of lat_idx = 0 (southernmost)
    lon0   double precision NOT NULL,  -- longitude of lon_idx = 0 (westernmost)
    step   double precision NOT NULL,  -- degrees, positive on both axes
    n_lat  smallint NOT NULL,
    n_lon  smallint NOT NULL
);

-- Metadata for the forecast currently live in pollen_forecast. Updated in the
-- same transaction as the table swap, so the API can never read a start_time
-- that disagrees with the data it indexes into.
CREATE TABLE IF NOT EXISTS forecast_run (
    id          smallint PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    start_time  timestamptz NOT NULL,  -- forecast t0; hourly[1] is this hour
    n_hours     smallint    NOT NULL,  -- length of the hourly array
    loaded_at   timestamptz NOT NULL DEFAULT now()
);

-- Gold layer. `hourly` holds n_hours values; element 1 (PostgreSQL arrays are
-- 1-based) corresponds to forecast_run.start_time.
CREATE TABLE IF NOT EXISTS pollen_forecast (
    species  text     NOT NULL,
    lat_idx  smallint NOT NULL,
    lon_idx  smallint NOT NULL,
    hourly   real[]   NOT NULL,
    PRIMARY KEY (species, lat_idx, lon_idx)
);

-- Cities, snapped to grid indices rather than to coordinates: the join to
-- pollen_forecast is then a plain integer match.
CREATE TABLE IF NOT EXISTS cities (
    geoname_id   integer PRIMARY KEY,
    name         text     NOT NULL,
    ascii_name   text     NOT NULL,
    country_code char(2)  NOT NULL,
    admin1_code  text,
    population   bigint,
    timezone     text,
    latitude     double precision NOT NULL,
    longitude    double precision NOT NULL,
    lat_idx      smallint NOT NULL,
    lon_idx      smallint NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now()
);

-- Autocomplete is a case-insensitive prefix match on ascii_name. text_pattern_ops
-- is what makes LIKE 'foo%' index-usable.
CREATE INDEX IF NOT EXISTS idx_cities_ascii_prefix
    ON cities (lower(ascii_name) text_pattern_ops);
-- Exact lookup accepts either the localised or the ASCII name.
CREATE INDEX IF NOT EXISTS idx_cities_lower_name  ON cities (lower(name));
CREATE INDEX IF NOT EXISTS idx_cities_lower_ascii ON cities (lower(ascii_name));

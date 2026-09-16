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

-- Accent-insensitive name matching.
--
-- unaccent() is only STABLE by default (it depends on which dictionary is
-- current), which makes it unusable in an index. Naming the dictionary
-- explicitly removes that dependency, so the wrapper below can honestly be
-- declared IMMUTABLE and indexed.
--
-- Keeping normalisation in the database rather than in Python means the ETL
-- that writes names and the API that queries them cannot drift apart: there is
-- exactly one definition of what "the same name" means.
CREATE EXTENSION IF NOT EXISTS unaccent;

CREATE OR REPLACE FUNCTION city_norm(t text) RETURNS text
    LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
    AS $$ SELECT lower(unaccent('unaccent', t)) $$;

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

-- Every name a city can be found by: its localised name, its ASCII form, and
-- the Latin-script entries from the GeoNames alternatenames column. Bilingual
-- places need this -- Loviisa and Lovisa are the same town, and GeoNames stores
-- only one of them in `name`.
CREATE TABLE IF NOT EXISTS city_alias (
    geoname_id integer NOT NULL REFERENCES cities (geoname_id) ON DELETE CASCADE,
    alias      text    NOT NULL,
    -- Normalised once at write time rather than per row at read time. This is
    -- only possible because city_norm() is genuinely IMMUTABLE.
    alias_norm text GENERATED ALWAYS AS (city_norm(alias)) STORED,
    PRIMARY KEY (geoname_id, alias)
);

-- Superseded by city_alias: autocomplete used to prefix-match ascii_name
-- directly, which found neither accented spellings nor other-language names.
-- Dropped here rather than only omitted, so databases created before the
-- change do not keep carrying three indexes nothing reads.
DROP INDEX IF EXISTS idx_cities_ascii_prefix;
DROP INDEX IF EXISTS idx_cities_lower_name;
DROP INDEX IF EXISTS idx_cities_lower_ascii;

-- Autocomplete is an accent-insensitive prefix match. text_pattern_ops is what
-- makes LIKE 'foo%' index-usable.
CREATE INDEX IF NOT EXISTS idx_city_alias_prefix
    ON city_alias (alias_norm text_pattern_ops);

-- Acurite TimescaleDB schema (empty database).
-- Prerequisites on the DB host:
--   CREATE DATABASE acurite;
--   \c acurite
--   CREATE EXTENSION IF NOT EXISTS timescaledb;
--   CREATE ROLE acurite LOGIN PASSWORD 'choose_a_password';
--
-- Load:
--   psql -d acurite -f sql/schema.sql

-- ---------------------------------------------------------------------------
-- Raw readings (parse_acurite_log.py -> load_readings.py)
-- ---------------------------------------------------------------------------

CREATE TABLE readings (
    reading_time timestamptz NOT NULL,
    location text NOT NULL,
    model text NOT NULL,
    sensor_id integer NOT NULL,
    channel text,
    battery_ok smallint,
    humidity smallint,
    rain_in double precision,
    temperature_c double precision,
    temperature_f double precision,
    wind_avg_km_h double precision,
    wind_avg_mph double precision,
    wind_dir text,
    wind_dir_deg double precision,
    storm_listener text,
    storm_strike_count integer,
    storm_dist_index smallint,
    storm_distance_mi double precision,
    storm_detect_rfi smallint,
    signal_freq double precision,
    signal_noise double precision,
    signal_rssi double precision,
    signal_snr double precision,
    signal_quality text,
    CONSTRAINT readings_location_len_chk CHECK (char_length(location) <= 20),
    CONSTRAINT readings_battery_ok_chk CHECK (battery_ok IS NULL OR battery_ok IN (0, 1)),
    CONSTRAINT readings_storm_listener_chk CHECK (
        storm_listener IS NULL OR storm_listener IN ('active', 'standby')
    ),
    CONSTRAINT readings_signal_quality_chk CHECK (
        signal_quality IS NULL OR signal_quality IN ('STRONG', 'WEAK')
    ),
    CONSTRAINT readings_storm_dist_index_chk CHECK (
        storm_dist_index IS NULL OR storm_dist_index BETWEEN 0 AND 31
    )
);

SELECT create_hypertable('readings', 'reading_time');

CREATE INDEX readings_location_time_idx
    ON readings (location, reading_time DESC);

CREATE INDEX readings_model_sensor_time_idx
    ON readings (model, sensor_id, reading_time DESC);

COMMENT ON TABLE readings IS
    'Normalized Acurite rtl_433 readings; one row per deduplicated JSONL record from parse_acurite_log.py.';

ALTER TABLE readings SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'location, sensor_id, model',
    timescaledb.compress_orderby = 'reading_time DESC'
);

SELECT add_compression_policy('readings', compress_after => INTERVAL '7 days');

-- ---------------------------------------------------------------------------
-- Lightning (Acurite-6045M): strike counter deltas + hourly rollups
-- ---------------------------------------------------------------------------

CREATE TABLE lightning_sensor_state (
    sensor_id integer PRIMARY KEY,
    location text NOT NULL,
    last_strike_count integer NOT NULL,
    last_reading_time timestamptz NOT NULL,
    CONSTRAINT lightning_sensor_state_count_chk CHECK (
        last_strike_count >= 0 AND last_strike_count < 256
    )
);

CREATE TABLE lightning_strike_events (
    event_time timestamptz NOT NULL,
    location text NOT NULL,
    sensor_id integer NOT NULL,
    strike_delta integer NOT NULL,
    storm_distance_mi double precision,
    storm_dist_index smallint,
    CONSTRAINT lightning_strike_events_delta_chk CHECK (strike_delta > 0),
    CONSTRAINT lightning_strike_events_dist_index_chk CHECK (
        storm_dist_index IS NULL OR storm_dist_index BETWEEN 0 AND 31
    )
);

SELECT create_hypertable('lightning_strike_events', 'event_time');

CREATE INDEX lightning_strike_events_location_time_idx
    ON lightning_strike_events (location, event_time DESC);

CREATE INDEX lightning_strike_events_sensor_time_idx
    ON lightning_strike_events (sensor_id, event_time DESC);

CREATE MATERIALIZED VIEW lightning_hourly
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('1 hour', event_time) AS bucket,
    location,
    sensor_id,
    sum(strike_delta)::bigint AS strikes,
    min(storm_distance_mi) AS dist_mi_min,
    max(storm_distance_mi) AS dist_mi_max,
    avg(storm_distance_mi) AS dist_mi_avg,
    count(*)::bigint AS observations
FROM lightning_strike_events
GROUP BY bucket, location, sensor_id
WITH NO DATA;

SELECT add_continuous_aggregate_policy(
    'lightning_hourly',
    start_offset => INTERVAL '3 hours',
    end_offset => INTERVAL '1 hour',
    schedule_interval => INTERVAL '15 minutes'
);

-- ---------------------------------------------------------------------------
-- Rain (Acurite-5-in-1): cumulative rain_in deltas + hourly rollups
-- ---------------------------------------------------------------------------

CREATE TABLE rain_sensor_state (
    sensor_id integer PRIMARY KEY,
    location text NOT NULL,
    last_rain_in double precision NOT NULL,
    last_reading_time timestamptz NOT NULL,
    CONSTRAINT rain_sensor_state_rain_chk CHECK (last_rain_in >= 0)
);

CREATE TABLE rain_increment_events (
    event_time timestamptz NOT NULL,
    location text NOT NULL,
    sensor_id integer NOT NULL,
    rain_delta_in double precision NOT NULL,
    CONSTRAINT rain_increment_events_delta_chk CHECK (rain_delta_in > 0)
);

SELECT create_hypertable('rain_increment_events', 'event_time');

CREATE INDEX rain_increment_events_location_time_idx
    ON rain_increment_events (location, event_time DESC);

CREATE INDEX rain_increment_events_sensor_time_idx
    ON rain_increment_events (sensor_id, event_time DESC);

CREATE MATERIALIZED VIEW rain_hourly
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('1 hour', event_time) AS bucket,
    location,
    sensor_id,
    sum(rain_delta_in) AS rain_in_per_hour,
    count(*)::bigint AS observations
FROM rain_increment_events
GROUP BY bucket, location, sensor_id
WITH NO DATA;

SELECT add_continuous_aggregate_policy(
    'rain_hourly',
    start_offset => INTERVAL '3 hours',
    end_offset => INTERVAL '1 hour',
    schedule_interval => INTERVAL '15 minutes'
);

-- ---------------------------------------------------------------------------
-- Loader + Grafana role
-- ---------------------------------------------------------------------------

GRANT SELECT, INSERT ON readings TO acurite;
GRANT SELECT, INSERT, UPDATE ON lightning_sensor_state TO acurite;
GRANT SELECT, INSERT ON lightning_strike_events TO acurite;
GRANT SELECT ON lightning_hourly TO acurite;
GRANT SELECT, INSERT, UPDATE ON rain_sensor_state TO acurite;
GRANT SELECT, INSERT ON rain_increment_events TO acurite;
GRANT SELECT ON rain_hourly TO acurite;

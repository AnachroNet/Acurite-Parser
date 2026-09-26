# Acurite → TimescaleDB ingest

**Repository:** [github.com/AnachroNet/Acurite-Parser](https://github.com/AnachroNet/Acurite-Parser)

Small toolchain that receives **Acurite** weather data from [rtl_433](https://github.com/merbanan/rtl_433) over RTL-SDR, normalizes it to a single JSON schema, maps each sensor to a site label, and inserts rows into a **TimescaleDB** hypertable on PostgreSQL. It is aimed at a Raspberry Pi (`raspberrypi.example.com`) talking to a central database host (`postgresql.example.com`), with a **systemd** unit keeping the pipeline running.

**Components:** `parse_acurite_log.py` (dedupe + derived fields + location lookup), `load_readings.py` (Postgres insert), `device_locations.csv`, `rtl_433.conf`, and `scripts/run-acurite-pipeline.sh`. Site-specific values are documented in [CONFIGURATION.txt](CONFIGURATION.txt).

## Database schema

All objects are created in one script, [`sql/schema.sql`](sql/schema.sql), for a fresh **`acurite`** database with TimescaleDB enabled. Sensor samples live in a wide **`readings`** hypertable partitioned on **`reading_time`**.

| Column | Type | Notes |
|--------|------|--------|
| `reading_time` | `timestamptz` | Partition key; from rtl_433 JSON `time` |
| `location` | `text` | Site label (≤ 20 chars); from `device_locations.csv` |
| `model` | `text` | e.g. `Acurite-Tower`, `Acurite-5n1`, `Acurite-6045M` |
| `sensor_id` | `integer` | rtl_433 device `id` |
| `channel` | `text` | Often `A` |
| `battery_ok` | `smallint` | 0 / 1 |
| `humidity` | `smallint` | |
| `rain_in` | `double precision` | Cumulative inches (5-in-1); use **`rain_hourly`** for in/hr |
| `temperature_c`, `temperature_f` | `double precision` | |
| `wind_avg_km_h`, `wind_avg_mph` | `double precision` | |
| `wind_dir`, `wind_dir_deg` | `text` / `double precision` | 8-point compass derived in parser |
| `storm_listener` | `text` | `active` / `standby` (6045M) |
| `storm_strike_count` | `integer` | |
| `storm_dist_index` | `smallint` | AS3935 index 0–31 |
| `storm_distance_mi` | `double precision` | Derived from index |
| `storm_detect_rfi` | `smallint` | |
| `signal_freq`, `signal_noise`, `signal_rssi`, `signal_snr` | `double precision` | RF metadata |
| `signal_quality` | `text` | `STRONG` / `WEAK` heuristic |

Columns are nullable when a device model does not report that field. Indexes support filtering by **`location`** and by **`model` + `sensor_id`** over time.

**Compression:** chunks of **`readings`** older than seven days are compressed automatically (no retention policy; data kept indefinitely).

### Lightning (Acurite-6045M)

The raw **`storm_strike_count`** in `readings` is an 8-bit cumulative counter (wraps at 256). Dashboards should use the loader’s delta logic and these objects from **`schema.sql`**:

| Object | Purpose |
|--------|---------|
| `lightning_sensor_state` | Last `strike_count` per `sensor_id` (Postgres state survives Pi reboots) |
| `lightning_strike_events` | Hypertable: rows when the counter **increases** (`strike_delta`, storm-front distance snapshot) |
| `lightning_hourly` | **Continuous aggregate**: per hour, `location`, `sensor_id` → `strikes`, `dist_mi_min` / `max` / `avg` |

First 6045M row after deploy only **seeds** state (no backfilled strikes). `storm_distance_mi` is **storm-front** distance at event time, not per-bolt range.

### Rain (Acurite-5-in-1)

Raw **`rain_in`** in `readings` is **cumulative inches** (tip bucket), not inches per hour. Delta tracking (parallel to lightning) uses:

| Object | Purpose |
|--------|---------|
| `rain_sensor_state` | Last `rain_in` per `sensor_id` (re-baseline when counter drops) |
| `rain_increment_events` | Hypertable: rows when **`rain_in` increases** (`rain_delta_in` inches) |
| `rain_hourly` | Continuous aggregate: **`rain_in_per_hour`** (= inches in that hour, same as avg in/hr over 1h) |

First row after deploy seeds state only (no backfill of prior total).

## Grafana query examples

Use a **PostgreSQL** datasource pointing at your `acurite` database. In panel query options, set **Format** to **Time series** for line graphs and **Table** for current-value / battery lists. These examples use Grafana macros **`$__timeFilter(reading_time)`** and **`$__timeFilter(bucket)`** on the time column (replace with `reading_time BETWEEN $__timeFrom() AND $__timeTo()` if your Grafana version differs).

Label each sensor consistently:

```sql
location || ' · ' || model || ' (' || sensor_id || ')' AS sensor
```

For **current** readings, restrict to recent data so stale sensors drop off (adjust the interval as needed):

```sql
AND reading_time > now() - interval '48 hours'
```

### Single location (Stat panel)

Use one **`location`** value from [`device_locations.csv`](device_locations.csv) (stored on every row) for tiles such as **Outdoor temperature**, **Patio humidity**, or **Back yard wind**. Panel type **Stat**, query **Format** **Table**; map the numeric column to **Value** and optionally show **`last_report`**.

**Outdoor temperature** (example site **`back_yard`**; use **`temperature_c`** if you prefer °C):

```sql
SELECT
  temperature_f AS value,
  reading_time AS last_report
FROM readings
WHERE location = 'back_yard'
  AND temperature_f IS NOT NULL
  AND reading_time > now() - interval '48 hours'
ORDER BY reading_time DESC
LIMIT 1;
```

Same pattern for other metrics: filter the same `location`, select **`humidity`**, **`wind_avg_mph`**, **`wind_dir`**, etc., and keep **`ORDER BY reading_time DESC LIMIT 1`**. To pin a specific rtl_433 id instead, use **`sensor_id = 3242`** (or combine **`location`** and **`model`** when one site has multiple devices).

In Grafana you can replace the literal with a dashboard variable, e.g. **`WHERE location = '$site'`**, with one option per outdoor/indoor label.

### Temperature

**Current value by sensor** (Table or Stat panel; one row per sensor):

```sql
SELECT DISTINCT ON (location, sensor_id, model)
  location || ' · ' || model || ' (' || sensor_id || ')' AS sensor,
  reading_time AS last_report,
  temperature_f AS temp_f,
  temperature_c AS temp_c
FROM readings
WHERE reading_time > now() - interval '48 hours'
  AND (temperature_f IS NOT NULL OR temperature_c IS NOT NULL)
ORDER BY location, sensor_id, model, reading_time DESC;
```

**History — line graph** (Time series; one series per sensor). Buckets smooth dense data; use `temperature_c` instead of `temperature_f` if you prefer:

```sql
SELECT
  time_bucket('5 minutes', reading_time) AS time,
  location || ' · ' || model || ' (' || sensor_id || ')' AS metric,
  avg(temperature_f) AS value
FROM readings
WHERE $__timeFilter(reading_time)
  AND temperature_f IS NOT NULL
GROUP BY 1, 2
ORDER BY 1;
```

### Humidity

**Current value by sensor:**

```sql
SELECT DISTINCT ON (location, sensor_id, model)
  location || ' · ' || model || ' (' || sensor_id || ')' AS sensor,
  reading_time AS last_report,
  humidity AS humidity_pct
FROM readings
WHERE reading_time > now() - interval '48 hours'
  AND humidity IS NOT NULL
ORDER BY location, sensor_id, model, reading_time DESC;
```

**History — line graph:**

```sql
SELECT
  time_bucket('5 minutes', reading_time) AS time,
  location || ' · ' || model || ' (' || sensor_id || ')' AS metric,
  avg(humidity) AS value
FROM readings
WHERE $__timeFilter(reading_time)
  AND humidity IS NOT NULL
GROUP BY 1, 2
ORDER BY 1;
```

### Wind speed

Wind appears on **5-in-1** rows (`wind_avg_mph` / `wind_avg_km_h`).

**Current value by sensor:**

```sql
SELECT DISTINCT ON (location, sensor_id, model)
  location || ' · ' || model || ' (' || sensor_id || ')' AS sensor,
  reading_time AS last_report,
  wind_avg_mph AS wind_mph,
  wind_avg_km_h AS wind_kmh
FROM readings
WHERE reading_time > now() - interval '48 hours'
  AND wind_avg_mph IS NOT NULL
ORDER BY location, sensor_id, model, reading_time DESC;
```

**History — line graph** (mph):

```sql
SELECT
  time_bucket('5 minutes', reading_time) AS time,
  location || ' · ' || model || ' (' || sensor_id || ')' AS metric,
  avg(wind_avg_mph) AS value
FROM readings
WHERE $__timeFilter(reading_time)
  AND wind_avg_mph IS NOT NULL
GROUP BY 1, 2
ORDER BY 1;
```

### Wind direction

**Current value by sensor** (compass label from parser; Table or Stat):

```sql
SELECT DISTINCT ON (location, sensor_id, model)
  location || ' · ' || model || ' (' || sensor_id || ')' AS sensor,
  reading_time AS last_report,
  wind_dir AS direction,
  wind_dir_deg AS direction_deg
FROM readings
WHERE reading_time > now() - interval '48 hours'
  AND wind_dir IS NOT NULL
ORDER BY location, sensor_id, model, reading_time DESC;
```

Direction is categorical; a time series of `wind_dir_deg` is possible but usually less readable than the compass text.

### Battery (all reporting sensors)

**Table** of last battery state per sensor:

```sql
SELECT DISTINCT ON (location, sensor_id, model)
  location || ' · ' || model || ' (' || sensor_id || ')' AS sensor,
  reading_time AS last_report,
  battery_ok,
  CASE WHEN battery_ok = 1 THEN 'OK' ELSE 'LOW' END AS status
FROM readings
WHERE reading_time > now() - interval '48 hours'
  AND battery_ok IS NOT NULL
ORDER BY location, sensor_id, model, reading_time DESC;
```

### Lightning (6045M)

Uses **`lightning_hourly`** (from [`sql/schema.sql`](sql/schema.sql)). **Strikes per hour — line graph:**

```sql
SELECT
  bucket AS time,
  location || ' (' || sensor_id || ')' AS metric,
  strikes AS value
FROM lightning_hourly
WHERE $__timeFilter(bucket)
ORDER BY 1;
```

**Hourly distance band** (min / max / avg storm-front miles, Table or multi-series):

```sql
SELECT
  bucket AS time,
  location,
  sensor_id,
  strikes,
  dist_mi_min,
  dist_mi_max,
  dist_mi_avg
FROM lightning_hourly
WHERE bucket > now() - interval '30 days'
ORDER BY bucket DESC, location;
```

### Rain (5-in-1)

Uses **`rain_hourly`**. **Inches per hour — line graph** (bar chart also works):

```sql
SELECT
  bucket AS time,
  location || ' (' || sensor_id || ')' AS metric,
  rain_in_per_hour AS value
FROM rain_hourly
WHERE $__timeFilter(bucket)
ORDER BY 1;
```

**Hourly totals table** (same numbers; useful for history reports):

```sql
SELECT
  bucket,
  location,
  sensor_id,
  rain_in_per_hour,
  observations
FROM rain_hourly
WHERE bucket > now() - interval '30 days'
ORDER BY bucket DESC, location;
```

Raw cumulative **`rain_in`** remains in **`readings`** for debugging; use **`rain_hourly`** for dashboards.

## Setup (Raspberry Pi OS Bookworm)

Example hostnames: Pi **`raspberrypi.example.com`**, database **`postgresql.example.com`**, Unix user **`sensors`**. Replace with your environment; see [CONFIGURATION.txt](CONFIGURATION.txt).

### 1. Base system

```bash
sudo apt update
sudo apt full-upgrade -y
sudo apt install -y git ca-certificates
sudo adduser sensors
sudo usermod -aG plugdev sensors
```

### 2. RTL-SDR (apt)

```bash
sudo apt install -y rtl-sdr librtlsdr-dev libusb-1.0-0-dev
echo 'blacklist dvb_usb_rtl28xx_s0' | sudo tee /etc/modprobe.d/blacklist-rtl-sdr.conf
sudo reboot
```

After reboot: `rtl_test -t` (as a user in `plugdev`).

### 3. Build rtl_433 from git

```bash
sudo apt install -y build-essential cmake pkg-config libtool librtlsdr-dev rtl-sdr
cd /tmp
git clone https://github.com/merbanan/rtl_433.git
cd rtl_433
cmake -B build
cmake --build build -j "$(nproc)"
sudo cmake --build build --target install
sudo ldconfig
rtl_433 -V
cd /opt/acurite && rtl_433 -c rtl_433.conf -T 30
```

Decoder tuning: [`rtl_433.conf`](rtl_433.conf) (25.x keyword format). Upstream reference: `/usr/local/etc/rtl_433/rtl_433.example.conf`.

### 4. TimescaleDB (on the database server)

```bash
sudo -u postgres psql -d postgres -c "CREATE DATABASE acurite;"
sudo -u postgres psql -d acurite -c "CREATE EXTENSION IF NOT EXISTS timescaledb;"
sudo -u postgres psql -d acurite -c "CREATE ROLE acurite LOGIN PASSWORD 'choose_a_password';"
sudo -u postgres psql -d acurite -f sql/schema.sql
```

Allow the Pi in `pg_hba.conf` and firewall (**5432**). The loader connects as **`acurite`** (see [`systemd/acurite.env.example`](systemd/acurite.env.example)).

### 5. Install under `/opt/acurite`

```bash
sudo install -d -o sensors -g sensors -m 755 /opt/acurite
sudo -u sensors git clone https://github.com/AnachroNet/Acurite-Parser.git /opt/acurite
sudo chmod 755 /opt/acurite/scripts/run-acurite-pipeline.sh
sudo apt install -y python3 python3-psycopg2
```

Edit [`device_locations.csv`](device_locations.csv) (`device_id`, `location`) and [`rtl_433.conf`](rtl_433.conf) as needed.

### 6. Secrets

```bash
sudo install -d -m 755 /etc/acurite
sudo cp /opt/acurite/systemd/acurite.env.example /etc/acurite/acurite.env
sudo chmod 600 /etc/acurite/acurite.env
```

Set at least **`ACURITE_DB_PASSWORD`**.

### 7. Manual test

```bash
cd /opt/acurite
set -a && source /etc/acurite/acurite.env && set +a
python3 parse_acurite_log.py acurite.log | python3 load_readings.py --stats   # optional file test
./scripts/run-acurite-pipeline.sh
```

### 8. systemd

```bash
sudo cp /opt/acurite/systemd/acurite.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now acurite.service
sudo journalctl -u acurite.service -f
```

### 9. Updates

On the Pi, pull the latest from GitHub and restart the service:

```bash
cd /opt/acurite
sudo -u sensors git pull
sudo systemctl restart acurite.service
```

Rebuild rtl_433 from `/tmp/rtl_433` when needed (see step 3).

## Troubleshooting

| Symptom | Check |
|--------|--------|
| No RTL device | `rtl_test -t`, blacklist, `plugdev` |
| No JSON | `rtl_433 -c rtl_433.conf -T 10`, antenna/gain/frequency |
| No DB rows | `device_locations.csv`, `/etc/acurite/acurite.env`, `nc -zv postgresql.example.com 5432` |
| Missing `psycopg2` | `apt install python3-psycopg2` |

**`bitbuffer_add_bit: row count limit (50 rows)`** comes from **rtl_433**, not the Python parser—usually RF noise after long runs. Mitigate with `pulse_detect squelch` / `autolevel` and Acurite-only `protocol` lines in `rtl_433.conf`; the service restarts rtl_433 every **12h** (`RuntimeMaxSec`). Set **`RTL433_LOG=1`** in `acurite.env` to log rtl_433 stderr while debugging.

The loader connects to Postgres only after the first JSON line so a slow database does not block the SDR pipeline.

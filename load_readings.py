#!/usr/bin/env python3
"""Insert normalized JSONL from parse_acurite_log.py into TimescaleDB readings."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Type

try:
    import psycopg

    db_connect: Callable[..., Any] = psycopg.connect
    DbError: Type[BaseException] = psycopg.Error
except ImportError:
    try:
        import psycopg2

        db_connect = psycopg2.connect
        DbError = psycopg2.Error
    except ImportError as exc:
        raise SystemExit(
            "PostgreSQL driver required. Install one of:\n"
            "  sudo apt install python3-psycopg2   # Raspberry Pi OS / Debian (recommended)\n"
            "  sudo apt install python3-psycopg3\n"
            "  python3 -m pip install -r requirements.txt"
        ) from exc

from zoneinfo import ZoneInfo

DEFAULT_DB_HOST = "postgresql.example.com"
DEFAULT_DB_PORT = 5432
DEFAULT_DB_NAME = "acurite"
DEFAULT_DB_USER = "acurite"
DEFAULT_DB_PASSWORD = "replace_me"
DEFAULT_TIMEZONE = "America/New_York"

INSERT_SQL = """
INSERT INTO readings (
    reading_time,
    location,
    model,
    sensor_id,
    channel,
    battery_ok,
    humidity,
    rain_in,
    temperature_c,
    temperature_f,
    wind_avg_km_h,
    wind_avg_mph,
    wind_dir,
    wind_dir_deg,
    storm_listener,
    storm_strike_count,
    storm_dist_index,
    storm_distance_mi,
    storm_detect_rfi,
    signal_freq,
    signal_noise,
    signal_rssi,
    signal_snr,
    signal_quality
) VALUES (
    %s, %s, %s, %s, %s,
    %s, %s, %s,
    %s, %s,
    %s, %s, %s, %s,
    %s, %s, %s, %s, %s,
    %s, %s, %s, %s, %s
)
"""

LIGHTNING_MODEL = "Acurite-6045M"
STRIKE_COUNT_MOD = 256
MAX_STRIKE_DELTA = 100

SELECT_STRIKE_STATE_SQL = """
SELECT last_strike_count
FROM lightning_sensor_state
WHERE sensor_id = %s
FOR UPDATE
"""

INSERT_STRIKE_STATE_SQL = """
INSERT INTO lightning_sensor_state (
    sensor_id, location, last_strike_count, last_reading_time
) VALUES (%s, %s, %s, %s)
"""

UPDATE_STRIKE_STATE_SQL = """
UPDATE lightning_sensor_state
SET location = %s,
    last_strike_count = %s,
    last_reading_time = %s
WHERE sensor_id = %s
"""

INSERT_STRIKE_EVENT_SQL = """
INSERT INTO lightning_strike_events (
    event_time,
    location,
    sensor_id,
    strike_delta,
    storm_distance_mi,
    storm_dist_index
) VALUES (%s, %s, %s, %s, %s, %s)
"""


def strike_count_delta(previous: int, current: int, *, modulo: int = STRIKE_COUNT_MOD) -> int:
    if current >= previous:
        return current - previous
    return (modulo - previous) + current


def process_lightning_row(cur: Any, row: tuple[Any, ...]) -> int:
    """Update sensor state; insert strike event when 6045M counter increases."""
    reading_time = row[0]
    location = row[1]
    model = row[2]
    sensor_id = row[3]
    storm_strike_count = row[15]
    storm_dist_index = row[16]
    storm_distance_mi = row[17]

    if model != LIGHTNING_MODEL or storm_strike_count is None:
        return 0

    current = int(storm_strike_count)
    if current < 0 or current >= STRIKE_COUNT_MOD:
        return 0

    cur.execute(SELECT_STRIKE_STATE_SQL, (sensor_id,))
    found = cur.fetchone()
    if found is None:
        cur.execute(
            INSERT_STRIKE_STATE_SQL,
            (sensor_id, location, current, reading_time),
        )
        return 0

    previous = int(found[0])
    delta = strike_count_delta(previous, current)
    cur.execute(
        UPDATE_STRIKE_STATE_SQL,
        (location, current, reading_time, sensor_id),
    )
    if delta <= 0:
        return 0
    if delta > MAX_STRIKE_DELTA:
        print(
            f"lightning: sensor_id={sensor_id} large strike_delta={delta} "
            f"(prev={previous}, cur={current}); capping to {MAX_STRIKE_DELTA}",
            file=sys.stderr,
            flush=True,
        )
        delta = MAX_STRIKE_DELTA

    cur.execute(
        INSERT_STRIKE_EVENT_SQL,
        (
            reading_time,
            location,
            sensor_id,
            delta,
            storm_distance_mi,
            storm_dist_index,
        ),
    )
    return 1


RAIN_MODEL = "Acurite-5n1"
MAX_RAIN_DELTA_IN = 2.0

SELECT_RAIN_STATE_SQL = """
SELECT last_rain_in
FROM rain_sensor_state
WHERE sensor_id = %s
FOR UPDATE
"""

INSERT_RAIN_STATE_SQL = """
INSERT INTO rain_sensor_state (
    sensor_id, location, last_rain_in, last_reading_time
) VALUES (%s, %s, %s, %s)
"""

UPDATE_RAIN_STATE_SQL = """
UPDATE rain_sensor_state
SET location = %s,
    last_rain_in = %s,
    last_reading_time = %s
WHERE sensor_id = %s
"""

INSERT_RAIN_EVENT_SQL = """
INSERT INTO rain_increment_events (
    event_time,
    location,
    sensor_id,
    rain_delta_in
) VALUES (%s, %s, %s, %s)
"""


def process_rain_row(cur: Any, row: tuple[Any, ...]) -> int:
    """Update sensor state; insert rain event when 5-in-1 rain_in increases."""
    reading_time = row[0]
    location = row[1]
    model = row[2]
    sensor_id = row[3]
    rain_in = row[7]

    if model != RAIN_MODEL or rain_in is None:
        return 0

    current = float(rain_in)
    if current < 0:
        return 0

    cur.execute(SELECT_RAIN_STATE_SQL, (sensor_id,))
    found = cur.fetchone()
    if found is None:
        cur.execute(
            INSERT_RAIN_STATE_SQL,
            (sensor_id, location, current, reading_time),
        )
        return 0

    previous = float(found[0])
    if current < previous:
        cur.execute(
            UPDATE_RAIN_STATE_SQL,
            (location, current, reading_time, sensor_id),
        )
        print(
            f"rain: sensor_id={sensor_id} counter reset "
            f"(prev={previous}, cur={current}); re-baselined",
            file=sys.stderr,
            flush=True,
        )
        return 0

    delta = current - previous
    cur.execute(
        UPDATE_RAIN_STATE_SQL,
        (location, current, reading_time, sensor_id),
    )
    if delta <= 0:
        return 0
    if delta > MAX_RAIN_DELTA_IN:
        print(
            f"rain: sensor_id={sensor_id} large rain_delta_in={delta} "
            f"(prev={previous}, cur={current}); capping to {MAX_RAIN_DELTA_IN}",
            file=sys.stderr,
            flush=True,
        )
        delta = MAX_RAIN_DELTA_IN

    cur.execute(
        INSERT_RAIN_EVENT_SQL,
        (reading_time, location, sensor_id, delta),
    )
    return 1


def db_connect_kwargs(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "host": args.db_host,
        "port": args.db_port,
        "dbname": args.db_name,
        "user": args.db_user,
        "password": args.db_password,
        "connect_timeout": 10,
    }


def configure_stdio() -> None:
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(line_buffering=True)


def open_db_connection(args: argparse.Namespace) -> Any:
    try:
        conn = db_connect(**db_connect_kwargs(args))
        conn.autocommit = False
    except DbError as exc:
        raise SystemExit(f"database connection failed: {exc}") from exc
    return conn


def parse_reading_time(raw: Any, tz: ZoneInfo) -> datetime:
    if raw is None:
        raise ValueError("missing time")
    text = str(raw).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            naive = datetime.strptime(text, fmt)
            return naive.replace(tzinfo=tz)
        except ValueError:
            continue
    raise ValueError(f"unsupported time format: {raw!r}")


def row_from_record(record: dict[str, Any], tz: ZoneInfo) -> tuple[Any, ...]:
    location = record.get("location")
    if not location:
        raise ValueError("missing location (add device to device_locations.csv)")

    sensor_id = record.get("id")
    model = record.get("model")
    if sensor_id is None or model is None:
        raise ValueError("missing id or model")

    return (
        parse_reading_time(record.get("time"), tz),
        location,
        model,
        int(sensor_id),
        record.get("channel"),
        record.get("battery_ok"),
        record.get("humidity"),
        record.get("rain_in"),
        record.get("temperature_C"),
        record.get("temperature_F"),
        record.get("wind_avg_km_h"),
        record.get("wind_avg_mph"),
        record.get("wind_dir"),
        record.get("wind_dir_deg"),
        record.get("storm_listener"),
        record.get("storm_strike_count"),
        record.get("storm_dist_index"),
        record.get("storm_distance_mi"),
        record.get("storm_detect_rfi"),
        record.get("signal_freq"),
        record.get("signal_noise"),
        record.get("signal_rssi"),
        record.get("signal_snr"),
        record.get("signal_quality"),
    )


def iter_jsonl_lines(lines: Iterable[str], *, source: str) -> Iterator[tuple[int, dict[str, Any]]]:
    for line_no, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{source}:{line_no}: invalid JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise ValueError(f"{source}:{line_no}: expected a JSON object")
        yield line_no, payload


def iter_input(path: Path | None) -> tuple[Iterator[tuple[int, dict[str, Any]]], str, bool]:
    if path is not None:
        if str(path) == "-":
            return iter_jsonl_lines(sys.stdin, source="-"), "-", True
        if not path.is_file():
            raise FileNotFoundError(f"input file not found: {path}")
        with path.open(encoding="utf-8") as handle:
            records = list(iter_jsonl_lines(handle, source=str(path)))
        return iter(records), str(path), False

    if not sys.stdin.isatty():
        return iter_jsonl_lines(sys.stdin, source="-"), "-", True

    raise ValueError(
        "no input: pipe JSONL on stdin, pass '-', or provide a file path"
    )


def commit_batch(conn: Any, batch: list[tuple[Any, ...]]) -> tuple[int, int, int]:
    if not batch:
        return 0, 0, 0
    readings, lightning_events, rain_events = insert_batch(conn, batch)
    conn.commit()
    batch.clear()
    return readings, lightning_events, rain_events


def insert_batch(
    conn: Any,
    rows: list[tuple[Any, ...]],
) -> tuple[int, int, int]:
    if not rows:
        return 0, 0, 0
    lightning_events = 0
    rain_events = 0
    with conn.cursor() as cur:
        for row in rows:
            cur.execute(INSERT_SQL, row)
            lightning_events += process_lightning_row(cur, row)
            rain_events += process_rain_row(cur, row)
    return len(rows), lightning_events, rain_events


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Load parse_acurite_log.py JSONL into Postgres readings hypertable.",
        epilog=(
            "Example: python parse_acurite_log.py acurite.log | python load_readings.py"
        ),
    )
    parser.add_argument(
        "input",
        nargs="?",
        type=Path,
        help="Normalized JSONL file, or '-' for stdin (default: stdin when piped)",
    )
    parser.add_argument(
        "--timezone",
        default=os.environ.get("ACURITE_TIMEZONE", DEFAULT_TIMEZONE),
        help=f"IANA zone for rtl_433 time strings (default: {DEFAULT_TIMEZONE})",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=100,
        help=(
            "Rows per INSERT batch when loading a file (default: 100). "
            "When reading stdin, each batch is committed as soon as it is full "
            "and partial batches commit after every line."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and validate rows without connecting to the database",
    )
    parser.add_argument(
        "--stats",
        action="store_true",
        help="Print load statistics to stderr",
    )
    parser.add_argument("--db-host", default=os.environ.get("ACURITE_DB_HOST", DEFAULT_DB_HOST))
    parser.add_argument(
        "--db-port",
        type=int,
        default=int(os.environ.get("ACURITE_DB_PORT", DEFAULT_DB_PORT)),
    )
    parser.add_argument("--db-name", default=os.environ.get("ACURITE_DB_NAME", DEFAULT_DB_NAME))
    parser.add_argument("--db-user", default=os.environ.get("ACURITE_DB_USER", DEFAULT_DB_USER))
    parser.add_argument(
        "--db-password",
        default=os.environ.get("ACURITE_DB_PASSWORD", DEFAULT_DB_PASSWORD),
        help="Override with ACURITE_DB_PASSWORD in production",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    configure_stdio()
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    try:
        tz = ZoneInfo(args.timezone)
    except Exception as exc:
        parser.error(f"invalid timezone {args.timezone!r}: {exc}")

    try:
        incoming, source, stream_mode = iter_input(args.input)
    except (ValueError, FileNotFoundError) as exc:
        print(exc, file=sys.stderr)
        return 1

    batch: list[tuple[Any, ...]] = []
    input_lines = 0
    inserted = 0
    lightning_events = 0
    rain_events = 0
    skipped = 0
    errors = 0
    warned_missing_location = False

    conn: Any | None = None
    if not args.dry_run and not stream_mode:
        conn = open_db_connection(args)

    try:
        for line_no, record in incoming:
            input_lines += 1
            try:
                row = row_from_record(record, tz)
            except ValueError as exc:
                skipped += 1
                msg = f"{source}:{line_no}: skip: {exc}"
                if "location" in str(exc).lower() and not warned_missing_location:
                    warned_missing_location = True
                    msg += (
                        f" (id={record.get('id')!r}, model={record.get('model')!r}; "
                        "add device_locations.csv beside parse_acurite_log.py or use --locations)"
                    )
                print(msg, file=sys.stderr, flush=True)
                continue

            if args.dry_run:
                inserted += 1
                continue

            if conn is None:
                conn = open_db_connection(args)

            batch.append(row)
            if len(batch) >= args.batch_size or stream_mode:
                n_readings, n_lightning, n_rain = commit_batch(conn, batch)
                inserted += n_readings
                lightning_events += n_lightning
                rain_events += n_rain

        if batch and conn is not None:
            n_readings, n_lightning, n_rain = commit_batch(conn, batch)
            inserted += n_readings
            lightning_events += n_lightning
            rain_events += n_rain
    except (ValueError, DbError) as exc:
        if conn is not None:
            conn.rollback()
        print(exc, file=sys.stderr, flush=True)
        errors += 1
        return 1
    except SystemExit as exc:
        print(exc, file=sys.stderr, flush=True)
        return 1
    finally:
        if conn is not None:
            conn.close()

    if args.stats:
        mode = "dry-run" if args.dry_run else "insert"
        print(
            f"source: {source}  mode: {mode}  input_lines: {input_lines}  "
            f"inserted: {inserted}  lightning_events: {lightning_events}  "
            f"rain_events: {rain_events}  skipped: {skipped}  errors: {errors}",
            file=sys.stderr,
        )
        if input_lines > 0 and inserted == 0 and not args.dry_run:
            print(
                "No rows were inserted. If skipped is high, fix location mapping "
                "(device_locations.csv). If skipped is 0, confirm load_readings.py "
                "is up to date (stream commits) and query the same database host above.",
                file=sys.stderr,
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

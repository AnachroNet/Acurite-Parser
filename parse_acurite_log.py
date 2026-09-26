#!/usr/bin/env python3
"""Parse rtl_433 JSON lines into normalized, deduplicated JSONL."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any, Iterable, Iterator, TextIO

# Union of fields seen across Acurite device models in rtl_433 output.
BASE_FIELDS: tuple[str, ...] = (
    "battery_ok",
    "channel",
    "humidity",
    "id",
    "location",
    "model",
    "rain_in",
    "signal_freq",
    "signal_noise",
    "signal_quality",
    "signal_rssi",
    "signal_snr",
    "storm_detect_rfi",
    "storm_dist_index",
    "storm_distance_mi",
    "storm_listener",
    "storm_strike_count",
    "temperature_C",
    "temperature_F",
    "time",
    "wind_avg_km_h",
    "wind_avg_mph",
    "wind_dir",
    "wind_dir_deg",
)

# RF capture metadata and 5-in-1 packet fragments; omit when deduping readings.
READING_DEDUPE_OMIT: frozenset[str] = frozenset(
    {
        "freq",
        "noise",
        "rssi",
        "sequence_num",
        "signal_freq",
        "signal_noise",
        "signal_rssi",
        "signal_snr",
        "snr",
    }
)

FIVE_IN_ONE_MODEL = "Acurite-5n1"

# rtl_433 metadata not needed in normalized output.
DROPPED_OUTPUT_FIELDS: frozenset[str] = frozenset(
    {
        "active",
        "exception",
        "freq",
        "message_type",
        "mic",
        "mod",
        "noise",
        "raw_msg",
        "rfi",
        "rssi",
        "sequence_num",
        "snr",
        "storm_dist",
        "strike_count",
    }
)

# rtl_433 rssi is normalized signal strength; snr is in dB (typical capture range ~9–30).
SIGNAL_RSSI_STRONG_MIN = 0.90
SIGNAL_SNR_STRONG_MIN = 10.0

# AS3935 / Acurite 6045M 5-bit storm-distance index (0–31) to estimated km.
# Index 31 is reserved (rtl_433: invalid / no storm at power-up).
STORM_DISTANCE_KM: tuple[float, ...] = (
    0,
    1,
    2,
    3,
    4,
    5,
    6,
    7,
    8,
    9,
    10,
    11,
    12,
    13,
    14,
    17,
    20,
    24,
    27,
    31,
    34,
    38,
    43,
    48,
    52,
    57,
    64,
    72,
    81,
    91,
    102,
    114,
)
STORM_DISTANCE_INVALID_INDEX = 31

LOCATION_MAX_LEN = 20
DEFAULT_DEVICE_LOCATIONS = Path(__file__).resolve().parent / "device_locations.csv"

# Fields merged from successive 5-in-1 packets (types 49 and 56).
FIVE_IN_ONE_MERGE_FIELDS: frozenset[str] = frozenset(
    {
        "battery_ok",
        "channel",
        "humidity",
        "id",
        "model",
        "rain_in",
        "temperature_C",
        "temperature_F",
        "time",
        "wind_avg_km_h",
        "wind_dir_deg",
    }
)

# Latest RF metadata wins when assembling a 5-in-1 reading.
FIVE_IN_ONE_RF_FIELDS: frozenset[str] = frozenset(
    {"signal_freq", "signal_noise", "signal_rssi", "signal_snr"}
)


def celsius_to_fahrenheit(celsius: float) -> float:
    return round(celsius * 9.0 / 5.0 + 32.0, 3)


def fahrenheit_to_celsius(fahrenheit: float) -> float:
    return round((fahrenheit - 32.0) * 5.0 / 9.0, 3)


def km_h_to_mph(km_h: float) -> float:
    return round(km_h * 0.621371, 3)


WIND_DIRECTIONS: tuple[str, ...] = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")


def degrees_to_wind_dir(degrees: float) -> str:
    """Map degrees to 8-point compass (cardinal and intercardinal)."""
    deg = float(degrees) % 360.0
    index = int((deg + 22.5) // 45) % 8
    return WIND_DIRECTIONS[index]


def compute_signal_quality(rssi: Any, snr: Any) -> str | None:
    if rssi is None or snr is None:
        return None
    if float(snr) >= SIGNAL_SNR_STRONG_MIN and float(rssi) >= SIGNAL_RSSI_STRONG_MIN:
        return "STRONG"
    return "WEAK"


def storm_listener_from_active(active: Any) -> str | None:
    if active is None:
        return None
    return "active" if int(active) else "standby"


def storm_distance_mi_from_index(index: Any) -> float | None:
    if index is None:
        return None
    idx = int(index)
    if idx < 0 or idx >= len(STORM_DISTANCE_KM):
        return None
    if idx == STORM_DISTANCE_INVALID_INDEX:
        return None
    km = STORM_DISTANCE_KM[idx]
    return round(km * 0.621371, 3)


def load_device_locations(path: Path) -> dict[int, str]:
    """Load device_id -> location from a CSV file (columns: device_id, location)."""
    if not path.is_file():
        raise FileNotFoundError(f"device locations file not found: {path}")

    mapping: dict[int, str] = {}
    with path.open(encoding="utf-8", newline="") as handle:
        lines = [
            line
            for line in handle
            if line.strip() and not line.lstrip().startswith("#")
        ]
    reader = csv.reader(lines)
    try:
        row = next(reader)
    except StopIteration:
        return mapping

    header = [cell.strip().lower() for cell in row]
    if header[:2] == ["device_id", "location"]:
        data_rows = reader
    else:
        data_rows = iter([row, *reader])

    for line_no, row in enumerate(data_rows, start=2 if header[:2] == ["device_id", "location"] else 1):
        if not row or all(not cell.strip() for cell in row):
            continue
        if len(row) < 2:
            raise ValueError(
                f"{path}:{line_no}: expected device_id,location (two columns)"
            )
        id_raw, location_raw = row[0].strip(), row[1].strip()
        try:
            device_id = int(id_raw)
        except ValueError as exc:
            raise ValueError(
                f"{path}:{line_no}: device_id must be an integer, got {id_raw!r}"
            ) from exc
        if not location_raw:
            raise ValueError(f"{path}:{line_no}: location must not be empty")
        if len(location_raw) > LOCATION_MAX_LEN:
            raise ValueError(
                f"{path}:{line_no}: location exceeds {LOCATION_MAX_LEN} characters: "
                f"{location_raw!r}"
            )
        if device_id in mapping:
            raise ValueError(f"{path}:{line_no}: duplicate device_id {device_id}")
        mapping[device_id] = location_raw

    return mapping


def attach_location(
    record: dict[str, Any], device_locations: dict[int, str] | None
) -> None:
    if device_locations is None:
        record["location"] = None
        return
    sensor_id = record.get("id")
    if sensor_id is None:
        record["location"] = None
        return
    record["location"] = device_locations.get(int(sensor_id))


def enrich_record(record: dict[str, Any]) -> dict[str, Any]:
    """Fill derived temperature, wind speed, and wind direction fields."""
    out = dict(record)
    temp_c = out.get("temperature_C")
    temp_f = out.get("temperature_F")
    if temp_c is not None and temp_f is None:
        out["temperature_F"] = celsius_to_fahrenheit(float(temp_c))
    elif temp_f is not None and temp_c is None:
        out["temperature_C"] = fahrenheit_to_celsius(float(temp_f))

    if out.get("wind_avg_km_h") is not None:
        out["wind_avg_mph"] = km_h_to_mph(float(out["wind_avg_km_h"]))
    else:
        out["wind_avg_mph"] = None

    if out.get("wind_dir_deg") is not None:
        out["wind_dir"] = degrees_to_wind_dir(out["wind_dir_deg"])
    else:
        out["wind_dir"] = None

    if "rfi" in out:
        out["storm_detect_rfi"] = out["rfi"]
    if "strike_count" in out:
        out["storm_strike_count"] = out["strike_count"]
    if "storm_dist" in out:
        out["storm_dist_index"] = out["storm_dist"]
        out["storm_distance_mi"] = storm_distance_mi_from_index(out["storm_dist"])
    if "active" in out:
        out["storm_listener"] = storm_listener_from_active(out["active"])
    if "rssi" in out:
        out["signal_rssi"] = out["rssi"]
    if "snr" in out:
        out["signal_snr"] = out["snr"]
    if "freq" in out:
        out["signal_freq"] = out["freq"]
    if "noise" in out:
        out["signal_noise"] = out["noise"]

    out["signal_quality"] = compute_signal_quality(
        out.get("signal_rssi"), out.get("signal_snr")
    )

    for key in DROPPED_OUTPUT_FIELDS:
        out.pop(key, None)

    return out


def parse_line(line: str, line_no: int, source: str) -> dict[str, Any] | None:
    stripped = line.strip()
    if not stripped:
        return None
    try:
        record = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{source}:{line_no}: invalid JSON: {exc}") from exc
    if not isinstance(record, dict):
        raise ValueError(f"{source}:{line_no}: expected a JSON object")
    return record


def extend_field_names(field_names: list[str], field_set: set[str], record: dict[str, Any]) -> None:
    for key in record:
        if key in DROPPED_OUTPUT_FIELDS or key in field_set:
            continue
        field_set.add(key)
        field_names.append(key)
        field_names.sort()


def normalize_record(record: dict[str, Any], field_names: list[str]) -> dict[str, Any]:
    return {name: record.get(name) for name in field_names}


def is_five_in_one(record: dict[str, Any]) -> bool:
    return record.get("model") == FIVE_IN_ONE_MODEL


def five_in_one_fragment_fingerprint(record: dict[str, Any]) -> str:
    payload = {
        k: v
        for k, v in record.items()
        if k not in READING_DEDUPE_OMIT and k != "message_type"
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def five_in_one_is_complete(pending: dict[str, Any]) -> bool:
    has_temp_humidity = (
        pending.get("temperature_F") is not None
        or pending.get("temperature_C") is not None
        or pending.get("humidity") is not None
    )
    has_wind_rain = (
        pending.get("wind_dir_deg") is not None or pending.get("rain_in") is not None
    )
    return has_temp_humidity and has_wind_rain


def merge_five_in_one_part(
    pending: dict[str, Any], record: dict[str, Any]
) -> dict[str, Any]:
    for key in FIVE_IN_ONE_MERGE_FIELDS | FIVE_IN_ONE_RF_FIELDS:
        value = record.get(key)
        if value is not None:
            pending[key] = value
    return pending


def finalize_five_in_one(pending: dict[str, Any]) -> dict[str, Any]:
    return enrich_record(dict(pending))


class FiveInOneAggregator:
    """Merge Acurite 5-in-1 type 49 and 56 packets into one reading per cycle."""

    def __init__(self) -> None:
        self._pending: dict[tuple[Any, ...], dict[str, Any]] = {}
        self._fragments_seen: dict[tuple[Any, ...], set[str]] = {}

    def _key(self, record: dict[str, Any]) -> tuple[Any, ...]:
        return (record.get("id"), record.get("channel"))

    def _reset(self, key: tuple[Any, ...]) -> None:
        self._pending.pop(key, None)

    def feed(self, record: dict[str, Any]) -> list[dict[str, Any]]:
        key = self._key(record)
        fragment_key = five_in_one_fragment_fingerprint(record)
        seen = self._fragments_seen.setdefault(key, set())
        if fragment_key in seen:
            return []
        seen.add(fragment_key)

        pending = self._pending.setdefault(key, {})
        merge_five_in_one_part(pending, record)

        if not five_in_one_is_complete(pending):
            return []

        combined = finalize_five_in_one(pending)
        self._reset(key)
        return [combined]

    def flush(self) -> list[dict[str, Any]]:
        combined: list[dict[str, Any]] = []
        for key in list(self._pending):
            pending = self._pending.get(key)
            if pending and five_in_one_is_complete(pending):
                combined.append(finalize_five_in_one(pending))
            self._reset(key)
        return combined


def fingerprint(record: dict[str, Any], *, mode: str) -> str:
    if mode == "exact":
        payload = record
    elif mode == "reading":
        payload = {k: v for k, v in record.items() if k not in READING_DEDUPE_OMIT}
    else:
        raise ValueError(f"unknown dedupe mode: {mode}")
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def iter_records_from_lines(
    lines: Iterable[str],
    *,
    source: str,
) -> Iterator[tuple[int, dict[str, Any]]]:
    for line_no, line in enumerate(lines, start=1):
        record = parse_line(line, line_no, source)
        if record is not None:
            yield line_no, record


def iter_records_from_path(path: Path) -> Iterator[tuple[int, dict[str, Any]]]:
    with path.open(encoding="utf-8") as handle:
        yield from iter_records_from_lines(handle, source=str(path))


def emit_record(
    record: dict[str, Any],
    *,
    field_names: list[str],
    field_set: set[str],
    seen: set[str],
    dedupe: bool,
    dedupe_mode: str,
    device_locations: dict[int, str] | None,
    out: TextIO,
    flush: bool,
) -> bool:
    record = enrich_record(record)
    attach_location(record, device_locations)
    extend_field_names(field_names, field_set, record)

    if dedupe:
        key = fingerprint(record, mode=dedupe_mode)
        if key in seen:
            return False
        seen.add(key)

    try:
        out.write(json.dumps(normalize_record(record, field_names), sort_keys=True))
        out.write("\n")
        if flush:
            out.flush()
    except BrokenPipeError:
        raise
    return True


def process_stream(
    incoming: Iterator[tuple[int, dict[str, Any]]],
    out: TextIO,
    *,
    dedupe: bool,
    dedupe_mode: str,
    device_locations: dict[int, str] | None,
    flush: bool,
) -> tuple[int, int, list[str]]:
    field_names = sorted(BASE_FIELDS)
    field_set = set(field_names)
    seen: set[str] = set()
    five_in_one = FiveInOneAggregator()
    input_lines = 0
    output_lines = 0

    for _, raw in incoming:
        input_lines += 1
        record = enrich_record(raw)

        if is_five_in_one(record):
            for combined in five_in_one.feed(record):
                if emit_record(
                    combined,
                    field_names=field_names,
                    field_set=field_set,
                    seen=seen,
                    dedupe=dedupe,
                    dedupe_mode=dedupe_mode,
                    device_locations=device_locations,
                    out=out,
                    flush=flush,
                ):
                    output_lines += 1
            continue

        if emit_record(
            record,
            field_names=field_names,
            field_set=field_set,
            seen=seen,
            dedupe=dedupe,
            dedupe_mode=dedupe_mode,
            device_locations=device_locations,
            out=out,
            flush=flush,
        ):
            output_lines += 1

    for combined in five_in_one.flush():
        if emit_record(
            combined,
            field_names=field_names,
            field_set=field_set,
            seen=seen,
            dedupe=dedupe,
            dedupe_mode=dedupe_mode,
            device_locations=device_locations,
            out=out,
            flush=flush,
        ):
            output_lines += 1

    return input_lines, output_lines, field_names


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Normalize rtl_433 Acurite JSON (stdin or file) to deduplicated JSONL "
            "with derived wind and temperature fields."
        ),
        epilog="Example: rtl_433 -F json | python parse_acurite_log.py",
    )
    parser.add_argument(
        "input",
        nargs="?",
        type=Path,
        help="Input log file, or '-' for stdin (default: stdin when piped)",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="Output file (default: stdout)",
    )
    parser.add_argument(
        "--no-dedupe",
        action="store_true",
        help="Emit every log line without deduplication",
    )
    parser.add_argument(
        "--dedupe-mode",
        choices=("reading", "exact"),
        default="reading",
        help=(
            "reading: drop retransmits and duplicate payloads "
            "(default); exact: only drop byte-identical records"
        ),
    )
    parser.add_argument(
        "--stats",
        action="store_true",
        help="Print parse statistics to stderr",
    )
    parser.add_argument(
        "--locations",
        type=Path,
        metavar="FILE",
        help=(
            "CSV mapping device_id,location (max 20 chars). "
            f"Default: {DEFAULT_DEVICE_LOCATIONS.name} beside this script if present"
        ),
    )
    parser.add_argument(
        "--no-locations",
        action="store_true",
        help="Do not load a device location CSV; emit location as null",
    )
    return parser


def resolve_device_locations(
    parser: argparse.ArgumentParser, args: argparse.Namespace
) -> dict[int, str] | None:
    if args.no_locations:
        return None
    path = args.locations
    if path is None:
        if DEFAULT_DEVICE_LOCATIONS.is_file():
            path = DEFAULT_DEVICE_LOCATIONS
        else:
            return None
    try:
        return load_device_locations(path)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    return None  # unreachable


def resolve_input(
    parser: argparse.ArgumentParser, path: Path | None
) -> tuple[Iterator[tuple[int, dict[str, Any]]], str, bool]:
    """Return (record iterator, source label, flush stdout after each line)."""
    if path is not None:
        if str(path) == "-":
            return iter_records_from_lines(sys.stdin, source="-"), "-", True
        if not path.is_file():
            parser.error(f"input file not found: {path}")
        return iter_records_from_path(path), str(path), False

    if not sys.stdin.isatty():
        return iter_records_from_lines(sys.stdin, source="-"), "-", True

    parser.error(
        "no input: pipe rtl_433 JSON on stdin, pass '-', or provide a log file path"
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    record_iter, source, flush_stdout = resolve_input(parser, args.input)
    device_locations = resolve_device_locations(parser, args)

    out_handle: TextIO
    close_out = False
    if args.output:
        out_handle = args.output.open("w", encoding="utf-8", newline="\n")
        close_out = True
        flush_each = False
    else:
        out_handle = sys.stdout
        flush_each = flush_stdout
        if flush_each and hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(line_buffering=True)

    try:
        input_lines, output_lines, field_names = process_stream(
            record_iter,
            out_handle,
            dedupe=not args.no_dedupe,
            dedupe_mode=args.dedupe_mode,
            device_locations=device_locations,
            flush=flush_each,
        )
    except BrokenPipeError:
        # Downstream (e.g. load_readings.py) exited; avoid traceback noise.
        return 0
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1
    finally:
        if close_out:
            out_handle.close()

    if args.stats:
        print(
            f"fields: {len(field_names)}  input_lines: {input_lines}  "
            f"output_lines: {output_lines}  dedupe: {not args.no_dedupe} "
            f"({args.dedupe_mode})  source: {source}",
            file=sys.stderr,
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

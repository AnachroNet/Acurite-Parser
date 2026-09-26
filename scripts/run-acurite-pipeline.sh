#!/bin/bash
# Run rtl_433 -> parse_acurite_log.py -> load_readings.py for systemd or manual use.
set -uo pipefail

ACURITE_DIR="${ACURITE_DIR:-/opt/acurite}"
RTL433_BIN="${RTL433_BIN:-rtl_433}"
RTL433_CONF="${RTL433_CONF:-$ACURITE_DIR/rtl_433.conf}"
PYTHON="${PYTHON:-python3}"

cd "$ACURITE_DIR" || exit 1

if ! command -v "$RTL433_BIN" >/dev/null 2>&1; then
  echo "rtl_433 not found: $RTL433_BIN" >&2
  exit 127
fi

rtl_cmd=()
if [[ -f "$RTL433_CONF" ]]; then
  rtl_cmd+=(-c "$RTL433_CONF")
else
  echo "missing $RTL433_CONF (see rtl_433.conf and upstream rtl_433.example.conf)" >&2
  exit 1
fi
if [[ -n "${RTL433_ARGS:-}" ]]; then
  # shellcheck disable=SC2206
  rtl_extra=($RTL433_ARGS)
  rtl_cmd+=("${rtl_extra[@]}")
fi

# rtl_433 diagnostics go to stderr (not the Python parser). Discard here so
# journalctl stays quiet; use RTL433_LOG=1 to keep them for debugging.
rtl_stderr=/dev/null
if [[ -n "${RTL433_LOG:-}" ]]; then
  rtl_stderr=/dev/stderr
fi

"${RTL433_BIN}" "${rtl_cmd[@]}" 2>"$rtl_stderr" \
  | "$PYTHON" -u parse_acurite_log.py \
  | "$PYTHON" -u load_readings.py

exit "$?"

#!/bin/bash
# Run verify_sqlite_parity.py across the canary channel's data products.
#
# Cron'd via timestd-sqlite-parity.timer (every 6 h). Exits 0 if all
# products agree between HDF5 and SQLite within the lookback window;
# non-zero if any divergence is found. Output is captured by systemd
# journal — `journalctl -u timestd-sqlite-parity` shows the history.
#
# The lookback window is intentionally small (6 minutes) so the
# comparison only includes rows written under dual-write — pre-cutover
# HDF5 rows are excluded.

set -uo pipefail

VENV_PY="${VENV_PY:-/opt/hf-timestd/venv/bin/python}"
PARITY_SCRIPT="${PARITY_SCRIPT:-/opt/git/sigmond/hf-timestd/scripts/verify_sqlite_parity.py}"
CHANNEL="${CHANNEL:-CHU_7850}"  # canary
HOURS="${HOURS:-0.1}"            # 6-minute lookback window

# (level, product) pairs to verify. Mirrors the writer set in
# metrology_service.py — every product the canary channel writes.
PRODUCTS=(
    "L1 metrology_measurements"
    "L1 all_arrivals"
    "L2 chu_fsk"
    "L2 detection_attempts"
    "L2 tick_phase"
    "L2 tick_timing"
)

if [[ ! -x "$VENV_PY" ]]; then
    echo "ERROR: python not found at $VENV_PY" >&2
    exit 2
fi
if [[ ! -f "$PARITY_SCRIPT" ]]; then
    echo "ERROR: parity script not found at $PARITY_SCRIPT" >&2
    exit 2
fi

n_total=0
n_fail=0
n_skip=0
failures=()

for entry in "${PRODUCTS[@]}"; do
    level="${entry%% *}"
    product="${entry#* }"
    n_total=$((n_total + 1))
    out=$("$VENV_PY" "$PARITY_SCRIPT" \
        --channel "$CHANNEL" \
        --product "$product" \
        --level "$level" \
        --hours "$HOURS" 2>&1)
    rc=$?

    # Pull the summary lines out of the verbose script output.
    h5_count=$(printf '%s\n' "$out" | awk '/^HDF5 rows:/ {print $3}')
    sql_count=$(printf '%s\n' "$out" | awk '/^SQLite rows:/ {print $3}')
    h5_count="${h5_count:-?}"
    sql_count="${sql_count:-?}"

    # Skip products that have legitimately zero rows in the window
    # (e.g. test_signal which only fires on WWV minutes 8/44).
    if [[ "$h5_count" == "0" && "$sql_count" == "0" ]]; then
        n_skip=$((n_skip + 1))
        printf '  %-30s SKIP  (no rows in window)\n' "${level}_${product}"
        continue
    fi

    if [[ $rc -eq 0 ]]; then
        printf '  %-30s OK    h5=%s sql=%s\n' "${level}_${product}" "$h5_count" "$sql_count"
    else
        n_fail=$((n_fail + 1))
        failures+=("${level}_${product}")
        printf '  %-30s FAIL  h5=%s sql=%s rc=%d\n' \
            "${level}_${product}" "$h5_count" "$sql_count" "$rc"
        # Echo the verbose script output so the journal carries the
        # full divergence detail for forensics.
        printf '    --- script output ---\n%s\n    --- end ---\n' "$out"
    fi
done

n_ok=$((n_total - n_fail - n_skip))
echo
echo "Summary: channel=$CHANNEL window=${HOURS}h  total=$n_total ok=$n_ok skip=$n_skip fail=$n_fail"
if [[ $n_fail -gt 0 ]]; then
    printf 'FAILING: %s\n' "${failures[*]}"
    exit 1
fi
exit 0

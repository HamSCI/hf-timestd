#!/bin/bash
# Run verify_sqlite_parity.py across every dual-written data product and
# report HDF5 <-> SQLite parity for the HDF5->SQLite migration.
#
# Cron'd via timestd-sqlite-parity.timer (every 6 h); the systemd
# journal keeps the history — `journalctl -u timestd-sqlite-parity`.
#
# Two product groups:
#   per-channel  — metrology + L2-calibration products, one daily .h5
#                  per channel under phase2/{channel}/... (registry-
#                  resolved). Checked for every channel in $CHANNELS.
#   L3 aggregate — fusion / reanalysis science products under
#                  phase2/fusion/ and phase2/science/*/, one channel
#                  each; located with verify_sqlite_parity.py's
#                  --hdf5-data-dir.
#
# The lookback window is intentionally small (6 min default) so the
# comparison only includes rows written under dual-write.
#
# Per-product result, from verify_sqlite_parity.py's exit code:
#   OK       rc 0  — backends agree
#   SKIP     rc 0  — both backends empty in the window (a product that
#                    only emits on certain minutes)
#   PENDING  rc 3  — SQLite table absent: the producer is not yet
#                    deployed with dual-write. Expected while the
#                    cutover is in progress; NOT counted as a failure.
#   FAIL     rc 1  — a real divergence (script output echoed for forensics)
#   ERROR    rc 2  — a backend could not be read
#
# Exit 0 unless some product FAILed or ERRORed. PENDING alone does not
# fail the run — but once every producer is deployed with dual-write,
# a clean run should show no PENDING left before the read-side flip.
#
# Env overrides: VENV_PY, PARITY_SCRIPT, HOURS, DATA_ROOT, CHANNELS.
# A full cross-channel sweep before the flip:
#   CHANNELS="CHU_3330 CHU_7850 CHU_14670 SHARED_2500 SHARED_5000 \
#             SHARED_10000 SHARED_15000 WWV_20000 WWV_25000" \
#     scripts/parity_check_all.sh

set -uo pipefail

VENV_PY="${VENV_PY:-/opt/hf-timestd/venv/bin/python}"
PARITY_SCRIPT="${PARITY_SCRIPT:-/opt/git/sigmond/hf-timestd/scripts/verify_sqlite_parity.py}"
HOURS="${HOURS:-0.1}"                       # 6-minute lookback window
DATA_ROOT="${DATA_ROOT:-/var/lib/timestd/phase2}"

# Channels carrying the per-channel products. Default: the CHU_7850
# canary (cheap, representative — every channel's metrology@ service
# writes the same products identically). Override CHANNELS for a sweep.
CHANNELS=( ${CHANNELS:-CHU_7850} )

# Per-channel products: "<level> <product>" — one daily .h5 per channel
# under phase2/{channel}/... Mirrors the writer set in
# metrology_service.py and l2_calibration_service.py.
PERCHAN_PRODUCTS=(
    "L1 metrology_measurements"
    "L1 all_arrivals"
    "L2 chu_fsk"
    "L2 detection_attempts"
    "L2 tick_phase"
    "L2 tick_timing"
    "L2 timing_measurements"
)

# L3 aggregate products: "<level> <product> <channel> <hdf5_data_dir>".
# Written by physics_fusion_service.py and ionospheric_reanalysis.py.
L3_PRODUCTS=(
    "L3 physics global ${DATA_ROOT}/fusion"
    "L3 tec AGGREGATED ${DATA_ROOT}/science/tec"
    "L3 dtec AGGREGATED ${DATA_ROOT}/science/dtec"
    "L3 dtec_timeseries AGGREGATED ${DATA_ROOT}/science/dtec_timeseries"
    "L3 dtec_diff AGGREGATED ${DATA_ROOT}/science/dtec_diff"
    "L3C propagation_stats REANALYSIS ${DATA_ROOT}/science/propagation_stats"
    "L3 tec REANALYZED ${DATA_ROOT}/science/tec_reanalyzed"
)

if [[ ! -x "$VENV_PY" ]]; then
    echo "ERROR: python not found at $VENV_PY" >&2
    exit 2
fi
if [[ ! -f "$PARITY_SCRIPT" ]]; then
    echo "ERROR: parity script not found at $PARITY_SCRIPT" >&2
    exit 2
fi

n_total=0; n_ok=0; n_skip=0; n_pending=0; n_fail=0; n_err=0
failures=()

# run_one <label> <level> <product> <channel> [hdf5_data_dir]
run_one() {
    local label="$1" level="$2" product="$3" channel="$4" hdf5_dir="${5:-}"
    local args=(--channel "$channel" --product "$product"
                --level "$level" --hours "$HOURS")
    [[ -n "$hdf5_dir" ]] && args+=(--hdf5-data-dir "$hdf5_dir")

    local out rc h5 sql
    out=$("$VENV_PY" "$PARITY_SCRIPT" "${args[@]}" 2>&1)
    rc=$?
    n_total=$((n_total + 1))

    h5=$(printf '%s\n' "$out" | awk '/^HDF5 rows:/ {print $3}')
    sql=$(printf '%s\n' "$out" | awk '/^SQLite rows:/ {print $3}')
    h5="${h5:-?}"; sql="${sql:-?}"

    case $rc in
        0)
            if [[ "$h5" == "0" && "$sql" == "0" ]]; then
                n_skip=$((n_skip + 1))
                printf '  %-36s SKIP     (no rows in window)\n' "$label"
            else
                n_ok=$((n_ok + 1))
                printf '  %-36s OK       h5=%s sql=%s\n' "$label" "$h5" "$sql"
            fi
            ;;
        3)
            n_pending=$((n_pending + 1))
            printf '  %-36s PENDING  (not dual-written yet)\n' "$label"
            ;;
        2)
            n_err=$((n_err + 1)); failures+=("$label")
            printf '  %-36s ERROR    rc=2\n' "$label"
            printf '    --- script output ---\n%s\n    --- end ---\n' "$out"
            ;;
        *)
            n_fail=$((n_fail + 1)); failures+=("$label")
            printf '  %-36s FAIL     h5=%s sql=%s rc=%d\n' \
                "$label" "$h5" "$sql" "$rc"
            printf '    --- script output ---\n%s\n    --- end ---\n' "$out"
            ;;
    esac
}

echo "== per-channel products =="
for channel in "${CHANNELS[@]}"; do
    for entry in "${PERCHAN_PRODUCTS[@]}"; do
        level="${entry%% *}"; product="${entry#* }"
        run_one "${channel}/${level}_${product}" \
            "$level" "$product" "$channel"
    done
done

echo "== L3 aggregate products =="
for entry in "${L3_PRODUCTS[@]}"; do
    read -r level product channel hdf5_dir <<< "$entry"
    run_one "${level}_${product}@${channel}" \
        "$level" "$product" "$channel" "$hdf5_dir"
done

echo
echo "Summary: window=${HOURS}h  total=$n_total ok=$n_ok skip=$n_skip" \
     "pending=$n_pending fail=$n_fail err=$n_err"
if (( n_fail > 0 || n_err > 0 )); then
    printf 'FAILING: %s\n' "${failures[*]}"
    exit 1
fi
exit 0

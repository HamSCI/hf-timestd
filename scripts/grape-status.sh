#!/bin/bash
# GRAPE pipeline status report
# Shows decimation, spectrogram, packaging, and upload status for all dates
# Usage: bash scripts/grape-status.sh [--config /path/to/config.toml] [--log]
#   --config  Path to timestd-config.toml (default: /etc/hf-timestd/timestd-config.toml)
#   --log     Append output to /var/log/hf-timestd/grape-status.log

CONFIG="/etc/hf-timestd/timestd-config.toml"
while [[ $# -gt 0 ]]; do
    case $1 in
        --config) CONFIG="$2"; shift 2 ;;
        --log)    DO_LOG=true; shift ;;
        *)        shift ;;
    esac
done

PYTHON="/opt/hf-timestd/venv/bin/python3"
DATA_ROOT="/var/lib/timestd"
LOG_FILE="/var/log/hf-timestd/grape-status.log"

# Read channel count from config
CHANNELS=$($PYTHON -c "
import tomllib
with open('$CONFIG', 'rb') as f:
    cfg = tomllib.load(f)
n = len(cfg.get('recorder', {}).get('channel_group', {}).get('timestd', {}).get('channels', []))
print(n)
" 2>/dev/null || echo 9)

# Header
header() {
    echo "============================================================"
    echo "GRAPE Pipeline Status — $(date -u '+%Y-%m-%d %H:%M:%S UTC')"
    echo "============================================================"
    printf "%-10s  %-7s  %-7s  %-5s  %-5s  %s\n" "Date" "Dec" "Spec" "Pkg" "Upl" "Notes"
    echo "------------------------------------------------------------"
}

# Scan all dates that have any decimated data
scan() {
    local ok=0 partial=0 missing_upl=0

    for d in $(ls "$DATA_ROOT"/products/CHU_3330/decimated/*.bin 2>/dev/null | sed 's|.*/||;s|\.bin||' | sort); do
        dec=$(ls "$DATA_ROOT"/products/*/decimated/${d}.bin 2>/dev/null | wc -l)
        spec=$(ls "$DATA_ROOT"/products/*/spectrograms/${d}_spectrogram.png 2>/dev/null | wc -l)
        pkg="—"
        upl="—"
        notes=""

        if [ -d "$DATA_ROOT/upload/${d}" ]; then
            pkg="yes"
            if find "$DATA_ROOT/upload/${d}" -name ".upload_complete" 2>/dev/null | grep -q .; then
                upl="✓"
                ok=$((ok + 1))
            else
                upl="NO"
                missing_upl=$((missing_upl + 1))
                notes="packaged but not uploaded"
            fi
        fi

        if [ "$dec" -lt "$CHANNELS" ]; then
            notes="only $dec/$CHANNELS decimated"
            partial=$((partial + 1))
        elif [ "$spec" -lt "$CHANNELS" ] && [ "$upl" != "✓" ]; then
            if [ "$spec" -eq 0 ]; then
                notes="needs spectrograms"
            else
                notes="only $spec/$CHANNELS spectrograms"
            fi
        fi

        printf "%-10s  %d/%-5d  %d/%-5d  %-5s  %-5s  %s\n" \
            "$d" "$dec" "$CHANNELS" "$spec" "$CHANNELS" "$pkg" "$upl" "$notes"
    done

    echo "------------------------------------------------------------"
    local total=$(ls "$DATA_ROOT"/products/CHU_3330/decimated/*.bin 2>/dev/null | wc -l)
    echo "Total: $total dates | Uploaded: $ok | Partial: $partial | Pending upload: $missing_upl"
    echo ""

    # Check for dates with raw archive but no decimated data
    echo "Dates with raw archive data but no decimation:"
    for dir in "$DATA_ROOT"/raw_archive/CHU_3330/*/; do
        [ -d "$dir" ] || continue
        d=$(basename "$dir")
        if [ ! -f "$DATA_ROOT/products/CHU_3330/decimated/${d}.bin" ]; then
            raw_count=$(ls "$dir"/*.json 2>/dev/null | wc -l)
            if [ "$raw_count" -gt 100 ]; then
                echo "  $d ($raw_count minutes in raw archive)"
            fi
        fi
    done
}

# Main
if [[ "${DO_LOG:-false}" == "true" ]]; then
    mkdir -p "$(dirname "$LOG_FILE")"
    { header; scan; } | tee -a "$LOG_FILE"
    echo "(Appended to $LOG_FILE)"
else
    header
    scan
fi

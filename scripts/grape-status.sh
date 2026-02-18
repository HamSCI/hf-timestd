#!/bin/bash
# GRAPE pipeline status report
# Shows decimation, spectrogram, packaging, and upload status for all dates
# Usage: bash scripts/grape-status.sh [--log]
#   --log  Append output to logs/grape-status.log

DATA_ROOT="/var/lib/timestd"
LOG_FILE="/home/mjh/git/hf-timestd/logs/grape-status.log"
CHANNELS=9

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
if [ "$1" = "--log" ]; then
    mkdir -p "$(dirname "$LOG_FILE")"
    { header; scan; } | tee -a "$LOG_FILE"
    echo "(Appended to $LOG_FILE)"
else
    header
    scan
fi

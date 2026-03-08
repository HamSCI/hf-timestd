#!/bin/bash
# prune-old-data.sh — Age-aware retention for hf-timestd data directories
#
# Deletes data older than the configured retention periods to prevent disk full.
# Safe to run while services are active (never deletes today's or yesterday's files).
#
# Usage:
#   prune-old-data.sh [--dry-run] [--data-root DIR]
#
# Install: deploy-prune.sh copies this to /usr/local/bin/ and installs the
#          systemd timer (timestd-prune.timer) which runs it daily at 03:00 UTC.
#
# Retention defaults (override via /etc/hf-timestd/prune.conf if it exists):
#   RAW_BUFFER_DAYS=3        raw IQ data  (~20 GB/channel/day — largest)
#   SCIENCE_DTEC_TS_DAYS=7   dtec_timeseries (~2 GB/day — second largest)
#   SCIENCE_DTEC_DAYS=30     dtec aggregated (~70 MB/day)
#   SCIENCE_DTEC_DIFF_DAYS=30 dtec_diff (~90 MB/day)
#   SCIENCE_TEC_DAYS=90      tec (~2 MB/day — tiny, keep longer)
#   L1_DAYS=30               L1 metrology per-channel
#   L2_DAYS=30               L2 clock_offset per-channel
#   DISK_WARN_PCT=85         log warning when usage exceeds this
#   DISK_CRIT_PCT=92         skip non-critical deletes above this (keep more free space)

set -euo pipefail

# ── Defaults ────────────────────────────────────────────────────────────────
DATA_ROOT="/var/lib/timestd"
RAW_BUFFER_DAYS=3
SCIENCE_DTEC_TS_DAYS=7
SCIENCE_DTEC_DAYS=30
SCIENCE_DTEC_DIFF_DAYS=30
SCIENCE_TEC_DAYS=90
L1_DAYS=30
L2_DAYS=30
DISK_WARN_PCT=85
DISK_CRIT_PCT=92
DRY_RUN=0

# ── Load site overrides ──────────────────────────────────────────────────────
if [[ -f /etc/hf-timestd/prune.conf ]]; then
    # shellcheck disable=SC1091
    source /etc/hf-timestd/prune.conf
fi

# ── Argument parsing ─────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run) DRY_RUN=1 ;;
        --data-root) DATA_ROOT="$2"; shift ;;
        *) echo "Unknown argument: $1" >&2; exit 1 ;;
    esac
    shift
done

LOG_PREFIX="[timestd-prune]"
[[ $DRY_RUN -eq 1 ]] && LOG_PREFIX="[timestd-prune DRY-RUN]"

log() { echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') $LOG_PREFIX $*"; }

# ── Disk usage check ─────────────────────────────────────────────────────────
disk_pct() {
    df --output=pcent "$DATA_ROOT" 2>/dev/null | tail -1 | tr -d ' %'
}

USED_PCT=$(disk_pct)
log "Disk usage: ${USED_PCT}% of $(df -h --output=size "$DATA_ROOT" 2>/dev/null | tail -1 | tr -d ' ')"

if [[ "$USED_PCT" -ge "$DISK_WARN_PCT" ]]; then
    log "WARNING: disk usage ${USED_PCT}% >= warn threshold ${DISK_WARN_PCT}%"
fi

# When critically full, tighten all retention windows by 50% to recover space faster
if [[ "$USED_PCT" -ge "$DISK_CRIT_PCT" ]]; then
    log "CRITICAL: disk usage ${USED_PCT}% >= ${DISK_CRIT_PCT}% — halving all retention windows"
    RAW_BUFFER_DAYS=$(( RAW_BUFFER_DAYS / 2 < 1 ? 1 : RAW_BUFFER_DAYS / 2 ))
    SCIENCE_DTEC_TS_DAYS=$(( SCIENCE_DTEC_TS_DAYS / 2 < 1 ? 1 : SCIENCE_DTEC_TS_DAYS / 2 ))
    SCIENCE_DTEC_DAYS=$(( SCIENCE_DTEC_DAYS / 2 < 3 ? 3 : SCIENCE_DTEC_DAYS / 2 ))
    SCIENCE_DTEC_DIFF_DAYS=$(( SCIENCE_DTEC_DIFF_DAYS / 2 < 3 ? 3 : SCIENCE_DTEC_DIFF_DAYS / 2 ))
    SCIENCE_TEC_DAYS=$(( SCIENCE_TEC_DAYS / 2 < 14 ? 14 : SCIENCE_TEC_DAYS / 2 ))
    L1_DAYS=$(( L1_DAYS / 2 < 7 ? 7 : L1_DAYS / 2 ))
    L2_DAYS=$(( L2_DAYS / 2 < 7 ? 7 : L2_DAYS / 2 ))
fi

log "Retention: raw_buffer=${RAW_BUFFER_DAYS}d dtec_ts=${SCIENCE_DTEC_TS_DAYS}d dtec=${SCIENCE_DTEC_DAYS}d dtec_diff=${SCIENCE_DTEC_DIFF_DAYS}d tec=${SCIENCE_TEC_DAYS}d L1=${L1_DAYS}d L2=${L2_DAYS}d"

# ── Helper: delete files/dirs older than N days, never touching today ────────
# For raw_buffer the structure is: .../raw_buffer/CHANNEL/YYYYMMDD/
# For HDF5 science outputs: .../science/TYPE/AGGREGATED_*_YYYYMMDD.h5

prune_hdf5_dir() {
    local dir="$1"
    local days="$2"
    local label="$3"

    [[ -d "$dir" ]] || return 0

    local count=0
    local freed=0

    while IFS= read -r -d '' f; do
        local size
        size=$(stat -c%s "$f" 2>/dev/null || echo 0)
        if [[ $DRY_RUN -eq 0 ]]; then
            rm -f "$f"
        fi
        (( freed += size )) || true
        (( count += 1 )) || true
    done < <(find "$dir" -maxdepth 1 -name "*.h5" -mtime "+${days}" -print0 2>/dev/null)

    if [[ $count -gt 0 ]]; then
        local freed_mb=$(( freed / 1048576 ))
        log "${label}: removed ${count} file(s), freed ~${freed_mb} MB (retention: ${days}d)"
    else
        log "${label}: nothing to prune (retention: ${days}d)"
    fi
}

prune_raw_buffer_channel() {
    local channel_dir="$1"
    local days="$2"
    local channel
    channel=$(basename "$channel_dir")

    [[ -d "$channel_dir" ]] || return 0

    local count=0
    local freed=0

    # Structure: channel_dir/YYYYMMDD/ — prune entire day directories
    while IFS= read -r -d '' daydir; do
        local size
        size=$(du -sb "$daydir" 2>/dev/null | cut -f1 || echo 0)
        if [[ $DRY_RUN -eq 0 ]]; then
            rm -rf "$daydir"
        fi
        (( freed += size )) || true
        (( count += 1 )) || true
    done < <(find "$channel_dir" -maxdepth 1 -mindepth 1 -type d -mtime "+${days}" -print0 2>/dev/null)

    if [[ $count -gt 0 ]]; then
        local freed_gb=$(( freed / 1073741824 ))
        local freed_mb=$(( (freed % 1073741824) / 1048576 ))
        log "raw_buffer/${channel}: removed ${count} day-dir(s), freed ~${freed_gb}G ${freed_mb}M (retention: ${days}d)"
    fi
}

# ── 1. Raw IQ buffer ─────────────────────────────────────────────────────────
log "=== raw_buffer (${RAW_BUFFER_DAYS}d retention) ==="
RAW_DIR="$DATA_ROOT/raw_buffer"
if [[ -d "$RAW_DIR" ]]; then
    for channel_dir in "$RAW_DIR"/*/; do
        prune_raw_buffer_channel "$channel_dir" "$RAW_BUFFER_DAYS"
    done
else
    log "raw_buffer directory not found: $RAW_DIR"
fi

# ── 2. Science outputs ───────────────────────────────────────────────────────
SCIENCE_DIR="$DATA_ROOT/phase2/science"

log "=== dtec_timeseries (${SCIENCE_DTEC_TS_DAYS}d retention) ==="
prune_hdf5_dir "$SCIENCE_DIR/dtec_timeseries" "$SCIENCE_DTEC_TS_DAYS" "science/dtec_timeseries"

log "=== dtec (${SCIENCE_DTEC_DAYS}d retention) ==="
prune_hdf5_dir "$SCIENCE_DIR/dtec" "$SCIENCE_DTEC_DAYS" "science/dtec"

log "=== dtec_diff (${SCIENCE_DTEC_DIFF_DAYS}d retention) ==="
prune_hdf5_dir "$SCIENCE_DIR/dtec_diff" "$SCIENCE_DTEC_DIFF_DAYS" "science/dtec_diff"

log "=== tec (${SCIENCE_TEC_DAYS}d retention) ==="
prune_hdf5_dir "$SCIENCE_DIR/tec" "$SCIENCE_TEC_DAYS" "science/tec"

# ── 3. Per-channel L1 / L2 ───────────────────────────────────────────────────
PHASE2_DIR="$DATA_ROOT/phase2"

log "=== L1 metrology + L2 clock_offset (${L1_DAYS}d / ${L2_DAYS}d retention) ==="
for channel_dir in "$PHASE2_DIR"/*/; do
    channel=$(basename "$channel_dir")
    # Skip non-channel dirs
    case "$channel" in science|fusion) continue ;; esac

    prune_hdf5_dir "$channel_dir/metrology" "$L1_DAYS"    "L1/${channel}/metrology"
    prune_hdf5_dir "$channel_dir/clock_offset" "$L2_DAYS" "L2/${channel}/clock_offset"
done

# ── 4. Corrupt file cleanup ───────────────────────────────────────────────────
log "=== corrupt HDF5 files ==="
corrupt_count=0
while IFS= read -r -d '' f; do
    log "Removing corrupt file: $f"
    if [[ $DRY_RUN -eq 0 ]]; then
        rm -f "$f"
    fi
    (( corrupt_count += 1 )) || true
done < <(find "$DATA_ROOT/phase2" -name "*.h5.corrupt*" -print0 2>/dev/null)
[[ $corrupt_count -eq 0 ]] && log "No corrupt files found"

# ── Summary ───────────────────────────────────────────────────────────────────
USED_PCT_AFTER=$(disk_pct)
log "=== Done. Disk usage after: ${USED_PCT_AFTER}% (was ${USED_PCT}%)"

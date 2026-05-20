#!/bin/bash
# =============================================================================
# pipeline-watchdog.sh — Auto-detect and restart stuck pipeline services
# =============================================================================
# Runs every 5 minutes via systemd timer. Checks each service for:
#   1. Is it supposed to be running? (enabled)
#   2. Is it actually running? (active)
#   3. Is it producing fresh output?
#        - Recorder: newest binary chunk under raw_buffer (mtime check)
#        - Metrology / Fusion / Physics: newest row in the corresponding
#          SQLite table at $SQLITE_DB (post-Phase-3b: SQLite is the sole
#          writer; HDF5 mtimes are frozen and cannot indicate liveness)
#        - L2 calibration: state-file mtime
#        - Web API: HTTP /health
#
# If a service is running but its output is stale beyond the threshold,
# the watchdog restarts it. This catches "zombie" services that appear
# healthy to systemd but have stopped doing useful work.
#
# Usage:
#   ./scripts/pipeline-watchdog.sh           # normal mode (restarts)
#   ./scripts/pipeline-watchdog.sh --dry-run # report only, no restarts
# =============================================================================

set -uo pipefail

# ── Paths ──
DATA_ROOT="/var/lib/timestd"
SQLITE_DB="${SQLITE_DB:-$DATA_ROOT/phase2/timestd.db}"
LOG_TAG="timestd-watchdog"
DRY_RUN=false

[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=true

# ── Helpers ──
log_info()  { logger -t "$LOG_TAG" -p user.info  "$*"; }
log_warn()  { logger -t "$LOG_TAG" -p user.warning "$*"; }
log_error() { logger -t "$LOG_TAG" -p user.err "$*"; }

# Seconds since a file/dir was last modified. Returns 999999 if not found.
file_age() {
    local path="$1"
    if [[ -e "$path" ]]; then
        local mtime
        mtime=$(stat -c %Y "$path" 2>/dev/null) || { echo 999999; return; }
        echo $(( $(date +%s) - mtime ))
    else
        echo 999999
    fi
}

# Newest file modification time under a directory (recursive).
# Returns seconds since last modification, or 999999 if empty/missing.
newest_file_age() {
    local dir="$1"
    local pattern="${2:-*}"
    if [[ ! -d "$dir" ]]; then
        echo 999999
        return
    fi
    local newest
    newest=$(find "$dir" -name "$pattern" -type f -printf '%T@\n' 2>/dev/null | sort -rn | head -1)
    if [[ -z "$newest" ]]; then
        echo 999999
        return
    fi
    # newest is epoch float, truncate to int
    local newest_int=${newest%%.*}
    echo $(( $(date +%s) - newest_int ))
}

# Age in seconds of the newest row in a SQLite table, filtered to a time
# column that holds UNIX epoch seconds.  Returns 999999 if the DB is
# missing, the query fails, or the table has no rows newer than "future
# grace".
#
# Phase 3b cutover (2026-05-20): SQLite is the sole writer for the
# pipeline data products, so freshness lives here, not on HDF5 mtimes.
# The future-grace clause guards against historical L1_metrology rows
# with minute_boundary_utc dated ~20 min ahead of real time (relics of
# an earlier clock-confused run, rowids ~186k vs current ~221k) — those
# would otherwise mask any genuine stall by reporting a negative age.
#
# $1: table name
# $2: time column name (must be INTEGER epoch seconds — minute_boundary
#     or minute_boundary_utc on the current schemas)
# $3: optional extra WHERE clause (e.g., "channel='CHU_3330'"), no
#     leading AND.  Caller is responsible for shell-quoting; callers
#     here only pass channel names matching [A-Z0-9_]+ from the
#     filesystem listing or the case statement above.
sqlite_age() {
    local table="$1"
    local time_col="$2"
    local extra_where="${3:-}"
    if [[ ! -f "$SQLITE_DB" ]]; then
        echo 999999
        return
    fi
    local where="WHERE $time_col <= strftime('%s','now') + 120"
    [[ -n "$extra_where" ]] && where="$where AND $extra_where"
    local age
    age=$(sqlite3 -readonly "$SQLITE_DB" \
            "SELECT CAST(strftime('%s','now') - max($time_col) AS INTEGER) FROM $table $where;" \
            2>/dev/null)
    # NULL (empty table) or any sqlite error → stale.
    if [[ -z "$age" ]] || ! [[ "$age" =~ ^-?[0-9]+$ ]]; then
        echo 999999
        return
    fi
    # Negative ages can still appear inside the future-grace window
    # (rows dated up to 120 s ahead) — clamp so the threshold compare
    # below behaves as "fresh".
    (( age < 0 )) && age=0
    echo "$age"
}

# Check if a systemd unit is enabled and supposed to be running
is_enabled() {
    systemctl is-enabled --quiet "$1" 2>/dev/null
}

# Check if a systemd unit is active
is_active() {
    systemctl is-active --quiet "$1" 2>/dev/null
}

# Restart a service with logging
do_restart() {
    local unit="$1"
    local reason="$2"
    if [[ "$DRY_RUN" == "true" ]]; then
        log_warn "[DRY-RUN] Would restart $unit: $reason"
        echo "[DRY-RUN] Would restart $unit: $reason"
    else
        log_warn "Restarting $unit: $reason"
        systemctl reset-failed "$unit" 2>/dev/null || true
        systemctl restart "$unit" 2>/dev/null || true
    fi
}

RESTARTS=0

# ── Thresholds (seconds) ──
# Recorder flushes a chunk every file_duration_sec (default 600 = 10 min).
# Threshold must exceed one chunk duration plus normal flush jitter, otherwise
# a healthy recorder mid-chunk trips the watchdog and gets killed — which
# leaves the in-progress chunk overwritten with zeros on the next start.
RECORDER_STALE=900      # 15 min: > one 10-min chunk duration + flush jitter
# Phase 2: metrology reads from the ring buffer and produces HDF5 data
# every 60 s.  Lowered from 600 s (set when chunks were 10 min) to 180 s
# so genuine stalls trip the watchdog within ~3 minutes.
METROLOGY_STALE=180
FUSION_STALE=600        # 10 min: fusion writes every ~60s
PHYSICS_STALE=3600      # 1 hour: physics may write less often

# ==========================================================================
# Check 1: Core Recorder
# ==========================================================================
# Recorder writes binary files to raw_buffer or /dev/shm/timestd/raw_buffer
# Check hot buffer first (tiered storage), then cold storage
check_recorder() {
    local unit="timestd-core-recorder.service"
    if ! is_enabled "$unit"; then return; fi

    if ! is_active "$unit"; then
        do_restart "$unit" "not running but enabled"
        RESTARTS=$((RESTARTS + 1))
        return
    fi

    # Recorder writes *.bin / *.bin.zst / *.bin.lz4 plus a *.json sidecar
    # at chunk flush.  The previous "*.raw" glob never matched anything.
    local age=999999
    for buf_dir in /dev/shm/timestd/raw_buffer "$DATA_ROOT/raw_buffer"; do
        if [[ -d "$buf_dir" ]]; then
            for pattern in "*.bin" "*.bin.zst" "*.bin.lz4" "*.json"; do
                local a
                a=$(newest_file_age "$buf_dir" "$pattern")
                [[ $a -lt $age ]] && age=$a
            done
        fi
    done

    if [[ $age -gt $RECORDER_STALE ]]; then
        do_restart "$unit" "running but no output for ${age}s (threshold: ${RECORDER_STALE}s)"
        RESTARTS=$((RESTARTS + 1))
    fi
}

# ==========================================================================
# Check 2: Metrology Workers
# ==========================================================================
check_metrology() {
    local phase2="$DATA_ROOT/phase2"
    if [[ ! -d "$phase2" ]]; then return; fi

    # Clear stale target failed state
    if systemctl is-failed --quiet timestd-metrology.target 2>/dev/null; then
        systemctl reset-failed timestd-metrology.target 2>/dev/null || true
    fi

    # Check each channel directory for fresh HDF5 output
    for channel_dir in "$phase2"/*/; do
        [[ -d "$channel_dir" ]] || continue
        local channel
        channel=$(basename "$channel_dir")

        # Skip non-channel dirs (fusion, science, etc.)
        case "$channel" in
            fusion|science|state|calibration) continue ;;
        esac

        local unit="timestd-metrology@${channel}.service"
        if ! is_enabled "$unit" 2>/dev/null; then continue; fi

        if ! is_active "$unit"; then
            do_restart "$unit" "not running but enabled"
            RESTARTS=$((RESTARTS + 1))
            continue
        fi

        # Validate channel name before string-composing it into SQL.
        # Channel dirs are produced by the recorder/metrology services
        # and the case-statement above already filters non-channel
        # entries; this is belt-and-suspenders.
        if ! [[ "$channel" =~ ^[A-Za-z0-9_]+$ ]]; then
            log_warn "skipping channel with unexpected name: $channel"
            continue
        fi

        local age
        age=$(sqlite_age "L1_metrology_measurements" "minute_boundary_utc" \
                        "channel='$channel'")
        if [[ $age -gt $METROLOGY_STALE ]]; then
            do_restart "$unit" "running but L1_metrology row for $channel stale for ${age}s (threshold: ${METROLOGY_STALE}s)"
            RESTARTS=$((RESTARTS + 1))
        fi
    done
}

# ==========================================================================
# Check 3: Fusion
# ==========================================================================
check_fusion() {
    local unit="timestd-fusion.service"
    if ! is_enabled "$unit"; then return; fi

    if ! is_active "$unit"; then
        do_restart "$unit" "not running but enabled"
        RESTARTS=$((RESTARTS + 1))
        return
    fi

    local age
    age=$(sqlite_age "L3_fusion_timing" "minute_boundary")
    if [[ $age -gt $FUSION_STALE ]]; then
        do_restart "$unit" "running but L3_fusion_timing stale for ${age}s (threshold: ${FUSION_STALE}s)"
        RESTARTS=$((RESTARTS + 1))
    fi
}

# ==========================================================================
# Check 4: Physics (TEC)
# ==========================================================================
check_physics() {
    local unit="timestd-physics.service"
    if ! is_enabled "$unit"; then return; fi

    if ! is_active "$unit"; then
        do_restart "$unit" "not running but enabled"
        RESTARTS=$((RESTARTS + 1))
        return
    fi

    # L3_tec covers both AGGREGATED and REANALYZED channels — physics
    # producing either keeps the table fresh, so no channel filter.
    local age
    age=$(sqlite_age "L3_tec" "minute_boundary")
    if [[ $age -gt $PHYSICS_STALE ]]; then
        do_restart "$unit" "running but L3_tec stale for ${age}s (threshold: ${PHYSICS_STALE}s)"
        RESTARTS=$((RESTARTS + 1))
    fi
}

# ==========================================================================
# Check 5: L2 Calibration
# ==========================================================================
check_calibration() {
    local unit="timestd-l2-calibration.service"
    if ! is_enabled "$unit"; then return; fi

    if ! is_active "$unit"; then
        do_restart "$unit" "not running but enabled"
        RESTARTS=$((RESTARTS + 1))
        return
    fi

    # Calibration state file should be updated frequently
    local state_file="$DATA_ROOT/state/broadcast_calibration.json"
    local age
    age=$(file_age "$state_file")
    if [[ $age -gt $METROLOGY_STALE ]]; then
        do_restart "$unit" "running but calibration state stale for ${age}s"
        RESTARTS=$((RESTARTS + 1))
    fi
}

# ==========================================================================
# Check 6: Web API
# ==========================================================================
check_webapi() {
    local unit="timestd-web-api.service"
    if ! is_enabled "$unit"; then return; fi

    if ! is_active "$unit"; then
        do_restart "$unit" "not running but enabled"
        RESTARTS=$((RESTARTS + 1))
        return
    fi

    # Quick HTTP health check
    if ! curl -sf -o /dev/null --max-time 5 http://localhost:8000/health 2>/dev/null; then
        # Try root path as fallback
        if ! curl -sf -o /dev/null --max-time 5 http://localhost:8000/ 2>/dev/null; then
            do_restart "$unit" "running but HTTP health check failed"
            RESTARTS=$((RESTARTS + 1))
        fi
    fi
}

# ==========================================================================
# Run all checks
# ==========================================================================
check_recorder
check_metrology
check_fusion
check_physics
check_calibration
check_webapi

if [[ $RESTARTS -gt 0 ]]; then
    log_warn "Watchdog: restarted $RESTARTS service(s)"
    echo "Watchdog: restarted $RESTARTS service(s)"
else
    log_info "Watchdog: all services healthy"
fi

exit 0

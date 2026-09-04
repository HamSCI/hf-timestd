#!/bin/bash
# =============================================================================
# pipeline-watchdog.sh — Auto-detect and restart stuck pipeline services
# =============================================================================
# Runs every 5 minutes via systemd timer. Checks each service for:
#   1. Is it supposed to be running? (enabled)
#   2. Is it actually running? (active)
#   3. Is it still doing its work?  LIVENESS, not detections (2026-09-04):
#        - Recorder: newest binary chunk under raw_buffer (mtime check)
#        - Metrology: newest row per channel in ANY of L2_detection_attempts,
#          L1_all_arrivals, L1_metrology_measurements at $SQLITE_DB.  The
#          first two land every processed minute; an L1 row lands only when
#          the marker correlator detects, which on a quiet channel is a few
#          minutes per hour.
#        - Fusion: mtime of $RUN_DIR/fusion_status.json (written every cycle
#          with or without input); L3 rows only where that file never existed
#        - Physics: enabled/active only -- systemd WatchdogSec covers a hang
#        - L2 calibration: enabled/active only -- systemd WatchdogSec covers
#          a hung loop, and the state file this once read belongs to fusion
#        - Web API: HTTP /health
#
# If a service is running but has stopped processing beyond the threshold,
# the watchdog restarts it. This catches "zombie" services that appear
# healthy to systemd but have stopped doing work.  A service that processes
# every minute and detects nothing is healthy; the ionosphere is not a fault.
#
# Usage:
#   ./scripts/pipeline-watchdog.sh           # normal mode (restarts)
#   ./scripts/pipeline-watchdog.sh --dry-run # report only, no restarts
# =============================================================================

set -uo pipefail

# ── Paths ──
DATA_ROOT="${DATA_ROOT:-/var/lib/timestd}"
RUN_DIR="${RUN_DIR:-/run/hf-timestd}"
SQLITE_DB="${SQLITE_DB:-$DATA_ROOT/phase2/timestd.db}"
LOG_TAG="timestd-watchdog"
DRY_RUN=false

[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=true

# ── Helpers ──
log_info()  { logger -t "$LOG_TAG" -p user.info  "$*"; }
log_warn()  { logger -t "$LOG_TAG" -p user.warning "$*"; }
log_error() { logger -t "$LOG_TAG" -p user.err "$*"; }

# True when sqlite_age() could not determine an age at all.  Distinct from a
# genuinely empty/stale table, which yields a real number.  An unknown must
# never be treated as evidence of a stall.
age_unknown() { [[ "$1" == "UNKNOWN" ]]; }

# Fail loudly and early if the freshness queries cannot run at all, rather
# than letting every check silently degrade into "stale".
if ! command -v sqlite3 >/dev/null 2>&1; then
    log_error "sqlite3 not installed: SQLite freshness checks cannot run; \
data-driven restarts are DISABLED this pass (install the sqlite3 package)"
fi

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
    local age rc
    age=$(sqlite3 -readonly "$SQLITE_DB" \
            "SELECT CAST(strftime('%s','now') - max($time_col) AS INTEGER) FROM $table $where;" \
            2>/dev/null)
    rc=$?
    # A failed query is NOT evidence of a stall.  sqlite3 missing (rc 127),
    # an unreadable/locked DB or a bad column all land here, and restarting
    # healthy services because our own query tool broke is exactly the
    # failure this watchdog is supposed to prevent -- it did precisely that
    # on B4 for hours when sqlite3 was not installed, reporting every table
    # as "stale for 999999s" while the data was fresh to the second.
    # Report UNKNOWN so callers can alarm instead of act.
    if (( rc != 0 )); then
        echo UNKNOWN
        return
    fi
    # Empty output is a genuine NULL from max() -- the table really has no
    # qualifying rows, which IS staleness.
    if [[ -z "$age" ]]; then
        echo 999999
        return
    fi
    if ! [[ "$age" =~ ^-?[0-9]+$ ]]; then
        echo UNKNOWN
        return
    fi
    # Negative ages can still appear inside the future-grace window
    # (rows dated up to 120 s ahead) — clamp so the threshold compare
    # below behaves as "fresh".
    (( age < 0 )) && age=0
    echo "$age"
}

# Check if a systemd unit is enabled and supposed to be running
# Per-channel age from the ISO write-time column, seconds.  hamsci-dsp
# indexes every L1/L2 table on (channel, timestamp_utc), so max(timestamp_utc)
# for one channel is an index-only lookup (~15 ms).  max(minute_boundary_utc)
# with a channel filter is NOT: on AC0G-B4 (2026-09-04) it scanned the
# channel's 5 M L1_all_arrivals rows in 35 s, six channels per tick, and the
# watchdog service outlived its own timer.  Same contract as sqlite_age():
# UNKNOWN when the query fails, 999999 when there is no row.
sqlite_channel_age() {
    local table="$1"
    local channel="$2"
    if [[ ! -f "$SQLITE_DB" ]]; then
        echo 999999
        return
    fi
    local age rc
    age=$(sqlite3 -readonly "$SQLITE_DB" \
            "SELECT CAST(strftime('%s','now') - strftime('%s', max(timestamp_utc)) AS INTEGER) \
             FROM $table WHERE channel='$channel' \
               AND timestamp_utc <= strftime('%Y-%m-%dT%H:%M:%f','now','+120 seconds');" \
            2>/dev/null)
    rc=$?
    if (( rc != 0 )); then
        echo UNKNOWN
        return
    fi
    if [[ -z "$age" ]]; then
        echo 999999
        return
    fi
    if ! [[ "$age" =~ ^-?[0-9]+$ ]]; then
        echo UNKNOWN
        return
    fi
    (( age < 0 )) && age=0
    echo "$age"
}

# Smallest of several ages, ignoring UNKNOWN; UNKNOWN only if all are.
min_known_age() {
    local best="UNKNOWN" a
    for a in "$@"; do
        age_unknown "$a" && continue
        if age_unknown "$best" || [[ $a -lt $best ]]; then best=$a; fi
    done
    echo "$best"
}

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

        # Liveness, not detections (2026-09-04).  An L1_metrology row exists
        # only when the 800 ms marker correlator DETECTS, and on AC0G-B4 that
        # is 1-5 minutes per hour.  Until 41d052a the noise tick ensembles
        # were promoted into two L1 rows every minute and hid that; the day
        # they stopped, this rule restarted every sparse channel every 5 min
        # on both stations.  A running service writes L2_detection_attempts
        # (every correlator attempt) and L1_all_arrivals (every edge search)
        # for each minute it processes, detection or not (all_arrivals every
        # minute in practice; attempts only when the engine reports them).
        # The service is alive if ANY of the three is fresh; it is restarted
        # only when all three are stale, i.e. no minute has been processed.
        # A per-channel heartbeat from the service itself would be the
        # principled signal; until it exists, these rows stand in for it.
        local age_meas age_att age_arr age
        age_meas=$(sqlite_channel_age "L1_metrology_measurements" "$channel")
        age_att=$(sqlite_channel_age "L2_detection_attempts" "$channel")
        age_arr=$(sqlite_channel_age "L1_all_arrivals" "$channel")
        age=$(min_known_age "$age_meas" "$age_att" "$age_arr")
        if age_unknown "$age"; then
            log_error "cannot assess metrology liveness for $channel (every SQLite query failed) - NOT restarting $unit"
        elif [[ $age -gt $METROLOGY_STALE ]]; then
            do_restart "$unit" "running but no processed minute for $channel in ${age}s (attempts=${age_att}s arrivals=${age_arr}s measurements=${age_meas}s; threshold: ${METROLOGY_STALE}s)"
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

    # Liveness, not output (2026-09-04).  Fusion writes an L3 row only when
    # it has L1/L2 input; with none it is idle, not dead, and a restart
    # throws away its convergence state ("Fusion not converged, skipping
    # calibration save").  It publishes $RUN_DIR/fusion_status.json every
    # cycle (~8 s) whatever the input, and systemd's WatchdogSec=120 on the
    # unit already catches a hung main loop.  Judge by the status file; fall
    # back to the L3 rule only where no status file has ever been written.
    local status_file="$RUN_DIR/fusion_status.json"
    local age
    if [[ -e "$status_file" ]]; then
        age=$(file_age "$status_file")
        if [[ $age -gt $FUSION_STALE ]]; then
            do_restart "$unit" "running but fusion_status.json not updated for ${age}s (threshold: ${FUSION_STALE}s)"
            RESTARTS=$((RESTARTS + 1))
        fi
        return
    fi
    log_warn "no $status_file; judging fusion by L3_fusion_timing rows (output, not liveness)"
    age=$(sqlite_age "L3_fusion_timing" "minute_boundary")
    if age_unknown "$age"; then
        log_error "cannot assess L3_fusion_timing freshness (SQLite query failed) - NOT restarting $unit"
    elif [[ $age -gt $FUSION_STALE ]]; then
        do_restart "$unit" "running but L3_fusion_timing stale for ${age}s (threshold: ${FUSION_STALE}s)"
        RESTARTS=$((RESTARTS + 1))
    fi
}

# ==========================================================================
# Check 4: Physics (TEC)
# ==========================================================================
check_physics() {
    local unit="hamsci-physics-fusion.service"   # moved out in the 2026-08-24 split
    if ! is_enabled "$unit"; then return; fi

    if ! is_active "$unit"; then
        do_restart "$unit" "not running but enabled"
        RESTARTS=$((RESTARTS + 1))
        return
    fi

    # L3_tec covers both AGGREGATED and REANALYZED channels — physics
    # producing either keeps the table fresh, so no channel filter.
    # No data-driven rule (2026-09-04).  An L3_tec row needs L2 input; when
    # the metrology chain is quiet the physics service is idle, not dead, and
    # this rule restarted it on every 5-minute tick on AC0G-B4 (the age only
    # grows).  The unit runs WatchdogSec=120 and pets it from its main loop
    # and its long passes; systemd restarts it if the loop hangs.
    :
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

    # No data-driven rule (2026-09-04).  The file this check used to read,
    # $DATA_ROOT/state/broadcast_calibration.json, is written by FUSION
    # (multi_broadcast_fusion.py), and only once fusion has converged -- so
    # this rule restarted L2-calibration for fusion's silence.  The unit
    # runs Type=notify with WatchdogSec=180 and pings WATCHDOG=1 every loop;
    # systemd already restarts it if the loop hangs.  Enabled-but-inactive
    # (above) is the only thing left for this script to catch.
    :
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

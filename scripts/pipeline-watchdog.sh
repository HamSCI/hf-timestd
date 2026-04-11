#!/bin/bash
# =============================================================================
# pipeline-watchdog.sh — Auto-detect and restart stuck pipeline services
# =============================================================================
# Runs every 5 minutes via systemd timer. Checks each service for:
#   1. Is it supposed to be running? (enabled)
#   2. Is it actually running? (active)
#   3. Is it producing fresh output? (file mtime checks)
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
RECORDER_STALE=300      # 5 min: recorder should write every ~20s
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

    # Check hot buffer (tiered) then cold buffer
    local age=999999
    for buf_dir in /dev/shm/timestd/raw_buffer "$DATA_ROOT/raw_buffer"; do
        if [[ -d "$buf_dir" ]]; then
            local a
            a=$(newest_file_age "$buf_dir" "*.raw")
            [[ $a -lt $age ]] && age=$a
        fi
    done

    # Also check .json sidecars
    for buf_dir in /dev/shm/timestd/raw_buffer "$DATA_ROOT/raw_buffer"; do
        if [[ -d "$buf_dir" ]]; then
            local a
            a=$(newest_file_age "$buf_dir" "*.json")
            [[ $a -lt $age ]] && age=$a
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

        local age
        age=$(newest_file_age "$channel_dir" "*.h5")
        if [[ $age -gt $METROLOGY_STALE ]]; then
            do_restart "$unit" "running but HDF5 stale for ${age}s (threshold: ${METROLOGY_STALE}s)"
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
    age=$(newest_file_age "$DATA_ROOT/phase2/fusion" "*.h5")
    if [[ $age -gt $FUSION_STALE ]]; then
        do_restart "$unit" "running but fusion HDF5 stale for ${age}s (threshold: ${FUSION_STALE}s)"
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

    local age
    age=$(newest_file_age "$DATA_ROOT/phase2/science" "*.h5")
    if [[ $age -gt $PHYSICS_STALE ]]; then
        do_restart "$unit" "running but TEC HDF5 stale for ${age}s (threshold: ${PHYSICS_STALE}s)"
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

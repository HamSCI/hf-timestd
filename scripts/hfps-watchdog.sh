#!/bin/bash
# /opt/git/sigmond/hf-timestd/scripts/hfps-watchdog.sh
#
# Restart timestd-core-recorder if HFPS (T6 diff-detector SHM
# refclock) goes dark.  Apples-to-apples sibling of hpps-watchdog.sh
# — same failure modes can wedge the SHM push thread for either
# detector, so each gets its own LastRx watchdog.
#
# The diff calibrator itself has an internal self-recovery path
# (see DIFF_REJECT_RECOVERY_THRESHOLD in bpsk_pps_calibrator_diff.py)
# that handles transient state-wedges within ~30s without a service
# restart.  This watchdog is the backstop for failures upstream of
# the calibrator: stalled SHM push thread, dropped multicast,
# downstream Python exception path that swallows the failure.
#
# Detection: LastRx > LASTRX_THRESHOLD_S means chrony hasn't sampled
# HFPS within the threshold — that's a definitive sign HFPS is dark
# regardless of what the journal says.
#
# Throttling: a state file under STATE_DIR records the timestamp of
# the last restart so we don't thrash if there's a deeper problem.
# Cooldown is shared in spirit with hpps-watchdog: a restart from
# either side resets both feeds (they live in the same process), so
# don't trigger another within the cooldown window.
#
# Environment variables:
#   HFPS_LASTRX_THRESHOLD_S   - dark-source restart threshold (default 600)
#   HFPS_RESTART_COOLDOWN_S   - minimum gap between auto-restarts (default 1800)
#   HFPS_STATE_DIR            - cooldown state file directory
#
# Exit codes:
#   0 - HFPS healthy, OR restart attempted, OR cooldown active
#   1 - chronyc query failed (transient — let systemd retry next tick)
#   2 - state file write failed (operator should investigate)

set -euo pipefail

LASTRX_THRESHOLD_S="${HFPS_LASTRX_THRESHOLD_S:-600}"
COOLDOWN_S="${HFPS_RESTART_COOLDOWN_S:-1800}"
STATE_DIR="${HFPS_STATE_DIR:-/var/lib/hf-timestd}"
STATE_FILE="$STATE_DIR/hfps-watchdog-last-restart"
LOG_TAG="hfps-watchdog"
TARGET_UNIT="timestd-core-recorder.service"

log() { logger -t "$LOG_TAG" -- "$@"; echo "[$LOG_TAG] $*"; }

lastrx_seconds() {
    local out
    out="$(chronyc -n sources 2>/dev/null)" || return 1
    local csv
    csv="$(chronyc -n -c sources 2>/dev/null)" || return 1
    # CSV columns: M,S,Name,Stratum,Poll,Reach,LastRx,LastSample,...
    # We want field 7 (LastRx) for the HFPS row.
    local lastrx
    lastrx="$(printf '%s\n' "$csv" \
              | awk -F, '$3 == "HFPS" { print $7; exit }')"
    if [ -z "$lastrx" ]; then
        printf 'INF\n'
        return 0
    fi
    printf '%s\n' "$lastrx"
}

cooldown_active() {
    [ -f "$STATE_FILE" ] || return 1
    local last_restart now elapsed
    last_restart="$(cat "$STATE_FILE" 2>/dev/null || echo 0)"
    now="$(date -u +%s)"
    elapsed=$(( now - last_restart ))
    [ "$elapsed" -lt "$COOLDOWN_S" ]
}

record_restart() {
    mkdir -p "$STATE_DIR" 2>/dev/null || return 2
    date -u +%s > "$STATE_FILE" 2>/dev/null || return 2
    chmod 0644 "$STATE_FILE" 2>/dev/null || true
}

main() {
    local lastrx
    lastrx="$(lastrx_seconds)" || {
        log "chronyc query failed; exiting cleanly so timer retries"
        return 1
    }

    if [ "$lastrx" = "INF" ]; then
        log "HFPS row missing or in transient state; treating as dark"
        lastrx="$LASTRX_THRESHOLD_S"
    fi

    if [ "$lastrx" -lt "$LASTRX_THRESHOLD_S" ]; then
        return 0
    fi

    log "HFPS LastRx=${lastrx}s exceeds threshold ${LASTRX_THRESHOLD_S}s"

    if cooldown_active; then
        log "restart cooldown active (last restart < ${COOLDOWN_S}s ago); skipping"
        return 0
    fi

    log "restarting $TARGET_UNIT to recover HFPS SHM"
    if systemctl restart "$TARGET_UNIT"; then
        if record_restart; then
            log "restart issued; state file updated"
        else
            log "WARNING: restart issued but state file write failed — cooldown disabled"
            return 2
        fi
    else
        log "ERROR: systemctl restart $TARGET_UNIT failed"
        return 1
    fi
}

main "$@"

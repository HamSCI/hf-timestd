#!/bin/bash
# /opt/hf-timestd/scripts/tsl3-watchdog.sh
#
# Restart timestd-core-recorder if TSL3 (T6 BPSK SHM refclock) goes
# dark.  Targets the specific failure mode where the matched-filter
# / calibrator keeps reporting `acquired=1, pps_consec>0` in the log,
# but the SHM push gate stops firing — chrony silently sees reach=0
# while everything LOOKS fine in the journal.
#
# Observed first on bee1 2026-05-12 ~07:01 UTC after ~5 hours of
# runtime; a `systemctl restart timestd-core-recorder` brought TSL3
# back within seconds.  This script automates that.
#
# Detection: LastRx > LASTRX_THRESHOLD_S means chrony hasn't sampled
# TSL3 within the threshold — that's the symptom that confirmed the
# failure during the incident.  reach=0 alone is noisier (it can
# happen transiently on chrony restart); LastRx is monotone since
# the last good sample.
#
# Throttling: a state file under STATE_DIR records the timestamp of
# the last restart so we don't thrash if there's a deeper problem
# (e.g., radiod completely missing).  Default cooldown is 5 minutes.
#
# Exit codes:
#   0 - TSL3 healthy, OR restart attempted, OR cooldown active
#   1 - chronyc query failed (transient — let systemd retry next tick)
#   2 - state file write failed (operator should investigate)

set -euo pipefail

LASTRX_THRESHOLD_S="${TSL3_LASTRX_THRESHOLD_S:-120}"
COOLDOWN_S="${TSL3_RESTART_COOLDOWN_S:-300}"
STATE_DIR="${TSL3_STATE_DIR:-/var/lib/hf-timestd}"
STATE_FILE="$STATE_DIR/tsl3-watchdog-last-restart"
LOG_TAG="tsl3-watchdog"
TARGET_UNIT="timestd-core-recorder.service"

log() { logger -t "$LOG_TAG" -- "$@"; echo "[$LOG_TAG] $*"; }

# Parse `chronyc -n sources` for the HPPS row.  Format (chrony 4.x):
#   MS Name/IP address    Stratum Poll Reach LastRx Last sample
#   #* HPPS                  0    0  377     1   -40us[ -14us] +/-   55us
#
# We want LastRx (col 6 when MS counts as one token).  `awk` with the
# # filter on the first column gets that.
lastrx_seconds() {
    local out
    out="$(chronyc -n sources 2>/dev/null)" || return 1
    # Use chrony's -c flag (comma-separated machine-parseable output)
    # — same fields, less brittle than awk on the human form.
    local csv
    csv="$(chronyc -n -c sources 2>/dev/null)" || return 1
    # CSV columns: M,S,Name,Stratum,Poll,Reach,LastRx,LastSample,...
    # We want field 7 (LastRx) for the HPPS row.
    local lastrx
    lastrx="$(printf '%s\n' "$csv" \
              | awk -F, '$3 == "HPPS" { print $7; exit }')"
    if [ -z "$lastrx" ]; then
        # Unit might be in a transient state ("-" or empty) — treat as
        # "no recent sample" to be conservative.  But also tolerate
        # chrony's textual "-" by mapping it to a high number.
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
        log "HPPS row missing or in transient state; treating as dark"
        lastrx="$LASTRX_THRESHOLD_S"
    fi

    if [ "$lastrx" -lt "$LASTRX_THRESHOLD_S" ]; then
        # Healthy — nothing to do.  Quiet exit (no log spam every minute).
        return 0
    fi

    log "TSL3 LastRx=${lastrx}s exceeds threshold ${LASTRX_THRESHOLD_S}s"

    if cooldown_active; then
        log "restart cooldown active (last restart < ${COOLDOWN_S}s ago); skipping"
        return 0
    fi

    log "restarting $TARGET_UNIT to recover T6 SHM"
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

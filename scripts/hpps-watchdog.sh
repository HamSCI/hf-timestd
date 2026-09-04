#!/bin/bash
# /opt/git/sigmond/hf-timestd/scripts/hpps-watchdog.sh
#
# Restart timestd-core-recorder if HPPS (T6 BPSK SHM refclock) goes
# dark.  Targets the specific failure mode where the matched-filter
# / calibrator keeps reporting `acquired=1, pps_consec>0` in the log,
# but the SHM push gate stops firing — chrony silently sees reach=0
# while everything LOOKS fine in the journal.
#
# Observed first on bee1 2026-05-12 ~07:01 UTC after ~5 hours of
# runtime; a `systemctl restart timestd-core-recorder` brought HPPS
# back within seconds.  This script automates that.
#
# Detection: LastRx > LASTRX_THRESHOLD_S means chrony hasn't sampled
# HPPS within the threshold — that's the symptom that confirmed the
# failure during the incident.  reach=0 alone is noisier (it can
# happen transiently on chrony restart); LastRx is monotone since
# the last good sample.
#
# Throttling: a state file under STATE_DIR records the timestamp of
# the last restart so we don't thrash if there's a deeper problem
# (e.g., radiod completely missing).  Default cooldown is 5 minutes.
#
# Environment variables (also accepts legacy TSL3_* names for
# in-flight rename compatibility — drop the legacy names after one
# stable week of operation):
#   HPPS_LASTRX_THRESHOLD_S   - dark-source restart threshold (default 120)
#   HPPS_RESTART_COOLDOWN_S   - minimum gap between auto-restarts (default 300)
#   HPPS_STATE_DIR            - cooldown state file directory
#   HPPS_STATUS_FILE          - producer status surface (authority.json)
#   HPPS_WITHDRAWN_GRACE_S    - how long to tolerate an HONEST withdrawal
#                               before restarting anyway (default 3600)
#
# Exit codes:
#   0 - HPPS healthy, OR restart attempted, OR cooldown active
#   1 - chronyc query failed (transient — let systemd retry next tick)
#   2 - state file write failed (operator should investigate)

set -euo pipefail

LASTRX_THRESHOLD_S="${HPPS_LASTRX_THRESHOLD_S:-${TSL3_LASTRX_THRESHOLD_S:-120}}"
COOLDOWN_S="${HPPS_RESTART_COOLDOWN_S:-${TSL3_RESTART_COOLDOWN_S:-300}}"
STATE_DIR="${HPPS_STATE_DIR:-${TSL3_STATE_DIR:-/var/lib/hf-timestd}}"
STATE_FILE="$STATE_DIR/hpps-watchdog-last-restart"
STATUS_FILE="${HPPS_STATUS_FILE:-/run/hf-timestd/authority.json}"
WITHDRAWN_GRACE_S="${HPPS_WITHDRAWN_GRACE_S:-3600}"
WITHDRAWN_FILE="$STATE_DIR/hpps-watchdog-withdrawn-since"
LOG_TAG="hpps-watchdog"
TARGET_UNIT="timestd-core-recorder.service"

log() { logger -t "$LOG_TAG" -- "$@"; echo "[$LOG_TAG] $*"; }

# Config gate: if the T6 BPSK PPS chain is disabled in config, HPPS can
# never feed and LastRx is infinite BY DESIGN - restarting the recorder
# "for" it just bounces the raw-archive chunks every cooldown period
# forever (observed on B4 2026-07-24: a 30-min core-recorder bounce loop
# corrupting 10-min archive chunks; same flap that got the HFPS twin
# retired 2026-06-28 and deleted 2026-09-04 with the diff-detector feed).  Accept both the canonical [timing.t6_pps] and
# the legacy [timing.l6_pps] section names; treat missing config or a
# missing enabled key as disabled - a host that never configured T6
# has no HPPS feed to guard.
CONFIG_FILE="${HPPS_CONFIG_FILE:-/etc/hf-timestd/timestd-config.toml}"
t6_enabled() {
    [[ -r "$CONFIG_FILE" ]] || return 1
    awk '
        /^\[timing\.(t6|l6)_pps\]/ { in_t6 = 1; next }
        /^\[/                        { in_t6 = 0 }
        in_t6 && /^[[:space:]]*enabled[[:space:]]*=[[:space:]]*true/ { found = 1 }
        END { exit found ? 0 : 1 }
    ' "$CONFIG_FILE"
}
if ! t6_enabled; then
    log "T6/HPPS disabled in $CONFIG_FILE; nothing to guard - exiting"
    exit 0
fi

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

# Does the producer BELIEVE it is feeding HPPS right now?
#
# This is the distinction the watchdog lacked.  Restarting the recorder
# is the right cure for the failure this script was built for -- the SHM
# push gate wedges while the calibrator still reports acquired=1, so the
# journal looks healthy and chrony silently sees reach=0.  It is the
# WRONG cure for an honest withdrawal: since the holdover coast landed,
# the producer says when it is deliberately not feeding, and a restart
# then destroys the frozen anchor the coast rests on and forces a fresh
# acquisition -- which guarantees another dark window and another
# restart.  On AC0G-B4 overnight 2026-08-17 that loop bounced the
# recorder 7 times.
#
# "unknown" (key absent) keeps the legacy behaviour, so an older
# producer is unaffected.
hpps_publishing() {
    local raw
    [ -r "$STATUS_FILE" ] || { printf 'unknown\n'; return 0; }
    raw="$(grep -o '"t6_hpps_publishing"[[:space:]]*:[[:space:]]*\(true\|false\)' \
           "$STATUS_FILE" 2>/dev/null | tail -1 || true)"
    case "$raw" in
        *true)  printf 'true\n' ;;
        *false) printf 'false\n' ;;
        *)      printf 'unknown\n' ;;
    esac
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
        rm -f "$WITHDRAWN_FILE" 2>/dev/null || true
        return 0
    fi

    log "HPPS LastRx=${lastrx}s exceeds threshold ${LASTRX_THRESHOLD_S}s"

    local pub now first elapsed
    pub="$(hpps_publishing)"
    if [ "$pub" = "false" ]; then
        now="$(date -u +%s)"
        if [ -f "$WITHDRAWN_FILE" ]; then
            first="$(cat "$WITHDRAWN_FILE" 2>/dev/null || echo "$now")"
        else
            first="$now"
            printf '%s\n' "$now" > "$WITHDRAWN_FILE" 2>/dev/null || true
        fi
        elapsed=$(( now - first ))
        if [ "$elapsed" -lt "$WITHDRAWN_GRACE_S" ]; then
            log "HPPS withdrawn BY THE PRODUCER for ${elapsed}s (not a wedged push gate; see the recorder journal for the reason) — a restart would destroy the anchor, holding off until ${WITHDRAWN_GRACE_S}s"
            return 0
        fi
        log "HPPS withdrawn for ${elapsed}s exceeds grace ${WITHDRAWN_GRACE_S}s — restarting as a last resort"
    else
        rm -f "$WITHDRAWN_FILE" 2>/dev/null || true
    fi

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

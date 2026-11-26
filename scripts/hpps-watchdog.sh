#!/bin/bash
# /opt/git/sigmond/hf-timestd/scripts/hpps-watchdog.sh
#
# Report a dark HPPS (T6 BPSK SHM refclock) to sigmond-recorder-watchdog,
# which alone restarts timestd-core-recorder.  This script files a
# request (cause hpps-stale) and never restarts the recorder itself.
# sigmond records the request and does not act on it: a stale HPPS is a
# downstream symptom, and a restart once cost four data gaps in a night.  Targets the specific failure mode where the matched-filter
# / calibrator keeps reporting `acquired=1, pps_consec>0` in the log,
# but the SHM push gate stops firing — chrony silently sees reach=0
# while everything LOOKS fine in the journal.
#
# Observed first on bee1 2026-05-12 ~07:01 UTC after ~5 hours of
# runtime; a `systemctl restart timestd-core-recorder` brought HPPS
# back within seconds.  This script used to automate that restart.
#
# Detection: LastRx > LASTRX_THRESHOLD_S means chrony hasn't sampled
# HPPS within the threshold — that's the symptom that confirmed the
# failure during the incident.  reach=0 alone is noisier (it can
# happen transiently on chrony restart); LastRx is monotone since
# the last good sample.
#
# Throttling: a state file under STATE_DIR records the timestamp of
# the last request so we don't repeat ourselves while a deeper problem
# lasts (e.g., radiod completely missing).  Default cooldown is 5 minutes.
#
# Environment variables (also accepts legacy TSL3_* names for
# in-flight rename compatibility — drop the legacy names after one
# stable week of operation):
#   HPPS_LASTRX_THRESHOLD_S   - dark-source restart threshold (default 120)
#   HPPS_RESTART_COOLDOWN_S   - minimum gap between restart requests (default 300)
#   HPPS_REQUEST_DIR          - where the request file goes
#                               (default /run/sigmond/recorder-restart)
#   HPPS_STATE_DIR            - cooldown state file directory
#   HPPS_STATUS_FILE          - producer status surface (authority.json)
#   HPPS_WITHDRAWN_GRACE_S    - how long to tolerate an HONEST withdrawal
#                               before requesting a restart anyway (default 3600)
#   HPPS_RECORDER_STATUS_FILE - the recorder's own status file, read for
#                               a T6 acquisition refusal a restart cannot
#                               cure (core-recorder-status.json)
#   HPPS_RECORDER_STATUS_MAX_AGE_S - older than this, the status file no
#                               longer speaks for a live recorder, and its
#                               refusal record holds off nothing (default 120)
#   HPPS_COMPACT_MARKER       - sigmond's compaction marker; while it
#                               exists this script files no request
#                               (default /run/sigmond/compact-in-progress.json)
#
# Exit codes:
#   0 - HPPS healthy, OR request filed, OR cooldown active
#   1 - chronyc query failed, or the request could not be written
#       (transient or unsafe request dir — let systemd retry next tick)
#   2 - state file write failed (operator should investigate)

set -euo pipefail

LASTRX_THRESHOLD_S="${HPPS_LASTRX_THRESHOLD_S:-${TSL3_LASTRX_THRESHOLD_S:-120}}"
COOLDOWN_S="${HPPS_RESTART_COOLDOWN_S:-${TSL3_RESTART_COOLDOWN_S:-300}}"
STATE_DIR="${HPPS_STATE_DIR:-${TSL3_STATE_DIR:-/var/lib/hf-timestd}}"
STATE_FILE="$STATE_DIR/hpps-watchdog-last-restart"
STATUS_FILE="${HPPS_STATUS_FILE:-/run/hf-timestd/authority.json}"
WITHDRAWN_GRACE_S="${HPPS_WITHDRAWN_GRACE_S:-3600}"
WITHDRAWN_FILE="$STATE_DIR/hpps-watchdog-withdrawn-since"
RECORDER_STATUS_FILE="${HPPS_RECORDER_STATUS_FILE:-/var/lib/timestd/status/core-recorder-status.json}"
RECORDER_STATUS_MAX_AGE_S="${HPPS_RECORDER_STATUS_MAX_AGE_S:-120}"
REFUSAL_FILE="$STATE_DIR/hpps-watchdog-refusal-since"
COMPACT_MARKER="${HPPS_COMPACT_MARKER:-/run/sigmond/compact-in-progress.json}"
LOG_TAG="hpps-watchdog"

log() { logger -t "$LOG_TAG" -- "$@"; echo "[$LOG_TAG] $*"; }
log_warn()  { log "$@"; }
log_error() { log "ERROR: $*"; }

# The shared request writer (same guarantees as pipeline-watchdog.sh).
REQUEST_DIR="${HPPS_REQUEST_DIR:-/run/sigmond/recorder-restart}"
# shellcheck source=recorder-request-lib.sh
source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/recorder-request-lib.sh"

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

# Does T6 report a refusal that a recorder restart cannot cure?
#
# T6 refuses to acquire when the best placement of its edge falls outside
# the physical bound.  Without T5 or a qualified T4 that placement rests on
# radiod's RTP->UTC pair, which outlives a recorder restart, so a restart
# meets the same refusal.  On AC0G-ND 2026-10-27..31 this script
# restarted the recorder ~41 times a day for 4.5 days that way, curing
# nothing and opening a data gap each time.  The recorder now publishes
# the refusal in its status file with `acquisition_refusal_restart_cures`
# false (t6_acquisition_refusal.py).
#
# Prints the reason and the implied chain delay when such a refusal is
# current; returns 1 otherwise.  A status file older than
# RECORDER_STATUS_MAX_AGE_S speaks for no live recorder, so a dead or
# wedged recorder still gets its restart.
incurable_refusal() {
    [ -r "$RECORDER_STATUS_FILE" ] || return 1
    local mtime now reason effective
    mtime="$(stat -c %Y "$RECORDER_STATUS_FILE" 2>/dev/null)" || return 1
    now="$(date -u +%s)"
    [ $(( now - mtime )) -le "$RECORDER_STATUS_MAX_AGE_S" ] || return 1
    grep -q '"acquisition_refusal_restart_cures"[[:space:]]*:[[:space:]]*false' \
        "$RECORDER_STATUS_FILE" 2>/dev/null || return 1
    reason="$(grep -o '"acquisition_refusal_reason"[[:space:]]*:[[:space:]]*"[^"]*"' \
              "$RECORDER_STATUS_FILE" 2>/dev/null | head -1 \
              | sed 's/.*:[[:space:]]*"\([^"]*\)"/\1/')"
    effective="$(grep -o '"acquisition_refusal_placement_ms"[[:space:]]*:[[:space:]]*[-0-9.]*' \
                 "$RECORDER_STATUS_FILE" 2>/dev/null | head -1 \
                 | sed 's/.*:[[:space:]]*//')"
    printf '%s (edge placed %s ms from its second)\n' "${reason:-unspecified}" "${effective:-?}"
}

# The refusal is a persistent condition: say so once on entry, once on
# exit, and nothing on the runs in between.
refusal_exit() {
    [ -f "$REFUSAL_FILE" ] || return 0
    local since now
    since="$(cat "$REFUSAL_FILE" 2>/dev/null || echo 0)"
    now="$(date -u +%s)"
    log "T6 no longer reports a refusal a restart cannot cure ($1) after $(( now - since ))s; normal restart policy resumes"
    rm -f "$REFUSAL_FILE" 2>/dev/null || true
}

# The cooldown limits how often this script files a request.
cooldown_active() {
    [ -f "$STATE_FILE" ] || return 1
    local last_restart now elapsed
    last_restart="$(cat "$STATE_FILE" 2>/dev/null || echo 0)"
    now="$(date -u +%s)"
    elapsed=$(( now - last_restart ))
    [ "$elapsed" -lt "$COOLDOWN_S" ]
}

record_request() {
    mkdir -p "$STATE_DIR" 2>/dev/null || return 2
    date -u +%s > "$STATE_FILE" 2>/dev/null || return 2
    chmod 0644 "$STATE_FILE" 2>/dev/null || true
}

main() {
    local lastrx
    # `smd storage compact --apply` stops the recorder on purpose; a dark
    # HPPS then says nothing about the recorder's health.
    if [ -e "$COMPACT_MARKER" ]; then
        log "sigmond compaction in progress ($COMPACT_MARKER); no check this run"
        return 0
    fi
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
        refusal_exit "HPPS sampled again"
        return 0
    fi

    # Checked before the LastRx line below, which would otherwise repeat
    # every minute for as long as the refusal lasts.
    local refusal
    if refusal="$(incurable_refusal)"; then
        if [ ! -f "$REFUSAL_FILE" ]; then
            log "HPPS dark (LastRx=${lastrx}s) because T6 refuses acquisition: ${refusal}.  A recorder restart keeps radiod's RTP->UTC pair and cannot cure this, so not restarting while the refusal stays current.  A radiod restart, a live T5 or a qualified T4 clears it; see the recorder journal.  Logged once; the exit is logged too."
            mkdir -p "$STATE_DIR" 2>/dev/null || true
            date -u +%s > "$REFUSAL_FILE" 2>/dev/null || true
        fi
        return 0
    fi
    refusal_exit "the recorder status no longer carries one"

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
        log "HPPS withdrawn for ${elapsed}s exceeds grace ${WITHDRAWN_GRACE_S}s — requesting a restart as a last resort"
    else
        rm -f "$WITHDRAWN_FILE" 2>/dev/null || true
    fi

    if cooldown_active; then
        log "request cooldown active (last request < ${COOLDOWN_S}s ago); skipping"
        return 0
    fi

    if request_recorder_restart hpps-watchdog hpps-stale \
            "HPPS LastRx=${lastrx}s exceeds threshold ${LASTRX_THRESHOLD_S}s"; then
        log "requested a recorder restart from sigmond-recorder-watchdog"
        if ! record_request; then
            log "WARNING: request filed but state file write failed — cooldown disabled"
            return 2
        fi
    else
        log "ERROR: could not file the restart request"
        return 1
    fi
}

main "$@"

#!/bin/bash
# =============================================================================
# Clock-health monitor for the sigmond timing host
# =============================================================================
# Detects the failure mode that silently broke FT8/FT4 (PSK) on sigma in
# 2026-06: the GPS reference died, chrony free-ran, and the system clock
# drifted ~6 s off true UTC.  WSPR's ~9 s window slack masked it, but FT8/FT4
# (±2.5 s) produced 0 spots for 8 days with no error anywhere.  This catches it
# within a minute and — once a source is selectable again — auto-corrects with
# `chronyc makestep` (chrony's own `makestep N M` only steps in its first M
# updates, so after a long outage it would otherwise just slew for hours).
#
# Checks (from `chronyc -c tracking` / `chronyc -c sources`):
#   - Reference ID == 00000000            -> free-running, no source selected
#   - Leap status   != Normal             -> not synchronised
#   - Root dispersion > --max-dispersion  -> clock uncertainty too high
#   - A reachable server source (mode '^', reach>0) whose offset exceeds
#       --max-offset                       -> chrony is ignoring a good source
#
# On unhealthy:
#   - always log CRITICAL and run --alert-command (if given)
#   - if --auto-makestep AND chrony currently has a SELECTED source
#     (state '*' or '+'): run `chronyc makestep` to step the clock immediately,
#     guarded by --cooldown so we don't step repeatedly.
#   - if no selectable source (e.g. GPS unplugged): alert only — a step can't
#     help; this needs hardware/operator intervention.
#
# Exit codes: 0 = healthy, 1 = unhealthy (alerted/corrected),
#             2 = chronyd not responding.
# =============================================================================

set -uo pipefail

MAX_DISPERSION=0.5      # seconds
MAX_OFFSET=1.0          # seconds (a reachable server source disagreeing by more)
AUTO_MAKESTEP=false
COOLDOWN=600            # seconds between auto-makesteps
ALERT_COMMAND=""
VERBOSE=false
STATE_DIR="/run/timestd"
STATE_FILE="${STATE_DIR}/clock-monitor.laststep"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --max-dispersion) MAX_DISPERSION="$2"; shift 2;;
        --max-offset)     MAX_OFFSET="$2"; shift 2;;
        --auto-makestep)  AUTO_MAKESTEP=true; shift;;
        --cooldown)       COOLDOWN="$2"; shift 2;;
        --alert-command)  ALERT_COMMAND="$2"; shift 2;;
        --verbose)        VERBOSE=true; shift;;
        *) echo "unknown arg: $1" >&2; exit 2;;
    esac
done

log()  { echo "$*"; }
vlog() { $VERBOSE && echo "$*"; }
alert() {
    # $1 = severity, $2 = message
    logger -t timestd-clock-monitor "$1: $2"
    [[ -n "$ALERT_COMMAND" ]] && eval "$ALERT_COMMAND" >/dev/null 2>&1 || true
}

# awk float compare: returns 0 (true) if $1 > $2
gt() { awk "BEGIN{exit !(($1) > ($2))}"; }
abs() { awk "BEGIN{v=$1; print (v<0)?-v:v}"; }

tracking=$(chronyc -c tracking 2>/dev/null)
if [[ -z "$tracking" ]]; then
    log "CRITICAL: chronyd not responding to 'chronyc tracking'"
    alert CRITICAL "chronyd not responding on $(hostname)"
    exit 2
fi

IFS=',' read -r refid refname stratum reftime sysoff lastoff rmsoff freq residfreq skew rootdelay rootdisp updateint leap <<<"$tracking"

problems=()
[[ "$refid" == "00000000" ]] && problems+=("no reference source selected (free-running)")
[[ "${leap,,}" != "normal" ]] && problems+=("leap status: ${leap}")
gt "$rootdisp" "$MAX_DISPERSION" && problems+=("root dispersion ${rootdisp}s > ${MAX_DISPERSION}s")

# Reachable server source ('^') disagreeing by more than MAX_OFFSET.
sources=$(chronyc -c sources 2>/dev/null)
disagree=""
selectable=false
while IFS=',' read -r mode state name sstratum poll reach lastrx adjoff measoff esterr; do
    [[ -z "${mode:-}" ]] && continue
    # any selected/combined source -> chrony has something to step to
    [[ "$state" == "*" || "$state" == "+" ]] && selectable=true
    # server-mode, reachable, large offset -> chrony ignoring a good source
    if [[ "$mode" == "^" && "${reach:-0}" != "0" ]]; then
        ao=$(abs "${adjoff:-0}")
        if gt "$ao" "$MAX_OFFSET"; then
            disagree="${name} off=${adjoff}s reach=${reach}"
        fi
    fi
done <<<"$sources"
[[ -n "$disagree" ]] && problems+=("reachable source disagrees: ${disagree}")

if [[ ${#problems[@]} -eq 0 ]]; then
    vlog "OK: ref=${refname}(${refid}) disp=${rootdisp}s leap=${leap} sysoff=${sysoff}s"
    exit 0
fi

msg="clock unhealthy on $(hostname): $(IFS='; '; echo "${problems[*]}") [ref=${refname} disp=${rootdisp}s]"
log "CRITICAL: ${msg}"
alert CRITICAL "${msg}"

# ── auto-correction ──────────────────────────────────────────────────────
if ! $AUTO_MAKESTEP; then
    log "  (auto-makestep disabled; alert only)"
    exit 1
fi
if ! $selectable; then
    log "  no selectable source — chronyc makestep cannot help; needs GPS/hardware/operator intervention"
    exit 1
fi

# cooldown guard
mkdir -p "$STATE_DIR" 2>/dev/null || true
now=$(date +%s)
if [[ -f "$STATE_FILE" ]]; then
    last=$(cat "$STATE_FILE" 2>/dev/null || echo 0)
    if (( now - last < COOLDOWN )); then
        log "  selectable source present but within makestep cooldown ($((now-last))s < ${COOLDOWN}s) — skipping"
        exit 1
    fi
fi

log "  selectable source present — issuing 'chronyc makestep' to correct now"
if chronyc makestep >/dev/null 2>&1; then
    echo "$now" > "$STATE_FILE" 2>/dev/null || true
    sleep 2
    newoff=$(chronyc -c tracking 2>/dev/null | cut -d',' -f5)
    log "  makestep issued; post-step system offset=${newoff}s"
    alert INFO "clock auto-corrected via makestep on $(hostname) (was: ${msg})"
    exit 1
else
    log "  ERROR: chronyc makestep failed"
    alert CRITICAL "chronyc makestep FAILED on $(hostname) — manual intervention needed"
    exit 1
fi

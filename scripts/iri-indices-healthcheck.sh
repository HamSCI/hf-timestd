#!/bin/bash
#
# IRI indices healthcheck — verifies that timestd-iri-update.timer is doing
# its job. Exits non-zero on any failure so that systemd's
# OnFailure=timestd-alert@%n.service triggers the alert pipeline.
#
# Run via systemd timer (timestd-iri-healthcheck.timer), weekly, after the
# update timer has had time to finish.
#
# Checks:
#   1. timestd-iri-update.timer is enabled
#   2. The update timer has fired within the last 14 days
#   3. /opt/pharlap_4.7.4/dat/iri2020/apf107.dat last entry is within 14 days

set -uo pipefail

TIMER="timestd-iri-update.timer"
APF107="/opt/pharlap_4.7.4/dat/iri2020/apf107.dat"
MAX_AGE_DAYS=14

errors=0
fail() { echo "FAIL: $*"; errors=$((errors + 1)); }
ok()   { echo "OK:   $*"; }

# ── Check 1: timer enabled ────────────────────────────────────────────────
if systemctl is-enabled --quiet "$TIMER"; then
    ok "$TIMER is enabled"
else
    fail "$TIMER is not enabled"
fi

# ── Check 2: update timer has fired recently ──────────────────────────────
last_trigger=$(systemctl show "$TIMER" -p LastTriggerUSec --value 2>/dev/null || true)
if [ -z "$last_trigger" ] || [ "$last_trigger" = "n/a" ]; then
    fail "$TIMER has never fired (LastTriggerUSec=$last_trigger)"
else
    last_epoch=$(date -d "$last_trigger" +%s 2>/dev/null || echo 0)
    now_epoch=$(date +%s)
    age_days=$(( (now_epoch - last_epoch) / 86400 ))
    if [ "$last_epoch" -eq 0 ]; then
        fail "could not parse LastTriggerUSec: $last_trigger"
    elif [ "$age_days" -gt "$MAX_AGE_DAYS" ]; then
        fail "$TIMER last fired $age_days days ago ($last_trigger); max=$MAX_AGE_DAYS"
    else
        ok "$TIMER last fired $age_days days ago"
    fi
fi

# ── Check 3: apf107.dat last entry date is recent ─────────────────────────
if [ ! -r "$APF107" ]; then
    fail "$APF107 missing or unreadable"
else
    # Last data line format: " YY MM DD ap8x... fluxes"
    read -r yy mm dd _ < <(tail -1 "$APF107" | awk '{print $1, $2, $3, $4}')
    if [ -z "${yy:-}" ] || [ -z "${mm:-}" ] || [ -z "${dd:-}" ]; then
        fail "cannot parse last line of $APF107"
    else
        # YY → YYYY: file uses 2-digit year; assume 20YY (file was created
        # post-2000 and won't survive past 2099 without format change).
        last_date=$(printf "20%02d-%02d-%02d" \
            "$((10#$yy))" "$((10#$mm))" "$((10#$dd))" 2>/dev/null || echo "")
        last_epoch=$(date -d "$last_date" +%s 2>/dev/null || echo 0)
        if [ "$last_epoch" -eq 0 ]; then
            fail "invalid date parsed from apf107.dat: '$yy $mm $dd'"
        else
            now_epoch=$(date +%s)
            age_days=$(( (now_epoch - last_epoch) / 86400 ))
            if [ "$age_days" -gt "$MAX_AGE_DAYS" ]; then
                fail "apf107.dat last entry $last_date is $age_days days old (max $MAX_AGE_DAYS)"
            else
                ok "apf107.dat last entry $last_date ($age_days days old)"
            fi
        fi
    fi
fi

if [ "$errors" -gt 0 ]; then
    echo "Healthcheck FAILED: $errors check(s) failed"
    exit 1
fi
echo "All checks passed."

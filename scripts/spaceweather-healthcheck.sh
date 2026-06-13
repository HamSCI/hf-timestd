#!/bin/bash
#
# Space-weather healthcheck — verifies that hf-timestd's near-real-time
# solar/geomagnetic indices are fresh and that the upstream feeds are
# reachable. Exits non-zero on failure so systemd's
# OnFailure=timestd-alert@%n.service triggers the alert pipeline.
#
# Unlike the IRI indices (a weekly systemd timer), space weather is fetched
# in-process by SpaceWeatherService (started by the metrology service), which
# writes a JSON snapshot to the iono cache on every refresh. This check
# therefore verifies:
#   1. the snapshot file exists and was updated recently (service is alive),
#   2. NOAA SWPC is reachable (primary source),
#   3. the snapshot carries a physically-plausible F10.7.
#
# Run via a systemd timer (e.g. daily), after the services are up.

set -uo pipefail

CACHE="${TIMESTD_IONO_CACHE:-/var/lib/timestd/iono_cache}/space_weather.json"
SWPC_F107="https://services.swpc.noaa.gov/products/summary/10cm-flux.json"
MAX_AGE_MIN=180          # snapshot should refresh every 30 min
UA="hf-timestd/spaceweather-healthcheck"

errors=0
fail() { echo "FAIL: $*"; errors=$((errors + 1)); }
ok()   { echo "OK:   $*"; }

# ── Check 1: snapshot exists and is fresh ─────────────────────────────────
if [ ! -r "$CACHE" ]; then
    fail "space-weather snapshot missing/unreadable: $CACHE"
else
    mtime=$(stat -c %Y "$CACHE" 2>/dev/null || echo 0)
    now=$(date +%s)
    age_min=$(( (now - mtime) / 60 ))
    if [ "$mtime" -eq 0 ]; then
        fail "cannot stat $CACHE"
    elif [ "$age_min" -gt "$MAX_AGE_MIN" ]; then
        fail "snapshot $age_min min old (max $MAX_AGE_MIN) — is SpaceWeatherService running?"
    else
        ok "snapshot $age_min min old"
    fi

    # ── Check 3: snapshot has a plausible F10.7 ──────────────────────────
    f107=$(grep -oE '"f107":[[:space:]]*[0-9.]+' "$CACHE" | head -1 \
        | sed -E 's/.*:[[:space:]]*//' || true)
    if [ -z "${f107:-}" ]; then
        fail "snapshot has no F10.7 value"
    else
        # plausible solar flux band ~60..400
        if awk "BEGIN{exit !($f107 >= 60 && $f107 <= 400)}"; then
            ok "F10.7 = $f107 sfu"
        else
            fail "F10.7 implausible: $f107"
        fi
    fi
fi

# ── Check 2: NOAA SWPC reachable ──────────────────────────────────────────
code=$(curl -sS -m 30 -A "$UA" -o /dev/null -w '%{http_code}' "$SWPC_F107" || echo 000)
if [ "$code" = "200" ]; then
    ok "NOAA SWPC reachable (HTTP 200)"
else
    fail "NOAA SWPC unreachable (HTTP $code)"
fi

if [ "$errors" -gt 0 ]; then
    echo "Healthcheck FAILED: $errors check(s) failed"
    exit 1
fi
echo "All checks passed."

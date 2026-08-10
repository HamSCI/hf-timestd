#!/bin/bash
# ts1-probe — detect and interrogate a Turn Island Systems TS-1 TimeSync
# injector over its USB CDC console.  READ-ONLY: sends only STAT (and a
# bare CR to elicit the prompt); never TX/SAVE/DEFAULT/UPDATE.
#
# Output (stdout, KEY=VALUE lines; exit 0 = present, 1 = absent):
#   TS1_PRESENT=yes|no
#   TS1_PORT=/dev/ttyACM0
#   TS1_FIRMWARE=TimeSync v1.8, Board ID # ...     (when console answers)
#   TS1_MODE=PPS
#   TS1_GPS_LOCK=yes|no  TS1_SATS=N
#   TS1_REF_HZ=27000000
#   TS1_TX_HZ=84225000
#   TS1_INJECTED_HZ=<alias under ADC_HZ>           (when ADC_HZ given)
#
# Usage: ts1-probe.sh [ADC_HZ]     e.g. ts1-probe.sh 129600000
# The TS-1 presents as an Adafruit Trinket M0 (USB 239a:801e); the
# firmware banner disambiguates it from any other Trinket.
set -u
ADC_HZ="${1:-}"

port=""
for p in /dev/serial/by-id/usb-Adafruit_Trinket_M0_*; do
    [ -e "$p" ] && port=$(readlink -f "$p") && break
done
if [ -z "$port" ]; then
    echo "TS1_PRESENT=no"
    exit 1
fi
if fuser -s "$port" 2>/dev/null; then
    echo "TS1_PRESENT=yes"
    echo "TS1_PORT=$port"
    echo "TS1_ERROR=port busy (another process holds $port)"
    exit 0
fi

stty -F "$port" 115200 raw -echo -hupcl 2>/dev/null
out=$(
    exec 3<>"$port" || exit
    # drain anything pending, elicit banner then status (both read-only)
    printf '\r' >&3; sleep 0.3
    printf '?\r' >&3;    sleep 0.5
    printf 'STAT\r' >&3
    timeout 5 cat <&3 &
    CATPID=$!
    sleep 4
    kill $CATPID 2>/dev/null
    wait $CATPID 2>/dev/null
    exec 3>&-
)

echo "TS1_PRESENT=yes"
echo "TS1_PORT=$port"
fw=$(printf '%s' "$out" | grep -m1 -oE 'TimeSync v[^,]+, Board ID #[^\r]*')
[ -n "$fw" ] && echo "TS1_FIRMWARE=$fw"
mode=$(printf '%s' "$out" | grep -m1 -oE 'Mode: *[A-Za-z0-9-]+' | awk '{print $2}')
[ -n "$mode" ] && echo "TS1_MODE=$mode"
if printf '%s' "$out" | grep -qiE 'GPS +locked'; then
    echo "TS1_GPS_LOCK=yes"
else
    echo "TS1_GPS_LOCK=no"
fi
sats=$(printf '%s' "$out" | grep -m1 -oiE '[0-9]+ satellites in view' | grep -oE '^[0-9]+')
[ -n "$sats" ] && echo "TS1_SATS=$sats"
ref=$(printf '%s' "$out" | grep -m1 -iE 'Reference clock ?\(' | grep -oE '[0-9,]+ *Hz' | tr -d ', ' | sed 's/Hz//')
[ -n "$ref" ] && echo "TS1_REF_HZ=$ref"
tx=$(printf '%s' "$out" | grep -m1 -iE '^Output frequency' | grep -oE '[0-9,]+\.[0-9]+' | tr -d ',' | cut -d. -f1)
[ -n "$tx" ] && echo "TS1_TX_HZ=$tx"
if [ -n "${tx:-}" ] && [ -n "$ADC_HZ" ]; then
    half=$((ADC_HZ / 2))
    if [ "$tx" -gt "$half" ]; then
        echo "TS1_INJECTED_HZ=$((ADC_HZ - tx))"
    else
        echo "TS1_INJECTED_HZ=$tx"
    fi
fi
exit 0

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
#   TS1_REF_SOURCE=external|internal                (internal = REF IN dead:
#                                                    the B4 09-28 / AI6VN fault)
#   TS1_TX_HZ=84225000
#   TS1_REF_OUT_HZ=27000000                         ("out[N]" dialect: out[2], to the RX888)
#   TS1_INJECTED_HZ=<alias under ADC_HZ>           (when ADC_HZ given)
#
# Usage: ts1-probe.sh [ADC_HZ]     e.g. ts1-probe.sh 129600000
# The TS-1 presents as an Adafruit Trinket M0 (USB 239a:801e); the
# firmware banner disambiguates it from any other Trinket.
set -u
ADC_HZ="${1:-}"

# TS1_PROBE_STAT_FILE: parse a captured console transcript instead of the
# port (tests; post-mortems).  The parser below is the only thing it skips to.
if [ -n "${TS1_PROBE_STAT_FILE:-}" ]; then
    # A missing transcript is NOT a present TS-1 (v3.68 pre-build review).
    [ -r "$TS1_PROBE_STAT_FILE" ] || { echo "TS1_PRESENT=no"; echo "TS1_ERROR=TS1_PROBE_STAT_FILE unreadable: $TS1_PROBE_STAT_FILE"; exit 1; }
    port="(file) $TS1_PROBE_STAT_FILE"
    out=$(cat "$TS1_PROBE_STAT_FILE")
else

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
fi

echo "TS1_PRESENT=yes"
echo "TS1_PORT=$port"
fw=$(printf '%s' "$out" | grep -m1 -oE 'TimeSync v[^,]+, Board ID #[^\r]*')
[ -n "$fw" ] && echo "TS1_FIRMWARE=$fw"
mode=$(printf '%s' "$out" | grep -m1 -oE 'Mode: *[A-Za-z0-9-]+' | awk '{print $2}')
[ -n "$mode" ] && echo "TS1_MODE=$mode"
# Two console dialects, named by what they print, not by version (B4 runs
# v2.6 and AI6VN v2.7, and both print the first):
#   "Output frequency" dialect: "GPS locked", "11 satellites in view",
#       "Reference clock(external): 27,000,000 Hz", "Output frequency ...".
#   "out[N]" dialect (v2.4 at AC0G-ND, 2026-10-05): "GPS lock, Satellites in
#       view: 11", "ref (external) : 27,000,000 Hz", and one "out[N]: ... Hz"
#       line per Si5351 output -- out[1] the carrier, out[2] REF OUT.
# The old patterns matched none of the second: a locked v2.4 unit read
# GPS_LOCK=no and gave no carrier, so T6 auto-arm could never compute its alias.
#
# Judge lock from the STAT reply only -- the '?' help printed before it may
# mention "lock" -- and from the line that names it.  Negative wording
# (lost, waiting, no, not, unlocked, searching) is never read as lock.
stat=$(printf '%s' "$out" | tr -d '\r' | awk 'f{print} /^ *STAT *$/{f=1}')
[ -n "$stat" ] || stat=$(printf '%s' "$out" | tr -d '\r')
lockline=$(printf '%s' "$stat" | grep -m1 -iE 'GPS[^a-z]+(un)?lock')
if [ -z "$lockline" ]; then
    echo "TS1_GPS_LOCK=no"
elif printf '%s' "$lockline" | grep -qiE '(^|[^a-z])(no|not|without|waiting|searching|lost)([^a-z]|$)|unlock|lock *: *(no|false|0)'; then
    echo "TS1_GPS_LOCK=no"
elif printf '%s' "$lockline" | grep -qiE 'GPS +lock(ed)?([^a-z]|$)'; then
    echo "TS1_GPS_LOCK=yes"
else
    echo "TS1_GPS_LOCK=no"
fi
sats=$(printf '%s' "$out" | grep -m1 -oiE '[0-9]+ satellites in view' | grep -oE '^[0-9]+')
[ -n "$sats" ] || sats=$(printf '%s' "$out" | grep -m1 -oiE 'satellites in view *: *[0-9]+' | grep -oE '[0-9]+$')
[ -n "$sats" ] && echo "TS1_SATS=$sats"
refline=$(printf '%s' "$out" | grep -m1 -iE 'Reference clock ?\(|^ *ref *\(')
ref=$(printf '%s' "$refline" | grep -oE '[0-9,]+ *Hz' | tr -d ', ' | sed 's/Hz//')
[ -n "$ref" ] && echo "TS1_REF_HZ=$ref"
src=$(printf '%s' "$refline" | grep -oiE '\((external|internal)\)' | tr -d '()' | tr 'A-Z' 'a-z')
[ -n "$src" ] && echo "TS1_REF_SOURCE=$src"
_outhz() {  # _outhz N -> integer Hz of the "out[N]:" line
    printf '%s' "$out" | grep -m1 -E "^ *out\[$1\]:" | grep -oE '[0-9,]+\.[0-9]+ *Hz' | tr -d ', ' | sed 's/Hz//' | cut -d. -f1
}
tx=$(printf '%s' "$out" | grep -m1 -iE '^Output frequency' | grep -oE '[0-9,]+\.[0-9]+' | tr -d ',' | cut -d. -f1)
[ -n "$tx" ] || tx=$(_outhz 1)
# A carrier outside any plausible plan (an output disabled -> 0 Hz, or a
# retuned out[1]) must not reach setup-station, which would arm T6 over DC.
if [ -n "$tx" ] && { [ "$tx" -lt 1000000 ] || [ "$tx" -gt 200000000 ]; }; then
    echo "TS1_WARN=implausible carrier ${tx} Hz ignored (enter it by hand)"
    tx=""
fi
[ -n "$tx" ] && echo "TS1_TX_HZ=$tx"
refout=$(_outhz 2)
[ -n "$refout" ] && echo "TS1_REF_OUT_HZ=$refout"
if [ -n "${tx:-}" ] && [ -n "$ADC_HZ" ] && [ "$ADC_HZ" -gt 0 ]; then
    # Fold TX into the first Nyquist zone. Reduce modulo the sample rate
    # FIRST, then reflect: the injector sits in whichever zone the ADC
    # clock puts it in, not necessarily the second.
    #
    # ⛔ This was `ADC_HZ - tx` whenever tx > ADC_HZ/2, i.e. the second
    # zone only. At 64.8 Msps — the other rate this project supports, and
    # the one config/timestd-config.toml.template documents as 19.425 MHz —
    # an 84.225 MHz TX gave 64_800_000 - 84_225_000 = -19_425_000. A
    # negative frequency, silently handed on to channel creation.
    #
    #   129_600_000: 84.225 mod 129.6 = 84.225 -> reflect -> 45.375 MHz
    #    64_800_000: 84.225 mod  64.8 = 19.425 -> keep    -> 19.425 MHz
    #
    # Kept in step with hf_timestd.core.ts1_channel.nyquist_alias_hz,
    # which carries the tests.
    r=$((tx % ADC_HZ))
    if [ $((r * 2)) -le "$ADC_HZ" ]; then
        echo "TS1_INJECTED_HZ=$r"
    else
        echo "TS1_INJECTED_HZ=$((ADC_HZ - r))"
    fi
fi
exit 0

#!/bin/bash
#
# Update PHaRLAP / IRI-2020 solar & geomagnetic index files.
#
# IRI-2020 reads two files at runtime to look up F10.7, Ap, IG12, and Rz12
# for the requested date. PHaRLAP ships them as a snapshot, which goes stale
# fast — when the requested date falls past the last entry, IRI prints
#   APF_ONLY: Date is outside range of F10.7D indices file ...
# and silently falls back to climatology, hurting ray-trace accuracy.
#
# Run via systemd timer (timestd-iri-update.timer), weekly. Output goes to
# the systemd journal.
#
# Files refreshed:
#   /opt/pharlap_4.7.4/dat/iri2020/apf107.dat   (daily F10.7 + Ap)
#   /opt/pharlap_4.7.4/dat/iri2020/ig_rz.dat    (monthly IG12 + Rz12)
#
# Sources (in priority order):
#   1. chain-new.chain-project.net (eCHAIM mirror — updated daily)
#   2. irimodel.org (IRI project — slower update cadence)

set -euo pipefail

DAT_DIR="/opt/pharlap_4.7.4/dat/iri2020"
UA="hf-timestd/update-iri-indices (https://github.com/mijahauan/hf-timestd)"

PRIMARY="https://chain-new.chain-project.net/echaim_downloads"
FALLBACK="https://irimodel.org/indices"

if [ ! -d "$DAT_DIR" ]; then
    echo "ERROR: PHaRLAP IRI data dir not found: $DAT_DIR"
    exit 1
fi

fetch_file() {
    local fname="$1"
    local target="$DAT_DIR/$fname"
    local tmp
    tmp=$(mktemp "${target}.new.XXXXXX")
    trap 'rm -f "$tmp"' RETURN

    for base in "$PRIMARY" "$FALLBACK"; do
        local url="$base/$fname"
        echo "Fetching $url"
        if curl -sSf --max-time 60 -A "$UA" -o "$tmp" "$url"; then
            local size
            size=$(stat -c %s "$tmp")
            if [ "$size" -lt 1024 ]; then
                echo "  rejected: $url returned only $size bytes"
                continue
            fi
            # Sanity: last non-empty line should be ASCII (basic format check)
            if ! tail -1 "$tmp" | grep -q '[0-9]'; then
                echo "  rejected: $url last line has no digits"
                continue
            fi
            # mktemp defaults to mode 0600; restore PHaRLAP's expected 0644
            # so the timestd user (and anyone running raytraces) can read.
            chmod 0644 "$tmp"
            mv "$tmp" "$target"
            local last
            last=$(tail -1 "$target" | tr -s ' ' | head -c 60)
            echo "  ok: $size bytes, last line: $last"
            return 0
        fi
        echo "  curl failed for $url"
    done

    echo "ERROR: all sources failed for $fname"
    return 1
}

echo "Updating IRI-2020 indices in $DAT_DIR"
fetch_file apf107.dat
fetch_file ig_rz.dat
echo "Done."

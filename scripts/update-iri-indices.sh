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
# Run via systemd timer (timestd-iri-update.timer), daily. Output goes
# to the systemd journal.
#
# Files refreshed:
#   apf107.dat   (daily F10.7 + Ap)
#   ig_rz.dat    (monthly IG12 + Rz12)
# in PHaRLAP's /opt/pharlap_4.7.4/dat/iri2020 where PHaRLAP is installed.
# hf-timestd's IRI reads the iri2020 package's own copy, which still ends
# 2023-06-29, and it can't take current files yet (see the discovery below).
#
# IRI_DAT_DIRS (space-separated) and IRI_SOURCES override discovery and the
# download sources, for tests.
#
# Sources (in priority order):
#   1. chain-new.chain-project.net (eCHAIM mirror — updated daily)
#   2. irimodel.org (IRI project — slower update cadence)

set -euo pipefail

UA="hf-timestd/update-iri-indices (https://github.com/HamSCI/hf-timestd)"

PRIMARY="https://chain-new.chain-project.net/echaim_downloads"
FALLBACK="https://irimodel.org/indices"
SOURCES=${IRI_SOURCES:-"$PRIMARY $FALLBACK"}

if [ -n "${IRI_DAT_DIRS:-}" ]; then
    read -r -a DAT_DIRS <<<"$IRI_DAT_DIRS"
else
    DAT_DIRS=()
    [ -d /opt/pharlap_4.7.4/dat/iri2020 ] && DAT_DIRS+=(/opt/pharlap_4.7.4/dat/iri2020)
    # NOT the iri2020 Python package's copy (site-packages/iri2020/data): its Fortran driver
    # segfaults on the current index files.  Installed fleet-wide 2026-11-13 02:05Z, it crashed
    # hf-timestd's L2 calibration thousands of times an hour and starved hamsci-physics; restored
    # to the package's own files at 03:00Z.  Refresh that copy only once its reader is understood.
fi
if [ ${#DAT_DIRS[@]} -eq 0 ]; then
    echo "No IRI data directory found (no PHaRLAP, no iri2020 package); nothing to update."
    exit 0
fi
WORK=$(mktemp -d)
chmod 0755 "$WORK"   # each directory owner reads the download from here
trap 'rm -rf "$WORK"' EXIT

fetch_file() {
    local fname="$1"
    local tmp="$WORK/$fname"

    for base in $SOURCES; do
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
            # apf107.dat's lines start "YY MM DD": a mirror can serve a months-old copy (KFS-OMNI got
            # one ending 2026-07-28 on 11-13), so a copy older than IRI_MAX_AGE_DAYS loses to the
            # next source.
            if [ "$fname" = apf107.dat ]; then
                local ymd age
                ymd=$(tail -1 "$tmp" | awk '{printf "20%02d-%02d-%02d", $1, $2, $3}')
                age=$(( ( $(date -u +%s) - $(date -u -d "$ymd" +%s 2>/dev/null || echo 0) ) / 86400 ))
                if [ "$age" -gt "${IRI_MAX_AGE_DAYS:-10}" ]; then
                    echo "  rejected: $url ends $ymd ($age days old)"
                    continue
                fi
            fi
            local last
            last=$(tail -1 "$tmp" | tr -s ' ' | head -c 60)
            echo "  ok: $size bytes, last line: $last"
            # Install into every directory, each by a rename beside the target, so a
            # reader never sees half a file, mode 0644 so every user reads it.  This
            # runs as root and the venv trees belong to sigmond, so under
            # /opt/git/sigmond every write runs as that tree's owner, never root;
            # root writes only PHaRLAP's exact directory, and only when nobody else
            # can write to it.  Paths are resolved first; anything else is skipped.
            # (Overrides in IRI_DAT_DIRS are for tests, run unprivileged.)
            chmod 0644 "$tmp"
            local d real as
            for d in "${DAT_DIRS[@]}"; do
                if [ -L "$d" ] || ! real=$(realpath -e -- "$d" 2>/dev/null) || [ ! -d "$real" ]; then
                    echo "  skipped $d (not a plain directory)"; continue
                fi
                as=()
                if [ "$(id -u)" -eq 0 ] && [ -z "${IRI_DAT_DIRS:-}" ]; then
                    case "$real" in
                        /opt/git/sigmond/?*/*)
                            # As the data directory's own owner (B2: the repo is sigmond's, its
                            # venv timestd's, mode 2755), but only a service account.
                            local ug u g
                            ug=$(stat -c '%U %G' "$real"); u=${ug% *}; g=${ug#* }
                            case "$u" in
                                sigmond|timestd) ;;     # the service accounts that own sigmond venvs
                                *) echo "  skipped $real (owned by '$u', not a service account)"; continue ;;
                            esac
                            as=(setpriv --reuid "$u" --regid "$g" --init-groups --) ;;
                        /opt/pharlap_4.7.4/dat/iri2020)
                            if [ -n "$(find /opt/pharlap_4.7.4 /opt/pharlap_4.7.4/dat "$real" -maxdepth 0 \( ! -user root -o -perm /022 \))" ]; then
                                echo "  skipped $real (writable by someone other than root)"; continue
                            fi ;;
                        *)
                            echo "  skipped $real (outside the expected trees)"; continue ;;
                    esac
                fi
                # Inside the (dropped) shell: enter the directory physically and confirm it is still
                # the one resolved above before writing, so a rename in between writes nowhere.
                if "${as[@]}" sh -c 'cd -P -- "$1" && [ "$(pwd -P)" = "$1" ] && n=$(mktemp "./.$2.XXXXXX") && install -m 0644 "$3" "$n" && mv -T "$n" "./$2"' \
                        _ "$real" "$fname" "$tmp"; then
                    echo "  installed $real/$fname"
                else
                    echo "  FAILED to install $real/$fname"
                fi
            done
            return 0
        fi
        echo "  curl failed for $url"
    done

    echo "ERROR: all sources failed for $fname"
    return 1
}

echo "Updating IRI-2020 indices in: ${DAT_DIRS[*]}"
fetch_file apf107.dat
fetch_file ig_rz.dat
echo "Done."

#!/bin/bash
#
# install-pharlap.sh — stage an operator-supplied PHaRLAP 4.7.4 distribution.
#
# PHaRLAP (Defence Science and Technology Group, Australia) is closed-source
# and its licence forbids redistribution to third parties. It therefore CANNOT
# be bundled in this repository. The operator obtains it once from
#   https://www.dst.defence.gov.au/partner-with-us/access-our-technology
# and this script stages that operator-supplied archive onto a host.
#
# It only *unpacks an archive you already have* — it never fetches PHaRLAP from
# a public location.
#
# PHaRLAP 4.7.4's Linux libraries are GCC/gfortran-compiled static archives
# (no Intel Fortran, no MATLAB MCR). Building pyLAP against them needs only
# gcc + gfortran (see install.sh Phase 4b / EXTERNAL_PREREQUISITES.md).
#
# Usage:
#   sudo ./install-pharlap.sh --zip /path/to/pharlap_4.7.4.zip
#   sudo ./install-pharlap.sh --url https://private.host/pharlap_4.7.4.zip
#   sudo PHARLAP_ZIP=/path/to/pharlap_4.7.4.zip ./install-pharlap.sh
#
# Options:
#   --zip PATH    local archive to stage (highest priority)
#   --url URL     download archive from URL (e.g. a private artifact store)
#   --dest DIR    install location (default: /opt/pharlap_4.7.4)
#   --force       re-stage even if an identical install is already present
#
# Idempotent: records the archive's sha256 in <dest>/.provenance and skips
# re-staging when the same archive is already installed.

set -euo pipefail

DEST="/opt/pharlap_4.7.4"
ZIP="${PHARLAP_ZIP:-}"
URL=""
FORCE=0

log()  { printf '[install-pharlap] %s\n' "$*"; }
err()  { printf '[install-pharlap] ERROR: %s\n' "$*" >&2; }
die()  { err "$*"; exit 1; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --zip)   ZIP="$2"; shift 2 ;;
        --url)   URL="$2"; shift 2 ;;
        --dest)  DEST="$2"; shift 2 ;;
        --force) FORCE=1; shift ;;
        -h|--help) sed -n '2,33p' "$0"; exit 0 ;;
        *) die "unknown argument: $1" ;;
    esac
done

[[ $EUID -eq 0 ]] || die "must run as root (writes $DEST)"

# ── Resolve the archive ───────────────────────────────────────────────────
TMP_DL=""
cleanup() { [[ -n "$TMP_DL" && -f "$TMP_DL" ]] && rm -f "$TMP_DL" || true; }
trap cleanup EXIT

if [[ -z "$ZIP" && -n "$URL" ]]; then
    TMP_DL="$(mktemp /tmp/pharlap.XXXXXX.zip)"
    log "Downloading PHaRLAP from $URL"
    curl -fSL --retry 3 -o "$TMP_DL" "$URL" || die "download failed: $URL"
    ZIP="$TMP_DL"
fi

[[ -n "$ZIP" ]] || die "no archive given — pass --zip PATH, --url URL, or set PHARLAP_ZIP
  (acquire PHaRLAP 4.7.4 from https://www.dst.defence.gov.au/partner-with-us/access-our-technology)"
[[ -f "$ZIP" ]] || die "archive not found: $ZIP"

# ── Validate it really is PHaRLAP 4.7.4 (not some other zip) ───────────────
log "Validating archive: $ZIP"
LISTING="$(unzip -l "$ZIP" 2>/dev/null || true)"
echo "$LISTING" | grep -q 'lib/linux/libpropagation.a' \
    || die "archive does not contain lib/linux/libpropagation.a — not a Linux PHaRLAP 4.7.4 distribution"
echo "$LISTING" | grep -q 'dat/iri2020/' \
    || die "archive does not contain dat/iri2020/ — incomplete PHaRLAP distribution"

SHA="$(sha256sum "$ZIP" | awk '{print $1}')"
log "Archive sha256: $SHA"

# ── Skip if an identical install is already present ────────────────────────
PROV="$DEST/.provenance"
if [[ $FORCE -eq 0 && -f "$DEST/lib/linux/libpropagation.a" && -f "$PROV" ]]; then
    if grep -q "sha256=$SHA" "$PROV" 2>/dev/null; then
        log "PHaRLAP already staged at $DEST (sha256 matches) — nothing to do."
        exit 0
    fi
    log "A different PHaRLAP is installed at $DEST; re-staging (sha differs)."
fi

# ── Stage into a temp dir, then atomically swap into place ─────────────────
STAGE="$(mktemp -d "${DEST}.stage.XXXXXX")"
cleanup_stage() { [[ -d "$STAGE" ]] && rm -rf "$STAGE" || true; cleanup; }
trap cleanup_stage EXIT

log "Unpacking into $STAGE"
unzip -q "$ZIP" -d "$STAGE"

# Some archives wrap everything in a single top-level dir; flatten if so.
if [[ ! -d "$STAGE/lib/linux" ]]; then
    inner="$(find "$STAGE" -maxdepth 2 -type d -name linux -path '*/lib/linux' | head -1)"
    [[ -n "$inner" ]] || die "could not locate lib/linux inside the unpacked archive"
    root="$(dirname "$(dirname "$inner")")"
    if [[ "$root" != "$STAGE" ]]; then
        log "Flattening wrapper directory $(basename "$root")/"
        shopt -s dotglob
        mv "$root"/* "$STAGE"/
        shopt -u dotglob
    fi
fi

[[ -f "$STAGE/lib/linux/libpropagation.a" ]] || die "staged tree missing lib/linux/libpropagation.a"

# Provenance record (also documents the no-redistribute licence).
cat > "$STAGE/.provenance" <<EOF
# PHaRLAP staging provenance — written by install-pharlap.sh
name=pharlap
version=4.7.4
sha256=$SHA
source=${URL:-$ZIP}
staged_by=$(id -un)
# PHaRLAP is licence-restricted (DST, Australia): do NOT redistribute.
EOF

# Atomic-ish swap: move old aside, move new in.
if [[ -e "$DEST" ]]; then
    BACKUP="${DEST}.old.$$"
    log "Moving existing $DEST aside to $BACKUP"
    mv "$DEST" "$BACKUP"
fi
mv "$STAGE" "$DEST"
trap cleanup EXIT   # STAGE no longer exists
[[ -n "${BACKUP:-}" ]] && rm -rf "$BACKUP" || true

chown -R root:root "$DEST"
chmod -R a+rX "$DEST"

log "PHaRLAP 4.7.4 staged at $DEST"
log "  libs: $(ls "$DEST"/lib/linux/*.a | xargs -n1 basename | tr '\n' ' ')"
log "  data: $DEST/dat/iri2020 ($(ls "$DEST"/dat/iri2020 | wc -l) files)"
log ""
log "Next: build pyLAP against it (install.sh Phase 4b does this automatically):"
log "  export PHARLAP_HOME=$DEST"
log "  export DIR_MODELS_REF_DAT=$DEST/dat"

#!/bin/bash
#
# ensure-pylap.sh — idempotently ensure pyLAP is built into the hf-timestd venv.
#
# Single source of truth for the pyLAP build, called from BOTH:
#   * scripts/install.sh Phase 4b (Model B: clone + install)
#   * deploy.toml [build].steps   (so `smd bringup` self-heals raytracing when
#     a clone's venv is rebuilt — pyLAP is intentionally NOT in pyproject, so a
#     fresh `pip install -e .` drops it)
#
# Behaviour (all paths exit 0 — raytracing is an OPTIONAL advisory overlay and
# must never block client bring-up):
#   - pyLAP already importable           -> nothing to do
#   - PHaRLAP absent                     -> log + skip (operator supplies it;
#                                           see docs/EXTERNAL_PREREQUISITES.md)
#   - gfortran absent                    -> log + skip (declared apt prereq)
#   - otherwise                          -> clone/pin the open pyLAP fork and
#                                           build it into the venv
#
# PHaRLAP itself is licence-restricted (DST, Australia) and is never fetched
# here — only the open pyLAP binding is. See native-binaries.md.

set -uo pipefail

# Stand-alone first: derive the client's own paths from this script's location
# so a checkout anywhere works with no sigmond present. Env vars override
# (install.sh passes TIMESTD_VENV explicitly; sigmond invokes via deploy.toml).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

PHARLAP_HOME="${PHARLAP_HOME:-/opt/pharlap_4.7.4}"
VENV="${TIMESTD_VENV:-$REPO_ROOT/venv}"
PYLAP_DIR="${PYLAP_DIR:-/opt/pylap}"
PYLAP_REPO="https://github.com/mijahauan/PyLap.git"
# Canonical pyLAP pin (never a bare branch/HEAD) — bump when a newer PyLap is
# validated. native-binaries.md points here for the hf-timestd pyLAP pin.
PYLAP_REF="${PYLAP_REF:-a61ded200c1aea68ee6f7f553c27520087449adc}"

log()  { printf '[ensure-pylap] %s\n' "$*"; }
warn() { printf '[ensure-pylap] WARN: %s\n' "$*" >&2; }

# Run a command as the venv's owner (so files aren't left root-owned in a
# service-user venv). When already that user (or venv owner is current), runs
# directly.
venv_owner="$(stat -c %U "$VENV" 2>/dev/null || echo "")"
run_as_owner() {
    if [[ $EUID -eq 0 && -n "$venv_owner" && "$venv_owner" != "root" ]]; then
        sudo -u "$venv_owner" "$@"
    else
        "$@"
    fi
}

# 0. venv must exist
if [[ ! -x "$VENV/bin/python" ]]; then
    warn "venv not found at $VENV — skipping pyLAP build"
    exit 0
fi

# 1. already importable?
if "$VENV/bin/python" -c 'import pylap.raytrace_2d' >/dev/null 2>&1; then
    log "pyLAP already present in venv — nothing to do"
    exit 0
fi

# 2. PHaRLAP present?  (Model B before the operator has supplied it, or any
#    host without raytracing — this is expected and fine.)
if [[ ! -d "$PHARLAP_HOME/lib" ]]; then
    log "PHaRLAP not installed at $PHARLAP_HOME — raytracing disabled (geometric fallback)."
    log "  Supply it via scripts/install-pharlap.sh / install.sh --pharlap-zip"
    log "  (see docs/EXTERNAL_PREREQUISITES.md §3)."
    exit 0
fi

# 3. gfortran present?  (declared apt prerequisite; check defensively)
if ! command -v gfortran >/dev/null 2>&1; then
    warn "gfortran not found — cannot build pyLAP (apt install build-essential gfortran)"
    exit 0
fi

# 4. obtain the pinned pyLAP fork (open binding; safe to fetch)
if [[ -d "$PYLAP_DIR/.git" ]]; then
    # A proper git checkout (Model B / install.sh): pin it.
    git -C "$PYLAP_DIR" fetch --quiet origin 2>/dev/null || warn "pyLAP fetch failed (using existing checkout)"
    git -C "$PYLAP_DIR" checkout --quiet "$PYLAP_REF" 2>/dev/null || warn "pyLAP checkout $PYLAP_REF failed — using current checkout"
elif [[ -f "$PYLAP_DIR/setup.py" ]]; then
    # A frozen source tree with no .git (Model A image clone): use as-is. The
    # image was baked from a host built at the pinned commit; the pin cannot be
    # re-verified here, which is expected.
    log "Using existing pyLAP source tree at $PYLAP_DIR (frozen, no .git)"
else
    log "Cloning pyLAP fork to $PYLAP_DIR"
    git clone --quiet "$PYLAP_REPO" "$PYLAP_DIR" 2>/dev/null || { warn "pyLAP clone failed — raytracing stays disabled"; exit 0; }
    git -C "$PYLAP_DIR" checkout --quiet "$PYLAP_REF" 2>/dev/null || warn "pyLAP checkout $PYLAP_REF failed — using current checkout"
fi

# Make the source tree writable by the venv owner so the build can write build/.
if [[ $EUID -eq 0 && -n "$venv_owner" && "$venv_owner" != "root" ]]; then
    chown -R "$venv_owner": "$PYLAP_DIR" 2>/dev/null || true
fi

# 5. build into the venv
log "Building pyLAP into $VENV (PHARLAP_HOME=$PHARLAP_HOME)"
run_as_owner "$VENV/bin/pip" install -q setuptools wheel numpy 2>/dev/null || true
rm -rf "$PYLAP_DIR/build" "$PYLAP_DIR"/pylap.egg-info 2>/dev/null || true
PYLAP_LOG="$(mktemp /tmp/pylap-build.XXXXXX.log)"
if PHARLAP_HOME="$PHARLAP_HOME" run_as_owner env PHARLAP_HOME="$PHARLAP_HOME" \
        "$VENV/bin/pip" install "$PYLAP_DIR" --no-build-isolation --no-cache-dir \
        >"$PYLAP_LOG" 2>&1; then
    :
else
    warn "pyLAP build failed — full log: $PYLAP_LOG"
    grep -iE 'error:|fatal|cannot find|undefined reference|No such file' "$PYLAP_LOG" | head -8 >&2 || true
    exit 0
fi

# 6. verify
if "$VENV/bin/python" -c 'import pylap.raytrace_2d' >/dev/null 2>&1; then
    log "pyLAP built and importable — raytracing enabled"
    rm -f "$PYLAP_LOG"
else
    warn "pyLAP installed but still not importable (see $PYLAP_LOG)"
fi
exit 0

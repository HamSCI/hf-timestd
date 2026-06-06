#!/bin/bash
# =============================================================================
# deploy.sh — Unified idempotent install & update for hf-timestd
# =============================================================================
# Replaces: install.sh, update-production.sh, ensure-venv.sh, reinstall.sh
#
# First run:  creates user, dirs, venv, config, systemd units, starts services
# Subsequent: syncs code, updates pip, restarts changed services
#
# Usage:
#   sudo ./scripts/deploy.sh [OPTIONS]
#
# Options:
#   --pull          Git pull before deploying
#   --no-restart    Sync everything but don't restart services
#   --restart-all   Restart all timestd services after deploy (the only way
#                   in-memory bytecode picks up Phase 4 source changes).
#                   Causes brief data gaps (especially core-recorder's
#                   ring buffer).  Without this, Phase 7 is a no-op:
#                   Phase 5 (apply_profile) handles enable/disable + start.
#   --force-pip     No-op under uv sync --frozen (always reproduces locked
#                   state).  Preserved for muscle-memory compatibility.
#   --reconfig      Re-run station configuration wizard
#   --yes|-y        Accept defaults, no interactive prompts
#   --verbose|-v    Verbose output
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

# ── Paths ───────────────────────────────────────────────────────────
# Sigmond-suite convention: source + venv live together under the
# canonical clone path so there's exactly one tree per consumer
# (no `/opt/hf-timestd/` duplication via rsync).  INSTALL_DIR equals
# PROJECT_DIR so the rsync steps below are source==dest no-ops and
# the venv is rebuilt in place rather than into a sibling tree.
INSTALL_USER="timestd"
INSTALL_DIR="$PROJECT_DIR"
CONFIG_DIR="/etc/hf-timestd"
DATA_ROOT="/var/lib/timestd"
VENV_DIR="$INSTALL_DIR/venv"
WEBUI_DIR="$INSTALL_DIR/web-api"
LOG_DIR="/var/log/hf-timestd"
SYSTEMD_DIR="/etc/systemd/system"
MAIN_CONFIG="$CONFIG_DIR/timestd-config.toml"
ENV_FILE="$CONFIG_DIR/environment"

# ── Flags ───────────────────────────────────────────────────────────
DO_GIT_PULL=false
DO_RESTART=true
RESTART_ALL=false
FORCE_PIP=false
RECONFIG=false
AUTO_YES=false
VERBOSE=false

# ── Colors ──────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log_info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $*"; }
log_step()  { echo -e "\n${BLUE}━━━ $* ━━━${NC}"; }

# ── Argument parsing ────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case $1 in
        --pull)        DO_GIT_PULL=true; shift ;;
        --no-restart)  DO_RESTART=false; shift ;;
        --restart-all) RESTART_ALL=true; shift ;;
        --force-pip)   FORCE_PIP=true; shift ;;
        --reconfig)    RECONFIG=true; shift ;;
        --yes|-y)      AUTO_YES=true; shift ;;
        --verbose|-v)  VERBOSE=true; shift ;;
        --mode)        shift 2 ;;  # Legacy flag — accepted, ignored
        --help|-h)
            echo "Usage: sudo $0 [--pull] [--no-restart] [--restart-all] [--force-pip] [--reconfig] [--yes|-y]"
            echo ""
            echo "Options:"
            echo "  --pull          Git pull before deploying"
            echo "  --no-restart    Sync everything but don't restart services"
            echo "  --restart-all   Restart all timestd services to pick up Phase 4 source changes"
            echo "                  (causes brief data gaps).  Without this, Phase 7 is a no-op."
            echo "  --force-pip     No-op under uv sync --frozen (preserved for compatibility)"
            echo "  --reconfig      Re-run station configuration wizard"
            echo "  --yes|-y        Accept defaults, no interactive prompts"
            echo "  --verbose|-v    Verbose output"
            exit 0 ;;
        *) log_error "Unknown option: $1"; exit 1 ;;
    esac
done

# Must be root
if [[ "$EUID" -ne 0 ]]; then
    log_error "Must run as root: sudo $0"
    exit 1
fi

# Validate project dir
if [[ ! -f "$PROJECT_DIR/pyproject.toml" ]]; then
    log_error "Cannot find pyproject.toml in $PROJECT_DIR"
    exit 1
fi

# Detect first run
FIRST_RUN=false
if [[ ! -x "$VENV_DIR/bin/python" ]] || [[ ! -f "$MAIN_CONFIG" ]]; then
    FIRST_RUN=true
fi

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
if [[ "$FIRST_RUN" == "true" ]]; then
    echo "  HF-TimeStd Deploy (first-run install)"
else
    echo "  HF-TimeStd Deploy (update)"
fi
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Project: $PROJECT_DIR"
echo "  Install: $INSTALL_DIR"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"


# ════════════════════════════════════════════════════════════════════
# Phase 0: Git Pull (optional)
# ════════════════════════════════════════════════════════════════════
if [[ "$DO_GIT_PULL" == "true" ]]; then
    log_step "Phase 0: Git Pull"
    OLD_COMMIT=$(git -C "$PROJECT_DIR" rev-parse --short HEAD 2>/dev/null || echo "unknown")
    REPO_OWNER=$(stat -c '%U' "$PROJECT_DIR")
    if sudo -u "$REPO_OWNER" git -C "$PROJECT_DIR" pull --ff-only; then
        NEW_COMMIT=$(git -C "$PROJECT_DIR" rev-parse --short HEAD 2>/dev/null || echo "unknown")
        if [[ "$OLD_COMMIT" == "$NEW_COMMIT" ]]; then
            log_info "Already up to date ($NEW_COMMIT)"
        else
            log_info "Updated: $OLD_COMMIT → $NEW_COMMIT"
        fi
    else
        log_error "Git pull failed. Resolve conflicts and re-run."
        exit 1
    fi
fi


# ════════════════════════════════════════════════════════════════════
# Phase 1: Bootstrap (apt, user, dirs) — skips if already done
# ════════════════════════════════════════════════════════════════════
log_step "Phase 1: Bootstrap"

# ── apt dependencies ──
APT_PACKAGES=(
    python3 python3-dev python3-venv python3-pip git
    libhdf5-dev libsndfile1-dev libsystemd-dev pkg-config
    rsync avahi-utils hdf5-tools
)
MISSING_APT=()
for pkg in "${APT_PACKAGES[@]}"; do
    if ! dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null | grep -q "install ok installed"; then
        MISSING_APT+=("$pkg")
    fi
done

if [[ ${#MISSING_APT[@]} -gt 0 ]]; then
    log_info "Installing missing apt packages: ${MISSING_APT[*]}"
    apt-get update -qq
    apt-get install -y "${MISSING_APT[@]}"
else
    log_info "apt dependencies: OK"
fi

# ── Python version check ──
PYTHON_VERSION=$(python3 --version 2>&1 | awk '{print $2}')
PYTHON_MAJOR=$(echo "$PYTHON_VERSION" | cut -d. -f1)
PYTHON_MINOR=$(echo "$PYTHON_VERSION" | cut -d. -f2)

if [[ "$PYTHON_MAJOR" -lt 3 ]] || [[ "$PYTHON_MINOR" -lt 10 ]]; then
    log_error "Python $PYTHON_VERSION found, but 3.10+ required"
    exit 1
fi
log_info "Python $PYTHON_VERSION: OK"

# ── Chrony ──
if ! command -v chronyd &>/dev/null && [[ ! -x /usr/sbin/chronyd ]]; then
    log_info "Installing chrony..."
    apt-get install -y chrony
fi

# ── Service user ──
if ! id -u "$INSTALL_USER" &>/dev/null; then
    useradd --system --no-create-home --shell /usr/sbin/nologin \
        --comment "HF Time Standard Service" "$INSTALL_USER"
    log_info "Created system user: $INSTALL_USER"
fi

# Add timestd to chrony group
CHRONY_GROUP=""
if getent group _chrony &>/dev/null; then
    CHRONY_GROUP="_chrony"
elif getent group chrony &>/dev/null; then
    CHRONY_GROUP="chrony"
fi
[[ -n "$CHRONY_GROUP" ]] && usermod -a -G "$CHRONY_GROUP" "$INSTALL_USER" 2>/dev/null || true

# ── Directories ──
ensure_dir() {
    mkdir -p "$1"
    chown "$INSTALL_USER:$INSTALL_USER" "$1"
}

for d in \
    "$DATA_ROOT" \
    "$DATA_ROOT/raw_buffer" \
    "$DATA_ROOT/phase2" \
    "$DATA_ROOT/products" \
    "$DATA_ROOT/state" \
    "$DATA_ROOT/status" \
    "$DATA_ROOT/drf" \
    "$DATA_ROOT/grape" \
    "$DATA_ROOT/upload" \
    "$DATA_ROOT/audio_buffers" \
    "$DATA_ROOT/raw_archive" \
    "$DATA_ROOT/processed" \
    "$DATA_ROOT/data" \
    "$DATA_ROOT/space_weather_cache" \
    "$DATA_ROOT/ionex" \
    "$LOG_DIR" \
    "$CONFIG_DIR" \
    "$INSTALL_DIR" \
    "$INSTALL_DIR/scripts" \
    "$WEBUI_DIR" \
    "$INSTALL_DIR/config" \
    "$INSTALL_DIR/docs"
do
    ensure_dir "$d"
done

# Shared memory hot buffer
mkdir -p /dev/shm/timestd
chown "$INSTALL_USER:$INSTALL_USER" /dev/shm/timestd

# tmpfiles.d (recreate /dev/shm/timestd on boot)
if [[ -f "$PROJECT_DIR/systemd/timestd-tmpfiles.conf" ]]; then
    cp "$PROJECT_DIR/systemd/timestd-tmpfiles.conf" /etc/tmpfiles.d/timestd.conf
fi

log_info "Directories: OK"


# ════════════════════════════════════════════════════════════════════
# Phase 2: Station Configuration
# ════════════════════════════════════════════════════════════════════
log_step "Phase 2: Configuration"

# When --yes is given but no config exists yet, defer the interactive
# wizard so the install can still complete unattended (e.g. invoked from
# `smd install hf-timestd --yes` or another non-TTY context).  Phase 7
# (service start) is skipped in this case — services would fail without
# config anyway.  The user is told how to finish the configuration.
WIZARD_DEFERRED=false
if [[ ! -f "$MAIN_CONFIG" ]]; then
    if [[ "$AUTO_YES" == "true" ]]; then
        log_warn "No config found and --yes given — setup wizard deferred."
        log_warn "Configure the station before starting services with:"
        log_warn "    sudo bash $PROJECT_DIR/scripts/setup-station.sh --config $MAIN_CONFIG"
        log_warn "or rerun this installer without --yes."
        WIZARD_DEFERRED=true
    else
        log_info "No config found — running setup wizard..."
        bash "$PROJECT_DIR/scripts/setup-station.sh" --config "$MAIN_CONFIG"
    fi
elif [[ "$RECONFIG" == "true" ]]; then
    bash "$PROJECT_DIR/scripts/setup-station.sh" --config "$MAIN_CONFIG" --reconfig
elif [[ "$FIRST_RUN" == "false" && "$AUTO_YES" == "false" ]]; then
    # On updates, offer config review if the script exists
    if [[ -f "$PROJECT_DIR/scripts/config-review.sh" ]]; then
        bash "$PROJECT_DIR/scripts/config-review.sh" --non-interactive 2>/dev/null || true
    fi
else
    log_info "Config exists: $MAIN_CONFIG"
fi

# ── Radiod co-location (auto-detect for CPU affinity only) ──
# hf-timestd connects via [ka9q] status regardless of where radiod
# runs.  The only reason to detect local radiod is CPU affinity pinning.
if pgrep -x radiod &>/dev/null; then
    RADIOD_LOCAL=true
    log_info "radiod detected locally (CPU affinity will be configured)"
else
    RADIOD_LOCAL=false
    log_info "radiod not running locally (CPU affinity not needed)"
fi

# ── Generate/update environment file ──
if [[ ! -f "$ENV_FILE" ]]; then
    cat > "$ENV_FILE" << EOF
# HF Time Standard Environment
# Generated by deploy.sh on $(date -u +%Y-%m-%dT%H:%M:%SZ)

TIMESTD_MODE=production
TIMESTD_DATA_ROOT=$DATA_ROOT
TIMESTD_LOG_DIR=$LOG_DIR
TIMESTD_CONFIG=$MAIN_CONFIG
TIMESTD_PROJECT=$INSTALL_DIR
TIMESTD_INSTALL_DIR=$INSTALL_DIR
TIMESTD_WEBUI=$WEBUI_DIR
TIMESTD_VENV=$VENV_DIR
TIMESTD_LOG_LEVEL=INFO
TIMESTD_RADIOD_LOCAL=${RADIOD_LOCAL}
EOF
    chown "$INSTALL_USER:$INSTALL_USER" "$ENV_FILE"
    log_info "Created: $ENV_FILE"
elif ! grep -q '^TIMESTD_RADIOD_LOCAL=' "$ENV_FILE"; then
    echo "TIMESTD_RADIOD_LOCAL=${RADIOD_LOCAL}" >> "$ENV_FILE"
else
    sed -i "s/^TIMESTD_RADIOD_LOCAL=.*/TIMESTD_RADIOD_LOCAL=${RADIOD_LOCAL}/" "$ENV_FILE"
fi

# Config symlink for web-api
ln -sf "$MAIN_CONFIG" "$INSTALL_DIR/config/timestd-config.toml"

log_info "Config: OK (RADIOD_LOCAL=$RADIOD_LOCAL)"


# ════════════════════════════════════════════════════════════════════
# Phase 3: Sync Files
# ════════════════════════════════════════════════════════════════════
log_step "Phase 3: Sync"

RSYNC_OPTS=(-a --exclude '__pycache__' --exclude '*.pyc' --exclude '*.egg-info')

# Source tree (pyproject.toml + src/).  Guard the copy: in the in-place
# deploy model INSTALL_DIR == PROJECT_DIR (see line ~40), so an unguarded
# `cp` of pyproject.toml onto itself aborts with "are the same file" and
# fails Phase 3.  The rsync steps below already no-op on src==dest; this
# makes the cp match.
[[ "$PROJECT_DIR/pyproject.toml" -ef "$INSTALL_DIR/pyproject.toml" ]] || \
    cp "$PROJECT_DIR/pyproject.toml" "$INSTALL_DIR/pyproject.toml"
rsync "${RSYNC_OPTS[@]}" "$PROJECT_DIR/src/" "$INSTALL_DIR/src/"
log_info "Source tree synced"

# Scripts
rsync "${RSYNC_OPTS[@]}" --delete "$PROJECT_DIR/scripts/" "$INSTALL_DIR/scripts/"
chmod +x "$INSTALL_DIR/scripts/"*.sh 2>/dev/null || true
log_info "Scripts synced"

# Web API
rsync "${RSYNC_OPTS[@]}" --delete --exclude '.pytest_cache' \
    "$PROJECT_DIR/web-api/" "$WEBUI_DIR/"
log_info "Web API synced"

# Schemas (web API resolves from src path at runtime, not venv)
mkdir -p "$INSTALL_DIR/src/hf_timestd/schemas/"
rsync -a "$PROJECT_DIR/src/hf_timestd/schemas/" "$INSTALL_DIR/src/hf_timestd/schemas/"
log_info "Schemas synced"

# Documentation (living docs feature)
if [[ -d "$PROJECT_DIR/docs" ]]; then
    rsync "${RSYNC_OPTS[@]}" --delete "$PROJECT_DIR/docs/" "$INSTALL_DIR/docs/"
    log_info "Documentation synced"
fi

# Cron jobs
if [[ -f "$PROJECT_DIR/config/cron.d/timestd-freshness-monitor" ]]; then
    cp "$PROJECT_DIR/config/cron.d/timestd-freshness-monitor" /etc/cron.d/timestd-freshness-monitor
    chmod 644 /etc/cron.d/timestd-freshness-monitor
fi

# Logrotate
if [[ -f "$PROJECT_DIR/config/logrotate-timestd" ]]; then
    cp "$PROJECT_DIR/config/logrotate-timestd" /etc/logrotate.d/hf-timestd
    chmod 644 /etc/logrotate.d/hf-timestd
else
    # Fallback: generate logrotate inline (if no config file in repo)
    tee "/etc/logrotate.d/hf-timestd" > /dev/null << EOF
$LOG_DIR/*.log {
    daily
    rotate 14
    compress
    delaycompress
    missingok
    notifempty
    copytruncate
}
EOF
fi

# Fix ownership
chown -R "$INSTALL_USER:$INSTALL_USER" \
    "$INSTALL_DIR/pyproject.toml" \
    "$INSTALL_DIR/src" \
    "$INSTALL_DIR/scripts" \
    "$WEBUI_DIR" \
    "$INSTALL_DIR/docs" 2>/dev/null || true

log_info "All files synced to $INSTALL_DIR"


# ════════════════════════════════════════════════════════════════════
# Phase 4: Python Virtual Environment
# ════════════════════════════════════════════════════════════════════
log_step "Phase 4: Python"

# Ensure uv (https://astral.sh/uv) is on PATH.  Delegates to sigmond's
# shared helper if present; falls back to an inline copy for the
# bootstrap case.  Keep the inline body in sync with
# sigmond/scripts/install/ensure_uv.sh.
_ENSURE_UV_SH="/opt/git/sigmond/sigmond/scripts/install/ensure_uv.sh"
if [[ -r "$_ENSURE_UV_SH" ]]; then
    # shellcheck source=/dev/null
    source "$_ENSURE_UV_SH"
else
    _ensure_uv() {
        if command -v uv >/dev/null 2>&1; then
            printf '[INFO]  uv %s at %s\n' "$(uv --version 2>/dev/null | awk '{print $2}')" "$(command -v uv)"
            return 0
        fi
        printf '[INFO]  uv not found -- installing system-wide to /usr/local/bin\n'
        command -v curl >/dev/null || { printf '[ERROR] curl not found (apt install curl)\n' >&2; return 1; }
        if ! curl -LsSf https://astral.sh/uv/install.sh | env XDG_BIN_HOME=/usr/local/bin UV_NO_MODIFY_PATH=1 sh; then
            printf '[ERROR] uv installer failed\n' >&2
            return 1
        fi
        command -v uv >/dev/null || { printf '[ERROR] uv installer ran but uv is still not on PATH\n' >&2; return 1; }
        printf '[INFO]  uv %s installed\n' "$(uv --version 2>/dev/null | awk '{print $2}')"
    }
fi
_ensure_uv || { log_error "_ensure_uv failed"; exit 1; }

# pyproject.toml's [tool.uv.sources] declares ka9q-python as a
# path-based editable dep at ../ka9q-python.  uv sync needs the
# directory to exist at /opt/git/sigmond/ka9q-python or it fails.
if [[ ! -f /opt/git/sigmond/ka9q-python/pyproject.toml ]]; then
    log_info "ka9q-python sibling repo not at /opt/git/sigmond/ka9q-python -- cloning"
    mkdir -p /opt/git/sigmond
    git clone https://github.com/mijahauan/ka9q-python /opt/git/sigmond/ka9q-python \
        || { log_error "Failed to clone ka9q-python"; exit 1; }
fi

# Create venv if missing.  --seed populates pip/setuptools/wheel for
# compatibility with tooling that shells out to pip; harmless overhead.
if [[ ! -x "$VENV_DIR/bin/python" ]]; then
    log_info "Creating venv at $VENV_DIR..."
    uv venv "$VENV_DIR" --python 3.11 --seed --quiet
    chown -R "$INSTALL_USER:$INSTALL_USER" "$VENV_DIR"
fi

# Version detection (informational logging only -- uv sync always
# reproduces the locked state regardless of installed version).
PROJECT_VER=$(python3 -c "
import re, pathlib
text = pathlib.Path('$PROJECT_DIR/pyproject.toml').read_text()
m = re.search(r'^version\s*=\s*\"([^\"]+)\"', text, re.M)
print(m.group(1) if m else '')" 2>/dev/null || echo "")

INSTALLED_VER=$("$VENV_DIR/bin/python" -c "
try:
    from importlib.metadata import version
    print(version('hf-timestd'))
except Exception:
    print('')" 2>/dev/null || echo "")

if [[ -z "$INSTALLED_VER" ]]; then
    log_info "hf-timestd not installed — uv sync will install"
elif [[ "$PROJECT_VER" != "$INSTALLED_VER" ]]; then
    log_info "Version change: $INSTALLED_VER → $PROJECT_VER (uv sync will apply)"
else
    log_info "hf-timestd $INSTALLED_VER — uv sync will refresh from uv.lock"
fi

# --force-pip is a no-op under uv sync --frozen (which always reproduces
# the locked state).  Flag is preserved for muscle-memory compatibility.
if [[ "$FORCE_PIP" == "true" ]]; then
    log_info "--force-pip noted (no-op: uv sync --frozen always reproduces locked state)"
fi

# uv sync reads pyproject.toml + uv.lock, resolves [tool.uv.sources]
# (ka9q-python editable from ../ka9q-python), installs hf-timestd
# editable + every locked dep into $VENV_DIR.  --no-dev skips dev
# extras (pytest, black, flake8, mypy); --extra lz4/gnss/iono pulls
# in deps required by timestd-iono-reanalysis.service +
# timestd-vtec.service (the lz4 transport, pyserial/pyubx2 for GNSS
# UBX framing, netCDF4/boto3 for IRI ionosphere reanalysis).
# --frozen requires uv.lock to be current; regenerate locally with
# `uv lock` if siblings or deps have shifted.
#
# uv.lock supersedes the legacy constraints.txt approach: a committed
# lockfile is itself a reproducible pinned dependency set, and uv
# resolves much faster than pip + --constraint did.
log_info "Syncing hf-timestd + extras (lz4, gnss, iono) into $VENV_DIR..."
UV_PROJECT_ENVIRONMENT="$VENV_DIR" \
    uv sync --project "$PROJECT_DIR" --frozen --no-dev \
            --extra lz4 --extra gnss --extra iono --quiet

# Re-assert ownership in case uv touched files as root.
chown -R "$INSTALL_USER:$INSTALL_USER" "$VENV_DIR"

INSTALLED_VER=$("$VENV_DIR/bin/python" -c "
from importlib.metadata import version
print(version('hf-timestd'))" 2>/dev/null || echo "unknown")
log_info "Installed hf-timestd $INSTALLED_VER"

# Verify critical import
"$VENV_DIR/bin/python" -c "import hf_timestd" 2>/dev/null || { log_error "hf_timestd import FAILED"; exit 1; }

log_info "Python: OK (hf-timestd $INSTALLED_VER)"


# ════════════════════════════════════════════════════════════════════
# Phase 4b: pyLAP / PHaRLAP (optional numerical raytracing)
# ════════════════════════════════════════════════════════════════════
log_step "Phase 4b: pyLAP (optional)"

PHARLAP_HOME="${PHARLAP_HOME:-/opt/pharlap_4.7.4}"
PYLAP_REPO="https://github.com/mijahauan/PyLap.git"
# Pin pylap to a known-good commit (never a bare branch/HEAD) per
# sigmond/docs/native-binaries.md.  Bump when a newer PyLap is validated.
PYLAP_REF="a61ded200c1aea68ee6f7f553c27520087449adc"   # main @ 2026-06-01
PYLAP_DIR="/opt/pylap"

if [[ -d "$PHARLAP_HOME/lib" ]]; then
    log_info "PHaRLAP found at $PHARLAP_HOME"

    # Check if gfortran is available (required for pyLAP build)
    if ! command -v gfortran &>/dev/null; then
        log_warn "gfortran not found — installing..."
        apt-get install -y gfortran
    fi

    # Clone or update pylap fork, then pin to PYLAP_REF (reproducible build)
    if [[ -d "$PYLAP_DIR/.git" ]]; then
        log_info "Fetching pylap fork..."
        git -C "$PYLAP_DIR" fetch --quiet origin 2>/dev/null || \
            log_warn "pylap git fetch failed (non-critical)"
    else
        log_info "Cloning pylap fork..."
        git clone "$PYLAP_REPO" "$PYLAP_DIR" 2>/dev/null || \
            { log_warn "pylap clone failed — raytracing will use geometric fallback"; }
    fi
    if [[ -d "$PYLAP_DIR/.git" ]]; then
        git -C "$PYLAP_DIR" checkout --quiet "$PYLAP_REF" 2>/dev/null || \
            log_warn "pylap checkout $PYLAP_REF failed — using current checkout"
    fi

    # Build pylap into the venv if source is present
    if [[ -f "$PYLAP_DIR/setup.py" ]]; then
        PYLAP_INSTALLED=$("$VENV_DIR/bin/python" -c "
try:
    import pylap; print('yes')
except Exception: print('no')" 2>/dev/null)

        if [[ "$PYLAP_INSTALLED" != "yes" ]] || [[ "$FORCE_PIP" == "true" ]]; then
            log_info "Installing pylap build dependencies..."
            "$VENV_DIR/bin/pip" install setuptools wheel numpy 2>&1 | tail -5

            # Clean stale build artifacts from previous attempts
            rm -rf "$PYLAP_DIR/build" "$PYLAP_DIR/pylap.egg-info"

            log_info "Building pylap into venv..."
            PYLAP_LOG="/tmp/pylap-build.log"
            PHARLAP_HOME="$PHARLAP_HOME" \
                "$VENV_DIR/bin/pip" install "$PYLAP_DIR" --no-build-isolation --no-cache-dir 2>&1 | tee "$PYLAP_LOG" | tail -15 || \
                { log_warn "pylap build failed — full log: $PYLAP_LOG"; \
                  grep -iE '(error:|fatal|cannot find|undefined reference|No such file)' "$PYLAP_LOG" | head -10; }
        else
            log_info "pylap already installed"
        fi
    fi

    # Ensure PHARLAP env vars are in the environment file
    if ! grep -q '^PHARLAP_HOME=' "$ENV_FILE" 2>/dev/null; then
        echo "PHARLAP_HOME=$PHARLAP_HOME" >> "$ENV_FILE"
        echo "DIR_MODELS_REF_DAT=$PHARLAP_HOME/dat" >> "$ENV_FILE"
        log_info "Added PHARLAP_HOME and DIR_MODELS_REF_DAT to $ENV_FILE"
    fi
else
    log_info "PHaRLAP not found at $PHARLAP_HOME — raytracing disabled"
    log_info "  See docs/EXTERNAL_PREREQUISITES.md for acquisition instructions"
fi


# ════════════════════════════════════════════════════════════════════
# Early-exit when station config is deferred
# ════════════════════════════════════════════════════════════════════
# Phases 5–8 all read values from $MAIN_CONFIG (CFG_TIERED, CFG_CALLSIGN,
# metrology channels, archive paths, …).  When --yes was passed without
# an existing config we deferred the wizard above; finishing those phases
# now would either crash on unbound variables or install half-configured
# units.  Exit cleanly so the operator can run setup-station.sh, then
# re-run this installer to complete Phases 5–8.
if [[ "$WIZARD_DEFERRED" == "true" ]]; then
    log_info ""
    log_info "Install partially complete — phases 5-8 deferred until config is set up."
    log_info "Next steps:"
    log_info "  1. sudo bash $PROJECT_DIR/scripts/setup-station.sh --config $MAIN_CONFIG"
    log_info "  2. sudo bash $PROJECT_DIR/scripts/install.sh   # re-run to install systemd units + start services"
    exit 0
fi


# ════════════════════════════════════════════════════════════════════
# Phase 5: Systemd Units
# ════════════════════════════════════════════════════════════════════
log_step "Phase 5: Systemd"

UNITS_CHANGED=false

# ── Helper: copy if changed ──
copy_unit() {
    local src="$1"
    local dst="$SYSTEMD_DIR/$(basename "$src")"
    if [[ -f "$src" ]] && ! diff -q "$src" "$dst" &>/dev/null; then
        cp "$src" "$dst"
        UNITS_CHANGED=true
        [[ "$VERBOSE" == "true" ]] && log_info "  Updated: $(basename "$src")"
        return 0
    fi
    return 1
}

# ── Core service files ──
CORE_UNITS=("timestd-core-recorder" "timestd-l2-calibration" "timestd-fusion" "timestd-physics" "timestd-web-api" "timestd-radiod-monitor")

UPDATED_COUNT=0
for svc in "${CORE_UNITS[@]}"; do
    copy_unit "$PROJECT_DIR/systemd/${svc}.service" && ((UPDATED_COUNT++)) || true
done

# ── Metrology template + target ──
copy_unit "$PROJECT_DIR/systemd/timestd-metrology@.service" && ((UPDATED_COUNT++)) || true
copy_unit "$PROJECT_DIR/systemd/timestd-metrology.target" && ((UPDATED_COUNT++)) || true

# Remove old monolithic metrology service
if [[ -f "$SYSTEMD_DIR/timestd-metrology.service" ]]; then
    systemctl disable timestd-metrology.service 2>/dev/null || true
    systemctl stop timestd-metrology.service 2>/dev/null || true
    mv "$SYSTEMD_DIR/timestd-metrology.service" "$SYSTEMD_DIR/timestd-metrology.service.disabled" 2>/dev/null || true
    UNITS_CHANGED=true
    log_warn "Removed old monolithic timestd-metrology.service"
fi

# ── Timer files and optional services ──
for tf in \
    timestd-ionex-download.service timestd-ionex-download.timer \
    timestd-chrony-monitor.service timestd-chrony-monitor.timer \
    timestd-iono-reanalysis.service timestd-iono-reanalysis.timer \
    timestd-iri-healthcheck.service timestd-iri-healthcheck.timer \
    timestd-iri-update.service timestd-iri-update.timer \
    timestd-pipeline-watchdog.service timestd-pipeline-watchdog.timer \
    timestd-prune.service timestd-prune.timer \
    timestd-vtec.service \
    grape-daily.service grape-daily.timer \
    grape-upload-retry.service grape-upload-retry.timer \
    timestd-alert@.service
do
    copy_unit "$PROJECT_DIR/systemd/$tf" && ((UPDATED_COUNT++)) || true
done

# ── VTEC service ──
VTEC_ENABLED=$("$VENV_DIR/bin/python3" -c "
import tomllib
try:
    with open('$MAIN_CONFIG', 'rb') as f: c = tomllib.load(f)
    print('true' if c.get('gnss_vtec', {}).get('enabled', False) else 'false')
except Exception: print('false')" 2>/dev/null)

if [[ "$VTEC_ENABLED" == "true" ]]; then
    copy_unit "$PROJECT_DIR/systemd/timestd-vtec.service" && ((UPDATED_COUNT++)) || true
fi

# ── Generate metrology channel .env files ──
METROLOGY_ENV_DIR="$CONFIG_DIR/metrology-channels"
mkdir -p "$METROLOGY_ENV_DIR"

# Read all station config in one Python call
eval "$("$VENV_DIR/bin/python3" -c "
import tomllib
with open('$MAIN_CONFIG', 'rb') as f:
    c = tomllib.load(f)
s = c.get('station', {})
r = c.get('recorder', {})
print(f\"CFG_CALLSIGN='{s.get('callsign', '')}'\")
print(f\"CFG_GRID='{s.get('grid_square', '')}'\")
print(f\"CFG_STATION_ID='{s.get('id', '')}'\")
print(f\"CFG_INSTRUMENT_ID='{s.get('instrument_id', '')}'\")
print(f\"CFG_LATITUDE='{s.get('latitude', '')}'\")
print(f\"CFG_LONGITUDE='{s.get('longitude', '')}'\")
print(f\"CFG_TIERED={'true' if r.get('tiered_storage', False) else 'false'}\")
print(f\"CFG_ARCHIVE_ROOT='{r.get('archive_root', '')}'\")
print(f\"CFG_ARCHIVE_RETENTION='{r.get('archive_retention_days', '')}'\")
" 2>/dev/null)"

if [[ "$CFG_TIERED" == "true" ]]; then
    ARCHIVE_ROOT="/dev/shm/timestd/raw_buffer"
else
    ARCHIVE_ROOT="$DATA_ROOT/raw_buffer"
fi

# ── Archive drive (optional) ──
if [[ -n "$CFG_ARCHIVE_ROOT" ]]; then
    if [[ -d "$CFG_ARCHIVE_ROOT" ]] && [[ -w "$CFG_ARCHIVE_ROOT" ]]; then
        for adir in "$CFG_ARCHIVE_ROOT/raw_buffer" "$CFG_ARCHIVE_ROOT/phase2"; do
            mkdir -p "$adir"
            chown "$INSTALL_USER:$INSTALL_USER" "$adir"
        done
        log_info "Archive drive: $CFG_ARCHIVE_ROOT (mounted, ready)"
    else
        log_warn "Archive drive configured ($CFG_ARCHIVE_ROOT) but not mounted/writable"
    fi
fi

# Channel definitions: CHANNEL_NAME=FREQUENCY_HZ
# radiod mode: 4 SHARED + 2 WWV-only + 3 CHU = 9 channels
METROLOGY_CHANNELS=(
    "SHARED_2500=2500000"
    "SHARED_5000=5000000"
    "SHARED_10000=10000000"
    "SHARED_15000=15000000"
    "WWV_20000=20000000"
    "WWV_25000=25000000"
    "CHU_3330=3330000"
    "CHU_7850=7850000"
    "CHU_14670=14670000"
)

for entry in "${METROLOGY_CHANNELS[@]}"; do
    CHANNEL="${entry%%=*}"
    FREQ_HZ="${entry#*=}"

    cat > "$METROLOGY_ENV_DIR/${CHANNEL}.env" << ENVEOF
# Auto-generated by deploy.sh
# Channel: $CHANNEL
FREQUENCY_HZ=$FREQ_HZ
CALLSIGN=$CFG_CALLSIGN
GRID_SQUARE=$CFG_GRID
STATION_ID=$CFG_STATION_ID
INSTRUMENT_ID=$CFG_INSTRUMENT_ID
LATITUDE=$CFG_LATITUDE
LONGITUDE=$CFG_LONGITUDE
DATA_ROOT=$DATA_ROOT
ARCHIVE_ROOT=$ARCHIVE_ROOT
ENVEOF
done

chown -R "$INSTALL_USER:$INSTALL_USER" "$METROLOGY_ENV_DIR"
log_info "Metrology .env files: ${#METROLOGY_CHANNELS[@]} channels"

# ── daemon-reload ──
if [[ "$UNITS_CHANGED" == "true" ]]; then
    systemctl daemon-reload
    log_info "Systemd daemon reloaded ($UPDATED_COUNT unit files updated)"
else
    log_info "No unit file changes"
fi

# ── Enable services via profile ──
# Read the [services] profile from config; fall back to "rtp" if not set.
# The profile controls which systemd units are enabled/disabled.
# See: hf-timestd profile list
PROFILE=$("$VENV_DIR/bin/python3" -c "
import toml, sys
try:
    c = toml.load('$MAIN_CONFIG')
    print(c.get('services', {}).get('profile', 'rtp'))
except Exception:
    print('rtp')
" 2>/dev/null)

log_info "Applying service profile: $PROFILE"

# Core recorder is always enabled (profile-independent)
systemctl enable timestd-core-recorder.service 2>/dev/null || true

# Metrology template instances must be enabled individually (systemd
# targets don't auto-enable template instances on 'systemctl enable target')
for entry in "${METROLOGY_CHANNELS[@]}"; do
    CHANNEL="${entry%%=*}"
    systemctl enable "timestd-metrology@${CHANNEL}.service" 2>/dev/null || true
done

# Apply the profile — enables/disables remaining services and timers
"$VENV_DIR/bin/python3" -m hf_timestd profile set "$PROFILE" --config "$MAIN_CONFIG" 2>&1 | while read -r line; do
    log_info "  $line"
done

log_info "Services enabled (profile: $PROFILE)"


# ════════════════════════════════════════════════════════════════════
# Phase 6: System Configuration (idempotent)
# ════════════════════════════════════════════════════════════════════
log_step "Phase 6: System Config"

# ── Chrony SHM refclocks ──
CHRONY_CONF=""
[[ -f "/etc/chrony/chrony.conf" ]] && CHRONY_CONF="/etc/chrony/chrony.conf"
[[ -z "$CHRONY_CONF" && -f "/etc/chrony.conf" ]] && CHRONY_CONF="/etc/chrony.conf"

if [[ -n "$CHRONY_CONF" ]]; then
    # FUSE (SHM 1) + HPPS (SHM 2) refclocks -- matches the code: timestd-fusion
    # writes ChronySHM(unit=1)=FUSE, core-recorder writes ChronySHM(unit=2)=HPPS.
    # SHM 0 is RESERVED for the host's GPS (gpsd / refid LG29) -- hf-timestd must
    # NOT use it (the old "refclock SHM 0 refid TSL1" collided with gpsd and put
    # the GPS reference on internet NTP).  Installed as a conf.d drop-in
    # (idempotent), never appended to chrony.conf.  Stable SHM ownership is owned
    # by sigmond-shm-precreate.service (NTP0-3 root:0666) on a sigmond/DASI2 host;
    # see sigmond docs/timing-chain-architecture.md.
    if [[ -d /etc/chrony/conf.d ]]; then
        install -m 0644 "$PROJECT_DIR/config/chrony-timestd-refclocks.conf" \
            /etc/chrony/conf.d/timestd-refclocks.conf
        log_info "Chrony FUSE/HPPS refclocks installed (conf.d)"
    elif ! grep -q 'refid FUSE' "$CHRONY_CONF" 2>/dev/null; then
        echo "include $PROJECT_DIR/config/chrony-timestd-refclocks.conf" >> "$CHRONY_CONF"
        log_info "Chrony FUSE/HPPS refclocks included in $CHRONY_CONF"
    fi

    # Ensure chrony logging is enabled (may be missing on older installs)
    if ! grep -q "^log tracking measurements statistics" "$CHRONY_CONF" 2>/dev/null; then
        echo "log tracking measurements statistics" >> "$CHRONY_CONF"
    fi

    # GNSS timeserver (VTEC host)
    if [[ "$VTEC_ENABLED" == "true" ]]; then
        GNSS_HOST=$("$VENV_DIR/bin/python3" -c "
import tomllib
with open('$MAIN_CONFIG', 'rb') as f: c = tomllib.load(f)
print(c.get('gnss_vtec', {}).get('host', ''))" 2>/dev/null)
        if [[ -n "$GNSS_HOST" ]] && ! grep -q "server $GNSS_HOST" "$CHRONY_CONF" 2>/dev/null; then
            echo -e "\n# GNSS Timeserver (ZED-F9P)\nserver $GNSS_HOST iburst prefer" >> "$CHRONY_CONF"
            log_info "Added GNSS timeserver: $GNSS_HOST"
        fi
    fi

    log_info "Chrony config: OK"
fi

# NOTE: the old chronyd-timestd-shm.conf drop-in (ordering chrony After the
# timestd writers so they would create the SHM first) is intentionally NOT
# installed.  It made chrony depend on hf-timestd (backwards -- chrony is shared
# infra), was written to chronyd.service.d (Debian uses chrony.service, so it
# never applied), and is superseded by stable SHM ownership from
# sigmond-shm-precreate.service (NTP0-3 root:0666 before any producer/consumer).
# A pure standalone hf-timestd host without that oneshot needs an equivalent SHM
# pre-create -- see sigmond docs/timing-chain-architecture.md.

# ── UDP receive buffers ──
if [[ ! -f "/etc/sysctl.d/99-timestd.conf" ]]; then
    tee /etc/sysctl.d/99-timestd.conf > /dev/null <<'EOF'
# HF-TimeStd: Increase UDP receive buffers to prevent packet loss
net.core.rmem_max = 16777216
net.core.rmem_default = 8388608
EOF
    sysctl -p /etc/sysctl.d/99-timestd.conf > /dev/null
    log_info "UDP buffers configured"
fi

# ── Clear stale SHM segments ──
for key in 0x4e545030 0x4e545031; do
    shmid=$(ipcs -m | grep "$key" | awk '{print $2}' || true)
    [[ -n "$shmid" ]] && ipcrm -m "$shmid" 2>/dev/null || true
done

# ── CPU affinity (radiod co-located only) ──
if [[ "$RADIOD_LOCAL" == "true" ]]; then
    if [[ -f "$PROJECT_DIR/scripts/setup-cpu-affinity.sh" ]]; then
        bash "$PROJECT_DIR/scripts/setup-cpu-affinity.sh" 2>/dev/null || \
            log_warn "CPU affinity setup failed (radiod may not be running yet)"
    fi
else
    log_info "Skipping CPU affinity (radiod runs remotely)"
fi

# ── IONEX download (first run only) ──
if [[ "$FIRST_RUN" == "true" ]] && [[ -f "$INSTALL_DIR/scripts/download_ionex_daily.sh" ]]; then
    log_info "Downloading initial IONEX data..."
    sudo -u "$INSTALL_USER" "$INSTALL_DIR/scripts/download_ionex_daily.sh" 2>&1 | head -10 || true
fi

log_info "System config: OK"


# ════════════════════════════════════════════════════════════════════
# Phase 7: Restart Services
# ════════════════════════════════════════════════════════════════════
if [[ "$DO_RESTART" == "true" ]]; then
    log_step "Phase 7: Restart Services"

    if [[ "$FIRST_RUN" == "true" ]] && [[ -x "$PROJECT_DIR/scripts/start-services.sh" ]]; then
        # First run: use start-services.sh for proper dependency ordering,
        # SHM cleanup, and chronyd restart sequence
        log_info "First run — using start-services.sh for ordered startup..."
        bash "$PROJECT_DIR/scripts/start-services.sh"
    else
        # Update path.  Phase 5 (apply_profile) is the canonical "ensure
        # right things are running" step -- it enables/disables units to
        # match the profile and starts anything that should be running but
        # isn't (systemctl enable --now is a true no-op on already-active
        # units, verified empirically).  So Phase 7 has only ONE remaining
        # job: refresh in-memory bytecode for already-running services so
        # they pick up source changes from Phase 4 (uv sync).  That's a
        # destructive operation (each restart bounces a service, briefly
        # gaps its outputs); we now require --restart-all to opt in.
        #
        # Background: prior to 2026-05-24 this loop ran unconditionally on
        # every deploy, restarting 5 services + 9 metrology workers each
        # time.  Combined with timestd-fusion.service's ExecStartPre/Post
        # chrony bounce (stops chrony, starts fusion, waits 3s, restarts
        # chrony), this could cascade into 30s+ profile-apply timeouts and
        # spurious metrology-cascade-stops.  See sigmond/CLAUDE.md "Fleet
        # upgrade pattern" + the project_sigmond_uv_standardization memory
        # for the full chain of events.
        if [[ "$RESTART_ALL" == "true" ]]; then
            log_info "  --restart-all: bouncing services to load fresh bytecode"

            # Metrology: restart each instance explicitly.
            # 'systemctl restart target' does NOT start template instances that
            # have never been loaded (e.g. first deploy after enable).
            # Reset failed state first — workers may have hit StartLimitBurst
            # from a previous bug and systemd refuses to restart them.
            systemctl reset-failed 'timestd-metrology@*' 2>/dev/null || true
            systemctl reset-failed timestd-metrology.target 2>/dev/null || true
            systemctl reset-failed timestd-core-recorder 2>/dev/null || true
            MET_STARTED=0
            for entry in "${METROLOGY_CHANNELS[@]}"; do
                CHANNEL="${entry%%=*}"
                systemctl restart "timestd-metrology@${CHANNEL}.service" 2>/dev/null || true
                MET_STARTED=$((MET_STARTED + 1))
            done
            log_info "  Restarted: $MET_STARTED metrology workers"

            # try-restart instead of restart so disabled-by-profile services
            # don't get started by accident; only refreshes services that
            # were already running.
            for service in \
                timestd-l2-calibration \
                timestd-fusion \
                timestd-physics \
                timestd-web-api \
                timestd-radiod-monitor \
                timestd-vtec ; do
                if systemctl try-restart "$service" 2>/dev/null; then
                    log_info "  Restarted: $service"
                fi
            done

            # Core recorder: --restart-all gates a real restart (causes a brief
            # data gap in the ring buffer; metrology@* may briefly observe
            # missing chunks via Requires=).
            systemctl restart timestd-core-recorder 2>/dev/null || true
            log_info "  Restarted: timestd-core-recorder"
        else
            log_info "  No services restarted (use --restart-all to refresh in-memory bytecode)"
            log_info "  Phase 5 (apply_profile) already started any units that should be running."
        fi

        # Always-on housekeeping: ensure timers are running (idempotent
        # start, regardless of --restart-all).  Profile-apply takes care of
        # enable; this is a defensive belt-and-suspenders to catch the
        # rare case where the timer file was just added by Phase 4 and the
        # enable in apply_profile would have run before daemon-reload saw
        # the new file.
        for timer in timestd-ionex-download timestd-chrony-monitor timestd-pipeline-watchdog; do
            systemctl start "${timer}.timer" 2>/dev/null || true
        done
        [[ -f "$SYSTEMD_DIR/timestd-iono-reanalysis.timer" ]] && systemctl start timestd-iono-reanalysis.timer 2>/dev/null || true
        [[ -f "$SYSTEMD_DIR/grape-daily.timer" ]] && systemctl start grape-daily.timer 2>/dev/null || true
    fi
else
    log_step "Phase 7: Restart (skipped — --no-restart)"
fi


# ════════════════════════════════════════════════════════════════════
# Phase 8: Verify
# ════════════════════════════════════════════════════════════════════
log_step "Phase 8: Verify"

# Check venv is using the editable install from $PROJECT_DIR (the new
# uv-native convention; matches sigmond/CLAUDE.md "Fleet upgrade
# pattern" -- a git pull of /opt/git/sigmond/hf-timestd propagates to
# the venv without a reinstall).
INSTALLED_PATH=$("$VENV_DIR/bin/python3" -c "import hf_timestd; print(hf_timestd.__file__)" 2>/dev/null || echo "FAILED")
if [[ "$INSTALLED_PATH" == *"$PROJECT_DIR"* ]]; then
    log_info "Venv OK: editable install resolved to $PROJECT_DIR"
elif [[ "$INSTALLED_PATH" == *"/opt/hf-timestd/venv/"* ]]; then
    log_warn "Venv has hf-timestd as a wheel install at $INSTALLED_PATH"
    log_warn "  (expected an editable install from $PROJECT_DIR -- re-run install.sh)"
else
    log_warn "Could not verify package location: $INSTALLED_PATH"
fi

# Check services are running
if [[ "$DO_RESTART" == "true" ]]; then
    sleep 2
    FAILED_SVCS=()
    for svc in timestd-core-recorder timestd-metrology.target timestd-l2-calibration timestd-fusion timestd-physics timestd-web-api; do
        if systemctl is-enabled --quiet "$svc" 2>/dev/null && ! systemctl is-active --quiet "$svc" 2>/dev/null; then
            FAILED_SVCS+=("$svc")
        fi
    done

    # Check metrology template instances
    MET_RUNNING=$(systemctl list-units 'timestd-metrology@*.service' --no-legend --all 2>/dev/null | grep -c 'running' || true)

    if [[ ${#FAILED_SVCS[@]} -gt 0 ]]; then
        log_warn "Failed services: ${FAILED_SVCS[*]}"
        log_info "  Check: journalctl -u <service> -n 50"
    else
        log_info "All services running ($MET_RUNNING metrology workers)"
    fi
fi


# ════════════════════════════════════════════════════════════════════
# Summary
# ════════════════════════════════════════════════════════════════════
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Deploy Complete (hf-timestd $INSTALLED_VER)"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "  Config:  $MAIN_CONFIG"
echo "  Data:    $DATA_ROOT"
echo "  Web API: http://localhost:8000"
echo "  Verify:  scripts/verify_pipeline.sh"
echo ""
echo "  Optional externals (see docs/EXTERNAL_PREREQUISITES.md):"
echo "    PHaRLAP raytracing · NASA Earthdata (IONEX) · PSWS uploads · GNSS VTEC"
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

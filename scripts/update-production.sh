#!/bin/bash
# =============================================================================
# DEPRECATED — Use deploy.sh instead
# =============================================================================
# This script is superseded by deploy.sh, which combines install and update
# into a single idempotent command:
#   sudo ./scripts/deploy.sh --pull    # git pull + update + restart
#   sudo ./scripts/deploy.sh --pull -y # non-interactive
# =============================================================================
#
#
# update-production.sh - Update production installation from git repository
#
# Usage:
#   sudo scripts/update-production.sh [--pull] [--yes|-y]
#
# Options:
#   --pull    Run 'git pull' before updating (recommended)
#   --yes|-y  Accept current configuration without prompting
#
# This script:
# 1. Optionally pulls latest code from git (--pull)
# 2. Reinstalls the Python package
# 3. Copies updated scripts, web-api, and docs to /opt/hf-timestd
# 4. Updates systemd service files if changed
# 5. Restarts affected services
# 6. Verifies the update was successful
#

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log_info() { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

# Parse arguments
DO_GIT_PULL=false
ACCEPT_CONFIG=false
while [[ $# -gt 0 ]]; do
    case $1 in
        --pull)
            DO_GIT_PULL=true
            shift
            ;;
        --yes|-y)
            ACCEPT_CONFIG=true
            shift
            ;;
        --help|-h)
            echo "Usage: sudo $0 [--pull] [--yes|-y]"
            echo ""
            echo "Options:"
            echo "  --pull    Run 'git pull' before updating (recommended)"
            echo "  --yes|-y  Accept current configuration without prompting"
            echo "  --help    Show this help"
            exit 0
            ;;
        *)
            log_error "Unknown option: $1"
            exit 1
            ;;
    esac
done

# Configuration
INSTALL_DIR="/opt/hf-timestd"
VENV_DIR="$INSTALL_DIR/venv"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  HF-TimeStd Production Update"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# Check we're running as root
if [[ $EUID -ne 0 ]]; then
    log_error "This script must be run as root (sudo)"
    exit 1
fi

# Check project directory
if [[ ! -f "$PROJECT_DIR/pyproject.toml" ]]; then
    log_error "Cannot find pyproject.toml in $PROJECT_DIR"
    log_error "Run this script from the git repository root"
    exit 1
fi

# Check venv exists
if [[ ! -d "$VENV_DIR" ]]; then
    log_error "Virtual environment not found at $VENV_DIR"
    log_error "Run scripts/install.sh first"
    exit 1
fi

# =============================================================================
# Step 0: Git Pull (optional)
# =============================================================================
if [[ "$DO_GIT_PULL" == "true" ]]; then
    log_info "Step 0: Pulling latest code from git..."
    
    # Get current commit for comparison
    OLD_COMMIT=$(git -C "$PROJECT_DIR" rev-parse --short HEAD 2>/dev/null || echo "unknown")
    
    # Pull as the owner of the repo (not root)
    REPO_OWNER=$(stat -c '%U' "$PROJECT_DIR")
    if sudo -u "$REPO_OWNER" git -C "$PROJECT_DIR" pull --ff-only; then
        NEW_COMMIT=$(git -C "$PROJECT_DIR" rev-parse --short HEAD 2>/dev/null || echo "unknown")
        if [[ "$OLD_COMMIT" == "$NEW_COMMIT" ]]; then
            log_info "  ✅ Already up to date ($NEW_COMMIT)"
        else
            log_info "  ✅ Updated: $OLD_COMMIT → $NEW_COMMIT"
        fi
    else
        log_error "Git pull failed. Resolve conflicts manually and re-run."
        exit 1
    fi
else
    log_info "Skipping git pull (use --pull to update from remote)"
fi

# =============================================================================
# Step 0.5: Configuration Review
# =============================================================================
log_info "Step 0.5: Configuration review..."

CONFIG_REVIEW_SCRIPT="$PROJECT_DIR/scripts/config-review.sh"

if [[ -f "$CONFIG_REVIEW_SCRIPT" ]]; then
    # Run config review (interactive by default, shows current settings)
    if [[ "$ACCEPT_CONFIG" == "true" ]]; then
        bash "$CONFIG_REVIEW_SCRIPT" --non-interactive
    else
        bash "$CONFIG_REVIEW_SCRIPT"
    fi
else
    log_warn "  Config review script not found, skipping"
fi

# =============================================================================
# Step 1: Update Python Package
# =============================================================================
log_info "Step 1: Updating Python package..."

# CRITICAL: Clean up stale .pyc files from the venv to prevent old compiled code
# from interfering with new behavior. This is a common source of subtle bugs.
log_info "  Cleaning stale .pyc files from venv..."
find "$VENV_DIR/lib" -name '*.pyc' -delete 2>/dev/null || true
find "$VENV_DIR/lib" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true

# CRITICAL: Remove any editable (development) installs before installing.
# Editable installs create symlinks from site-packages back to the git repo,
# meaning production depends on the repo directory. If the repo moves, changes
# during development, or is on a different filesystem, production breaks.
# Production must always use a COPIED install.
for pip_cmd in "$VENV_DIR/bin/pip" "pip3"; do
    if $pip_cmd show hf-timestd 2>/dev/null | grep -q "Editable project location"; then
        log_warn "  Removing editable install from $($pip_cmd --version 2>/dev/null | head -1)..."
        $pip_cmd uninstall hf-timestd -y --quiet 2>/dev/null || true
    fi
done

# Also clean any stale .pth or .egg-link files that editable installs leave behind
find "$VENV_DIR/lib" -name 'hf-timestd.egg-link' -delete 2>/dev/null || true
find "$VENV_DIR/lib" -name '__editable__.hf_timestd*' -delete 2>/dev/null || true
find /usr/local/lib/python*/dist-packages -name 'hf-timestd.egg-link' -delete 2>/dev/null || true
find /usr/local/lib/python*/dist-packages -name '__editable__.hf_timestd*' -delete 2>/dev/null || true

# Install the package (NOT editable) to ensure production uses copied code
# pip will skip if version hasn't changed; bump version in pyproject.toml to force
"$VENV_DIR/bin/pip" install "$PROJECT_DIR" --quiet
log_info "  ✅ Python package updated in venv (copied, not linked)"

# =============================================================================
# Step 1b: Sync source tree to INSTALL_DIR for ensure-venv.sh
# =============================================================================
# ensure-venv.sh (run as ExecStartPre by core-recorder) pip-installs from a
# temp copy of $INSTALL_DIR.  It needs pyproject.toml and src/ to be present
# there, otherwise any unattended restart (OOM kill, watchdog, etc.) fails.
log_info "Step 1b: Syncing source tree to $INSTALL_DIR..."

cp "$PROJECT_DIR/pyproject.toml" "$INSTALL_DIR/pyproject.toml"
# NOTE: no --delete here — compiled .so extensions in the venv are not in the repo
rsync -a \
    --exclude '__pycache__' \
    --exclude '*.pyc' \
    --exclude '*.egg-info' \
    "$PROJECT_DIR/src/" "$INSTALL_DIR/src/"
chown -R timestd:timestd "$INSTALL_DIR/src/" "$INSTALL_DIR/pyproject.toml"
log_info "  ✅ pyproject.toml + src/ synced to $INSTALL_DIR"

# =============================================================================
# Step 2: Copy Updated Scripts
# =============================================================================
log_info "Step 2: Copying updated scripts..."

# Sync scripts (--delete removes scripts that no longer exist in repo)
rsync -a --delete \
    --exclude '__pycache__' \
    --exclude '*.pyc' \
    "$PROJECT_DIR/scripts/" "$INSTALL_DIR/scripts/"
chmod +x "$INSTALL_DIR/scripts/"*.sh 2>/dev/null || true
chown -R timestd:timestd "$INSTALL_DIR/scripts/"

log_info "  ✅ Scripts synced to $INSTALL_DIR/scripts/"

# =============================================================================
# Step 2b: Sync Web API Directory
# =============================================================================
log_info "Step 2b: Syncing web-api directory..."

# Sync web-api (excluding __pycache__ and .pyc files)
rsync -a --delete \
    --exclude '__pycache__' \
    --exclude '*.pyc' \
    --exclude '.pytest_cache' \
    "$PROJECT_DIR/web-api/" "$INSTALL_DIR/web-api/"

# Ensure correct ownership
chown -R timestd:timestd "$INSTALL_DIR/web-api/"

log_info "  ✅ Web API synced to $INSTALL_DIR/web-api/"

# =============================================================================
# Step 2b2: Sync Schema Files
# =============================================================================
# The web API resolves schemas from $INSTALL_DIR/src/hf_timestd/schemas/ at
# runtime (not from the venv site-packages). pip install copies schemas to
# the venv but NOT to the src tree, so new schema files must be synced here.
log_info "Step 2b2: Syncing schema files..."
mkdir -p "$INSTALL_DIR/src/hf_timestd/schemas/"
rsync -a \
    "$PROJECT_DIR/src/hf_timestd/schemas/" "$INSTALL_DIR/src/hf_timestd/schemas/"
chown -R timestd:timestd "$INSTALL_DIR/src/hf_timestd/schemas/"
log_info "  ✅ Schemas synced to $INSTALL_DIR/src/hf_timestd/schemas/"

# =============================================================================
# Step 2c: Sync Documentation (for Living Docs)
# =============================================================================
log_info "Step 2c: Syncing documentation..."

# Sync docs directory (for living documentation feature)
if [[ -d "$PROJECT_DIR/docs" ]]; then
    mkdir -p "$INSTALL_DIR/docs"
    rsync -a --delete \
        --exclude '__pycache__' \
        "$PROJECT_DIR/docs/" "$INSTALL_DIR/docs/"
    chown -R timestd:timestd "$INSTALL_DIR/docs/"
    log_info "  ✅ Documentation synced to $INSTALL_DIR/docs/"
fi

# =============================================================================
# Step 2d: Sync Cron Jobs
# =============================================================================
log_info "Step 2d: Syncing cron jobs..."

if [[ -f "$PROJECT_DIR/config/cron.d/timestd-freshness-monitor" ]]; then
    if ! diff -q "$PROJECT_DIR/config/cron.d/timestd-freshness-monitor" /etc/cron.d/timestd-freshness-monitor > /dev/null 2>&1; then
        cp "$PROJECT_DIR/config/cron.d/timestd-freshness-monitor" /etc/cron.d/timestd-freshness-monitor
        chmod 644 /etc/cron.d/timestd-freshness-monitor
        log_info "  ✅ Updated freshness monitor cron job"
    else
        log_info "  ✅ Cron jobs unchanged"
    fi
fi

# =============================================================================
# Step 2e: Sync logrotate configuration
# =============================================================================
log_info "Step 2e: Syncing logrotate config..."

if [[ -f "$PROJECT_DIR/config/logrotate-timestd" ]]; then
    if ! diff -q "$PROJECT_DIR/config/logrotate-timestd" /etc/logrotate.d/hf-timestd > /dev/null 2>&1; then
        cp "$PROJECT_DIR/config/logrotate-timestd" /etc/logrotate.d/hf-timestd
        chmod 644 /etc/logrotate.d/hf-timestd
        log_info "  ✅ Updated /etc/logrotate.d/hf-timestd"
    else
        log_info "  ✅ Logrotate config unchanged"
    fi
fi

# =============================================================================
# Step 3: Update Systemd Service Files (if changed)
# =============================================================================
log_info "Step 3: Checking systemd service files..."

SYSTEMD_DIR="/etc/systemd/system"
SERVICES_UPDATED=false

for service_file in "$PROJECT_DIR/systemd/"*.service "$PROJECT_DIR/systemd/"*.timer; do
    if [[ -f "$service_file" ]]; then
        filename=$(basename "$service_file")
        if [[ -f "$SYSTEMD_DIR/$filename" ]]; then
            # Update existing service file if changed
            if ! diff -q "$service_file" "$SYSTEMD_DIR/$filename" > /dev/null 2>&1; then
                cp "$service_file" "$SYSTEMD_DIR/$filename"
                log_info "  Updated: $filename"
                SERVICES_UPDATED=true
            fi
        else
            # Install new service file (not present on this system yet)
            cp "$service_file" "$SYSTEMD_DIR/$filename"
            log_info "  Installed new: $filename"
            SERVICES_UPDATED=true
        fi
    fi
done

if [[ "$SERVICES_UPDATED" == "true" ]]; then
    systemctl daemon-reload
    log_info "  ✅ Systemd daemon reloaded"
else
    log_info "  ✅ No service file changes detected"
fi

# =============================================================================
# Step 4: Restart Services
# =============================================================================
log_info "Step 4: Restarting services..."

# List of services to restart (in order)
# Note: Matches services in start-services.sh and install.sh
SERVICES=(
    "timestd-metrology.target"
    "timestd-l2-calibration"
    "timestd-fusion"
    "timestd-physics"
    "timestd-web-api"
    "timestd-radiod-monitor"
)

for service in "${SERVICES[@]}"; do
    if systemctl is-enabled --quiet "$service" 2>/dev/null; then
        if ! systemctl is-active --quiet "$service"; then
            log_warn "  $service was not running (may have died during pip reinstall)"
        fi
        systemctl restart "$service"
        log_info "  Restarted: $service"
    fi
done

# Note: We don't restart core-recorder to avoid data gaps
if systemctl is-active --quiet "timestd-core-recorder"; then
    log_warn "  ⚠️  timestd-core-recorder NOT restarted (to avoid data gaps)"
    log_info "     Restart manually if needed: sudo systemctl restart timestd-core-recorder"
fi

# Restart optional VTEC service if running
if systemctl is-active --quiet "timestd-vtec"; then
    systemctl restart "timestd-vtec"
    log_info "  Restarted: timestd-vtec"
fi

# =============================================================================
# Step 5: Verify Update
# =============================================================================
log_info "Step 5: Verifying update..."

# Verify the venv is using the installed package, not the repo
INSTALLED_PATH=$("$VENV_DIR/bin/python3" -c "import hf_timestd; print(hf_timestd.__file__)" 2>/dev/null || echo "FAILED")
if [[ "$INSTALLED_PATH" == *"/opt/hf-timestd/venv/"* ]]; then
    log_info "  ✅ Venv using installed package: $INSTALLED_PATH"
elif [[ "$INSTALLED_PATH" == *"/home/"* ]] || [[ "$INSTALLED_PATH" == *"$PROJECT_DIR"* ]]; then
    log_warn "  ⚠️  Venv may be using repo path (editable install?): $INSTALLED_PATH"
    log_warn "     This can cause production/development confusion!"
else
    log_warn "  ⚠️  Could not verify package location: $INSTALLED_PATH"
fi

# Check services are running
FAILED_SERVICES=()
for service in "${SERVICES[@]}"; do
    if systemctl is-enabled --quiet "$service" 2>/dev/null; then
        if ! systemctl is-active --quiet "$service"; then
            FAILED_SERVICES+=("$service")
        fi
    fi
done

if [[ ${#FAILED_SERVICES[@]} -gt 0 ]]; then
    log_warn "  ⚠️  Some services failed to restart:"
    for service in "${FAILED_SERVICES[@]}"; do
        log_warn "     - $service"
    done
    log_info "     Check logs: journalctl -u <service> -n 50"
else
    log_info "  ✅ All restarted services are running"
fi

# =============================================================================
# Summary
# =============================================================================
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Update Complete!"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
log_info "Python package and scripts updated"
log_info "Services restarted (except core-recorder)"
echo ""
log_info "Monitor fusion: journalctl -u timestd-fusion -f"
log_info "Check status:   systemctl status timestd-fusion timestd-metrology"
echo ""

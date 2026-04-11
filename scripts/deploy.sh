#!/bin/bash
# =============================================================================
# deploy.sh — small Pattern A reload for hf-timestd
# =============================================================================
#
# This script does NOT install hf-timestd from scratch.  For first-run
# install (apt deps, user creation, dirs, venv), see scripts/install.sh.
#
# What this script does, and only this:
#
#   1. Refuse to run if the canonical repo has uncommitted changes
#      (unless --force-dirty).  This is the single rule that keeps
#      production from drifting away from the git history.
#   2. Optionally `git pull` (--pull).
#   3. Verify the service user (timestd) can traverse and read the
#      source tree.  An editable install into a directory the service
#      user cannot reach will succeed as root but fail at systemd
#      runtime — we catch that here before pip writes a broken .pth.
#   4. `pip install -e .` into /opt/hf-timestd/venv.  No-op unless
#      pyproject.toml or its dependencies changed; refreshes
#      entry-point shims.  Post-install verify runs as the service
#      user, not root.
#   5. `systemctl restart` the units listed in deploy.toml [systemd].
#      core-recorder is held back unless --restart-recorder, since
#      restarting it causes a brief data gap.
#   6. Print the active git SHA so you can see what just deployed.
#
# Pattern A means: the venv imports source files from this repo via an
# editable install (pip install -e .).  After deploy.sh, what is running
# in production is byte-identical to `git rev-parse HEAD` here.  No
# wheel snapshot, no /opt copy, no drift.  The canonical repo lives at
# /opt/git/hf-timestd so the service user can reach it without relying
# on home-directory permissions.
#
# Usage:
#   sudo ./scripts/deploy.sh                  # check, install editable, restart
#   sudo ./scripts/deploy.sh --pull           # git pull first
#   sudo ./scripts/deploy.sh --restart-recorder
#   sudo ./scripts/deploy.sh --no-restart     # sync only, leave services alone
#   sudo ./scripts/deploy.sh --force-dirty    # bypass clean-tree check
#   sudo ./scripts/deploy.sh --dry-run        # print what would happen
#
# Exit codes:
#   0  success
#   1  uncommitted changes blocked the run
#   2  pip install failed or post-install import verify failed
#   3  systemctl restart failed
#   4  generic error (including source tree unreachable by service user)
# =============================================================================

set -euo pipefail

# ── Paths and defaults ──────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
INSTALL_DIR="/opt/hf-timestd"
VENV_DIR="$INSTALL_DIR/venv"
DEPLOY_TOML="$PROJECT_DIR/deploy.toml"

# Service user the systemd units run as.  An editable install is only
# useful if this user can actually reach the source tree at runtime,
# so we verify traversability before `pip install -e` and re-verify by
# importing as this user after.  Keep in sync with systemd/*.service
# User= directives.
SERVICE_USER="timestd"

DO_GIT_PULL=false
FORCE_DIRTY=false
DO_RESTART=true
RESTART_RECORDER=false
DRY_RUN=false

# ── Output helpers (stderr for humans; stdout reserved for data) ────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
log_info()  { echo -e "${GREEN}[INFO]${NC} $*" >&2; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC} $*" >&2; }
log_error() { echo -e "${RED}[ERROR]${NC} $*" >&2; }
log_step()  { echo -e "\n${BLUE}━━━ $* ━━━${NC}" >&2; }

usage() {
    sed -n '2,49p' "$0" | sed 's/^# \{0,1\}//'
    exit 0
}

# ── Argument parsing ────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case $1 in
        --pull)              DO_GIT_PULL=true; shift ;;
        --force-dirty)       FORCE_DIRTY=true; shift ;;
        --no-restart)        DO_RESTART=false; shift ;;
        --restart-recorder)  RESTART_RECORDER=true; shift ;;
        --dry-run|-n)        DRY_RUN=true; shift ;;
        --help|-h)           usage ;;
        *) log_error "Unknown option: $1"; exit 4 ;;
    esac
done

if [[ "$EUID" -ne 0 ]]; then
    log_error "Must run as root: sudo $0"
    exit 4
fi

if [[ ! -f "$PROJECT_DIR/pyproject.toml" ]]; then
    log_error "Not in an hf-timestd repo: no pyproject.toml at $PROJECT_DIR"
    exit 4
fi

if [[ ! -x "$VENV_DIR/bin/python" ]]; then
    log_error "$VENV_DIR/bin/python not found — run scripts/install.sh first"
    exit 4
fi

# Determine the user that owns the repo so git operations run as them.
REPO_OWNER="$(stat -c '%U' "$PROJECT_DIR")"

# ── Step 1: clean-tree guard ────────────────────────────────────────────────
log_step "Step 1: verify clean working tree"
if ! sudo -u "$REPO_OWNER" git -C "$PROJECT_DIR" rev-parse --git-dir &>/dev/null; then
    log_error "$PROJECT_DIR is not a git repository"
    exit 4
fi

DIRTY="$(sudo -u "$REPO_OWNER" git -C "$PROJECT_DIR" status --porcelain)"
if [[ -n "$DIRTY" ]]; then
    if [[ "$FORCE_DIRTY" == "true" ]]; then
        log_warn "uncommitted changes present, proceeding because --force-dirty was passed"
        echo "$DIRTY" | sed 's/^/    /' >&2
    else
        log_error "uncommitted changes in $PROJECT_DIR — refusing to deploy."
        log_error "    commit or stash them, or pass --force-dirty if you really mean it."
        echo "$DIRTY" | sed 's/^/    /' >&2
        exit 1
    fi
else
    log_info "working tree is clean"
fi

OLD_SHA="$(sudo -u "$REPO_OWNER" git -C "$PROJECT_DIR" rev-parse --short HEAD)"
log_info "current HEAD: $OLD_SHA"

# ── Step 2: optional git pull ───────────────────────────────────────────────
if [[ "$DO_GIT_PULL" == "true" ]]; then
    log_step "Step 2: git pull --ff-only"
    if [[ "$DRY_RUN" == "true" ]]; then
        log_info "(dry run) would: sudo -u $REPO_OWNER git -C $PROJECT_DIR pull --ff-only"
    else
        if ! sudo -u "$REPO_OWNER" git -C "$PROJECT_DIR" pull --ff-only; then
            log_error "git pull failed — resolve and rerun"
            exit 4
        fi
        NEW_SHA="$(sudo -u "$REPO_OWNER" git -C "$PROJECT_DIR" rev-parse --short HEAD)"
        if [[ "$OLD_SHA" == "$NEW_SHA" ]]; then
            log_info "already up to date ($NEW_SHA)"
        else
            log_info "updated $OLD_SHA → $NEW_SHA"
        fi
    fi
fi

# ── Step 3: source-tree traversability pre-flight ──────────────────────────
# An editable install records $PROJECT_DIR inside the venv's
# __editable__*.pth file.  If the service user cannot traverse the path
# (e.g. repo cloned under /home/<someuser> with mode 700), the install
# succeeds as root but every systemd-managed import fails at runtime.
# We hit this on bee3 (/home/mjh 700) and resolved it by relocating
# the canonical repo to /opt/git/hf-timestd.  Catch the trap here
# before pip writes the broken .pth file.
log_step "Step 3: source-tree traversability as $SERVICE_USER"
if ! id -u "$SERVICE_USER" &>/dev/null; then
    log_warn "user '$SERVICE_USER' does not exist — run scripts/install.sh first"
    log_warn "skipping traversability check"
else
    PROBE="$PROJECT_DIR/src/hf_timestd/__init__.py"
    if ! sudo -u "$SERVICE_USER" test -r "$PROBE"; then
        log_error "$SERVICE_USER cannot read $PROBE"
        log_error ""
        log_error "an editable install into $VENV_DIR would succeed here as"
        log_error "root but the service would fail to import hf_timestd at"
        log_error "runtime, because one of the parent directories of"
        log_error "$PROJECT_DIR is not traversable by $SERVICE_USER"
        log_error "(typically a home directory with mode 700)."
        log_error ""
        log_error "fix, in order of preference:"
        log_error "  1. relocate the canonical repo to /opt/git/hf-timestd"
        log_error "     (the 'Pattern A' layout used on bee3 — symlink"
        log_error "     ~/git/hf-timestd back for developer convenience)"
        log_error "  2. chmod g+rx the parent directories and add"
        log_error "     $SERVICE_USER to the owning group"
        log_error ""
        log_error "refusing to deploy."
        exit 4
    fi
    log_info "$SERVICE_USER can read $PROBE"
fi

# ── Step 4: editable install refresh ────────────────────────────────────────
log_step "Step 4: pip install -e ."
if [[ "$DRY_RUN" == "true" ]]; then
    log_info "(dry run) would: $VENV_DIR/bin/pip install -e $PROJECT_DIR"
else
    if ! "$VENV_DIR/bin/pip" install -q -e "$PROJECT_DIR" >/dev/null 2>&1; then
        log_warn "quiet pip install failed — retrying with full output"
        if ! "$VENV_DIR/bin/pip" install -e "$PROJECT_DIR"; then
            log_error "pip install failed"
            exit 2
        fi
    fi
    log_info "editable install refreshed"

    # Confirm site-packages now resolves to the canonical source path,
    # and — crucially — that $SERVICE_USER can actually import it.
    # The root check alone is not enough: it will happily bless an
    # install that systemd later cannot read.
    RESOLVED="$("$VENV_DIR/bin/python" -c 'import hf_timestd, inspect; print(inspect.getfile(hf_timestd))')"
    EXPECTED_PREFIX="$PROJECT_DIR/src/hf_timestd/"
    if [[ "$RESOLVED" != "$EXPECTED_PREFIX"* ]]; then
        log_error "venv resolves hf_timestd from $RESOLVED — expected $EXPECTED_PREFIX*"
        log_error "the editable install did not take effect; investigate before restarting services"
        exit 2
    fi
    log_info "venv import path (root): $RESOLVED"

    if id -u "$SERVICE_USER" &>/dev/null; then
        if ! sudo -u "$SERVICE_USER" "$VENV_DIR/bin/python" -c 'import hf_timestd' &>/dev/null; then
            log_error "$SERVICE_USER cannot 'import hf_timestd' from $VENV_DIR"
            log_error "the editable install blessed by root is not reachable by the service user;"
            log_error "check path traversability, .pth file readability, and venv ownership"
            exit 2
        fi
        log_info "import verified as $SERVICE_USER"
    fi
fi

# ── Step 5: systemctl restart (units from deploy.toml) ──────────────────────
read_units_from_deploy_toml() {
    # Tiny TOML reader good enough for [systemd] units = [...] block.
    # No tomllib in bash, but the structure is regular enough.
    awk '
        /^\[systemd\]/        { in_systemd = 1; next }
        in_systemd && /^\[/   { in_systemd = 0 }
        in_systemd && /^units[[:space:]]*=/ { in_units = 1 }
        in_units {
            while (match($0, /"([^"]+)"/, m)) {
                print m[1]
                $0 = substr($0, RSTART + RLENGTH)
            }
            if ($0 ~ /\]/) { in_units = 0 }
        }
    ' "$1"
}

if [[ "$DO_RESTART" == "true" ]]; then
    log_step "Step 5: restart services from deploy.toml"

    if [[ ! -f "$DEPLOY_TOML" ]]; then
        log_error "deploy.toml not found at $DEPLOY_TOML"
        exit 4
    fi

    UNITS=()
    while IFS= read -r unit; do
        [[ -z "$unit" ]] && continue
        if [[ "$RESTART_RECORDER" != "true" && "$unit" == "timestd-core-recorder.service" ]]; then
            log_info "skipping $unit (use --restart-recorder to bounce it; causes brief data gap)"
            continue
        fi
        UNITS+=("$unit")
    done < <(read_units_from_deploy_toml "$DEPLOY_TOML")

    if [[ ${#UNITS[@]} -eq 0 ]]; then
        log_warn "deploy.toml lists no units to restart"
    fi

    for unit in "${UNITS[@]}"; do
        if ! systemctl list-unit-files --no-legend --no-pager --type=service,target "$unit" &>/dev/null; then
            log_warn "$unit: not installed, skipping"
            continue
        fi
        if [[ "$DRY_RUN" == "true" ]]; then
            log_info "(dry run) would: systemctl restart $unit"
            continue
        fi
        if systemctl restart "$unit"; then
            STATE="$(systemctl is-active "$unit" 2>/dev/null || echo unknown)"
            log_info "$unit: $STATE"
        else
            log_error "$unit: restart failed"
            exit 3
        fi
    done

    if [[ "$RESTART_RECORDER" == "true" ]]; then
        log_warn "core-recorder bounced — expect a few seconds of missing IQ"
    fi
else
    log_info "step 5 skipped (--no-restart)"
fi

# ── Step 6: report ─────────────────────────────────────────────────────────
log_step "Step 6: summary"
FINAL_SHA="$(sudo -u "$REPO_OWNER" git -C "$PROJECT_DIR" rev-parse --short HEAD)"
FINAL_DESC="$(sudo -u "$REPO_OWNER" git -C "$PROJECT_DIR" log -1 --pretty=format:'%h %s')"
log_info "deployed: $FINAL_DESC"
echo "$FINAL_SHA"

#!/bin/bash
# =============================================================================
# DEPRECATED — Use deploy.sh instead
# =============================================================================
# This script is superseded by deploy.sh Phase 4 (Python), which handles
# venv creation, pip install, and verification in a single command.
# The ExecStartPre that called this has been removed from the systemd unit.
# =============================================================================
#
# ensure-venv.sh — Ensure Python venv exists and hf-timestd is installed
# =============================================================================
# Idempotent: safe to re-run. Skips reinstall if the installed version
# matches pyproject.toml (unless --force is given).
#
# Usage:
#   sudo ./scripts/ensure-venv.sh [--venv /path] [--python python3] [--force]
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

VENV_DIR="/opt/hf-timestd/venv"
PYTHON_BIN="python3"
FORCE=false

usage() {
  echo "Usage: $0 [--venv <path>] [--python <python3>] [--force]"
  echo ""
  echo "Ensures a working virtualenv exists and hf-timestd is installed."
  echo ""
  echo "Options:"
  echo "  --venv PATH    Venv location (default: /opt/hf-timestd/venv)"
  echo "  --python BIN   Python interpreter (default: python3)"
  echo "  --force        Reinstall even if version matches"
  # Accept and ignore legacy --mode flag for backward compat with systemd units
  echo "  --mode MODE    (ignored, accepted for backward compatibility)"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --venv)
      VENV_DIR="$2"; shift 2 ;;
    --python)
      PYTHON_BIN="$2"; shift 2 ;;
    --force)
      FORCE=true; shift ;;
    --mode)
      # Legacy flag — accept and ignore for backward compat with
      # systemd ExecStartPre lines that still pass --mode production
      shift 2 ;;
    --help|-h)
      usage; exit 0 ;;
    *)
      echo "ERROR: Unknown option: $1" >&2
      usage
      exit 2
      ;;
  esac
done

# ── Helpers ──────────────────────────────────────────────────────────────
ensure_python() {
  if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
    echo "ERROR: '$PYTHON_BIN' not found. Install python3 and python3-venv." >&2
    exit 1
  fi
}

create_venv_if_missing() {
  if [[ -x "$VENV_DIR/bin/python" ]]; then
    return 0
  fi

  echo "Creating venv at $VENV_DIR ..."
  if [[ ! -w "$(dirname "$VENV_DIR")" ]]; then
    sudo mkdir -p "$(dirname "$VENV_DIR")"
    sudo "$PYTHON_BIN" -m venv "$VENV_DIR"
    sudo chown -R "${SUDO_USER:-$USER}:${SUDO_USER:-$USER}" "$VENV_DIR" 2>/dev/null || true
  else
    mkdir -p "$(dirname "$VENV_DIR")"
    "$PYTHON_BIN" -m venv "$VENV_DIR"
  fi
}

get_project_version() {
  # Read version from pyproject.toml (avoids importing anything)
  "$PYTHON_BIN" -c "
import re, pathlib
text = pathlib.Path('$PROJECT_DIR/pyproject.toml').read_text()
m = re.search(r'^version\s*=\s*\"([^\"]+)\"', text, re.M)
print(m.group(1) if m else '')
" 2>/dev/null || echo ""
}

get_installed_version() {
  "$VENV_DIR/bin/python" -c "
try:
    from importlib.metadata import version
    print(version('hf-timestd'))
except Exception:
    print('')
" 2>/dev/null || echo ""
}

install_package() {
  # Production installs must not be editable and must not depend on the repo path.
  # Install from a temporary copy so the venv is self-contained.
  if ! command -v rsync >/dev/null 2>&1; then
    echo "ERROR: rsync is required for venv bootstrap" >&2
    exit 1
  fi

  # shellcheck disable=SC1090
  source "$VENV_DIR/bin/activate"

  python -m pip install --upgrade pip --quiet

  TEMP_INSTALL_DIR="$(mktemp -d)"
  trap 'rm -rf "$TEMP_INSTALL_DIR"' EXIT

  rsync -a --exclude='setup.py' --exclude='requirements.txt' --exclude='requirements-dev.txt' \
    "$PROJECT_DIR/" "$TEMP_INSTALL_DIR/"

  python -m pip install "$TEMP_INSTALL_DIR"
  rm -rf "$TEMP_INSTALL_DIR"
  trap - EXIT

  deactivate
}

verify() {
  # shellcheck disable=SC1090
  source "$VENV_DIR/bin/activate"

  python -c "import hf_timestd; print('hf_timestd import ok')" >/dev/null
  python -c "import ka9q; print('ka9q import ok')" >/dev/null

  deactivate
}

# ── Main ─────────────────────────────────────────────────────────────────
ensure_python
create_venv_if_missing

PROJECT_VER="$(get_project_version)"
INSTALLED_VER="$(get_installed_version)"

if [[ "$FORCE" == "false" && -n "$PROJECT_VER" && "$PROJECT_VER" == "$INSTALLED_VER" ]]; then
  echo "OK: hf-timestd $INSTALLED_VER already installed at $VENV_DIR (up to date)"
  exit 0
fi

if [[ -n "$INSTALLED_VER" ]]; then
  echo "Upgrading hf-timestd: $INSTALLED_VER → $PROJECT_VER"
else
  echo "Installing hf-timestd $PROJECT_VER into $VENV_DIR ..."
fi

install_package
verify

echo "OK: venv ready at $VENV_DIR (hf-timestd $(get_installed_version))"

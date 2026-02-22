#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

MODE="test"
VENV_DIR=""
PYTHON_BIN="python3"

usage() {
  echo "Usage: $0 [--mode test|production] [--venv <path>] [--python <python3>]"
  echo ""
  echo "Ensures a working virtualenv exists and hf-timestd is installed into it."
  echo ""
  echo "Defaults:"
  echo "  --mode test         (repo/dev)"
  echo "  --venv ./venv       (repo/dev)"
  echo "  --python python3"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode)
      MODE="$2"; shift 2 ;;
    --venv)
      VENV_DIR="$2"; shift 2 ;;
    --python)
      PYTHON_BIN="$2"; shift 2 ;;
    --help|-h)
      usage; exit 0 ;;
    *)
      echo "ERROR: Unknown option: $1" >&2
      usage
      exit 2
      ;;
  esac
done

if [[ -z "$VENV_DIR" ]]; then
  if [[ "$MODE" == "production" ]]; then
    VENV_DIR="/opt/hf-timestd/venv"
  else
    VENV_DIR="$PROJECT_DIR/venv"
  fi
fi

if [[ "$MODE" != "test" && "$MODE" != "production" ]]; then
  echo "ERROR: Invalid mode '$MODE' (must be test|production)" >&2
  exit 2
fi

ensure_python() {
  if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
    echo "ERROR: '$PYTHON_BIN' not found. Install python3 and python3-venv." >&2
    exit 1
  fi
}

need_sudo_for_venv() {
  if [[ "$MODE" != "production" ]]; then
    return 1
  fi
  if [[ -w "$(dirname "$VENV_DIR")" ]]; then
    return 1
  fi
  return 0
}

create_venv_if_missing() {
  if [[ -x "$VENV_DIR/bin/python" ]]; then
    return 0
  fi

  if need_sudo_for_venv; then
    sudo mkdir -p "$(dirname "$VENV_DIR")"
    sudo "$PYTHON_BIN" -m venv "$VENV_DIR"
    sudo chown -R "${SUDO_USER:-$USER}:${SUDO_USER:-$USER}" "$VENV_DIR" 2>/dev/null || true
  else
    mkdir -p "$(dirname "$VENV_DIR")"
    "$PYTHON_BIN" -m venv "$VENV_DIR"
  fi
}

install_package() {
  # shellcheck disable=SC1090
  source "$VENV_DIR/bin/activate"

  python -m pip install --upgrade pip

  if [[ "$MODE" == "production" ]]; then
    # Production installs must not be editable and must not depend on the repo path.
    # Install from a temporary copy.
    if ! command -v rsync >/dev/null 2>&1; then
      echo "ERROR: rsync is required for production venv bootstrap" >&2
      exit 1
    fi

    TEMP_INSTALL_DIR="$(mktemp -d)"
    rsync -a --exclude='setup.py' --exclude='requirements.txt' --exclude='requirements-dev.txt' \
      "$PROJECT_DIR/" "$TEMP_INSTALL_DIR/"

    python -m pip install "$TEMP_INSTALL_DIR"
    rm -rf "$TEMP_INSTALL_DIR"
  else
    python -m pip install -e "$PROJECT_DIR"
  fi

  deactivate
}

verify() {
  # shellcheck disable=SC1090
  source "$VENV_DIR/bin/activate"

  python -c "import hf_timestd; print('hf_timestd import ok')" >/dev/null
  python -c "import ka9q; print('ka9q import ok')" >/dev/null

  deactivate
}

ensure_python
create_venv_if_missing
install_package
verify

echo "OK: venv ready at $VENV_DIR"

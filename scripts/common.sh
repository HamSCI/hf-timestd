#!/bin/bash
# Common settings for all HF Time Standard Analysis (hf-timestd) scripts
# Source this at the top of every shell script:
#   source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

# Determine project directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

# Source environment file
if [ -f "/etc/hf-timestd/environment" ]; then
    source "/etc/hf-timestd/environment"
elif [ -f "$PROJECT_DIR/config/environment" ]; then
    source "$PROJECT_DIR/config/environment"
fi

# Production paths
VENV_PATH="${TIMESTD_VENV:-/opt/git/sigmond/hf-timestd/venv}"
DEFAULT_CONFIG="${TIMESTD_CONFIG:-/etc/hf-timestd/timestd-config.toml}"
DATA_ROOT="${TIMESTD_DATA_ROOT:-/var/lib/timestd}"
LOG_DIR="${TIMESTD_LOG_DIR:-/var/log/hf-timestd}"

# Set Python to use venv - MANDATORY
if [ -f "$VENV_PATH/bin/python" ]; then
    PYTHON="$VENV_PATH/bin/python"
    export VIRTUAL_ENV="$VENV_PATH"
    export PATH="$VENV_PATH/bin:$PATH"
else
    echo " ERROR: venv not found at $VENV_PATH"
    echo "   Run: sudo $PROJECT_DIR/scripts/ensure-venv.sh"
    exit 1
fi

# Helper to get data root from config
get_data_root() {
    local config="${1:-$DEFAULT_CONFIG}"

    if [ -f "$config" ]; then
        grep '^production_data_root' "$config" | cut -d'"' -f2
        return
    fi

    echo "$DATA_ROOT"
}

# Helper to get log directory
get_log_dir() {
    echo "$LOG_DIR"
}

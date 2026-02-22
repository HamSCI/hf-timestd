#!/bin/bash
# Common settings for all HF Time Standard Analysis (hf-timestd) scripts
# Source this at the top of every shell script:
#   source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

# Determine project directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

# Source environment file if exists (production mode)
# Order: /etc/hf-timestd/environment -> PROJECT_DIR/config/environment
if [ -f "/etc/hf-timestd/environment" ]; then
    source "/etc/hf-timestd/environment"
elif [ -f "$PROJECT_DIR/config/environment" ]; then
    source "$PROJECT_DIR/config/environment"
fi

# Determine venv location (from env or default)
VENV_PATH="${TIMESTD_VENV:-$PROJECT_DIR/venv}"

# Set Python to use venv - MANDATORY
if [ -f "$VENV_PATH/bin/python" ]; then
    PYTHON="$VENV_PATH/bin/python"
    export VIRTUAL_ENV="$VENV_PATH"
    export PATH="$VENV_PATH/bin:$PATH"
else
    echo " ERROR: venv not found at $VENV_PATH"
    echo "   Run: $PROJECT_DIR/scripts/ensure-venv.sh --mode test --venv $VENV_PATH"
    exit 1
fi

# Default config location (from env or default)
# Support both new TIMESTD_CONFIG and legacy GRAPE_CONFIG
# Try new config name first, fall back to legacy
if [ -f "$PROJECT_DIR/config/timestd-config.toml" ]; then
    DEFAULT_CONFIG="${TIMESTD_CONFIG:-$PROJECT_DIR/config/timestd-config.toml}"
else
    DEFAULT_CONFIG="${TIMESTD_CONFIG:-$PROJECT_DIR/config/timestd-config.toml}"
fi

# Helper to get current mode - CONFIG FILE IS AUTHORITATIVE
# The timestd-config.toml mode setting takes precedence over environment variables
get_mode() {
    local config="${1:-$DEFAULT_CONFIG}"
    
    # Config file is the single source of truth for mode
    if [ -f "$config" ]; then
        grep '^mode' "$config" | cut -d'"' -f2
        return
    fi

    # Fall back to environment only if no config file
    if [ -n "${TIMESTD_MODE:-}" ]; then
        echo "$TIMESTD_MODE"
        return
    fi

    echo "test"
}

# Helper to get data root from config - CONFIG FILE IS AUTHORITATIVE
get_data_root() {
    local config="${1:-$DEFAULT_CONFIG}"

    # Config file is the single source of truth
    if [ -f "$config" ]; then
        local mode=$(get_mode "$config")
        if [ "$mode" = "production" ]; then
            grep '^production_data_root' "$config" | cut -d'"' -f2
        else
            grep '^test_data_root' "$config" | cut -d'"' -f2
        fi
        return
    fi

    # Fall back to environment variable only if no config file
    if [ -n "${TIMESTD_DATA_ROOT:-}" ]; then
        echo "$TIMESTD_DATA_ROOT"
        return
    fi

    echo "/tmp/timestd-test"
}

# Helper to get log directory - CONFIG FILE IS AUTHORITATIVE
get_log_dir() {
    local config="${1:-$DEFAULT_CONFIG}"
    local mode=$(get_mode "$config")
    
    if [ "$mode" = "production" ]; then
        echo "/var/log/hf-timestd"
    else
        echo "$(get_data_root "$config")/logs"
    fi
}

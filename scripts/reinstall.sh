#!/bin/bash
# Force-reinstall the hf-timestd Python package into the production venv.
# This is a convenience wrapper around ensure-venv.sh --force.
#
# Usage: sudo bash scripts/reinstall.sh
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec bash "$SCRIPT_DIR/ensure-venv.sh" --force

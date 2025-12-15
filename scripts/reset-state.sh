#!/bin/bash
#
# GRAPE State Reset Script
#
# Safely clears persistent state files to allow fresh start.
# Use this when:
#   - Switching between TEST and PRODUCTION modes
#   - State has become corrupted (e.g., D_clock drift)
#   - After major configuration changes
#
# Created: 2025-12-08
# Issues: 2.4 (mode coordination), 2.5 (storage quota)
#

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
CONFIG_FILE="${PROJECT_ROOT}/config/timestd-config.toml"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Parse config to get data root
get_data_root() {
    local mode=$(grep -A10 '^\[recorder\]' "$CONFIG_FILE" | grep 'mode' | head -1 | cut -d'"' -f2)
    
    if [ "$mode" = "production" ]; then
        grep -A10 '^\[recorder\]' "$CONFIG_FILE" | grep 'production_data_root' | head -1 | cut -d'"' -f2
    else
        grep -A10 '^\[recorder\]' "$CONFIG_FILE" | grep 'test_data_root' | head -1 | cut -d'"' -f2
    fi
}

# Get current mode from config
get_mode() {
    grep -A10 '^\[recorder\]' "$CONFIG_FILE" | grep 'mode' | head -1 | cut -d'"' -f2
}

usage() {
    echo "GRAPE State Reset Script"
    echo ""
    echo "Usage: $0 [OPTIONS]"
    echo ""
    echo "Options:"
    echo "  --all              Reset all state files (convergence, calibration, CSVs)"
    echo "  --convergence      Reset only convergence/Kalman state files"
    echo "  --calibration      Reset only calibration files"
    echo "  --csv              Reset only CSV time series files"
    echo "  --channel NAME     Reset state for specific channel only (e.g., 'WWV 10 MHz')"
    echo "  --force            Skip confirmation prompts"
    echo "  --dry-run          Show what would be deleted without actually deleting"
    echo "  -h, --help         Show this help message"
    echo ""
    echo "Examples:"
    echo "  $0 --all                      # Full reset"
    echo "  $0 --convergence              # Reset Kalman filters only"
    echo "  $0 --channel 'WWV 10 MHz'     # Reset single channel"
    echo "  $0 --all --dry-run            # Preview changes"
    echo ""
    echo "Current configuration:"
    echo "  Mode: $(get_mode)"
    echo "  Data root: $(get_data_root)"
}

# Parse arguments
ALL=false
CONVERGENCE=false
CALIBRATION=false
CSV=false
CHANNEL=""
FORCE=false
DRY_RUN=false

while [[ $# -gt 0 ]]; do
    case $1 in
        --all)
            ALL=true
            shift
            ;;
        --convergence)
            CONVERGENCE=true
            shift
            ;;
        --calibration)
            CALIBRATION=true
            shift
            ;;
        --csv)
            CSV=true
            shift
            ;;
        --channel)
            CHANNEL="$2"
            shift 2
            ;;
        --force)
            FORCE=true
            shift
            ;;
        --dry-run)
            DRY_RUN=true
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo -e "${RED}Unknown option: $1${NC}"
            usage
            exit 1
            ;;
    esac
done

# If no specific option given, show usage
if [ "$ALL" = false ] && [ "$CONVERGENCE" = false ] && [ "$CALIBRATION" = false ] && [ "$CSV" = false ]; then
    usage
    exit 1
fi

# Get data root
DATA_ROOT=$(get_data_root)
MODE=$(get_mode)

if [ ! -d "$DATA_ROOT" ]; then
    echo -e "${RED}Error: Data root does not exist: $DATA_ROOT${NC}"
    exit 1
fi

echo -e "${GREEN}GRAPE State Reset${NC}"
echo "Mode: $MODE"
echo "Data root: $DATA_ROOT"
echo ""

# Convert channel name to directory format
channel_to_dir() {
    echo "$1" | tr ' ' '_'
}

# Collect files to delete
FILES_TO_DELETE=()

if [ "$ALL" = true ] || [ "$CONVERGENCE" = true ]; then
    if [ -n "$CHANNEL" ]; then
        CHANNEL_DIR=$(channel_to_dir "$CHANNEL")
        FILES_TO_DELETE+=("$DATA_ROOT/phase2/$CHANNEL_DIR/status/convergence_state.json")
    else
        for f in "$DATA_ROOT"/phase2/*/status/convergence_state.json; do
            [ -f "$f" ] && FILES_TO_DELETE+=("$f")
        done
    fi
fi

if [ "$ALL" = true ] || [ "$CALIBRATION" = true ]; then
    [ -f "$DATA_ROOT/state/broadcast_calibration.json" ] && \
        FILES_TO_DELETE+=("$DATA_ROOT/state/broadcast_calibration.json")
fi

if [ "$ALL" = true ] || [ "$CSV" = true ]; then
    if [ -n "$CHANNEL" ]; then
        CHANNEL_DIR=$(channel_to_dir "$CHANNEL")
        for f in "$DATA_ROOT/phase2/$CHANNEL_DIR/clock_offset"/*.csv; do
            [ -f "$f" ] && FILES_TO_DELETE+=("$f")
        done
    else
        for f in "$DATA_ROOT"/phase2/*/clock_offset/*.csv; do
            [ -f "$f" ] && FILES_TO_DELETE+=("$f")
        done
    fi
fi

# Show what will be deleted
if [ ${#FILES_TO_DELETE[@]} -eq 0 ]; then
    echo -e "${YELLOW}No state files found to delete.${NC}"
    exit 0
fi

echo "Files to be deleted:"
for f in "${FILES_TO_DELETE[@]}"; do
    echo "  - $f"
done
echo ""
echo "Total: ${#FILES_TO_DELETE[@]} file(s)"
echo ""

# Dry run mode
if [ "$DRY_RUN" = true ]; then
    echo -e "${YELLOW}Dry run mode - no files were deleted.${NC}"
    exit 0
fi

# Confirmation
if [ "$FORCE" = false ]; then
    echo -e "${YELLOW}Warning: This will delete persistent state.${NC}"
    echo "Services should be stopped before resetting state."
    echo ""
    read -p "Continue? [y/N] " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        echo "Aborted."
        exit 1
    fi
fi

# Delete files
echo ""
for f in "${FILES_TO_DELETE[@]}"; do
    if [ -f "$f" ]; then
        rm -f "$f"
        echo -e "${GREEN}Deleted:${NC} $f"
    fi
done

echo ""
echo -e "${GREEN}State reset complete.${NC}"
echo ""
echo "Next steps:"
echo "  1. Restart services: ./scripts/grape-analytics.sh -start"
echo "  2. Monitor convergence: ./scripts/grape-analytics.sh -status"
echo "  3. Wait ~30 minutes for calibration to re-learn"

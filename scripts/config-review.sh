#!/bin/bash
#
# config-review.sh - Interactive configuration review and update
#
# Usage:
#   scripts/config-review.sh [--non-interactive]
#
# This script:
# 1. Shows current configuration values for review
# 2. Identifies missing sections/keys from the template
# 3. Prompts user to add missing sections interactively
# 4. Allows confirming or changing existing critical settings
#

set -uo pipefail

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

log_info() { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

# Parse arguments
INTERACTIVE=true
while [[ $# -gt 0 ]]; do
    case $1 in
        --non-interactive)
            INTERACTIVE=false
            shift
            ;;
        --help|-h)
            echo "Usage: $0 [--non-interactive]"
            echo ""
            echo "Options:"
            echo "  --non-interactive  Only show status, don't prompt for changes"
            echo "  --help             Show this help"
            exit 0
            ;;
        *)
            shift
            ;;
    esac
done

# Configuration paths
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEMPLATE_CONFIG="$PROJECT_DIR/config/timestd-config.toml.template"
PROD_CONFIG="/etc/hf-timestd/timestd-config.toml"

# =============================================================================
# Helper Functions
# =============================================================================

# Get value from TOML file (simple parser for key = "value" or key = value)
get_toml_value() {
    local file="$1"
    local section="$2"
    local key="$3"
    
    # Find section and extract key value
    awk -v section="$section" -v key="$key" '
        /^\[/ { in_section = ($0 == "[" section "]") }
        in_section && $1 == key && $2 == "=" {
            # Remove key = 
            sub(/^[^=]*=[ \t]*/, "")
            # Remove trailing comments (# ...)
            sub(/[ \t]*#.*$/, "")
            # Remove quotes
            gsub(/^"/, ""); gsub(/"$/, "")
            gsub(/^'"'"'/, ""); gsub(/'"'"'$/, "")
            print
            exit
        }
    ' "$file" 2>/dev/null
}

# Extract a full section from TOML (including comments)
extract_section() {
    local file="$1"
    local section="$2"
    
    awk -v section="$section" '
        /^\[/ { 
            if (in_section) exit
            in_section = ($0 == "[" section "]")
        }
        in_section { print }
    ' "$file" 2>/dev/null
}

# Prompt for a value with default
prompt_value() {
    local prompt="$1"
    local default="$2"
    local result
    
    if [[ -n "$default" ]]; then
        read -p "$prompt [$default]: " result
        echo "${result:-$default}"
    else
        read -p "$prompt: " result
        echo "$result"
    fi
}

# =============================================================================
# Main Review Function
# =============================================================================

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  HF-TimeStd Configuration Review"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# Check files exist
if [[ ! -f "$TEMPLATE_CONFIG" ]]; then
    log_error "Template not found: $TEMPLATE_CONFIG"
    exit 1
fi

if [[ ! -f "$PROD_CONFIG" ]]; then
    log_warn "Production config not found: $PROD_CONFIG"
    if [[ "$INTERACTIVE" == "true" ]]; then
        read -p "Create from template? [y/N] " -n 1 -r
        echo ""
        if [[ $REPLY =~ ^[Yy]$ ]]; then
            sudo cp "$TEMPLATE_CONFIG" "$PROD_CONFIG"
            log_info "Created $PROD_CONFIG from template"
            log_warn "Please edit with your station-specific values!"
        fi
    fi
    exit 0
fi

# =============================================================================
# Section 1: Review Current Critical Settings
# =============================================================================
echo -e "${BOLD}${CYAN}Current Configuration:${NC}"
echo ""

# Station settings
CALLSIGN=$(get_toml_value "$PROD_CONFIG" "station" "callsign")
GRID=$(get_toml_value "$PROD_CONFIG" "station" "grid_square")
LAT=$(get_toml_value "$PROD_CONFIG" "station" "latitude")
LON=$(get_toml_value "$PROD_CONFIG" "station" "longitude")

echo -e "  ${BOLD}[station]${NC}"
echo -e "    callsign    = ${GREEN}$CALLSIGN${NC}"
echo -e "    grid_square = ${GREEN}$GRID${NC}"
echo -e "    latitude    = ${GREEN}$LAT${NC}"
echo -e "    longitude   = ${GREEN}$LON${NC}"
echo ""

# ka9q settings
STATUS_ADDR=$(get_toml_value "$PROD_CONFIG" "ka9q" "status_address")
SOURCE=$(get_toml_value "$PROD_CONFIG" "ka9q" "source")

echo -e "  ${BOLD}[ka9q]${NC}"
echo -e "    status_address = ${GREEN}$STATUS_ADDR${NC}"
echo -e "    source         = ${GREEN}${SOURCE:-radiod}${NC}"
echo ""

# Recorder settings
MODE=$(get_toml_value "$PROD_CONFIG" "recorder" "mode")
COMPRESSION=$(get_toml_value "$PROD_CONFIG" "recorder" "compression")
TIERED=$(get_toml_value "$PROD_CONFIG" "recorder" "tiered_storage")

echo -e "  ${BOLD}[recorder]${NC}"
echo -e "    mode           = ${GREEN}$MODE${NC}"
echo -e "    compression    = ${GREEN}${COMPRESSION:-none}${NC}"
echo -e "    tiered_storage = ${GREEN}${TIERED:-false}${NC}"
echo ""

# Timing settings (critical - new in v5.4.0)
AUTHORITY=$(get_toml_value "$PROD_CONFIG" "timing" "authority")

echo -e "  ${BOLD}[timing]${NC}"
if [[ -n "$AUTHORITY" ]]; then
    if [[ "$AUTHORITY" == "rtp" ]]; then
        echo -e "    authority = ${GREEN}$AUTHORITY${NC} (GPS+PPS via radiod - authoritative)"
    elif [[ "$AUTHORITY" == "fusion" ]]; then
        echo -e "    authority = ${YELLOW}$AUTHORITY${NC} (NTP only - HF fusion disciplines clock)"
    else
        echo -e "    authority = ${RED}$AUTHORITY${NC} (unknown mode!)"
    fi
else
    echo -e "    ${RED}[timing] section MISSING${NC}"
fi
echo ""

# Count channels
CHANNEL_COUNT=$(grep -c '^\[\[recorder.channels\]\]' "$PROD_CONFIG" 2>/dev/null || echo "0")
echo -e "  ${BOLD}Channels:${NC} $CHANNEL_COUNT configured"
echo ""

# =============================================================================
# Section 2: Check for Missing Sections
# =============================================================================
echo -e "${BOLD}${CYAN}Template Comparison:${NC}"
echo ""

# Get sections (excluding array sections like [[recorder.channels]])
TEMPLATE_SECTIONS=$(grep -E '^\[[a-z]' "$TEMPLATE_CONFIG" | grep -v '^\[\[' | sort -u)
PROD_SECTIONS=$(grep -E '^\[[a-z]' "$PROD_CONFIG" | grep -v '^\[\[' | sort -u)

# Check which optional features are disabled (skip their subsections)
UPLOADER_ENABLED=$(get_toml_value "$PROD_CONFIG" "uploader" "enabled")
GNSS_ENABLED=$(get_toml_value "$PROD_CONFIG" "gnss_vtec" "enabled")

# Define optional sections that depend on parent being enabled
declare -A OPTIONAL_SECTIONS
OPTIONAL_SECTIONS["[uploader.sftp]"]="uploader"
OPTIONAL_SECTIONS["[uploader.metadata]"]="uploader"

MISSING_SECTIONS=()
SKIPPED_SECTIONS=()
while IFS= read -r section; do
    if ! echo "$PROD_SECTIONS" | grep -qF "$section"; then
        # Check if this is an optional section for a disabled feature
        PARENT="${OPTIONAL_SECTIONS[$section]:-}"
        SKIP=false
        
        if [[ "$PARENT" == "uploader" ]] && [[ "$UPLOADER_ENABLED" != "true" ]]; then
            SKIP=true
            SKIPPED_SECTIONS+=("$section (uploader disabled)")
        fi
        
        if [[ "$SKIP" == "false" ]]; then
            MISSING_SECTIONS+=("$section")
        fi
    fi
done <<< "$TEMPLATE_SECTIONS"

if [[ ${#MISSING_SECTIONS[@]} -eq 0 ]]; then
    echo -e "  ${GREEN}✅ All required template sections present${NC}"
else
    echo -e "  ${YELLOW}⚠️  Missing sections:${NC}"
    for section in "${MISSING_SECTIONS[@]}"; do
        echo -e "     ${RED}- $section${NC}"
    done
fi

if [[ ${#SKIPPED_SECTIONS[@]} -gt 0 ]]; then
    echo -e "  ${BLUE}ℹ️  Skipped (feature disabled):${NC}"
    for section in "${SKIPPED_SECTIONS[@]}"; do
        echo -e "     - $section"
    done
fi
echo ""

# =============================================================================
# Section 3: Interactive Updates (if enabled)
# =============================================================================
if [[ "$INTERACTIVE" == "true" ]]; then
    CHANGES_MADE=false
    
    # Confirm critical settings
    echo -e "${BOLD}${CYAN}Confirm Settings:${NC}"
    echo ""
    read -p "Are the above settings correct? [Y/n] " -n 1 -r
    echo ""
    
    if [[ $REPLY =~ ^[Nn]$ ]]; then
        echo ""
        echo "Which setting needs correction?"
        echo "  1) Station (callsign, grid, coordinates)"
        echo "  2) ka9q (status_address)"
        echo "  3) Timing authority (rtp/fusion)"
        echo "  4) Skip - I'll edit manually"
        echo ""
        read -p "Choice [1-4]: " -n 1 -r
        echo ""
        
        case $REPLY in
            1)
                echo ""
                # Defaults: existing config wins; sigmond's commons fill in
                # any field that's empty in the live config (CONTRACT-v0.5 §14).
                NEW_CALL=$(prompt_value "  Callsign" "${CALLSIGN:-${STATION_CALL:-}}")
                NEW_GRID=$(prompt_value "  Grid square" "${GRID:-${STATION_GRID:-}}")
                NEW_LAT=$(prompt_value "  Latitude"  "${LAT:-${STATION_LAT:-}}")
                NEW_LON=$(prompt_value "  Longitude" "${LON:-${STATION_LON:-}}")
                
                if [[ "$NEW_CALL" != "$CALLSIGN" ]] || [[ "$NEW_GRID" != "$GRID" ]] || \
                   [[ "$NEW_LAT" != "$LAT" ]] || [[ "$NEW_LON" != "$LON" ]]; then
                    sudo sed -i "s/^callsign = .*/callsign = \"$NEW_CALL\"/" "$PROD_CONFIG"
                    sudo sed -i "s/^grid_square = .*/grid_square = \"$NEW_GRID\"/" "$PROD_CONFIG"
                    sudo sed -i "s/^latitude = .*/latitude = $NEW_LAT/" "$PROD_CONFIG"
                    sudo sed -i "s/^longitude = .*/longitude = $NEW_LON/" "$PROD_CONFIG"
                    CHANGES_MADE=true
                    log_info "Station settings updated"
                fi
                ;;
            2)
                echo ""
                NEW_ADDR=$(prompt_value "  Status address" "$STATUS_ADDR")
                if [[ "$NEW_ADDR" != "$STATUS_ADDR" ]]; then
                    sudo sed -i "s/^status_address = .*/status_address = \"$NEW_ADDR\"/" "$PROD_CONFIG"
                    CHANGES_MADE=true
                    log_info "ka9q settings updated"
                fi
                ;;
            3)
                echo ""
                echo "  Timing authority options:"
                echo "    rtp    - Radiod has GPS+PPS (authoritative timing)"
                echo "    fusion - NTP only (HF fusion disciplines clock)"
                echo ""
                NEW_AUTH=$(prompt_value "  Authority" "${AUTHORITY:-rtp}")
                if [[ "$NEW_AUTH" != "$AUTHORITY" ]]; then
                    if grep -q '^\[timing\]' "$PROD_CONFIG"; then
                        sudo sed -i "s/^authority = .*/authority = \"$NEW_AUTH\"/" "$PROD_CONFIG"
                    else
                        # Section doesn't exist, add it
                        echo "" | sudo tee -a "$PROD_CONFIG" > /dev/null
                        echo "[timing]" | sudo tee -a "$PROD_CONFIG" > /dev/null
                        echo "authority = \"$NEW_AUTH\"" | sudo tee -a "$PROD_CONFIG" > /dev/null
                    fi
                    CHANGES_MADE=true
                    log_info "Timing authority updated to: $NEW_AUTH"
                fi
                ;;
            *)
                log_info "Edit manually: sudo nano $PROD_CONFIG"
                ;;
        esac
    fi
    
    # Add missing sections
    if [[ ${#MISSING_SECTIONS[@]} -gt 0 ]]; then
        echo ""
        read -p "Add missing sections from template? [y/N] " -n 1 -r
        echo ""
        
        if [[ $REPLY =~ ^[Yy]$ ]]; then
            for section in "${MISSING_SECTIONS[@]}"; do
                # Extract section name without brackets
                section_name="${section#[}"
                section_name="${section_name%]}"
                
                echo ""
                echo -e "${BOLD}Adding: $section${NC}"
                
                # Extract section from template
                SECTION_CONTENT=$(extract_section "$TEMPLATE_CONFIG" "$section_name")
                
                if [[ -n "$SECTION_CONTENT" ]]; then
                    echo "$SECTION_CONTENT"
                    echo ""
                    read -p "Add this section? [Y/n] " -n 1 -r
                    echo ""
                    
                    if [[ ! $REPLY =~ ^[Nn]$ ]]; then
                        echo "" | sudo tee -a "$PROD_CONFIG" > /dev/null
                        echo "$SECTION_CONTENT" | sudo tee -a "$PROD_CONFIG" > /dev/null
                        CHANGES_MADE=true
                        log_info "Added $section"
                    fi
                fi
            done
        fi
    fi
    
    if [[ "$CHANGES_MADE" == "true" ]]; then
        echo ""
        log_info "Configuration updated: $PROD_CONFIG"
        log_warn "Restart services to apply changes"
    fi
else
    # Non-interactive: auto-apply missing sections from template
    if [[ ${#MISSING_SECTIONS[@]} -gt 0 ]]; then
        echo ""
        log_info "Auto-applying missing sections (non-interactive)..."
        for section in "${MISSING_SECTIONS[@]}"; do
            section_name="${section#[}"
            section_name="${section_name%]}"
            SECTION_CONTENT=$(extract_section "$TEMPLATE_CONFIG" "$section_name")
            if [[ -n "$SECTION_CONTENT" ]]; then
                echo "" | sudo tee -a "$PROD_CONFIG" > /dev/null
                echo "$SECTION_CONTENT" | sudo tee -a "$PROD_CONFIG" > /dev/null
                log_info "Added $section (defaults from template)"
            fi
        done
        log_warn "Restart services to apply changes"
    fi
fi

# =============================================================================
# Summary
# =============================================================================
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Config file: $PROD_CONFIG"
echo "  Template:    $TEMPLATE_CONFIG"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

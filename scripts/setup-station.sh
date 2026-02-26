#!/bin/bash
# =============================================================================
# HF Time Standard — Interactive Station Configuration Wizard
# =============================================================================
# Collects station-specific information and generates timestd-config.toml.
#
# Prerequisites the operator should have in hand:
#   - Station callsign and Maidenhead grid square
#   - Precise latitude/longitude (decimal degrees)
#   - ka9q-radio status multicast address (e.g. hf-status.local)
#   - Knowledge of timing source: GPS+PPS on radiod LAN, or NTP only
#   - (Optional) PSWS station ID and instrument ID for GRAPE uploads
#   - (Optional) GNSS VTEC receiver host:port (e.g. ZED-F9P via ser2net)
#
# Usage:
#   sudo ./scripts/setup-station.sh                 # interactive
#   sudo ./scripts/setup-station.sh --config /path   # write to specific path
#   sudo ./scripts/setup-station.sh --reconfig       # re-run even if config exists
#
# This script is idempotent: it can be re-run to change settings.
# It will not overwrite an existing config unless --reconfig is given
# or the operator confirms.
# =============================================================================

set -euo pipefail

# =============================================================================
# Constants
# =============================================================================
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
TEMPLATE="$PROJECT_DIR/config/timestd-config.toml.template"
DEFAULT_CONFIG="/etc/hf-timestd/timestd-config.toml"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
BOLD='\033[1m'
DIM='\033[2m'
NC='\033[0m'

log_info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $*"; }

# =============================================================================
# Parse Arguments
# =============================================================================
CONFIG_PATH="$DEFAULT_CONFIG"
RECONFIG=false

while [[ $# -gt 0 ]]; do
    case $1 in
        --config)  CONFIG_PATH="$2"; shift 2 ;;
        --reconfig) RECONFIG=true; shift ;;
        --help|-h)
            echo "Usage: $0 [--config /path/to/config.toml] [--reconfig]"
            echo ""
            echo "Options:"
            echo "  --config PATH   Write config to PATH (default: $DEFAULT_CONFIG)"
            echo "  --reconfig      Re-run wizard even if config already exists"
            echo "  --help          Show this help"
            exit 0
            ;;
        *) log_error "Unknown option: $1"; exit 1 ;;
    esac
done

# =============================================================================
# Preflight
# =============================================================================
if [[ ! -f "$TEMPLATE" ]]; then
    log_error "Config template not found: $TEMPLATE"
    log_error "Are you running from the hf-timestd repository root?"
    exit 1
fi

if [[ -f "$CONFIG_PATH" && "$RECONFIG" == "false" ]]; then
    echo ""
    echo -e "${YELLOW}Configuration already exists:${NC} $CONFIG_PATH"
    read -rp "Overwrite with new settings? [y/N] " overwrite
    if [[ ! "$overwrite" =~ ^[Yy]$ ]]; then
        log_info "Keeping existing configuration."
        exit 0
    fi
fi

# Ensure config directory exists
CONFIG_DIR="$(dirname "$CONFIG_PATH")"
if [[ ! -d "$CONFIG_DIR" ]]; then
    if [[ "$EUID" -eq 0 ]]; then
        mkdir -p "$CONFIG_DIR"
    else
        sudo mkdir -p "$CONFIG_DIR"
    fi
fi

# =============================================================================
# Helper: prompt with default, validation, and help text
# =============================================================================
prompt() {
    local varname="$1"
    local prompt_text="$2"
    local default="${3:-}"
    local help_text="${4:-}"
    local required="${5:-false}"
    local result=""

    if [[ -n "$help_text" ]]; then
        echo -e "  ${DIM}${help_text}${NC}"
    fi

    while true; do
        if [[ -n "$default" ]]; then
            read -rp "  $prompt_text [$default]: " result
            result="${result:-$default}"
        else
            read -rp "  $prompt_text: " result
        fi

        if [[ -z "$result" && "$required" == "true" ]]; then
            echo -e "  ${RED}This field is required.${NC}"
            continue
        fi
        break
    done

    # Set the variable in the caller's scope
    printf -v "$varname" '%s' "$result"
}

prompt_yn() {
    local varname="$1"
    local prompt_text="$2"
    local default="${3:-n}"
    local result=""

    while true; do
        read -rp "  $prompt_text [$(echo "$default" | sed 's/y/Y\/n/;s/n/y\/N/')]: " result
        result="${result:-$default}"
        case "$result" in
            [Yy]*) printf -v "$varname" 'true'; return ;;
            [Nn]*) printf -v "$varname" 'false'; return ;;
            *) echo "  Please enter y or n." ;;
        esac
    done
}

prompt_choice() {
    local varname="$1"
    local prompt_text="$2"
    shift 2
    local options=("$@")

    echo ""
    for i in "${!options[@]}"; do
        echo -e "    $((i+1))) ${options[$i]}"
    done
    echo ""

    while true; do
        read -rp "  $prompt_text [1-${#options[@]}]: " choice
        if [[ "$choice" =~ ^[0-9]+$ ]] && (( choice >= 1 && choice <= ${#options[@]} )); then
            # Extract just the key (first word before the dash/space description)
            local selected="${options[$((choice-1))]}"
            selected="${selected%% —*}"
            selected="${selected%% -*}"
            selected="$(echo "$selected" | xargs)"  # trim whitespace
            printf -v "$varname" '%s' "$selected"
            return
        fi
        echo "  Invalid choice."
    done
}

# =============================================================================
# Banner
# =============================================================================
clear 2>/dev/null || true
echo ""
echo -e "${BOLD}${CYAN}"
echo "  ╔══════════════════════════════════════════════════════════╗"
echo "  ║       HF Time Standard — Station Configuration         ║"
echo "  ╚══════════════════════════════════════════════════════════╝"
echo -e "${NC}"
echo -e "  Before you begin, have the following information ready:"
echo ""
echo -e "    ${BOLD}Required:${NC}"
echo "      - Your amateur radio callsign"
echo "      - Your Maidenhead grid square (6 or 10 character)"
echo "      - Your precise latitude and longitude (decimal degrees)"
echo "      - Your ka9q-radio status multicast address"
echo ""
echo -e "    ${BOLD}Optional:${NC}"
echo "      - PSWS station ID and instrument ID (for GRAPE uploads)"
echo "      - PSWS TOKEN (shown on your PSWS site admin page)"
echo "      - GNSS VTEC receiver address (if you have a ZED-F9P or similar)"
echo ""
read -rp "  Press Enter to continue..."

# =============================================================================
# Section 1: Station Identity
# =============================================================================
echo ""
echo -e "${BOLD}${BLUE}━━━ Section 1: Station Identity ━━━${NC}"
echo ""

prompt CALLSIGN "Callsign" "" "Your amateur radio callsign (e.g. W1ABC)" true
prompt GRID_SQUARE "Grid square" "" "Maidenhead locator, 6 or 10 chars (e.g. FN42ab12cd)" true
prompt LATITUDE "Latitude" "" "Decimal degrees, positive = North (e.g. 42.3601)" true
prompt LONGITUDE "Longitude" "" "Decimal degrees, positive = East, negative = West (e.g. -71.0589)" true
prompt DESCRIPTION "Station description" "${CALLSIGN} hf-timestd" "Free text description of your setup"

# =============================================================================
# Section 2: PSWS Upload (Optional)
# =============================================================================
echo ""
echo -e "${BOLD}${BLUE}━━━ Section 2: PSWS / GRAPE Upload ━━━${NC}"
echo ""
echo -e "  ${DIM}The Personal Space Weather Station network collects HF propagation data.${NC}"
echo -e "  ${DIM}If you have a PSWS account, enter your station and instrument IDs.${NC}"
echo -e "  ${DIM}You can set this up later by re-running this wizard.${NC}"
echo ""

prompt_yn PSWS_ENABLED "Enable PSWS/GRAPE uploads?" "n"

STATION_ID=""
INSTRUMENT_ID=""
UPLOADER_ENABLED="false"

if [[ "$PSWS_ENABLED" == "true" ]]; then
    prompt STATION_ID "PSWS Station ID" "" "e.g. S000171 (shown on your PSWS site admin page)" true
    prompt INSTRUMENT_ID "PSWS Instrument ID" "" "e.g. 172 (shown on your PSWS site admin page)" true
    UPLOADER_ENABLED="true"
fi

# =============================================================================
# Section 3: Radio Source
# =============================================================================
echo ""
echo -e "${BOLD}${BLUE}━━━ Section 3: Radio Source (ka9q-radio) ━━━${NC}"
echo ""

prompt KA9Q_STATUS "ka9q-radio status address" "" \
    "Multicast address or mDNS name (e.g. hf-status.local or 239.x.x.x)" true

echo ""
echo -e "  ${DIM}Data source mode:${NC}"
prompt_choice KA9Q_SOURCE "Select source mode" \
    "radiod — Single SDR receiver (standard)" \
    "phase-engine — Multi-antenna coherent beamformer"

# =============================================================================
# Section 4: Timing Authority
# =============================================================================
echo ""
echo -e "${BOLD}${BLUE}━━━ Section 4: Timing Authority ━━━${NC}"
echo ""
echo -e "  ${DIM}How does your radiod get its time reference?${NC}"
echo ""

prompt_choice TIMING_AUTHORITY "Select timing mode" \
    "rtp — Radiod has GPS+PPS feed (most accurate, recommended)" \
    "fusion — Radiod uses NTP only (HF fusion disciplines the clock)"

RTP_ACCURACY="0.001"
if [[ "$TIMING_AUTHORITY" == "rtp" ]]; then
    echo ""
    echo -e "  ${DIM}GPS+PPS accuracy depends on connection type:${NC}"
    prompt_choice RTP_ACCURACY_CHOICE "GPS+PPS connection" \
        "lan — GPS+PPS via LAN (1 us accuracy, most common)" \
        "local — GPS+PPS directly connected (100 ns accuracy)" \
        "ntp — GPS via NTP only (1 ms accuracy)"

    case "$RTP_ACCURACY_CHOICE" in
        local) RTP_ACCURACY="0.0001" ;;
        lan)   RTP_ACCURACY="0.001" ;;
        ntp)   RTP_ACCURACY="1.0" ;;
    esac
fi

# =============================================================================
# Section 5: GNSS VTEC (Optional)
# =============================================================================
echo ""
echo -e "${BOLD}${BLUE}━━━ Section 5: GNSS VTEC Monitoring (Optional) ━━━${NC}"
echo ""
echo -e "  ${DIM}A dual-frequency GNSS receiver (e.g. ZED-F9P) provides real-time${NC}"
echo -e "  ${DIM}ionospheric TEC measurements for improved L2 timing corrections.${NC}"
echo ""

prompt_yn VTEC_ENABLED "Do you have a GNSS VTEC receiver?" "n"

VTEC_HOST=""
VTEC_PORT="9000"

if [[ "$VTEC_ENABLED" == "true" ]]; then
    prompt VTEC_HOST "GNSS receiver host" "" "IP address or hostname (e.g. 192.168.0.203)" true
    prompt VTEC_PORT "GNSS receiver port" "9000" "TCP port for UBX data stream"
fi

# =============================================================================
# Section 6: Compression
# =============================================================================
echo ""
echo -e "${BOLD}${BLUE}━━━ Section 6: Storage Options ━━━${NC}"
echo ""

prompt_choice COMPRESSION "IQ archive compression" \
    "zstd — Best compression ratio, moderate CPU (recommended)" \
    "lz4 — Fastest, lower compression ratio" \
    "none — No compression (highest disk usage)"

# =============================================================================
# Summary and Confirmation
# =============================================================================
echo ""
echo -e "${BOLD}${CYAN}"
echo "  ╔══════════════════════════════════════════════════════════╗"
echo "  ║                Configuration Summary                    ║"
echo "  ╚══════════════════════════════════════════════════════════╝"
echo -e "${NC}"
echo -e "  ${BOLD}Station:${NC}"
echo "    Callsign:     $CALLSIGN"
echo "    Grid square:  $GRID_SQUARE"
echo "    Latitude:     $LATITUDE"
echo "    Longitude:    $LONGITUDE"
echo "    Description:  $DESCRIPTION"
echo ""
echo -e "  ${BOLD}Radio:${NC}"
echo "    ka9q status:  $KA9Q_STATUS"
echo "    Source mode:   $KA9Q_SOURCE"
echo ""
echo -e "  ${BOLD}Timing:${NC}"
echo "    Authority:    $TIMING_AUTHORITY"
if [[ "$TIMING_AUTHORITY" == "rtp" ]]; then
echo "    RTP accuracy: ${RTP_ACCURACY} ms"
fi
echo ""
echo -e "  ${BOLD}GNSS VTEC:${NC}"
if [[ "$VTEC_ENABLED" == "true" ]]; then
echo "    Enabled:      yes ($VTEC_HOST:$VTEC_PORT)"
else
echo "    Enabled:      no"
fi
echo ""
echo -e "  ${BOLD}PSWS Upload:${NC}"
if [[ "$UPLOADER_ENABLED" == "true" ]]; then
echo "    Enabled:      yes (Station: $STATION_ID, Instrument: $INSTRUMENT_ID)"
else
echo "    Enabled:      no"
fi
echo ""
echo -e "  ${BOLD}Storage:${NC}"
echo "    Compression:  $COMPRESSION"
echo ""
echo -e "  ${BOLD}Config will be written to:${NC} $CONFIG_PATH"
echo ""

read -rp "  Write this configuration? [Y/n] " confirm
confirm="${confirm:-Y}"
if [[ ! "$confirm" =~ ^[Yy]$ ]]; then
    log_info "Aborted. No changes made."
    exit 0
fi

# =============================================================================
# Generate Config from Template
# =============================================================================
log_info "Generating configuration..."

# Use Python for section-aware TOML substitution.
# Keys like 'host', 'port', 'enabled' appear in multiple sections —
# sed would clobber the wrong one.  This script reads the template line by
# line, tracks the current [section], and only substitutes values when
# section + key match.

# Export collected values for the Python config generator.
# The heredoc is single-quoted (no bash expansion inside), so we pass
# values through the environment with a WIZ_ prefix.
export WIZ_CALLSIGN="$CALLSIGN"
export WIZ_GRID_SQUARE="$GRID_SQUARE"
export WIZ_LATITUDE="$LATITUDE"
export WIZ_LONGITUDE="$LONGITUDE"
export WIZ_DESCRIPTION="$DESCRIPTION"
export WIZ_STATION_ID="$STATION_ID"
export WIZ_INSTRUMENT_ID="$INSTRUMENT_ID"
export WIZ_KA9Q_STATUS="$KA9Q_STATUS"
export WIZ_KA9Q_SOURCE="$KA9Q_SOURCE"
export WIZ_COMPRESSION="$COMPRESSION"
export WIZ_TIMING_AUTHORITY="$TIMING_AUTHORITY"
export WIZ_RTP_ACCURACY="$RTP_ACCURACY"
export WIZ_VTEC_ENABLED="$VTEC_ENABLED"
export WIZ_VTEC_HOST="$VTEC_HOST"
export WIZ_VTEC_PORT="$VTEC_PORT"
export WIZ_UPLOADER_ENABLED="$UPLOADER_ENABLED"

PYTHON_BIN="python3"
# If venv exists (re-run scenario), prefer it for consistency
if [[ -x "/opt/hf-timestd/venv/bin/python3" ]]; then
    PYTHON_BIN="/opt/hf-timestd/venv/bin/python3"
fi

"$PYTHON_BIN" - "$TEMPLATE" "$CONFIG_PATH" <<'PYEOF'
import sys, re

template_path = sys.argv[1]
output_path   = sys.argv[2]

# Substitutions: (section_prefix, key) -> new_value
# section_prefix uses dotted form matching the TOML header,
# e.g. "station", "ka9q", "uploader.sftp", "gnss_vtec"
# A value of None means "leave template default".
import os
env = os.environ

def e(name, default=""):
    return env.get(name, default)

subs = {}

def set_str(section, key, val):
    """Set a string value (will be quoted in TOML)."""
    if val:
        subs[(section, key)] = f'"{val}"'

def set_bare(section, key, val):
    """Set a bare value (number or boolean, not quoted)."""
    if val:
        subs[(section, key)] = val

# Station
set_str("station", "callsign", e("WIZ_CALLSIGN"))
set_str("station", "grid_square", e("WIZ_GRID_SQUARE"))
set_bare("station", "latitude", e("WIZ_LATITUDE"))
set_bare("station", "longitude", e("WIZ_LONGITUDE"))
set_str("station", "description", e("WIZ_DESCRIPTION"))
set_str("station", "id", e("WIZ_STATION_ID"))
set_str("station", "instrument_id", e("WIZ_INSTRUMENT_ID"))

# ka9q
set_str("ka9q", "status_address", e("WIZ_KA9Q_STATUS"))
set_str("ka9q", "source", e("WIZ_KA9Q_SOURCE"))

# Recorder
set_str("recorder", "mode", "production")
set_str("recorder", "compression", e("WIZ_COMPRESSION"))

# Timing
set_str("timing", "authority", e("WIZ_TIMING_AUTHORITY"))
rtp_acc = e("WIZ_RTP_ACCURACY")
if rtp_acc:
    set_bare("timing", "rtp_expected_accuracy_ms", rtp_acc)

# GNSS VTEC
set_bare("gnss_vtec", "enabled", e("WIZ_VTEC_ENABLED"))
vtec_host = e("WIZ_VTEC_HOST")
if vtec_host:
    set_str("gnss_vtec", "host", vtec_host)
vtec_port = e("WIZ_VTEC_PORT")
if vtec_port:
    set_bare("gnss_vtec", "port", vtec_port)

# Uploader
set_bare("uploader", "enabled", e("WIZ_UPLOADER_ENABLED"))

# SFTP key path
station_id = e("WIZ_STATION_ID")
if station_id:
    set_str("uploader.sftp", "ssh_key",
            f"/home/timestd/.ssh/id_rsa_psws_{station_id}")

# --- Process template ---
KEY_RE = re.compile(r'^(\s*)([\w_]+)(\s*=\s*)(.*)')
SECTION_RE = re.compile(r'^\s*\[([^\]]+)\]')

current_section = ""
out_lines = []

with open(template_path, "r") as f:
    for line in f:
        # Track current section header
        m = SECTION_RE.match(line)
        if m:
            current_section = m.group(1).strip()

        # Check if this line has a key we want to substitute
        km = KEY_RE.match(line)
        if km:
            indent, key, eq, old_val = km.groups()
            lookup = (current_section, key)
            if lookup in subs:
                new_val = subs[lookup]
                # Preserve inline comment if present
                # Comments start after the value, separated by whitespace + #
                comment = ""
                # Strip the old value to find trailing comment
                # TOML inline comments: value then optional whitespace then #
                # Be careful not to match # inside quoted strings
                stripped = old_val.strip()
                if stripped.startswith('"'):
                    # find closing quote, then look for #
                    end_q = stripped.find('"', 1)
                    if end_q > 0:
                        rest = stripped[end_q+1:].strip()
                        if rest.startswith('#'):
                            comment = "  " + rest
                elif stripped.startswith("'"):
                    end_q = stripped.find("'", 1)
                    if end_q > 0:
                        rest = stripped[end_q+1:].strip()
                        if rest.startswith('#'):
                            comment = "  " + rest
                else:
                    # bare value — find first #
                    parts = stripped.split('#', 1)
                    if len(parts) > 1:
                        comment = "  # " + parts[1].strip()

                line = f"{indent}{key}{eq}{new_val}{comment}\n"

        out_lines.append(line)

with open(output_path, "w") as f:
    f.writelines(out_lines)

print(f"Config written: {output_path}")
PYEOF

# Fix ownership and permissions
if [[ "$EUID" -eq 0 ]]; then
    if id -u timestd &>/dev/null; then
        chown timestd:timestd "$CONFIG_PATH"
    fi
fi
chmod 640 "$CONFIG_PATH" 2>/dev/null || sudo chmod 640 "$CONFIG_PATH"

log_info "Configuration written to: $CONFIG_PATH"

# =============================================================================
# Generate environment file
# =============================================================================
ENV_FILE="$CONFIG_DIR/environment"

ENV_CONTENT="# HF Time Standard Environment
# Generated by setup-station.sh on $(date -u +%Y-%m-%dT%H:%M:%SZ)

TIMESTD_MODE=production
TIMESTD_DATA_ROOT=/var/lib/timestd
TIMESTD_LOG_DIR=/var/log/hf-timestd
TIMESTD_CONFIG=$CONFIG_PATH
TIMESTD_PROJECT=/opt/hf-timestd
TIMESTD_INSTALL_DIR=/opt/hf-timestd
TIMESTD_WEBUI=/opt/hf-timestd/web-api
TIMESTD_VENV=/opt/hf-timestd/venv
TIMESTD_LOG_LEVEL=INFO"

if [[ "$EUID" -eq 0 ]]; then
    echo "$ENV_CONTENT" > "$ENV_FILE"
    if id -u timestd &>/dev/null; then
        chown timestd:timestd "$ENV_FILE"
    fi
else
    echo "$ENV_CONTENT" | sudo tee "$ENV_FILE" > /dev/null
fi

log_info "Environment file written to: $ENV_FILE"

# =============================================================================
# Next Steps
# =============================================================================
echo ""
echo -e "${BOLD}${GREEN}"
echo "  ╔══════════════════════════════════════════════════════════╗"
echo "  ║              Station Configuration Complete              ║"
echo "  ╚══════════════════════════════════════════════════════════╝"
echo -e "${NC}"

if [[ "$UPLOADER_ENABLED" == "true" ]]; then
    echo -e "  ${BOLD}PSWS Key Setup:${NC}"
    echo "    Run after install.sh completes to set up secure SFTP uploads:"
    echo "      sudo ./scripts/setup-psws-keys.sh"
    echo "    You will need your PSWS TOKEN (from https://pswsnetwork.caps.ua.edu/)"
    echo ""
fi

echo -e "  ${BOLD}To review or change settings later:${NC}"
echo "    sudo ./scripts/setup-station.sh --reconfig"
echo "    sudo nano $CONFIG_PATH"
echo ""

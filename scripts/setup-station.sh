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

# =============================================================================
# Helper: auto-fill from sigmond's env-var bag (CONTRACT-v0.5 §14.2),
# fall through to prompt() when sigmond hasn't published the value.
#
# Cross-cutting fields (callsign, grid/location, ka9q-radio status address)
# live in /etc/sigmond/coordination.toml and are exposed to client config
# wizards via STATION_CALL / STATION_GRID / STATION_LAT / STATION_LON /
# SIGMOND_RADIOD_STATUS.  When sigmond knows them, we silently use the
# value and tell the operator we did — no need to ask the same callsign
# into five different wizards.
# =============================================================================
auto_or_prompt() {
    local varname="$1"
    local prompt_text="$2"
    local source_var="$3"        # env var name, e.g. STATION_CALL
    local help_text="${4:-}"
    local required="${5:-false}"

    local source_value="${!source_var:-}"
    if [[ -n "$source_value" ]]; then
        printf -v "$varname" '%s' "$source_value"
        echo -e "  ${GREEN}✓${NC} $prompt_text: ${BOLD}${source_value}${NC}  ${DIM}(from sigmond \$$source_var)${NC}"
        return
    fi
    prompt "$varname" "$prompt_text" "" "$help_text" "$required"
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

# Show what sigmond has already provided so the operator knows we won't re-ask.
_sigmond_known=()
[[ -n "${STATION_CALL:-}" ]]            && _sigmond_known+=("Callsign:           ${STATION_CALL}")
[[ -n "${STATION_GRID:-}" ]]            && _sigmond_known+=("Grid square:        ${STATION_GRID}")
[[ -n "${STATION_LAT:-}" ]]             && _sigmond_known+=("Latitude:           ${STATION_LAT}")
[[ -n "${STATION_LON:-}" ]]             && _sigmond_known+=("Longitude:          ${STATION_LON}")
[[ -n "${SIGMOND_RADIOD_STATUS:-}" ]]   && _sigmond_known+=("ka9q-radio status:  ${SIGMOND_RADIOD_STATUS}")

if [[ ${#_sigmond_known[@]} -gt 0 ]]; then
    echo -e "  ${BOLD}Already known from sigmond${NC} ${DIM}(no input needed)${NC}:"
    for line in "${_sigmond_known[@]}"; do
        echo -e "    ${GREEN}✓${NC} $line"
    done
    echo ""
fi

echo -e "  ${BOLD}You'll be asked about:${NC}"
[[ -z "${STATION_CALL:-}" ]] && echo "    - Your amateur radio callsign"
if [[ -z "${STATION_GRID:-}" && ( -z "${STATION_LAT:-}" || -z "${STATION_LON:-}" ) ]]; then
    echo "    - Your station location (Maidenhead grid OR latitude/longitude)"
fi
[[ -z "${SIGMOND_RADIOD_STATUS:-}" ]] && echo "    - Your ka9q-radio status multicast address"
echo "    - Timing source (radiod's clock authority)"
echo "    - Optional: PSWS station/instrument IDs + TOKEN (for GRAPE uploads)"
echo "    - Optional: GNSS VTEC receiver address (if you have a ZED-F9P)"
echo ""
read -rp "  Press Enter to continue..."

# =============================================================================
# Section 1: Station Identity
# =============================================================================
echo ""
echo -e "${BOLD}${BLUE}━━━ Section 1: Station Identity ━━━${NC}"
echo ""

# Cross-cutting fields published by sigmond (CONTRACT-v0.5 §14.2) are
# silently auto-filled; we only prompt when sigmond hasn't supplied them.
auto_or_prompt CALLSIGN "Callsign" STATION_CALL "Your amateur radio callsign (e.g. W1ABC)" true

GRID_SQUARE=""
LATITUDE=""
LONGITUDE=""

# Pick the location-mode from whatever sigmond gave us.  If neither
# coordinate form is published, ask the operator which they prefer.
if [[ -n "${STATION_GRID:-}" ]]; then
    LOCATION_MODE="grid"
elif [[ -n "${STATION_LAT:-}" && -n "${STATION_LON:-}" ]]; then
    LOCATION_MODE="latlon"
else
    echo ""
    echo -e "  ${DIM}Location entry method:${NC}"
    prompt_choice LOCATION_MODE "Select location input" \
        "grid — Enter Maidenhead grid square (6 or 10 chars)" \
        "latlon — Enter latitude/longitude (decimal degrees)"
fi

if [[ "$LOCATION_MODE" == "grid" ]]; then
    auto_or_prompt GRID_SQUARE "Grid square" STATION_GRID \
        "Maidenhead locator, 6 or 10 chars (e.g. FN42ab12cd). More precision is better." true
    # Lat/Lon will be derived from grid in the config generator.
else
    auto_or_prompt LATITUDE  "Latitude"  STATION_LAT \
        "Decimal degrees, positive = North. More precision is better." true
    auto_or_prompt LONGITUDE "Longitude" STATION_LON \
        "Decimal degrees, positive = East, negative = West. More precision is better." true
    # Grid will be derived from lat/lon in the config generator.
fi

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

auto_or_prompt KA9Q_STATUS "ka9q-radio status address" SIGMOND_RADIOD_STATUS \
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

# L6 BPSK PPS chain-delay calibration (advanced, requires WB6CXC injector)
echo ""
echo -e "  ${DIM}L6 chain-delay calibration uses a local BPSK PPS signal injected${NC}"
echo -e "  ${DIM}into the RF front-end to measure and correct end-to-end timing.${NC}"
echo -e "  ${DIM}This requires the WB6CXC PPS injector hardware and GPS+PPS.${NC}"
echo ""
prompt_yn L6_PPS_ENABLED "Do you have a BPSK PPS injector?" "n"

L6_PPS_FREQUENCY=""
if [[ "$L6_PPS_ENABLED" == "true" ]]; then
    prompt L6_PPS_FREQUENCY "Injector RF frequency (Hz)" "" "e.g. 3500000 for 3.5 MHz" true
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

echo -e "  ${DIM}IQ archiving writes raw data to disk for physics post-processing.${NC}"
echo -e "  ${DIM}Disable if you only need real-time timing (WSPR, CODAR, etc.).${NC}"
echo ""
prompt_yn ARCHIVE_ENABLED "Archive time standard IQ data to disk?" "y"

COMPRESSION="zstd"
if [[ "$ARCHIVE_ENABLED" == "true" ]]; then
    echo ""
    prompt_choice COMPRESSION "IQ archive compression" \
        "zstd — Best compression ratio, moderate CPU (recommended)" \
        "lz4 — Fastest, lower compression ratio" \
        "none — No compression (highest disk usage)"
fi

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
echo "    Location mode: $LOCATION_MODE"
echo "    Grid square:  ${GRID_SQUARE:-<derived>}"
echo "    Latitude:     ${LATITUDE:-<derived>}"
echo "    Longitude:    ${LONGITUDE:-<derived>}"
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
if [[ "$L6_PPS_ENABLED" == "true" ]]; then
echo "    L6 PPS:       enabled (${L6_PPS_FREQUENCY} Hz)"
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
if [[ "$ARCHIVE_ENABLED" == "true" ]]; then
echo "    IQ archiving: enabled (compression: $COMPRESSION)"
else
echo "    IQ archiving: disabled (stream-only, no cold storage)"
fi
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
export WIZ_L6_PPS_ENABLED="$L6_PPS_ENABLED"
export WIZ_L6_PPS_FREQUENCY="$L6_PPS_FREQUENCY"
export WIZ_ARCHIVE_ENABLED="$ARCHIVE_ENABLED"

PYTHON_BIN="python3"
# If venv exists (re-run scenario), prefer it for consistency
if [[ -x "/opt/git/sigmond/hf-timestd/venv/bin/python3" ]]; then
    PYTHON_BIN="/opt/git/sigmond/hf-timestd/venv/bin/python3"
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
grid_in = e("WIZ_GRID_SQUARE")
lat_in = e("WIZ_LATITUDE")
lon_in = e("WIZ_LONGITUDE")

def _grid_to_latlon_center(grid: str):
    g = "".join(ch for ch in (grid or "") if not ch.isspace())
    g = g.strip()
    if not g:
        return None
    g = g.upper()
    if len(g) < 4:
        return None
    if len(g) not in (4, 6, 8, 10):
        return None
    A = ord('A')
    lon = -180.0 + 20.0 * (ord(g[0]) - A)
    lat = -90.0 + 10.0 * (ord(g[1]) - A)
    lon_w = 20.0
    lat_h = 10.0
    lon += 2.0 * int(g[2])
    lat += 1.0 * int(g[3])
    lon_w = 2.0
    lat_h = 1.0
    if len(g) >= 6:
        lon += (5.0/60.0) * (ord(g[4]) - A)
        lat += (2.5/60.0) * (ord(g[5]) - A)
        lon_w = 5.0/60.0
        lat_h = 2.5/60.0
    if len(g) >= 8:
        lon += (0.5/60.0) * int(g[6])
        lat += (0.25/60.0) * int(g[7])
        lon_w = 0.5/60.0
        lat_h = 0.25/60.0
    if len(g) >= 10:
        # subsquares: 24 letters a-x
        lon += (0.5/60.0/24.0) * (ord(g[8]) - A)
        lat += (0.25/60.0/24.0) * (ord(g[9]) - A)
        lon_w = 0.5/60.0/24.0
        lat_h = 0.25/60.0/24.0
    return (lat + lat_h/2.0, lon + lon_w/2.0)

def _latlon_to_grid(lat: float, lon: float, precision: int = 10) -> str:
    # precision: 4,6,8,10
    if precision not in (4, 6, 8, 10):
        precision = 10
    lon = float(lon)
    lat = float(lat)
    # clamp to valid range (avoid edge cases exactly at +/-180)
    lon = max(-179.999999, min(179.999999, lon))
    lat = max(-89.999999, min(89.999999, lat))
    A = ord('A')
    lon_adj = lon + 180.0
    lat_adj = lat + 90.0
    lon_field = int(lon_adj // 20.0)
    lat_field = int(lat_adj // 10.0)
    g = [chr(A + lon_field), chr(A + lat_field)]
    lon_adj -= lon_field * 20.0
    lat_adj -= lat_field * 10.0
    lon_square = int(lon_adj // 2.0)
    lat_square = int(lat_adj // 1.0)
    g += [str(lon_square), str(lat_square)]
    if precision >= 6:
        lon_adj -= lon_square * 2.0
        lat_adj -= lat_square * 1.0
        lon_sub = int(lon_adj / (5.0/60.0))
        lat_sub = int(lat_adj / (2.5/60.0))
        g += [chr(A + lon_sub), chr(A + lat_sub)]
    if precision >= 8:
        lon_adj -= lon_sub * (5.0/60.0)
        lat_adj -= lat_sub * (2.5/60.0)
        lon_ext = int(lon_adj / (0.5/60.0))
        lat_ext = int(lat_adj / (0.25/60.0))
        g += [str(lon_ext), str(lat_ext)]
    if precision >= 10:
        lon_adj -= lon_ext * (0.5/60.0)
        lat_adj -= lat_ext * (0.25/60.0)
        lon_sub2 = int(lon_adj / ((0.5/60.0)/24.0))
        lat_sub2 = int(lat_adj / ((0.25/60.0)/24.0))
        g += [chr(A + lon_sub2), chr(A + lat_sub2)]
    return "".join(g)

def _parse_float(s: str):
    s = (s or "").strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None

lat_val = _parse_float(lat_in)
lon_val = _parse_float(lon_in)
grid_val = (grid_in or "").strip()

if grid_val and (lat_val is None or lon_val is None):
    ll = _grid_to_latlon_center(grid_val)
    if ll is None:
        raise SystemExit(f"Invalid grid square: {grid_val}")
    lat_val, lon_val = ll

if (lat_val is not None and lon_val is not None) and not grid_val:
    grid_val = _latlon_to_grid(lat_val, lon_val, precision=10)

if not grid_val:
    raise SystemExit("Missing station location: provide grid square or latitude/longitude")
if lat_val is None or lon_val is None:
    raise SystemExit("Missing station location: could not derive latitude/longitude")

set_str("station", "grid_square", grid_val)
set_bare("station", "latitude", f"{lat_val:.8f}")
set_bare("station", "longitude", f"{lon_val:.8f}")
set_str("station", "description", e("WIZ_DESCRIPTION"))
set_str("station", "id", e("WIZ_STATION_ID"))
set_str("station", "instrument_id", e("WIZ_INSTRUMENT_ID"))

# ka9q
set_str("ka9q", "status_address", e("WIZ_KA9Q_STATUS"))
set_str("ka9q", "source", e("WIZ_KA9Q_SOURCE"))
set_str("recorder", "engine", e("WIZ_KA9Q_SOURCE"))

# Recorder
set_str("recorder", "mode", "production")
set_str("recorder", "compression", e("WIZ_COMPRESSION"))

# Timing
set_str("timing", "authority", e("WIZ_TIMING_AUTHORITY"))
rtp_acc = e("WIZ_RTP_ACCURACY")
if rtp_acc:
    set_bare("timing", "rtp_expected_accuracy_ms", rtp_acc)

# L6 BPSK PPS calibration
l6_enabled = e("WIZ_L6_PPS_ENABLED")
if l6_enabled == "true":
    set_bare("timing.l6_pps", "enabled", "true")
    l6_freq = e("WIZ_L6_PPS_FREQUENCY")
    if l6_freq:
        set_bare("timing.l6_pps", "frequency_hz", l6_freq)

# Archive control (per channel group)
archive_enabled = e("WIZ_ARCHIVE_ENABLED", "true")
if archive_enabled == "false":
    set_bare("recorder.channel_group.timestd", "archive", "false")

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
TIMESTD_PROJECT=/opt/git/sigmond/hf-timestd
TIMESTD_INSTALL_DIR=/opt/git/sigmond/hf-timestd
TIMESTD_WEBUI=/opt/git/sigmond/hf-timestd/web-api
TIMESTD_VENV=/opt/git/sigmond/hf-timestd/venv
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

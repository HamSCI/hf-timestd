#!/bin/bash
# =============================================================================
# PSWS SSH Key Exchange Setup
# =============================================================================
# Automates first-time SSH key exchange with the PSWS upload server.
# The PSWS server (pswsnetwork.eng.ua.edu) is SFTP-only — no shell access.
#
# What this script does:
#   1. Reads station.id from timestd-config.toml (used as SFTP username)
#   2. Generates an RSA keypair for the timestd user (if not already present)
#   3. Caches the server's host key in known_hosts
#   4. Uploads the public key to ~/.ssh/authorized_keys on the PSWS server
#      using one-time password authentication (via sshpass + sftp batch mode)
#   5. Verifies that subsequent logins are passwordless
#
# Usage:
#   sudo ./setup-psws-keys.sh
#
# You will be prompted for:
#   - Your PSWS TOKEN (the password shown on your PSWS site admin page)
#
# Prerequisites:
#   - /etc/hf-timestd/timestd-config.toml configured with station.id
#   - Network access to pswsnetwork.eng.ua.edu
# =============================================================================

set -euo pipefail

# Use the user who invoked sudo; fall back to current user
TIMESTD_USER="${SUDO_USER:-$(whoami)}"
TIMESTD_HOME=$(getent passwd "${TIMESTD_USER}" | cut -d: -f6)
PSWS_HOST="pswsnetwork.eng.ua.edu"
PSWS_PORT=22
KEY_FILE="${TIMESTD_HOME}/.ssh/id_rsa_psws"
CONFIG_FILE="/etc/hf-timestd/timestd-config.toml"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log_info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $*"; }
log_step()  { echo -e "${BLUE}[STEP]${NC} $*"; }

# =============================================================================
# Preflight checks
# =============================================================================

if [[ $EUID -ne 0 ]]; then
    log_error "Run with sudo: sudo $0"
    exit 1
fi

log_info "Running as user: ${TIMESTD_USER} (home: ${TIMESTD_HOME})"

if [[ ! -f "${CONFIG_FILE}" ]]; then
    log_error "Config not found: ${CONFIG_FILE}"
    log_error "Run install.sh first, then edit the config with your station details."
    exit 1
fi

# =============================================================================
# Step 1: Read station.id from config (SFTP username)
# =============================================================================
log_step "Reading station ID from config..."

STATION_ID=$(grep -E '^\s*id\s*=' "${CONFIG_FILE}" | head -1 | sed 's/.*=\s*"\(.*\)".*/\1/')

if [[ -z "${STATION_ID}" || "${STATION_ID}" == "<YOUR_STATION_ID>" ]]; then
    log_error "station.id is not set in ${CONFIG_FILE}"
    log_error "Edit the config and set your PSWS SITE_ID (e.g. id = \"S000171\")"
    exit 1
fi

log_info "  Station ID (SFTP username): ${STATION_ID}"

# =============================================================================
# Step 2: Install sshpass if needed (one-time password use only)
# =============================================================================
log_step "Checking for sshpass..."

if ! command -v sshpass &>/dev/null; then
    log_info "  Installing sshpass (needed for one-time password authentication)..."
    apt-get install -y -qq sshpass
    log_info "  ✅ sshpass installed"
else
    log_info "  ✅ sshpass found"
fi

# =============================================================================
# Step 3: Generate keypair (as timestd user) if not already present
# =============================================================================
log_step "Checking for existing PSWS keypair..."

mkdir -p "${TIMESTD_HOME}/.ssh"
chmod 700 "${TIMESTD_HOME}/.ssh"
chown "${TIMESTD_USER}:${TIMESTD_USER}" "${TIMESTD_HOME}/.ssh"

if [[ -f "${KEY_FILE}" ]]; then
    log_info "  ✅ Keypair already exists: ${KEY_FILE}"
else
    log_info "  Generating RSA-4096 keypair for PSWS uploads..."
    sudo -u "${TIMESTD_USER}" ssh-keygen \
        -t rsa -b 4096 \
        -f "${KEY_FILE}" \
        -N "" \
        -C "PSWS upload key for ${STATION_ID}"
    chmod 600 "${KEY_FILE}"
    chmod 644 "${KEY_FILE}.pub"
    chown "${TIMESTD_USER}:${TIMESTD_USER}" "${KEY_FILE}" "${KEY_FILE}.pub"
    log_info "  ✅ Keypair generated: ${KEY_FILE}"
fi

PUBKEY=$(cat "${KEY_FILE}.pub")

# =============================================================================
# Step 4: Cache the server's host key
# =============================================================================
log_step "Caching PSWS server host key..."

KNOWN_HOSTS="${TIMESTD_HOME}/.ssh/known_hosts"
touch "${KNOWN_HOSTS}"
chown "${TIMESTD_USER}:${TIMESTD_USER}" "${KNOWN_HOSTS}"
chmod 644 "${KNOWN_HOSTS}"

if ! grep -q "${PSWS_HOST}" "${KNOWN_HOSTS}" 2>/dev/null; then
    ssh-keyscan -p "${PSWS_PORT}" -H "${PSWS_HOST}" >> "${KNOWN_HOSTS}" 2>/dev/null
    log_info "  ✅ Host key cached for ${PSWS_HOST}"
else
    log_info "  ✅ Host key already cached"
fi

# =============================================================================
# Step 5: Check if key is already accepted (skip password step if so)
# =============================================================================
log_step "Checking if key is already accepted by PSWS server..."

if sudo -u "${TIMESTD_USER}" sftp \
        -i "${KEY_FILE}" \
        -o BatchMode=yes \
        -o ConnectTimeout=10 \
        -P "${PSWS_PORT}" \
        "${STATION_ID}@${PSWS_HOST}" <<< "quit" &>/dev/null; then
    log_info "  ✅ Key already accepted — no password needed."
    KEY_ALREADY_INSTALLED=true
else
    KEY_ALREADY_INSTALLED=false
fi

# =============================================================================
# Step 6: Upload public key using one-time password (sftp-only method)
# =============================================================================
if [[ "${KEY_ALREADY_INSTALLED}" == "false" ]]; then
    echo ""
    echo "  Your PSWS TOKEN is the password shown on your site admin page at:"
    echo "  https://pswsnetwork.caps.ua.edu/"
    echo ""
    read -rsp "  Enter PSWS TOKEN for ${STATION_ID}: " PSWS_TOKEN
    echo ""

    if [[ -z "${PSWS_TOKEN}" ]]; then
        log_error "No token entered. Aborting."
        exit 1
    fi

    log_step "Uploading public key to PSWS server via SFTP..."

    # Strategy: use sftp batch to:
    #   1. Download existing authorized_keys (may not exist — that's OK)
    #   2. Append our public key locally
    #   3. Upload the merged authorized_keys back
    #   4. Ensure ~/.ssh directory exists on the server

    TMPDIR=$(mktemp -d)
    trap 'rm -rf "${TMPDIR}"' EXIT

    # Try to fetch existing authorized_keys (ignore failure if it doesn't exist)
    SFTP_FETCH_BATCH="${TMPDIR}/fetch.sftp"
    cat > "${SFTP_FETCH_BATCH}" <<EOF
-mkdir .ssh
get .ssh/authorized_keys ${TMPDIR}/authorized_keys_remote
quit
EOF

    sshpass -p "${PSWS_TOKEN}" sftp \
        -o BatchMode=no \
        -o StrictHostKeyChecking=no \
        -o ConnectTimeout=15 \
        -P "${PSWS_PORT}" \
        -b "${SFTP_FETCH_BATCH}" \
        "${STATION_ID}@${PSWS_HOST}" 2>/dev/null || true

    # Merge: existing keys (if any) + our new key (deduplicated)
    MERGED="${TMPDIR}/authorized_keys"
    if [[ -f "${TMPDIR}/authorized_keys_remote" ]]; then
        cp "${TMPDIR}/authorized_keys_remote" "${MERGED}"
    else
        touch "${MERGED}"
    fi

    # Append only if not already present
    if ! grep -qF "${PUBKEY}" "${MERGED}" 2>/dev/null; then
        echo "${PUBKEY}" >> "${MERGED}"
        log_info "  Public key appended to authorized_keys"
    else
        log_info "  Public key already present in authorized_keys"
    fi

    # Upload merged authorized_keys back
    SFTP_PUT_BATCH="${TMPDIR}/put.sftp"
    cat > "${SFTP_PUT_BATCH}" <<EOF
-mkdir .ssh
put ${MERGED} .ssh/authorized_keys
quit
EOF

    if sshpass -p "${PSWS_TOKEN}" sftp \
            -o BatchMode=no \
            -o StrictHostKeyChecking=no \
            -o ConnectTimeout=15 \
            -P "${PSWS_PORT}" \
            -b "${SFTP_PUT_BATCH}" \
            "${STATION_ID}@${PSWS_HOST}" 2>&1; then
        log_info "  ✅ Public key uploaded to ${PSWS_HOST}"
    else
        log_error "  SFTP upload failed. Check your TOKEN and try again."
        log_error "  TOKEN is shown on your site page at https://pswsnetwork.caps.ua.edu/"
        exit 1
    fi

    # Unset token immediately — don't leave it in memory longer than needed
    unset PSWS_TOKEN
fi

# =============================================================================
# Step 7: Verify passwordless login
# =============================================================================
log_step "Verifying passwordless SFTP login..."

if sudo -u "${TIMESTD_USER}" sftp \
        -i "${KEY_FILE}" \
        -o BatchMode=yes \
        -o ConnectTimeout=15 \
        -P "${PSWS_PORT}" \
        "${STATION_ID}@${PSWS_HOST}" <<< "quit" 2>/dev/null; then
    log_info "  ✅ Passwordless SFTP login verified!"
else
    log_warn "  ⚠️  Passwordless login test failed."
    log_warn "     The server may take a moment to apply the new key."
    log_warn "     Try again in a minute: sudo sftp -i ${KEY_FILE} ${STATION_ID}@${PSWS_HOST}"
fi

# =============================================================================
# Summary
# =============================================================================
echo ""
echo "=============================================="
echo "  PSWS Key Exchange Complete"
echo "=============================================="
echo ""
echo "  Station ID : ${STATION_ID}"
echo "  SFTP host  : ${PSWS_HOST}"
echo "  Private key: ${KEY_FILE}"
echo "  Public key : ${KEY_FILE}.pub"
echo ""
echo "  The grape-daily service will upload automatically."
echo "  To test manually:"
echo "    sudo -u timestd hf-timestd grape upload --dry-run"
echo ""

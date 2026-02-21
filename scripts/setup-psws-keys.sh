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
# Step 5: Check if key is already accepted (skip portal step if so)
# =============================================================================
log_step "Checking if key is already accepted by PSWS server..."

if sudo -u "${TIMESTD_USER}" sftp \
        -i "${KEY_FILE}" \
        -o BatchMode=yes \
        -o ConnectTimeout=10 \
        -P "${PSWS_PORT}" \
        "${STATION_ID}@${PSWS_HOST}" <<< "quit" &>/dev/null; then
    log_info "  ✅ Key already accepted — skipping portal registration step."
    KEY_ALREADY_INSTALLED=true
else
    KEY_ALREADY_INSTALLED=false
fi

# =============================================================================
# Step 6: Register public key via PSWS web portal
# =============================================================================
# The PSWS server manages authorized keys centrally — uploading to
# ~/.ssh/authorized_keys via sftp does not work. Keys must be registered
# through the web portal at https://pswsnetwork.caps.ua.edu/
# =============================================================================
if [[ "${KEY_ALREADY_INSTALLED}" == "false" ]]; then
    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "  ACTION REQUIRED: Register your SSH public key with PSWS"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo ""
    echo "  1. Open: https://pswsnetwork.caps.ua.edu/"
    echo "  2. Log in and navigate to your site: ${STATION_ID}"
    echo "  3. Find the SSH Key / Public Key field and paste this key:"
    echo ""
    echo "─────────────────────────────────────────────────────────────"
    cat "${KEY_FILE}.pub"
    echo "─────────────────────────────────────────────────────────────"
    echo ""
    echo "  4. Save the key on the portal."
    echo ""
    read -rp "  Press Enter once you have saved the key on the portal..." _
    echo ""

    # Poll for up to 2 minutes for the key to become active
    log_step "Waiting for server to accept the key (up to 2 minutes)..."
    ATTEMPTS=0
    MAX_ATTEMPTS=12  # 12 × 10s = 2 minutes
    while [[ ${ATTEMPTS} -lt ${MAX_ATTEMPTS} ]]; do
        if sudo -u "${TIMESTD_USER}" sftp \
                -i "${KEY_FILE}" \
                -o BatchMode=yes \
                -o ConnectTimeout=10 \
                -P "${PSWS_PORT}" \
                "${STATION_ID}@${PSWS_HOST}" <<< "quit" &>/dev/null; then
            log_info "  ✅ Key accepted by server!"
            KEY_ALREADY_INSTALLED=true
            break
        fi
        ATTEMPTS=$(( ATTEMPTS + 1 ))
        echo "  Attempt ${ATTEMPTS}/${MAX_ATTEMPTS} — waiting 10s..."
        sleep 10
    done

    if [[ "${KEY_ALREADY_INSTALLED}" == "false" ]]; then
        log_warn "  Key not yet accepted after 2 minutes."
        log_warn "  The server may still be propagating the key."
        log_warn "  Re-run this script later to complete the setup:"
        log_warn "    sudo $0"
        exit 0
    fi
fi

# =============================================================================
# Step 7: Verify passwordless login (as the key-generating user)
# =============================================================================
log_step "Verifying passwordless SFTP login as ${TIMESTD_USER}..."

LOGIN_OK=false
if sudo -u "${TIMESTD_USER}" sftp \
        -i "${KEY_FILE}" \
        -o BatchMode=yes \
        -o ConnectTimeout=15 \
        -P "${PSWS_PORT}" \
        "${STATION_ID}@${PSWS_HOST}" <<< "quit" 2>/dev/null; then
    log_info "  ✅ Passwordless SFTP login verified!"
    LOGIN_OK=true
else
    log_warn "  ⚠️  Passwordless login test failed."
    log_warn "     The server may take a moment to apply the new key."
    log_warn "     Try again in a minute: sudo sftp -i ${KEY_FILE} ${STATION_ID}@${PSWS_HOST}"
fi

# =============================================================================
# Step 8: Install keys into production location (timestd user) if needed
# =============================================================================
PRODUCTION_USER="timestd"
PRODUCTION_KEY="/home/${PRODUCTION_USER}/.ssh/id_rsa_psws"

if [[ "${LOGIN_OK}" == "true" && "${TIMESTD_USER}" != "${PRODUCTION_USER}" ]] \
        && id "${PRODUCTION_USER}" &>/dev/null; then

    log_step "Installing keys into production location for ${PRODUCTION_USER}..."

    PROD_SSH_DIR="/home/${PRODUCTION_USER}/.ssh"
    mkdir -p "${PROD_SSH_DIR}"
    chmod 700 "${PROD_SSH_DIR}"

    cp "${KEY_FILE}"     "${PRODUCTION_KEY}"
    cp "${KEY_FILE}.pub" "${PRODUCTION_KEY}.pub"
    chmod 600 "${PRODUCTION_KEY}"
    chmod 644 "${PRODUCTION_KEY}.pub"

    # Copy known_hosts so timestd trusts the server too
    cp "${TIMESTD_HOME}/.ssh/known_hosts" "${PROD_SSH_DIR}/known_hosts" 2>/dev/null || true
    chmod 644 "${PROD_SSH_DIR}/known_hosts" 2>/dev/null || true

    chown -R "${PRODUCTION_USER}:${PRODUCTION_USER}" "${PROD_SSH_DIR}"
    log_info "  ✅ Keys installed to ${PRODUCTION_KEY}"

    # Update ssh_key path in config to point to production location
    if grep -q 'ssh_key\s*=' "${CONFIG_FILE}"; then
        sed -i "s|^\(ssh_key\s*=\s*\).*|\1\"${PRODUCTION_KEY}\"|" "${CONFIG_FILE}"
        log_info "  ✅ Updated ssh_key in ${CONFIG_FILE} → ${PRODUCTION_KEY}"
    fi

    # Verify passwordless login as production user
    log_step "Verifying passwordless SFTP login as ${PRODUCTION_USER}..."
    if sudo -u "${PRODUCTION_USER}" sftp \
            -i "${PRODUCTION_KEY}" \
            -o BatchMode=yes \
            -o ConnectTimeout=15 \
            -P "${PSWS_PORT}" \
            "${STATION_ID}@${PSWS_HOST}" <<< "quit" 2>/dev/null; then
        log_info "  ✅ Passwordless SFTP login as ${PRODUCTION_USER} verified!"
        FINAL_KEY="${PRODUCTION_KEY}"
        FINAL_USER="${PRODUCTION_USER}"
    else
        log_warn "  ⚠️  Login as ${PRODUCTION_USER} failed — using ${TIMESTD_USER} key as fallback."
        FINAL_KEY="${KEY_FILE}"
        FINAL_USER="${TIMESTD_USER}"
    fi
else
    FINAL_KEY="${KEY_FILE}"
    FINAL_USER="${TIMESTD_USER}"
fi

# =============================================================================
# Summary
# =============================================================================
echo ""
echo "=============================================="
echo "  PSWS Key Exchange Complete"
echo "=============================================="
echo ""
echo "  Station ID  : ${STATION_ID}"
echo "  SFTP host   : ${PSWS_HOST}"
echo "  Service user: ${FINAL_USER}"
echo "  Private key : ${FINAL_KEY}"
echo "  Public key  : ${FINAL_KEY}.pub"
echo ""
echo "  The grape-daily service will upload automatically."
echo "  To test manually:"
echo "    sudo -u ${FINAL_USER} hf-timestd grape upload --dry-run"
echo ""

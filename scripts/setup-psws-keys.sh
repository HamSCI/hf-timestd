#!/bin/bash
# Setup PSWS SSH keys for the timestd user
# This script copies existing PSWS SSH keys to the timestd user's home directory
# and sets up the known_hosts file for the PSWS server.
#
# Usage:
#   sudo ./setup-psws-keys.sh [source_key_path]
#
# If source_key_path is not provided, the script will look for keys in:
#   1. /home/*/id_rsa_psws (any user's home directory)
#   2. /root/.ssh/id_rsa_psws
#
# The script can be run:
#   - During initial install (if keys were exchanged beforehand)
#   - After install (once keys are obtained from PSWS)

set -e

TIMESTD_USER="timestd"
TIMESTD_HOME="/home/${TIMESTD_USER}"
PSWS_HOST="pswsnetwork.eng.ua.edu"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Check if running as root
if [[ $EUID -ne 0 ]]; then
    log_error "This script must be run as root (use sudo)"
    exit 1
fi

# Check if timestd user exists
if ! id "${TIMESTD_USER}" &>/dev/null; then
    log_error "User '${TIMESTD_USER}' does not exist. Install hf-timestd first."
    exit 1
fi

# Find source key
SOURCE_KEY=""

if [[ -n "$1" ]]; then
    # User provided a path
    if [[ -f "$1" ]]; then
        SOURCE_KEY="$1"
        log_info "Using provided key: ${SOURCE_KEY}"
    else
        log_error "Provided key file not found: $1"
        exit 1
    fi
else
    # Search for existing keys
    log_info "Searching for existing PSWS SSH keys..."
    
    # Check common locations
    for dir in /home/*/.ssh /root/.ssh; do
        if [[ -f "${dir}/id_rsa_psws" ]]; then
            SOURCE_KEY="${dir}/id_rsa_psws"
            log_info "Found key: ${SOURCE_KEY}"
            break
        fi
    done
    
    if [[ -z "${SOURCE_KEY}" ]]; then
        log_error "No PSWS SSH key found."
        echo ""
        echo "To set up PSWS uploads, you need an SSH key registered with PSWS."
        echo ""
        echo "Options:"
        echo "  1. If you have a key, run: sudo $0 /path/to/id_rsa_psws"
        echo "  2. To generate a new key and register with PSWS:"
        echo "     a. Generate: ssh-keygen -t rsa -b 4096 -f ~/.ssh/id_rsa_psws -N ''"
        echo "     b. Register the public key at: https://pswsnetwork.eng.ua.edu"
        echo "     c. Run this script again"
        echo ""
        exit 1
    fi
fi

# Create .ssh directory for timestd user
log_info "Setting up SSH directory for ${TIMESTD_USER}..."
mkdir -p "${TIMESTD_HOME}/.ssh"
chmod 700 "${TIMESTD_HOME}/.ssh"

# Copy private key
log_info "Copying private key..."
cp "${SOURCE_KEY}" "${TIMESTD_HOME}/.ssh/id_rsa_psws"
chmod 600 "${TIMESTD_HOME}/.ssh/id_rsa_psws"

# Copy public key if it exists
if [[ -f "${SOURCE_KEY}.pub" ]]; then
    log_info "Copying public key..."
    cp "${SOURCE_KEY}.pub" "${TIMESTD_HOME}/.ssh/id_rsa_psws.pub"
    chmod 644 "${TIMESTD_HOME}/.ssh/id_rsa_psws.pub"
fi

# Set ownership
chown -R "${TIMESTD_USER}:${TIMESTD_USER}" "${TIMESTD_HOME}/.ssh"

# Add PSWS host to known_hosts
log_info "Adding ${PSWS_HOST} to known_hosts..."
if ! grep -q "${PSWS_HOST}" "${TIMESTD_HOME}/.ssh/known_hosts" 2>/dev/null; then
    ssh-keyscan -H "${PSWS_HOST}" >> "${TIMESTD_HOME}/.ssh/known_hosts" 2>/dev/null
    chown "${TIMESTD_USER}:${TIMESTD_USER}" "${TIMESTD_HOME}/.ssh/known_hosts"
    chmod 644 "${TIMESTD_HOME}/.ssh/known_hosts"
    log_info "Added ${PSWS_HOST} to known_hosts"
else
    log_info "${PSWS_HOST} already in known_hosts"
fi

# Test connection
log_info "Testing SSH connection to PSWS..."
if sudo -u "${TIMESTD_USER}" ssh -i "${TIMESTD_HOME}/.ssh/id_rsa_psws" \
    -o BatchMode=yes -o ConnectTimeout=10 \
    "${PSWS_HOST}" "echo 'Connection successful'" 2>/dev/null; then
    log_info "SSH connection test successful!"
else
    log_warn "SSH connection test failed. This may be normal if:"
    log_warn "  - The key is not yet registered with PSWS"
    log_warn "  - The PSWS server is temporarily unavailable"
    echo ""
    echo "To register your key with PSWS:"
    echo "  1. Visit: https://pswsnetwork.eng.ua.edu"
    echo "  2. Log in with your station credentials"
    echo "  3. Upload the public key: ${TIMESTD_HOME}/.ssh/id_rsa_psws.pub"
fi

echo ""
log_info "PSWS SSH key setup complete!"
echo ""
echo "Key location: ${TIMESTD_HOME}/.ssh/id_rsa_psws"
echo ""
echo "The grape-daily service will now be able to upload to PSWS."
echo "Uploads run automatically at 1:00 AM UTC daily."
echo ""
echo "To manually test an upload:"
echo "  sudo -u timestd python3 -m hf_timestd.cli grape upload --dry-run"

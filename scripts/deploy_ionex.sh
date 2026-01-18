#!/bin/bash
#
# Deploy IONEX Integration
#
# This script installs the necessary dependencies and updates the
# production environment with the IONEX integration code.
#

set -e

# Configuration
SRC_DIR="/home/mjh/git/hf-timestd"
INSTALL_DIR="/opt/hf-timestd"
VENV_PIP="$INSTALL_DIR/venv/bin/pip"
VENV_PYTHON="$INSTALL_DIR/venv/bin/python3"

echo "=== Deploying IONEX Integration ==="

# 1. Install dependencies
echo "Installing dependencies..."
$VENV_PIP install requests xarray netCDF4

# 2. Update source code
echo "Updating source code..."
# Copy core modules
cp -r $SRC_DIR/src/hf_timestd/core/ionospheric_model.py $INSTALL_DIR/src/hf_timestd/core/
cp -r $SRC_DIR/src/hf_timestd/core/physics_propagation.py $INSTALL_DIR/src/hf_timestd/core/
# Copy scripts
cp $SRC_DIR/scripts/ionex_integration.py $INSTALL_DIR/scripts/
cp $SRC_DIR/scripts/download_ionex_daily.sh $INSTALL_DIR/scripts/
chmod +x $INSTALL_DIR/scripts/download_ionex_daily.sh

# 3. Create required directories
echo "Creating directories..."
mkdir -p /var/lib/timestd/ionex
chown -R timestd:timestd /var/lib/timestd/ionex

# 4. Check for .netrc
echo "Checking authentication..."
if [ ! -f /home/timestd/.netrc ]; then
    echo "WARNING: /home/timestd/.netrc not found!"
    echo "Please create it with NASA Earthdata credentials:"
    echo "  machine urs.earthdata.nasa.gov login <USER> password <PASS>"
    echo "  chmod 600 /home/timestd/.netrc"
    echo "  chown timestd:timestd /home/timestd/.netrc"
else
    echo "Found .netrc"
fi

# 5. Reinstall package to ensure compiled extensions/paths are correct
echo "Reinstalling package..."
cd $INSTALL_DIR
# Using --no-deps to avoid messing up other things, though regular install is usually fine
$VENV_PIP install .

# 6. Restart services
echo "Restarting services..."
systemctl restart timestd-fusion
systemctl restart timestd-metrology

echo "=== Deployment Complete ==="
echo "Monitor logs: journalctl -u timestd-fusion -f"

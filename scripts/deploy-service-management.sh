#!/bin/bash
# Deployment script for service management improvements
# Run this script to deploy all service management enhancements

set -e

echo "=== HF-TimeStd Service Management Deployment ==="
echo

# Check if running as root
if [ "$EUID" -ne 0 ]; then
    echo "ERROR: This script must be run as root (use sudo)"
    exit 1
fi

REPO_DIR="/home/mjh/git/hf-timestd"
INSTALL_DIR="/opt/hf-timestd"

echo "Step 1: Installing systemd-python dependency..."
$INSTALL_DIR/venv/bin/pip install systemd-python>=235
echo "✓ systemd-python installed"
echo

echo "Step 2: Copying health check scripts..."
cp $REPO_DIR/scripts/health-check-*.sh $INSTALL_DIR/scripts/
chmod +x $INSTALL_DIR/scripts/health-check-*.sh
echo "✓ Health check scripts installed"
echo

echo "Step 3: Copying email alert script..."
cp $REPO_DIR/scripts/service-alert.sh $INSTALL_DIR/scripts/
chmod +x $INSTALL_DIR/scripts/service-alert.sh
echo "✓ Email alert script installed"
echo

echo "Step 4: Installing systemd service files..."
# Backup existing service files
mkdir -p /tmp/timestd-service-backup
cp /etc/systemd/system/timestd-*.service /tmp/timestd-service-backup/ 2>/dev/null || true

# Copy new service files
cp $REPO_DIR/systemd/timestd-core-recorder.service /etc/systemd/system/
cp $REPO_DIR/systemd/timestd-analytics.service /etc/systemd/system/
cp $REPO_DIR/systemd/timestd-fusion.service /etc/systemd/system/
cp $REPO_DIR/systemd/timestd-web-ui-fastapi.service /etc/systemd/system/
cp $REPO_DIR/systemd/timestd-alert@.service /etc/systemd/system/

echo "✓ Service files installed"
echo "  Backups saved to: /tmp/timestd-service-backup/"
echo

echo "Step 5: Reloading systemd daemon..."
systemctl daemon-reload
echo "✓ Systemd reloaded"
echo

echo "Step 6: Testing health check scripts..."
echo -n "  - Recorder health check: "
$INSTALL_DIR/scripts/health-check-recorder.sh && echo "✓ PASS" || echo "⚠ FAIL (may be normal if not running)"

echo -n "  - Analytics health check: "
$INSTALL_DIR/scripts/health-check-analytics.sh && echo "✓ PASS" || echo "⚠ FAIL (may be normal if not running)"

echo -n "  - Fusion health check: "
$INSTALL_DIR/scripts/health-check-fusion.sh && echo "✓ PASS" || echo "⚠ FAIL (may be normal if not running)"

echo

echo "=== Deployment Complete ==="
echo
echo "Next steps:"
echo "1. Configure email alerts by setting TIMESTD_ALERT_EMAIL environment variable"
echo "   Example: echo 'TIMESTD_ALERT_EMAIL=admin@example.com' >> /etc/hf-timestd/environment"
echo
echo "2. Restart services to enable watchdog support:"
echo "   sudo systemctl restart timestd-core-recorder"
echo "   sudo systemctl restart timestd-analytics"
echo "   sudo systemctl restart timestd-fusion"
echo "   sudo systemctl restart timestd-web-ui-fastapi"
echo
echo "3. Verify watchdog status:"
echo "   systemctl show timestd-core-recorder | grep Watchdog"
echo "   systemctl show timestd-fusion | grep Watchdog"
echo
echo "4. Monitor service health:"
echo "   journalctl -u timestd-core-recorder -f"
echo "   journalctl -u timestd-fusion -f"

#!/bin/bash
# Deploy v3.10.1 Critical Fixes
# Fixes for 2026-01-04 chrony feed degradation
#
# Root Causes:
# 1. HDF5 schema incompatibility in SWMR mode
# 2. Systemd watchdog timeout too aggressive (30s → 120s)

set -e

echo "=========================================="
echo "Deploying v3.10.1 Critical Fixes"
echo "=========================================="
echo ""

# Check if running as root or with sudo
if [ "$EUID" -ne 0 ]; then 
    echo "ERROR: This script must be run with sudo"
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

echo "Repository root: $REPO_ROOT"
echo ""

# Step 1: Update systemd service file (watchdog timeout fix)
echo "Step 1: Updating fusion service configuration..."
echo "  - Increasing watchdog timeout from 30s to 120s"

if [ -f "$REPO_ROOT/systemd/timestd-fusion.service" ]; then
    cp "$REPO_ROOT/systemd/timestd-fusion.service" /etc/systemd/system/
    echo "  ✅ Copied timestd-fusion.service to /etc/systemd/system/"
else
    echo "  ❌ ERROR: timestd-fusion.service not found in $REPO_ROOT/systemd/"
    exit 1
fi

# Step 2: Reload systemd
echo ""
echo "Step 2: Reloading systemd daemon..."
systemctl daemon-reload
echo "  ✅ Systemd daemon reloaded"

# Step 3: Install updated Python code (HDF5 SWMR fix)
echo ""
echo "Step 3: Installing updated Python code..."
echo "  - HDF5 writer SWMR mode check"

cd "$REPO_ROOT"
/opt/hf-timestd/venv/bin/pip install -e . --no-deps
echo "  ✅ Python code installed"

# Step 4: Restart services
echo ""
echo "Step 4: Restarting services..."
echo ""
echo "  Restarting timestd-fusion service..."
systemctl restart timestd-fusion

echo "  Waiting 5 seconds for service to start..."
sleep 5

# Step 5: Verify deployment
echo ""
echo "=========================================="
echo "Verifying Deployment"
echo "=========================================="
echo ""

# Check fusion service status
echo "Fusion Service Status:"
if systemctl is-active --quiet timestd-fusion; then
    echo "  ✅ timestd-fusion is running"
    
    # Check for watchdog timeout in recent logs
    if journalctl -u timestd-fusion --since "1 minute ago" | grep -q "Watchdog timeout"; then
        echo "  ⚠️  WARNING: Watchdog timeout detected in recent logs"
        echo "     Service may still be experiencing issues"
    else
        echo "  ✅ No watchdog timeouts in recent logs"
    fi
else
    echo "  ❌ timestd-fusion is NOT running"
    echo "     Check logs: journalctl -u timestd-fusion -n 50"
fi

echo ""
echo "Analytics Service Status:"
if systemctl is-active --quiet timestd-metrology; then
    echo "  ✅ timestd-metrology is running"
else
    echo "  ❌ timestd-metrology is NOT running"
fi

echo ""
echo "Chrony SHM Status:"
chronyc sources | grep -E "REFID|SHM" || echo "  ⚠️  No SHM source found"

echo ""
echo "=========================================="
echo "Deployment Complete"
echo "=========================================="
echo ""
echo "Next Steps:"
echo "  1. Monitor fusion service for 5-10 minutes"
echo "     journalctl -u timestd-fusion -f"
echo ""
echo "  2. Verify chrony reach improves"
echo "     watch -n 10 'chronyc sources | grep SHM'"
echo ""
echo "  3. Check for any remaining errors"
echo "     journalctl -u timestd-fusion --since '5 minutes ago' | grep -i error"
echo ""
echo "Documentation:"
echo "  - Root cause analysis: DEGRADATION_ROOT_CAUSE_2026-01-04.md"
echo "  - Changelog: CHANGELOG.md (v3.10.1)"
echo ""

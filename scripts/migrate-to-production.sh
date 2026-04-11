#!/bin/bash
# migrate-to-production.sh - Migrate hf-timestd from test mode to production
#
# This script:
# 1. Stops running test processes
# 2. Copies test data to production location
# 3. Updates config to production mode
# 4. Sets up and starts systemd services
#
# Usage: sudo ./migrate-to-production.sh

set -e

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

# Paths
TEST_ROOT="/tmp/timestd-test"
PROD_ROOT="/var/lib/timestd"
PROD_LOG_ROOT="/var/log/timestd"
PROD_CONFIG_DIR="/etc/hf-timestd"
CONFIG_FILE="/home/mjh/git/hf-timestd/config/timestd-config.toml"
PROJECT_DIR="/home/mjh/git/hf-timestd"

echo -e "${GREEN}=== HF-TimeStd Migration: Test → Production ===${NC}"
echo ""

# Check if running as root or with sudo
if [ "$EUID" -ne 0 ]; then
    echo -e "${RED}Please run with sudo${NC}"
    exit 1
fi

# Step 1: Stop running test processes
echo -e "${YELLOW}Step 1: Stopping test processes...${NC}"
# Use specific patterns to avoid killing unrelated processes (like IDEs)
pkill -f "python.*hf_timestd.core.core_recorder_v2" 2>/dev/null || true
pkill -f "python.*hf_timestd.core.metrology_service" 2>/dev/null || true
pkill -f "python.*hf_timestd.core.multi_broadcast_fusion" 2>/dev/null || true
sleep 2

# Verify stopped - only count python hf_timestd processes
REMAINING=$(pgrep -f "python.*hf_timestd" | wc -l)
if [ "$REMAINING" -gt 0 ]; then
    echo -e "${YELLOW}Warning: $REMAINING processes still running, sending SIGKILL...${NC}"
    pkill -9 -f "python.*hf_timestd.core" 2>/dev/null || true
    sleep 1
fi
echo -e "${GREEN}Test processes stopped${NC}"

# Step 2: Check test data exists
echo ""
echo -e "${YELLOW}Step 2: Checking test data...${NC}"
if [ ! -d "$TEST_ROOT" ]; then
    echo -e "${RED}Test data directory not found: $TEST_ROOT${NC}"
    exit 1
fi

TEST_SIZE=$(du -sh "$TEST_ROOT" | cut -f1)
echo "Test data size: $TEST_SIZE"

# Step 3: Clear and prepare production directories
echo ""
echo -e "${YELLOW}Step 3: Clearing and preparing production directories...${NC}"
# Remove any existing data in production directories
rm -rf "$PROD_ROOT/raw_buffer"/* 2>/dev/null || true
rm -rf "$PROD_ROOT/phase2"/* 2>/dev/null || true
rm -rf "$PROD_ROOT/state"/* 2>/dev/null || true
rm -rf "$PROD_ROOT/status"/* 2>/dev/null || true
rm -rf "$PROD_LOG_ROOT"/* 2>/dev/null || true

# Create directory structure (following Linux conventions)
# /var/lib/timestd - variable data (IQ recordings, analytics)
# /var/log/timestd - logs
# /etc/hf-timestd  - configuration
mkdir -p "$PROD_ROOT"/{raw_buffer,phase2,state,status,products,raw_archive}
mkdir -p "$PROD_LOG_ROOT"
mkdir -p "$PROD_CONFIG_DIR"
echo "Production directories cleared and prepared"

# Step 4: Copy data (preserving timestamps)
echo ""
echo -e "${YELLOW}Step 4: Copying data to production location...${NC}"
echo "This may take a few minutes..."

# Copy raw_buffer (the IQ recordings)
if [ -d "$TEST_ROOT/raw_buffer" ]; then
    echo "  Copying raw_buffer..."
    cp -a "$TEST_ROOT/raw_buffer"/* "$PROD_ROOT/raw_buffer/" 2>/dev/null || true
fi

# Copy phase2 (analytics results)
if [ -d "$TEST_ROOT/phase2" ]; then
    echo "  Copying phase2..."
    cp -a "$TEST_ROOT/phase2"/* "$PROD_ROOT/phase2/" 2>/dev/null || true
fi

# Copy state files
if [ -d "$TEST_ROOT/state" ]; then
    echo "  Copying state..."
    cp -a "$TEST_ROOT/state"/* "$PROD_ROOT/state/" 2>/dev/null || true
fi

# Copy logs to /var/log/timestd
if [ -d "$TEST_ROOT/logs" ]; then
    echo "  Copying logs to /var/log/timestd..."
    cp -a "$TEST_ROOT/logs"/* "$PROD_LOG_ROOT/" 2>/dev/null || true
fi

PROD_SIZE=$(du -sh "$PROD_ROOT" | cut -f1)
echo -e "${GREEN}Data copied. Production size: $PROD_SIZE${NC}"

# Step 5: Update config to production mode
echo ""
echo -e "${YELLOW}Step 5: Updating config to production mode...${NC}"
if [ -f "$CONFIG_FILE" ]; then
    # Backup config
    cp "$CONFIG_FILE" "${CONFIG_FILE}.bak.$(date +%Y%m%d_%H%M%S)"
    
    # Change mode from test to production
    sed -i 's/^mode = "test"/mode = "production"/' "$CONFIG_FILE"
    
    echo -e "${GREEN}Config updated: mode = \"production\"${NC}"
else
    echo -e "${RED}Config file not found: $CONFIG_FILE${NC}"
    exit 1
fi

# Step 6: Set ownership
echo ""
echo -e "${YELLOW}Step 6: Setting ownership...${NC}"
# Check if hf-timestd user exists
if id "hf-timestd" &>/dev/null; then
    chown -R hf-timestd:hf-timestd "$PROD_ROOT"
    chown -R hf-timestd:hf-timestd "$PROD_LOG_ROOT"
    chown -R hf-timestd:hf-timestd "$PROD_CONFIG_DIR"
    echo "Ownership set to hf-timestd:hf-timestd"
else
    # Use current user (mjh)
    CURRENT_USER=$(logname 2>/dev/null || echo "mjh")
    chown -R "$CURRENT_USER:$CURRENT_USER" "$PROD_ROOT"
    chown -R "$CURRENT_USER:$CURRENT_USER" "$PROD_LOG_ROOT"
    chown -R "$CURRENT_USER:$CURRENT_USER" "$PROD_CONFIG_DIR"
    echo "Ownership set to $CURRENT_USER:$CURRENT_USER"
    echo -e "${YELLOW}Note: hf-timestd user doesn't exist. Services will run as $CURRENT_USER${NC}"
fi

# Step 7: Install systemd services
echo ""
echo -e "${YELLOW}Step 7: Installing systemd services...${NC}"

# Copy config to /etc/hf-timestd/
echo "  Copying config to $PROD_CONFIG_DIR..."
cp "$CONFIG_FILE" "$PROD_CONFIG_DIR/timestd-config.toml"

# Create environment file
cat > "$PROD_CONFIG_DIR/environment" << EOF
TIMESTD_PROJECT=$PROJECT_DIR
TIMESTD_VENV=$PROJECT_DIR/venv
TIMESTD_CONFIG=$PROD_CONFIG_DIR/timestd-config.toml
TIMESTD_DATA_ROOT=$PROD_ROOT
TIMESTD_LOG_ROOT=$PROD_LOG_ROOT
TIMESTD_WEBUI=$PROJECT_DIR/web-ui
EOF

echo "Config copied to: $PROD_CONFIG_DIR/timestd-config.toml"
echo "Environment file created: $PROD_CONFIG_DIR/environment"

# Copy service files and substitute environment variables
# (systemd doesn't expand env vars in WorkingDirectory)
for svc in timestd-core-recorder timestd-metrology timestd-web-ui; do
    sed -e "s|\${TIMESTD_PROJECT}|$PROJECT_DIR|g" \
        -e "s|\${TIMESTD_VENV}|$PROJECT_DIR/venv|g" \
        -e "s|\${TIMESTD_CONFIG}|$PROD_CONFIG_DIR/timestd-config.toml|g" \
        -e "s|\${TIMESTD_WEBUI}|$PROJECT_DIR/web-ui|g" \
        "$PROJECT_DIR/systemd/${svc}.service" > "/etc/systemd/system/${svc}.service"
done

# Update service files to use current user if hf-timestd doesn't exist
if ! id "hf-timestd" &>/dev/null; then
    CURRENT_USER=$(logname 2>/dev/null || echo "mjh")
    sed -i "s/User=hf-timestd/User=$CURRENT_USER/" /etc/systemd/system/timestd-*.service
    sed -i "s/Group=hf-timestd/Group=$CURRENT_USER/" /etc/systemd/system/timestd-*.service
    # Relax security for user home access
    sed -i "s/ProtectHome=read-only/ProtectHome=false/" /etc/systemd/system/timestd-*.service
fi

# Reload systemd
systemctl daemon-reload

echo -e "${GREEN}Systemd services installed${NC}"

# Step 8: Enable and start services
echo ""
echo -e "${YELLOW}Step 8: Enabling and starting services...${NC}"

systemctl enable timestd-core-recorder
systemctl enable timestd-metrology
systemctl enable timestd-web-ui

echo ""
echo -e "${GREEN}=== Migration Complete ===${NC}"
echo ""
echo "To start services now:"
echo "  sudo systemctl start timestd-core-recorder"
echo "  sudo systemctl start timestd-metrology"
echo "  sudo systemctl start timestd-web-ui"
echo ""
echo "Or start all at once:"
echo "  sudo systemctl start timestd-core-recorder timestd-metrology timestd-web-ui"
echo ""
echo "To check status:"
echo "  sudo systemctl status timestd-core-recorder timestd-metrology timestd-web-ui"
echo ""
echo "To view logs:"
echo "  journalctl -u timestd-core-recorder -f"
echo "  journalctl -u timestd-metrology -f"
echo ""
echo -e "${YELLOW}Note: Test data preserved at $TEST_ROOT (can be deleted after verification)${NC}"

#!/bin/bash
# Start hf-timestd FastAPI Web UI

set -e

# Colors for output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}hf-timestd FastAPI Web UI${NC}"
echo -e "${GREEN}========================================${NC}"

# Check if we're in the right directory
if [ ! -f "main.py" ]; then
    echo -e "${RED}Error: main.py not found. Please run from web-api directory.${NC}"
    exit 1
fi

# Check if virtual environment exists
if [ ! -d "venv" ]; then
    echo -e "${YELLOW}Creating virtual environment...${NC}"
    python3 -m venv venv
fi

# Activate virtual environment
echo -e "${YELLOW}Activating virtual environment...${NC}"
source venv/bin/activate

# Install/upgrade dependencies
echo -e "${YELLOW}Installing dependencies...${NC}"
pip install -q --upgrade pip
pip install -q -r requirements.txt

# Check if config file exists
CONFIG_FILE="../config/timestd-config.toml"
if [ ! -f "$CONFIG_FILE" ]; then
    echo -e "${RED}Error: Configuration file not found at $CONFIG_FILE${NC}"
    exit 1
fi

echo -e "${GREEN}Configuration found: $CONFIG_FILE${NC}"

# Check if data directory exists
DATA_ROOT=$(python3 -c "import tomllib; f=open('$CONFIG_FILE','rb'); c=tomllib.load(f); print(c['recorder'].get('production_data_root','/var/lib/timestd'))")
if [ ! -d "$DATA_ROOT" ]; then
    echo -e "${YELLOW}Warning: Data root $DATA_ROOT does not exist${NC}"
    echo -e "${YELLOW}Some features may not work until data is available${NC}"
fi

echo ""
echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}Starting FastAPI server...${NC}"
echo -e "${GREEN}========================================${NC}"
echo ""
echo -e "Server will be available at:"
echo -e "  ${GREEN}http://localhost:8000${NC}          - Main UI"
echo -e "  ${GREEN}http://localhost:8000/api/docs${NC} - API Documentation"
echo ""
echo -e "Press ${YELLOW}Ctrl+C${NC} to stop the server"
echo ""

# Start uvicorn
python3 main.py

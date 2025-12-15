#!/bin/bash
#
# Test script for coherent integration implementation
# Validates that the new tick detection with phase tracking is working
#

echo "========================================="
echo "Coherent Integration Test"
echo "========================================="
echo

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

DATA_ROOT="${1:-/tmp/timestd-test}"
CHANNEL="WWV_10_MHz"
TODAY=$(date -u +%Y%m%d)

echo "Data root: $DATA_ROOT"
echo "Channel: $CHANNEL"
echo "Date: $TODAY"
echo

# Test 1: Check if discrimination log exists
echo "Test 1: Checking for discrimination data..."
DISC_FILE="$DATA_ROOT/analytics/$CHANNEL/discrimination/${CHANNEL}_discrimination_${TODAY}.csv"

if [ -f "$DISC_FILE" ]; then
    echo -e "${GREEN}✓${NC} Found discrimination file: $DISC_FILE"
    LINE_COUNT=$(wc -l < "$DISC_FILE")
    echo "  Lines: $LINE_COUNT"
else
    echo -e "${RED}✗${NC} No discrimination file found at: $DISC_FILE"
    echo "  Service may not be running or no data yet today."
    exit 1
fi
echo

# Test 2: Check CSV header for new tick_windows_10sec column
echo "Test 2: Checking CSV header..."
HEADER=$(head -n1 "$DISC_FILE")
if echo "$HEADER" | grep -q "tick_windows_10sec"; then
    echo -e "${GREEN}✓${NC} CSV header includes tick_windows_10sec column"
else
    echo -e "${RED}✗${NC} CSV header missing tick_windows_10sec column"
    echo "  Header: $HEADER"
    exit 1
fi
echo

# Test 3: Check for tick window data in recent entries
echo "Test 3: Checking for tick window data..."
RECENT=$(tail -n1 "$DISC_FILE")
if echo "$RECENT" | grep -q "coherent_wwv_snr_db"; then
    echo -e "${GREEN}✓${NC} Found coherent integration fields in data"
    
    # Extract and parse JSON (if jq is available)
    if command -v jq &> /dev/null; then
        # Extract the tick_windows JSON field (last column, quoted)
        TICK_DATA=$(echo "$RECENT" | rev | cut -d'"' -f2 | rev)
        if [ -n "$TICK_DATA" ]; then
            echo "  Sample window:"
            echo "$TICK_DATA" | jq '.[0]' 2>/dev/null || echo "  (JSON parsing failed)"
        fi
    fi
else
    echo -e "${YELLOW}⚠${NC} No coherent integration data yet (may need to wait for next minute)"
fi
echo

# Test 4: Check service logs for coherent integration messages
echo "Test 4: Checking service logs for coherent integration..."
if command -v journalctl &> /dev/null; then
    COHERENT_COUNT=$(journalctl -u grape-analytics-service --since "5 minutes ago" 2>/dev/null | grep -c "COHERENT\|INCOHERENT" || echo "0")
    
    if [ "$COHERENT_COUNT" -gt "0" ]; then
        echo -e "${GREEN}✓${NC} Found $COHERENT_COUNT coherent integration log messages"
        echo "  Recent logs:"
        journalctl -u grape-analytics-service --since "5 minutes ago" 2>/dev/null | grep -E "COHERENT|INCOHERENT|coherence" | tail -n3
    else
        echo -e "${YELLOW}⚠${NC} No coherent integration logs found in last 5 minutes"
        echo "  Service may not have processed data yet."
    fi
else
    echo -e "${YELLOW}⚠${NC} journalctl not available, skipping log check"
fi
echo

# Test 5: API test (if server is running)
echo "Test 5: Testing API endpoint..."
API_URL="http://localhost:3000/api/v1/channels/WWV%2010%20MHz/discrimination/$TODAY"

if command -v curl &> /dev/null; then
    if curl -sf "$API_URL" -o /dev/null 2>&1; then
        if command -v jq &> /dev/null; then
            echo -e "${GREEN}✓${NC} API endpoint accessible"
            
            # Check first window of first data point
            FIRST_WINDOW=$(curl -s "$API_URL" | jq -r '.data[0].tick_windows_10sec[0] // empty' 2>/dev/null)
            
            if [ -n "$FIRST_WINDOW" ]; then
                echo "  Sample tick window from API:"
                echo "$FIRST_WINDOW" | jq '.' 2>/dev/null
                
                # Check for coherent fields
                if echo "$FIRST_WINDOW" | jq -e '.coherent_wwv_snr_db' &>/dev/null; then
                    echo -e "${GREEN}✓${NC} Coherent integration fields present in API response"
                else
                    echo -e "${RED}✗${NC} Missing coherent integration fields in API response"
                fi
            else
                echo -e "${YELLOW}⚠${NC} No tick window data in API response yet"
            fi
        else
            echo -e "${GREEN}✓${NC} API endpoint accessible (jq not available for detailed check)"
        fi
    else
        echo -e "${YELLOW}⚠${NC} API endpoint not accessible (server may not be running)"
    fi
else
    echo -e "${YELLOW}⚠${NC} curl not available, skipping API test"
fi
echo

echo "========================================="
echo "Test Summary"
echo "========================================="
echo
echo "To monitor coherent integration in real-time:"
echo "  journalctl -u grape-analytics-service -f | grep -E '(COHERENT|INCOHERENT|coherence)'"
echo
echo "To check coherent gain:"
echo "  curl -s $API_URL | jq '.data[0].tick_windows_10sec[] | \"Window \\(.second)s: Gain = \\(.coherent_wwv_snr_db - .incoherent_wwv_snr_db | floor) dB, Quality = \\(.coherence_quality_wwv)\"'"
echo

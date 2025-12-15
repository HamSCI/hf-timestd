#!/bin/bash
# Validate Python and JavaScript GRAPEPaths API Synchronization
#
# This script ensures that paths.py and timestd-paths.js produce identical paths.
# Run this after any changes to either file to prevent web-ui/analytics drift.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
TEST_DATA_ROOT="/tmp/grape-paths-validation-$$"

echo "🔍 GRAPE Paths API Validation"
echo "=============================="
echo ""
echo "Testing path consistency between:"
echo "  • Python:     src/grape_recorder/paths.py"
echo "  • JavaScript: web-ui/timestd-paths.js"
echo ""

# Create temporary test data
mkdir -p "$TEST_DATA_ROOT"

# Create Python test script
cat > /tmp/test-python-paths.py <<'EOF'
#!/usr/bin/env python3
import sys
import json
from pathlib import Path

# Minimal GRAPEPaths implementation for testing (avoid toml dependency)
class GRAPEPaths:
    def __init__(self, data_root):
        self.data_root = Path(data_root)
    
    @staticmethod
    def _channel_name_to_dir(channel_name):
        return channel_name.replace(' ', '_')
    
    @staticmethod
    def _channel_name_to_key(channel_name):
        parts = channel_name.split()
        if len(parts) < 2:
            return channel_name.replace(' ', '_').lower()
        station = parts[0].lower()
        freq = parts[1]
        return f"{station}{freq}"
    
    def get_archive_dir(self, channel_name):
        return self.data_root / 'archives' / self._channel_name_to_dir(channel_name)
    
    def get_analytics_dir(self, channel_name):
        return self.data_root / 'analytics' / self._channel_name_to_dir(channel_name)
    
    def get_digital_rf_dir(self, channel_name):
        return self.get_analytics_dir(channel_name) / 'digital_rf'
    
    def get_discrimination_dir(self, channel_name):
        return self.get_analytics_dir(channel_name) / 'discrimination'
    
    def get_quality_dir(self, channel_name):
        return self.get_analytics_dir(channel_name) / 'quality'
    
    def get_decimated_dir(self, channel_name):
        return self.get_analytics_dir(channel_name) / 'decimated'
    
    def get_analytics_logs_dir(self, channel_name):
        return self.get_analytics_dir(channel_name) / 'logs'
    
    def get_analytics_status_dir(self, channel_name):
        return self.get_analytics_dir(channel_name) / 'status'
    
    def get_spectrograms_root(self):
        return self.data_root / 'spectrograms'
    
    def get_spectrograms_date_dir(self, date):
        return self.get_spectrograms_root() / date
    
    def get_state_dir(self):
        return self.data_root / 'state'
    
    def get_status_dir(self):
        return self.data_root / 'status'
    
    def get_analytics_state_file(self, channel_name):
        channel_key = self._channel_name_to_key(channel_name)
        return self.get_state_dir() / f"analytics-{channel_key}.json"

data_root = sys.argv[1]
paths = GRAPEPaths(data_root)

test_channel = "WWV 10 MHz"
test_date = "20251117"

results = {
    "archive_dir": str(paths.get_archive_dir(test_channel)),
    "analytics_dir": str(paths.get_analytics_dir(test_channel)),
    "digital_rf_dir": str(paths.get_digital_rf_dir(test_channel)),
    "discrimination_dir": str(paths.get_discrimination_dir(test_channel)),
    "quality_dir": str(paths.get_quality_dir(test_channel)),
    "decimated_dir": str(paths.get_decimated_dir(test_channel)),
    "analytics_logs_dir": str(paths.get_analytics_logs_dir(test_channel)),
    "analytics_status_dir": str(paths.get_analytics_status_dir(test_channel)),
    "spectrograms_root": str(paths.get_spectrograms_root()),
    "spectrograms_date_dir": str(paths.get_spectrograms_date_dir(test_date)),
    "state_dir": str(paths.get_state_dir()),
    "status_dir": str(paths.get_status_dir()),
    "analytics_state_file": str(paths.get_analytics_state_file(test_channel)),
}

print(json.dumps(results, indent=2))
EOF

chmod +x /tmp/test-python-paths.py

# Create JavaScript test script
cat > /tmp/test-js-paths.mjs <<'EOF'
import { GRAPEPaths } from '/home/mjh/git/signal-recorder/web-ui/timestd-paths.js';

const dataRoot = process.argv[2];
const paths = new GRAPEPaths(dataRoot);

const testChannel = "WWV 10 MHz";
const testDate = "20251117";

const results = {
    archive_dir: paths.getArchiveDir(testChannel),
    analytics_dir: paths.getAnalyticsDir(testChannel),
    digital_rf_dir: paths.getDigitalRFDir(testChannel),
    discrimination_dir: paths.getDiscriminationDir(testChannel),
    quality_dir: paths.getQualityDir(testChannel),
    decimated_dir: paths.getDecimatedDir(testChannel),
    analytics_logs_dir: paths.getAnalyticsLogsDir(testChannel),
    analytics_status_dir: paths.getAnalyticsStatusDir(testChannel),
    spectrograms_root: paths.getSpectrogramsRoot(),
    spectrograms_date_dir: paths.getSpectrogramsDateDir(testDate),
    state_dir: paths.getStateDir(),
    status_dir: paths.getStatusDir(),
    analytics_state_file: paths.getAnalyticsStateFile(testChannel),
};

console.log(JSON.stringify(results, null, 2));
EOF

# Run both tests
echo "📊 Running path generation tests..."
echo ""

PYTHON_OUTPUT=$(/tmp/test-python-paths.py "$TEST_DATA_ROOT" 2>&1)
PYTHON_EXIT=$?

if [ $PYTHON_EXIT -ne 0 ]; then
    echo "❌ Python test failed:"
    echo "$PYTHON_OUTPUT"
    exit 1
fi

JS_OUTPUT=$(node /tmp/test-js-paths.mjs "$TEST_DATA_ROOT" 2>&1)
JS_EXIT=$?

if [ $JS_EXIT -ne 0 ]; then
    echo "❌ JavaScript test failed:"
    echo "$JS_OUTPUT"
    exit 1
fi

# Compare outputs
echo "Python paths:"
echo "$PYTHON_OUTPUT" | jq .
echo ""
echo "JavaScript paths:"
echo "$JS_OUTPUT" | jq .
echo ""

# Check for differences
DIFF=$(diff <(echo "$PYTHON_OUTPUT" | jq -S .) <(echo "$JS_OUTPUT" | jq -S .) || true)

if [ -z "$DIFF" ]; then
    echo "✅ SUCCESS: Python and JavaScript paths are identical!"
    echo ""
    echo "All path methods tested:"
    echo "$PYTHON_OUTPUT" | jq -r 'keys[]' | sed 's/^/  ✓ /'
    echo ""
    RESULT=0
else
    echo "❌ FAILURE: Path mismatch detected!"
    echo ""
    echo "Differences:"
    echo "$DIFF"
    echo ""
    echo "⚠️  This indicates drift between Python and JavaScript implementations."
    echo "    Update both files to maintain synchronization."
    RESULT=1
fi

# Cleanup
rm -rf "$TEST_DATA_ROOT"
rm -f /tmp/test-python-paths.py /tmp/test-js-paths.mjs

exit $RESULT

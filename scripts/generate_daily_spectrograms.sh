#!/bin/bash
# Daily spectrogram generation for GRAPE recorder
# Run via cron at 00:15 UTC to process previous day's data

set -e

# Configuration
DATA_ROOT="${GRAPE_DATA_ROOT:-/tmp/timestd-test}"
SCRIPT_DIR="$(dirname "$(readlink -f "$0")")"
LOG_DIR="${DATA_ROOT}/logs"
mkdir -p "${LOG_DIR}"

# Get yesterday's date
YESTERDAY=$(date -u -d "yesterday" +%Y%m%d)
LOG_FILE="${LOG_DIR}/spectrograms_${YESTERDAY}.log"

echo "$(date -u) - Starting daily spectrogram generation for ${YESTERDAY}" | tee -a "${LOG_FILE}"

# Generate spectrograms (all 9 channels)
cd "${SCRIPT_DIR}/.."
python3 scripts/generate_spectrograms_drf.py \
    --date "${YESTERDAY}" \
    --data-root "${DATA_ROOT}" \
    2>&1 | tee -a "${LOG_FILE}"

EXIT_CODE=$?

if [ $EXIT_CODE -eq 0 ]; then
    echo "$(date -u) - ✅ Daily spectrograms completed successfully" | tee -a "${LOG_FILE}"
else
    echo "$(date -u) - ❌ Daily spectrograms failed with exit code ${EXIT_CODE}" | tee -a "${LOG_FILE}"
fi

exit $EXIT_CODE

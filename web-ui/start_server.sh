#!/bin/bash
# Startup script for HF-TimeStd FastAPI Monitoring Server

set -e

# Get the directory where this script is located
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Use the venv from parent directory
VENV_DIR="/opt/hf-timestd/venv"

if [ ! -d "$VENV_DIR" ]; then
    echo "ERROR: Virtual environment not found at $VENV_DIR"
    exit 1
fi

PYTHON="$VENV_DIR/bin/python"
UVICORN="$VENV_DIR/bin/uvicorn"

if [ ! -f "$PYTHON" ]; then
    echo "ERROR: Python not found at $PYTHON"
    exit 1
fi

if [ ! -f "$UVICORN" ]; then
    echo "ERROR: Uvicorn not found at $UVICORN"
    exit 1
fi

echo "Starting HF-TimeStd Monitoring Server (FastAPI)..."
echo "Python: $PYTHON"
echo "Uvicorn: $UVICORN"
echo "Working directory: $SCRIPT_DIR"

# Start uvicorn server
exec "$UVICORN" monitoring_server:app \
    --host 0.0.0.0 \
    --port 8080 \
    --workers 4 \
    --log-level info \
    --access-log

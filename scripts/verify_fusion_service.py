import sys
from pathlib import Path
import logging

# Add src to path
sys.path.insert(0, '/home/mjh/git/hf-timestd/src')
sys.path.insert(0, '/home/mjh/git/hf-timestd/web-api')

from services.fusion_service import FusionService
from config import config

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def verify_fusion_data():
    print("Initializing FusionService...")
    # Use production data root as per config
    service = FusionService(fusion_dir=config.fusion_dir)
    print(f"Fusion dir: {service.fusion_dir}")
    
    print("\nFetching latest data...")
    latest = service.get_latest()
    
    if latest:
        print("Latest data found:")
        for k, v in latest.items():
            print(f"  {k}: {v}")
    else:
        print("No latest data found.")

    print("\nFetching history (last 6 hours)...")
    from datetime import datetime, timedelta
    end = datetime.utcnow()
    start = end - timedelta(hours=6)
    history = service.get_history(start, end)
    
    print(f"History count: {history.get('count', 0)}")
    if history.get('count', 0) > 0:
        print(f"First timestamp: {history['timestamps'][0]}")
        print(f"Last timestamp: {history['timestamps'][-1]}")
        print(f"First d_clock_ms: {history['d_clock_ms'][0]}")
        print(f"Last d_clock_ms: {history['d_clock_ms'][-1]}")

if __name__ == "__main__":
    verify_fusion_data()

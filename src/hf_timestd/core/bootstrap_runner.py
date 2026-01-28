#!/usr/bin/env python3
"""
Bootstrap Runner: Run simple bootstrap on live archived data.

This service monitors the archive directory for new IQ data and runs
the simple bootstrap to find minute boundaries and decode UTC time.

Once bootstrap completes, it writes the result to the DTO file that
metrology reads.

Author: HF Time Standard Team
"""

import logging
import time
import json
import signal
import sys
from pathlib import Path
from typing import Optional, Dict, Any
from datetime import datetime, timezone
import numpy as np

from hf_timestd.core.bootstrap_simple import SimpleBootstrap, BootstrapResult

logger = logging.getLogger(__name__)

# Default paths
DEFAULT_ARCHIVE_DIR = Path("/var/lib/timestd/archive")
DEFAULT_STATE_FILE = Path("/var/lib/timestd/state/bootstrap_timing_reference.json")


class BootstrapRunner:
    """
    Run bootstrap on live archived data.
    
    Monitors archive directory, accumulates data, runs bootstrap,
    and writes result to DTO file for metrology.
    """
    
    def __init__(
        self,
        archive_dir: Path = DEFAULT_ARCHIVE_DIR,
        state_file: Path = DEFAULT_STATE_FILE,
        receiver_lat: float = 40.0,
        receiver_lon: float = -105.0,
        sample_rate: int = 24000,
        poll_interval_sec: float = 5.0,
        max_bootstrap_minutes: int = 5
    ):
        self.archive_dir = Path(archive_dir)
        self.state_file = Path(state_file)
        self.receiver_lat = receiver_lat
        self.receiver_lon = receiver_lon
        self.sample_rate = sample_rate
        self.poll_interval_sec = poll_interval_sec
        self.max_bootstrap_minutes = max_bootstrap_minutes
        
        self.running = False
        self.bootstrap: Optional[SimpleBootstrap] = None
        self.result: Optional[BootstrapResult] = None
        self.processed_files: set = set()
        
        # Ensure state directory exists
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        
        logger.info(f"BootstrapRunner initialized")
        logger.info(f"  Archive: {self.archive_dir}")
        logger.info(f"  State file: {self.state_file}")
        logger.info(f"  Receiver: ({self.receiver_lat:.2f}, {self.receiver_lon:.2f})")
    
    def start(self):
        """Start the bootstrap runner."""
        self.running = True
        self.bootstrap = SimpleBootstrap(
            self.receiver_lat,
            self.receiver_lon,
            self.sample_rate
        )
        
        logger.info("BootstrapRunner starting...")
        
        while self.running and self.result is None:
            try:
                # Look for new archive files
                new_files = self._find_new_files()
                
                if new_files:
                    logger.info(f"Found {len(new_files)} new archive files")
                    
                    for file_path in new_files:
                        self._process_file(file_path)
                    
                    # Try to get bootstrap result
                    self.result = self.bootstrap.get_result()
                    
                    if self.result:
                        self._write_result()
                        logger.info("Bootstrap complete!")
                        break
                
                time.sleep(self.poll_interval_sec)
                
            except Exception as e:
                logger.error(f"Error in bootstrap loop: {e}")
                time.sleep(self.poll_interval_sec)
        
        if self.result:
            logger.info(f"Bootstrap result: {self.result.decoded_hour:02d}:{self.result.decoded_minute:02d} UTC")
        else:
            logger.warning("Bootstrap did not complete")
    
    def stop(self):
        """Stop the bootstrap runner."""
        self.running = False
        logger.info("BootstrapRunner stopping...")
    
    def _find_new_files(self) -> list:
        """Find new archive files to process."""
        new_files = []
        
        # Look for .bin files in archive directory
        for bin_file in self.archive_dir.glob("**/*.bin"):
            if str(bin_file) not in self.processed_files:
                new_files.append(bin_file)
        
        # Sort by modification time (oldest first)
        new_files.sort(key=lambda f: f.stat().st_mtime)
        
        # Limit to max_bootstrap_minutes worth of files
        return new_files[:self.max_bootstrap_minutes]
    
    def _process_file(self, file_path: Path):
        """Process a single archive file."""
        try:
            # Parse channel from path
            # Expected: archive_dir/CHANNEL_NAME/YYYYMMDD/HHMM.bin
            channel = file_path.parent.parent.name
            
            # Load samples
            samples = np.fromfile(file_path, dtype=np.complex64)
            
            # Try to get RTP from metadata file
            meta_file = file_path.with_suffix('.json')
            rtp_start = 0
            
            if meta_file.exists():
                try:
                    with open(meta_file) as f:
                        meta = json.load(f)
                    rtp_start = meta.get('start_rtp_timestamp', 0)
                except Exception as e:
                    logger.debug(f"Could not read metadata: {e}")
            
            # Add to bootstrap
            self.bootstrap.add_samples(channel, samples, rtp_start)
            self.processed_files.add(str(file_path))
            
            logger.debug(f"Processed {file_path.name}: {len(samples)} samples, RTP={rtp_start}")
            
        except Exception as e:
            logger.error(f"Failed to process {file_path}: {e}")
    
    def _write_result(self):
        """Write bootstrap result to DTO file."""
        if self.result is None:
            return
        
        dto = {
            "locked": True,
            "lock_tier": "CONFIRMED",
            "reference_rtp": self.result.reference_rtp,
            "sample_rate": self.sample_rate,
            "minute_offset": 0,
            "decoded_hour": self.result.decoded_hour,
            "decoded_minute": self.result.decoded_minute,
            "time_confirmed": True,
            "reference_utc": self.result.reference_utc,
            "uncertainty_ms": self.result.uncertainty_ms,
            "lock_time": datetime.now(timezone.utc).isoformat(),
            "stations_used": self.result.stations_used
        }
        
        # Write atomically
        tmp_file = self.state_file.with_suffix('.tmp')
        with open(tmp_file, 'w') as f:
            json.dump(dto, f, indent=2)
        tmp_file.rename(self.state_file)
        
        logger.info(f"Wrote bootstrap DTO to {self.state_file}")


def main():
    """Run bootstrap as standalone service."""
    import argparse
    
    parser = argparse.ArgumentParser(description="Bootstrap Runner")
    parser.add_argument("--archive-dir", type=Path, default=DEFAULT_ARCHIVE_DIR)
    parser.add_argument("--state-file", type=Path, default=DEFAULT_STATE_FILE)
    parser.add_argument("--lat", type=float, default=40.0, help="Receiver latitude")
    parser.add_argument("--lon", type=float, default=-105.0, help="Receiver longitude")
    parser.add_argument("--poll-interval", type=float, default=5.0)
    parser.add_argument("--max-minutes", type=int, default=5)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()
    
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper()),
        format="%(asctime)s - %(levelname)s - %(message)s"
    )
    
    runner = BootstrapRunner(
        archive_dir=args.archive_dir,
        state_file=args.state_file,
        receiver_lat=args.lat,
        receiver_lon=args.lon,
        poll_interval_sec=args.poll_interval,
        max_bootstrap_minutes=args.max_minutes
    )
    
    # Handle signals
    def signal_handler(sig, frame):
        logger.info("Received shutdown signal")
        runner.stop()
        sys.exit(0)
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    runner.start()


if __name__ == "__main__":
    main()

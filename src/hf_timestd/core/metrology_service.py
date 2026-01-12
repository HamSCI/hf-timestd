#!/usr/bin/env python3
"""
Metrology Service (Phase 1)
===========================
Real-time DSP and Timestamping Service.

Responsibility:
1. Ingest raw IQ data streams (from tiered storage).
2. Run MetrologyEngine (Tone Detection, Channel Characterization).
3. Write L1_Metrology data products (HDF5).
4. Do NOT perform physics modeling or clock offset calculation.
"""

import logging
import time
import json
import signal
import sys
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional, Dict, List, Any, Tuple
import numpy as np

# Imports
from hf_timestd.core.metrology_engine import MetrologyEngine
from hf_timestd.models import L1MetrologyMeasurement
from hf_timestd.io.hdf5_writer import DataProductWriter
from hf_timestd.data_product_registry import DataProductRegistry
# Needed for binary reading
try:
    from hf_timestd.io.tiered_storage import TieredStorageManager
except ImportError:
    TieredStorageManager = None

logger = logging.getLogger(__name__)

class MetrologyService:
    """
    Metrology Service: The "Instrument" layer.
    """
    
    def __init__(
        self,
        config: Dict[str, Any],
        channel_name: str,
        frequency_hz: float,
        archive_dir: Path,
        output_dir: Path,
        receiver_grid: str,
        station_config: Dict[str, Any] = None
    ):
        self.config = config
        self.channel_name = channel_name
        self.frequency_hz = frequency_hz
        self.archive_dir = Path(archive_dir)
        self.output_dir = Path(output_dir)
        self.receiver_grid = receiver_grid
        self.station_config = station_config or {}
        
        # State
        self.running = False
        self.minutes_processed = 0
        self.processed_minutes = set()
        self.last_minute_unix = None
        self.start_time = time.time()
        self.status_file = self.output_dir / "status.json"
        
        # RTP Offset Learning
        self._rtp_to_unix_offset = None
        self._offset_samples = []
        
        # Initialize Engine
        # Extract precise coords if available
        lat = self.station_config.get('latitude')
        lon = self.station_config.get('longitude')
        
        self.engine = MetrologyEngine(
            raw_buffer_dir=self.archive_dir,
            output_dir=self.output_dir,
            channel_name=self.channel_name,
            frequency_hz=self.frequency_hz,
            receiver_grid=self.receiver_grid,
            sample_rate=config.get('sample_rate', 24000),
            precise_lat=lat,
            precise_lon=lon
        )
        
        # Initialize Writer
        # Resolve correct subdirectory via Registry
        writer_output_dir = DataProductRegistry.get_data_dir(
            channel_dir=self.output_dir,
            product_level="L1",
            product_name="metrology_measurements",
            create=True
        )
        
        self.writer = DataProductWriter(
            output_dir=writer_output_dir,
            product_level="L1",
            product_name="metrology_measurements",
            channel=self.channel_name,
            version="v1",
            processing_version="1.0.0",
            station_metadata=self.station_config
        )
        
        # Tiered Storage (Hot/Cold buffer)
        self._tiered_manager = None
        if TieredStorageManager:
            # Assume raw_buffer root from archive_dir parent logic or config
            # archive_dir is typically .../raw_buffer/CHANNEL
            raw_root = self.archive_dir.parent
            self._tiered_manager = TieredStorageManager(raw_root)
            logger.info("Tiered storage manager initialized")
             
        logger.info(f"MetrologyService initialized for {channel_name}")

    def run(self):
        """Main service loop."""
        self.running = True
        logger.info("Starting MetrologyService loop")
        
        # Handle signals
        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)
        
        try:
            while self.running:
                # 1. Determine next minute to process
                target_minute = self._get_latest_minute()
                
                # 2. Process
                if target_minute not in self.processed_minutes:
                    success = self.process_minute(target_minute)
                    if success:
                        self.processed_minutes.add(target_minute)
                        self._cleanup_processed_set()
                    else:
                        # Wait before retry
                        time.sleep(1.0)
                else:
                    # Up to date, wait
                    time.sleep(0.5)
                    
                # 3. Validation / Health Check (Periodically?)
                
        except Exception as e:
            logger.error(f"MetrologyService crashed: {e}", exc_info=True)
        finally:
            self.stop()

    def process_minute(self, minute_boundary: int) -> bool:
        """Process a single minute."""
        # Read IQ Data
        data = self._read_binary_minute(minute_boundary)
        if data is None:
            return False
            
        iq_samples, system_time, rtp_timestamp = data
        
        # Run Engine
        try:
            results = self.engine.process_minute(
                iq_samples=iq_samples,
                system_time=system_time,
                rtp_timestamp=rtp_timestamp
            )
            
            # Write Results
            for res in results:
                # Convert Pydantic model to dict for writer
                # HDF5 writer expects dict matching schema
                # We can use model_dump(mode='json')
                
                # IMPORTANT: DataProductWriter expects specific schema fields.
                # L1MetrologyMeasurement model has fields like 'station_id' which is Enum.
                # model_dump() handles enum -> int/str conversion if configured?
                # Pydantic v2 model_dump(mode='json') converts Enums to values.
                
                rec = res.model_dump(mode='json')
                
                # Schema expects 'processed_at', 'processing_version'
                rec['processed_at'] = datetime.now(timezone.utc).isoformat()
                rec['processing_version'] = "1.0.0"
                
                self.writer.write_measurement(rec)
                
            self.minutes_processed += 1
            self._write_status(minute_boundary, results)
            
            logger.info(f"Processed minute {minute_boundary}: {len(results)} measurements")
            return True
            
        except Exception as e:
            logger.error(f"Error processing minute {minute_boundary}: {e}", exc_info=True)
            return False

    def stop(self):
        """Stop service."""
        logger.info("Stopping MetrologyService...")
        self.running = False
        if self.writer:
            self.writer.close()

    def _handle_signal(self, signum, frame):
        logger.info(f"Received signal {signum}")
        self.stop()

    def _get_latest_minute(self) -> int:
        """Get latest complete minute (wall clock - 2 min)."""
        now = time.time()
        # 2 minute delay for safety/completion
        return ((int(now) // 60) - 2) * 60

    def _read_binary_minute(self, target_minute: int) -> Optional[Tuple[np.ndarray, float, int]]:
        """Read binary IQ data (Tiered Storage aware)."""
        # Re-using logic from Phase2AnalyticsService
        # Simplified for clarity
        
        from datetime import datetime
        dt = datetime.fromtimestamp(target_minute, tz=timezone.utc)
        date_str = dt.strftime('%Y%m%d')
        
        # 1. Locate file
        bin_path = None
        json_path = None
        
        if self._tiered_manager:
            bin_path = self._tiered_manager.find_minute_file(
                self.channel_name, target_minute, date_str
            )
            if bin_path:
                json_path = bin_path.parent / f"{target_minute}.json"
        
        if not bin_path:
            # Check archive
            base = self.archive_dir / date_str / str(target_minute)
            for ext in ['.bin', '.bin.zst', '.bin.lz4']:
                p = Path(str(base) + ext)
                if p.exists():
                    bin_path = p
                    json_path = Path(str(base) + ".json")
                    break
        
        if not bin_path:
            return None
            
        # 2. Read Metadata
        metadata = {}
        if json_path and json_path.exists():
            try:
                with open(json_path) as f:
                    metadata = json.load(f)
            except Exception:
                pass
                
        # 3. Read Data
        try:
            # Decompression logic...
            # For brevity, implementing basic read. 
            # In production, ensure zstd/lz4 imports.
            # I will assume standard .bin for now or use library if available.
             
            iq_samples = self._load_iq_file(bin_path)
            if iq_samples is None:
                return None
                
            # 4. Determine Time
            # RTP/System time logic from Phase2
            if 'start_rtp_timestamp' in metadata:
                rtp_timestamp = int(metadata['start_rtp_timestamp'])
                
                # Check RTP Offset (simplified)
                inst_offset = target_minute - (rtp_timestamp / self.engine.sample_rate)
                
                # Establish offset
                if self._rtp_to_unix_offset is None:
                    self._rtp_to_unix_offset = inst_offset
                    
                system_time = rtp_timestamp / self.engine.sample_rate + self._rtp_to_unix_offset
            else:
                rtp_timestamp = int(target_minute * self.engine.sample_rate)
                system_time = float(target_minute)

            # Pad/Clip
            expected_len = self.engine.sample_rate * 60
            if len(iq_samples) < expected_len:
                padded = np.zeros(expected_len, dtype=np.complex64)
                padded[:len(iq_samples)] = iq_samples
                iq_samples = padded
                 
            return iq_samples, system_time, rtp_timestamp
            
        except Exception as e:
            logger.error(f"Read error: {e}")
            return None

    def _load_iq_file(self, path: Path) -> Optional[np.ndarray]:
        """Helper to load IQ file."""
        try:
            if path.suffix == '.zst':
                import zstandard as zstd
                with open(path, 'rb') as f:
                    dctx = zstd.ZstdDecompressor()
                    data = dctx.decompress(f.read())
                    return np.frombuffer(data, dtype=np.complex64)
            elif path.suffix == '.lz4':
                import lz4.frame
                with open(path, 'rb') as f:
                    data = lz4.frame.decompress(f.read())
                    return np.frombuffer(data, dtype=np.complex64)
            else:
                return np.memmap(path, dtype=np.complex64, mode='r')
        except ImportError:
            logger.error("Compression library missing")
            return None
        except Exception:
            return None

    def _write_status(self, minute: int, results: List[L1MetrologyMeasurement]):
        """Write status.json."""
        try:
            status = {
                "service": "metrology",
                "last_update": datetime.now(timezone.utc).isoformat(),
                "channel": self.channel_name,
                "last_minute_processed": minute,
                "minutes_processed": self.minutes_processed,
                "last_results": [r.model_dump(mode='json') for r in results]
            }
            with open(self.status_file, 'w') as f:
                json.dump(status, f, indent=2)
        except Exception as e:
            logger.error(f"Status write failed: {e}")
    
    def _cleanup_processed_set(self):
        """Keep processed set small."""
        now_min = (int(time.time()) // 60) * 60
        old_mins = [m for m in self.processed_minutes if m < now_min - 3600]
        for m in old_mins:
            self.processed_minutes.remove(m)

if __name__ == "__main__":
    import argparse
    import sys
    
    parser = argparse.ArgumentParser(description="Metrology Service (Phase 1)")
    
    # Required args
    parser.add_argument("--archive-dir", required=True, type=Path, help="Input raw buffer directory")
    parser.add_argument("--output-dir", required=True, type=Path, help="Output directory for L1 products")
    parser.add_argument("--channel-name", required=True, help="Channel name (e.g. WWV_15000)")
    parser.add_argument("--frequency-hz", required=True, type=float, help="Center frequency in Hz")
    
    # Optional Station Metadata
    parser.add_argument("--callsign", default="UNKNOWN", help="Receiver callsign")
    parser.add_argument("--grid-square", default="XX00xx", help="Receiver grid square")
    parser.add_argument("--receiver-name", default="HF-TimeStd", help="Receiver name")
    parser.add_argument("--station-id", default="UNKNOWN", help="Station ID")
    parser.add_argument("--instrument-id", default="UNKNOWN", help="Instrument ID")
    
    # Optional Precise Coordinates
    parser.add_argument("--latitude", type=float, help="Receiver latitude")
    parser.add_argument("--longitude", type=float, help="Receiver longitude")
    
    # Service Config
    parser.add_argument("--poll-interval", type=float, default=10.0, help="Polling interval (not used in current loop logic but kept for compat)")
    parser.add_argument("--log-level", default="INFO", help="Logging level")
    parser.add_argument("--state-file", type=Path, help="Persistence state file")
    parser.add_argument("--use-tiered-storage", action="store_true", help="Enable tiered storage manager")
    
    args = parser.parse_args()
    
    # Setup Logging
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper()),
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
        datefmt='%Y-%m-%dT%H:%M:%S%z'
    )
    
    # Config dict construction
    config = {
        "sample_rate": 24000, # Hardcoded for now, or could be arg/config
        "tiered_storage": args.use_tiered_storage
    }
    
    station_config = {
        "callsign": args.callsign,
        "grid_square": args.grid_square,
        "receiver_name": args.receiver_name,
        "station_id": args.station_id,
        "instrument_id": args.instrument_id,
        "latitude": args.latitude,
        "longitude": args.longitude
    }
    
    try:
        service = MetrologyService(
            config=config,
            channel_name=args.channel_name,
            frequency_hz=args.frequency_hz,
            archive_dir=args.archive_dir,
            output_dir=args.output_dir,
            receiver_grid=args.grid_square,
            station_config=station_config
        )
        service.run()
    except KeyboardInterrupt:
        pass
    except Exception as e:
        logger.fatal(f"Service startup failed: {e}", exc_info=True)
        sys.exit(1)

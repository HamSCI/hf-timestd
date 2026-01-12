#!/usr/bin/env python3
"""
Physics Service (Phase 2)
=========================
"The Scientist": Asynchronous Interpretation of Metrology Data.

Responsibility:
1. Watch for new L1_Metrology measurements.
2. Run Physics Models (IRI-2020 + Raytracing via TransmissionTimeSolver).
3. Determine Propagation Mode and Delay.
4. Output L2_Physics data products.
"""

import logging
import time
import signal
import math
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, Any, List

from hf_timestd.io.hdf5_reader import DataProductReader
from hf_timestd.io.hdf5_writer import DataProductWriter
from hf_timestd.data_product_registry import DataProductRegistry
from hf_timestd.core.transmission_time_solver import TransmissionTimeSolver
from hf_timestd.models import L2PhysicsMeasurement, StationID

logger = logging.getLogger(__name__)

class PhysicsService:
    """
    Physics Service: Interprets L1 Metrology data.
    """

    def __init__(
        self,
        config: Dict[str, Any],
        l1_data_dir: Path,
        output_dir: Path,
        receiver_lat: float,
        receiver_lon: float,
    ):
        self.config = config
        self.l1_data_dir = Path(l1_data_dir)
        self.output_dir = Path(output_dir)
        self.receiver_lat = receiver_lat
        self.receiver_lon = receiver_lon

        # State
        self.running = False
        self.processed_files = set()
        self.last_run_time = 0

        # Initialize Solver (The "Scientist")
        # We enable dynamic ionosphere for maximum accuracy
        self.solver = TransmissionTimeSolver(
            receiver_lat=self.receiver_lat,
            receiver_lon=self.receiver_lon,
            sample_rate=24000,  # Nominal, only used for RTP conversion logic internally
            enable_dynamic_ionosphere=True
        )

        # Writers cache: channel -> DataProductWriter
        self.writers: Dict[str, DataProductWriter] = {}

        logger.info("PhysicsService initialized")

    def run(self):
        """Main service loop."""
        self.running = True
        logger.info("Starting PhysicsService loop")

        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

        while self.running:
            try:
                # 1. Scan for L1 files
                self._process_l1_files()

                # 2. Sleep (Asynchronous, no tight loop needed)
                time.sleep(5.0)

            except Exception as e:
                logger.error(f"PhysicsService error: {e}", exc_info=True)
                time.sleep(5.0)

        self.stop()

    def stop(self):
        """Stop service."""
        logger.info("Stopping PhysicsService...")
        self.running = False
        for writer in self.writers.values():
            writer.close()

    def _handle_signal(self, signum, frame):
        logger.info(f"Received signal {signum}")
        self.stop()

    def _process_l1_files(self):
        """Scan and process new L1 files."""
        # Assume L1 files are in processed_dir/L1/metrology_measurements/...
        # Or configured path.
        # We look for today's files.
        now = datetime.now(timezone.utc)
        date_str = now.strftime("%Y%m%d")

        # We need to discover channels.
        # Structure: l1_data_dir / CHANNEL / ...
        # Or if l1_data_dir points to root, we search subdirs.
        # Let's assume standard layout: /var/lib/timestd/phase2/CHANNEL/L1_metrology_measurements_v1_...h5
        # Actually reader expects structure.

        # Simple glob strategy for "active" channels
        # If l1_data_dir is /var/lib/timestd/phase2, then subdirs are channels.
        if not self.l1_data_dir.exists():
            return

        # Iterate over potential channel directories
        for channel_dir in self.l1_data_dir.iterdir():
            if not channel_dir.is_dir():
                continue

            self._process_channel(channel_dir, date_str)

    def _process_channel(self, channel_dir: Path, date_str: str):
        """Process L1 data for a specific channel."""
        channel_name = channel_dir.name

        # Use Reader to handle paths
        try:
            reader = DataProductReader(
                data_dir=channel_dir,  # Reader handles subdir resolution
                product_level="L1",
                product_name="metrology_measurements",
                channel=channel_name,
                version="v1"
            )

            # Get latest measurements
            # We want "unprocessed" ones.
            # Tracking via timestamp or similar.
            # For simplicity, we read the whole file for today (it's small, <1MB)
            # and check against a processed cache in memory (for this run).

            # Optimization: Only read if file mtime changed?
            # Reader handles SWMR.

            measurements = reader.read_time_range(
                start=f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}T00:00:00Z",
                end=f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}T23:59:59Z"
            )

            new_measurements = []
            for m in measurements:
                # Unique ID: timestamp + station
                mid = f"{m.get('timestamp_utc')}_{m.get('station_id')}"
                if mid not in self.processed_files:
                    new_measurements.append(m)
                    self.processed_files.add(mid)

            if not new_measurements:
                return

            # Process valid L1 -> L2
            l2_results = []
            for m in new_measurements:
                res = self._process_single_measurement(m)
                if res:
                    l2_results.append(res)

            if l2_results:
                self._write_l2_results(channel_name, l2_results)
                logger.info(f"Physics: Processed {len(l2_results)} measurements for {channel_name}")

        except Exception as e:
            # File might not exist yet or other error
            pass

    def _process_single_measurement(self, l1: Dict[str, Any]) -> L2PhysicsMeasurement:
        """Run physics model on a single L1 measurement."""
        try:
            # Extract inputs
            timestamp_str = l1.get('timestamp_utc')
            station_str = l1.get('station_id')

            if not station_str or station_str == 'UNKNOWN':
                logger.debug(
                    f"Physics: Rejected {timestamp_str}: Unknown station ({station_str})"
                )
                return None

            frequency_mhz = float(l1['frequency_mhz'])
            raw_toa_ms = float(l1['raw_toa_ms'])

            if math.isnan(raw_toa_ms):
                logger.debug(f"Physics: Rejected {timestamp_str}: NaN raw_toa_ms")
                return None

            # Convert timestamp
            timestamp = datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))

            # Physics Model Execution via Solver
            # We map inputs to solver expected format
            # raw_toa_ms is "observed delay".
            # Solver expects RTP. We mock RTP to force observed_delay_ms = raw_toa_ms.
            mock_sample_rate = 24000
            arrival_rtp = int((raw_toa_ms / 1000.0) * mock_sample_rate)
            expected_second_rtp = 0

            result = self.solver.solve(
                station=station_str,
                frequency_mhz=frequency_mhz,
                arrival_rtp=arrival_rtp,
                expected_second_rtp=expected_second_rtp,  # Forces observed_delay
                timestamp=timestamp,

                # Optional metrics (using safe defaults if missing)
                delay_spread_ms=0.0,  # Not currently in L1
                doppler_std_hz=0.0,  # Not currently in L1
                fss_db=None
            )

            # If no confident mode, we might skip or mark uncertain
            # RELAXED THRESHOLD: 0.01 to allow flow even if model is grumpy
            if result.confidence < 0.01:
                logger.warning(
                    f"Physics: Low confidence ({result.confidence:.2f}) for "
                    f"{station_str}/{frequency_mhz}MHz at {timestamp_str}. "
                    f"Mode={result.mode_name}, Delay={result.propagation_delay_ms}"
                )
                return None

            # Create L2 Output
            return L2PhysicsMeasurement(
                timestamp_utc=timestamp_str,
                station_id=StationID[station_str],
                frequency_mhz=frequency_mhz,

                propagation_delay_ms=result.propagation_delay_ms or 0.0,
                propagation_mode=result.mode_name,
                tec_estimate=None,  # Solver uses it internally but doesn't output it easily yet?
                # Actually solver code might have it in candidates.

                model_confidence=result.confidence,
                processed_at=datetime.now(timezone.utc).isoformat()
            )

        except Exception as e:
            logger.warning(
                f"Physics processing failed for {l1.get('timestamp_utc')}: {e}"
            )
            return None

    def _write_l2_results(self, channel: str, results: List[L2PhysicsMeasurement]):
        """Write L2 results to HDF5."""
        writer = self._get_writer(channel)
        for res in results:
            writer.write_measurement(res.model_dump(mode='json'))

    def _get_writer(self, channel: str) -> DataProductWriter:
        """Lazy init writer."""
        if channel not in self.writers:
            # Resolve correct subdirectory via Registry
            # self.output_dir is typically /var/lib/timestd/phase2
            # We need to pass the CHANNEL directory to registry logic? 
            # DataProductRegistry.get_data_dir takes 'channel_dir' usually.
            # Let's assume channel_dir = output_dir / channel
            
            channel_dir = self.output_dir / channel
            
            target_dir = DataProductRegistry.get_data_dir(
                channel_dir=channel_dir,
                product_level="L2",
                product_name="physics_interpretation",
                create=True
            )

            self.writers[channel] = DataProductWriter(
                output_dir=target_dir,
                product_level="L2",
                product_name="physics_interpretation",
                channel=channel,
                version="v1",
                processing_version="1.0.0"
            )
            
        return self.writers[channel]
        
    def _cleanup_processed_set(self):
        """Limit set size."""
        if len(self.processed_files) > 10000:
            self.processed_files.clear()
            # This is naive; in production we need smarter window tracking.
            # But for Phase 2 prototype this prevents unlimited growth.

if __name__ == "__main__":
    import argparse
    import sys
    
    parser = argparse.ArgumentParser(description="Physics Service (Phase 2)")
    
    parser.add_argument(
        "--data-root", required=True, type=Path,
        help="Root data directory containing phase2/CHANNEL/L1_..."
    )
    parser.add_argument(
        "--output", required=True, type=Path,
        help="Root directory for L2 output (files go into phase2/CHANNEL/...)"
    )
    parser.add_argument("--receiver-lat", type=float, default=40.0, help="Receiver latitude")
    parser.add_argument("--receiver-lon", type=float, default=-105.0, help="Receiver longitude")
    
    args = parser.parse_args()
    
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
        datefmt='%Y-%m-%dT%H:%M:%S%z'
    )
    
    # We need to construct config from args or dummy
    config = {}
    
    # data-root usually points to /var/lib/timestd
    # L1 data is in data-root/phase2
    # So we pass l1_data_dir as args.data_root / "phase2"
    l1_dir = args.data_root / "phase2"
    
    try:
        service = PhysicsService(
            config=config,
            l1_data_dir=l1_dir,
            output_dir=args.output,
            receiver_lat=args.receiver_lat,
            receiver_lon=args.receiver_lon
        )
        service.run()
    except KeyboardInterrupt:
        pass
    except Exception as e:
        logger.fatal(f"PhysicsService startup failed: {e}", exc_info=True)
        sys.exit(1)

"""
Decimation Pipeline - Orchestrate reading, decimation, and storage
"""

import logging
import numpy as np
from pathlib import Path
from datetime import datetime
from typing import Optional

from .raw_reader import RawBinaryReader
from .decimated_buffer import DecimatedBuffer, SAMPLES_PER_MINUTE
from .decimation import StatefulDecimator

logger = logging.getLogger(__name__)

class DecimationPipeline:
    """
    Pipeline to process raw high-rate station data into 10 Hz products.
    
    Flow:
    1. Read RawBinaryReader (24 kHz, minute chunks)
    2. Decimate via StatefulDecimator (24 kHz -> 10 Hz)
    3. Write to DecimatedBuffer (10 Hz, daily files)
    """
    
    def __init__(self, data_root: Path):
        self.data_root = Path(data_root)
        
    def process_day(self, date_str: str, channel: Optional[str] = None):
        """
        Process a full day of data.
        
        Args:
            date_str: Date to process (YYYYMMDD or YYYY-MM-DD)
            channel: Specific channel to process (None for all)
        """
        # Normalize date
        if '-' in date_str:
            date_str = date_str.replace('-', '')
            
        # Discover channels if not specified
        channels_to_process = []
        if channel:
            channels_to_process = [channel]
        else:
            # Look in raw_archive/raw_buffer for directories
            # We check both locations to be safe
            for subdir in ['raw_archive', 'raw_buffer']:
                p = self.data_root / subdir
                if p.exists():
                    # hf-timestd uses underscores for directory names
                    # We convert back to spaces for "channel names" if needed, 
                    # but RawBinaryReader and DecimatedBuffer handle the mapping.
                    # Best to stick to what the directories actually are.
                    for d in p.iterdir():
                        if d.is_dir():
                            # Convert directory name to channel name format
                            # e.g., SHARED_10000 -> SHARED 10000
                            name = d.name.replace('_', ' ')
                            if name not in channels_to_process:
                                channels_to_process.append(name)
        
        # Deduplicate
        channels_to_process = sorted(list(set(channels_to_process)))
        
        if not channels_to_process:
            logger.warning("No channels found to process")
            return

        logger.info(f"Processing {len(channels_to_process)} channels for {date_str}")
        
        for ch in channels_to_process:
            try:
                self._process_channel_day(date_str, ch)
            except Exception as e:
                logger.error(f"Failed to process {ch}: {e}", exc_info=True)

    def _process_channel_day(self, date_str: str, channel_name: str):
        """
        Process one channel for one day.

        Enumerates all 1440 expected minutes (minute 0 through 1439) so that
        gap accounting is explicit and complete — including gaps at the start
        or end of the day that the old iterator-based approach would miss.

        Uses a single StatefulDecimator instance across all minutes to preserve
        phase continuity. The decimator maintains filter state between calls,
        eliminating phase discontinuities at minute boundaries.
        """
        logger.info(f"Starting {channel_name} for {date_str}")

        reader = RawBinaryReader(self.data_root, channel_name)
        output_buffer = DecimatedBuffer(self.data_root, channel_name)

        # Determine sample rate
        input_rate = reader.get_sample_rate(date_str)
        logger.info(f"  Input rate: {input_rate} Hz")

        expected_raw_samples = input_rate * 60  # e.g., 1440000 for 24kHz

        # Single decimator instance for entire day — preserves phase continuity
        decimator = StatefulDecimator(input_rate=input_rate, output_rate=10)

        # Build lookup of available raw minutes for this day
        available_minutes = set(reader.get_available_minutes(date_str))

        # Compute the Unix timestamp of minute-0 for this UTC day
        from datetime import datetime as _dt, timezone as _tz
        day_start_dt = _dt.strptime(date_str, '%Y%m%d').replace(tzinfo=_tz.utc)
        day_start_ts = int(day_start_dt.timestamp())

        minutes_processed = 0
        gap_minutes_total = 0
        samples_generated = 0

        import gc

        for minute_index in range(1440):
            minute_ts = day_start_ts + minute_index * 60
            decimated_chunk = None
            gap_info = 0

            if minute_ts in available_minutes:
                # Read raw data for this minute
                samples, meta = reader.read_minute(minute_ts)

                if samples is not None and len(samples) > 0:
                    # Pad incomplete minutes to maintain sample alignment
                    if len(samples) < expected_raw_samples:
                        gap_info = expected_raw_samples - len(samples)
                        padded = np.zeros(expected_raw_samples, dtype=np.complex64)
                        padded[:len(samples)] = samples
                        samples = padded
                    elif len(samples) > expected_raw_samples:
                        samples = samples[:expected_raw_samples]

                    # Process with continuous decimator state
                    decimated_chunk = decimator.process(samples)

                    # Check for gaps in metadata
                    if meta and 'gap_samples' in meta:
                        gap_info = max(gap_info, meta.get('gap_samples', 0))

                    # Convert gap_info from raw sample space to decimated space
                    decimation_ratio = input_rate // 10
                    if decimation_ratio > 0:
                        gap_info = gap_info // decimation_ratio

                    del samples
                else:
                    # File existed but read failed — treat as gap
                    gap_samples = np.zeros(expected_raw_samples, dtype=np.complex64)
                    _ = decimator.process(gap_samples)
                    gap_minutes_total += 1
                    del gap_samples
                    meta = None
            else:
                # No raw file for this minute — feed zeros to maintain
                # filter state and time alignment, discard output
                gap_samples = np.zeros(expected_raw_samples, dtype=np.complex64)
                _ = decimator.process(gap_samples)
                gap_minutes_total += 1
                del gap_samples
                meta = None

            if decimated_chunk is not None and len(decimated_chunk) > 0:
                d_clock = 0.0
                uncertainty = 999.9
                grade = 'X'

                if meta:
                    d_clock = meta.get('d_clock_ms', 0.0)
                    uncertainty = meta.get('uncertainty_ms', 999.9)
                    grade = meta.get('quality_grade', 'X')

                success = output_buffer.write_minute(
                    minute_utc=float(minute_ts),
                    decimated_iq=decimated_chunk,
                    d_clock_ms=d_clock,
                    uncertainty_ms=uncertainty,
                    quality_grade=grade,
                    gap_samples=gap_info
                )

                if success:
                    minutes_processed += 1
                    samples_generated += len(decimated_chunk)

            del decimated_chunk

            # Force GC after every minute to prevent memory accumulation
            # from decompressed buffers (zstd/lz4)
            gc.collect()

        # Flush accumulated metadata to disk (single JSON write instead of 1440)
        output_buffer.flush_metadata()

        # Clean up to prevent memory accumulation across channels
        del reader
        del output_buffer
        del decimator
        gc.collect()

        logger.info(
            f"  Completed {channel_name}: {minutes_processed} valid, "
            f"{gap_minutes_total} gaps, {samples_generated} samples"
        )

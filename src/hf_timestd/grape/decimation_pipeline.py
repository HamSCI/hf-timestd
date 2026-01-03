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
                            # Convert directory name back to channel name format if possible
                            # e.g., WWV_10_MHz -> WWV 10 MHz
                            # But DecimatedBuffer expects "WWV 10 MHz" and converts to "WWV_10_MHz"
                            # So let's try to reverse map or just support directory names
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
        """Process one channel for one day."""
        logger.info(f"Starting {channel_name} for {date_str}")
        
        reader = RawBinaryReader(self.data_root, channel_name)
        output_buffer = DecimatedBuffer(self.data_root, channel_name)
        
        # Determine sample rate
        input_rate = reader.get_sample_rate(date_str)
        logger.info(f"  Input rate: {input_rate} Hz")
        
        # Initialize decimator
        decimator = StatefulDecimator(input_rate=input_rate, output_rate=10)
        
        minutes_processed = 0
        samples_generated = 0
        gaps_detected = 0
        
        # Iterate over minutes
        for minute_ts, samples, meta in reader.read_day(date_str):
            decimated_chunk = None
            gap_info = 0
            
            if samples is not None and len(samples) > 0:
                # Process valid samples
                decimated_chunk = decimator.process(samples)
                
                # Check for gaps in input
                if meta and 'gap_samples' in meta:
                    gap_info = meta['gap_samples']
            else:
                # Missing input data for this minute
                # We should produce a gap minute (zeros or just flag it)
                # StatefulDecimator.process([]) returns empty array
                # But we need to maintain 10 Hz output continuity?
                # DecimatedBuffer expects 600 samples for a valid minute
                # If we have NO data, we can't really "decimate" it.
                # However, to keep phase continuity, we might feed zeros to the filter?
                # Feeding zeros to IIR can ring. 
                # Better approach: Just skip writing valid=True for this minute 
                # or write zeros. DecimatedBuffer handles "valid=False" implictly 
                # if we don't call write_minute.
                pass

            if decimated_chunk is not None and len(decimated_chunk) > 0:
                # Write to output
                # We need minute_utc from timestamp
                
                # Metadata extraction
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
            
            # Handle gaps in output continuity?
            # The reader yields available minutes. If a minute is missing entirely
            # from the archive, we skip it here. DecimatedBuffer won't have it.
        
        logger.info(f"  Completed {channel_name}: {minutes_processed} minutes, {samples_generated} samples")

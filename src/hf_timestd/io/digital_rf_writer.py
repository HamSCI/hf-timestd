"""
Digital RF Writer Module

Wraps the digital_rf library to provide a standardized interface for writing 
L0 raw IQ data in the Digital RF HDF5 format.
"""

import logging
import time
import numpy as np
from pathlib import Path
from typing import Optional, Dict, Any, Union

try:
    import digital_rf as drf
except ImportError:
    drf = None

logger = logging.getLogger(__name__)


class DigitalRFWriter:
    """
    Writes continuous IQ data to Digital RF format.
    
    Attributes:
        output_dir: Base directory for this channel's DRF data
        sample_rate_numerator: Sample rate numerator (samples/sec)
        sample_rate_denominator: Sample rate denominator
        compression_level: GZIP compression level (0-9)
        files_per_directory: Number of files per subdirectory
    """
    
    def __init__(
        self,
        output_dir: Union[str, Path],
        sample_rate: int,
        channel_name: str,
        compression_level: int = 1,
        files_per_directory: int = 100,
        uuid: Optional[str] = None
    ):
        """
        Initialize Digital RF writer.
        
        Args:
            output_dir: Directory to write data to
            sample_rate: Sample rate in Hz
            channel_name: Name of the channel (subkey)
            compression_level: HDF5 compression level (0-9)
            files_per_directory: Subdirectory organization
            uuid: Optional UUID for the writer
        """
        if drf is None:
            raise ImportError("digital_rf library not installed")
            
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        
        self.sample_rate = sample_rate
        self.channel_name = channel_name
        self.compression_level = compression_level
        self.files_per_directory = files_per_directory
        
        self.writer = None
        self._is_open = False
        
        try:
            # Initialize the writer
            logger.info(f"Initializing Digital RF writer in {self.output_dir}")
            logger.info(f"  Channel: {channel_name}, Rate: {sample_rate} Hz")
            
            self.writer = drf.DigitalRFWriter(
                str(self.output_dir),
                dtype=np.complex64,
                subdir_cadence_secs=3600,  # New directory every hour
                file_cadence_millisecs=60000, # New file every minute
                start_global_index=0,      # Will be set on first write
                sample_rate_numerator=sample_rate,
                sample_rate_denominator=1,
                is_complex=True,
                num_subchannels=1,
                uuid_str=uuid,
                compression_level=compression_level
            )
            self._is_open = True
            
        except Exception as e:
            logger.error(f"Failed to initialize DigitalRFWriter: {e}")
            raise

    def write_samples(
        self, 
        samples: np.ndarray, 
        timestamp_samples: int
    ) -> int:
        """
        Write samples to Digital RF.
        
        Args:
            samples: Complex64 IQ samples
            timestamp_samples: Global sample index (e.g. from system time or RTP)
            
        Returns:
            Number of samples written
        """
        if not self._is_open or self.writer is None:
            return 0
            
        try:
            # Setup channel mapping on first write for this block
            # digital_rf APIs often take dict {channel: index}
            
            # Ensure complex64
            if samples.dtype != np.complex64:
                samples = samples.astype(np.complex64)
            
            # Write to the channel
            # DigitalRFWriter.rf_write(samples, global_sample_index=None)
            # If global_sample_index is None, it appends. If provided, it seeks/fills.
            # We want to be explicit with timestamps.
            
            self.writer.rf_write(samples, timestamp_samples)
            
            return len(samples)
            
        except Exception as e:
            logger.error(f"Error writing to Digital RF: {e}")
            return 0

    def close(self):
        """Close the writer and release resources."""
        if self._is_open and self.writer is not None:
            try:
                self.writer.close()
                self._is_open = False
                logger.info("Digital RF writer closed")
            except Exception as e:
                logger.error(f"Error closing Digital RF writer: {e}")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

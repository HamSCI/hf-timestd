"""
Decimation and Digital RF Writer Interface (Functions 4 + 5)

Defines the contract for decimating samples and writing to Digital RF format.
These are combined because they're tightly coupled - always decimate before
writing to Digital RF for upload.
"""

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional, Dict
import numpy as np
from .data_models import QualityInfo, TimeSnapReference, FileMetadata


class DecimatorWriter(ABC):
    """
    Interface for Functions 4+5: Decimation + Digital RF Format
    
    Combines decimation (16 kHz → 10 Hz) with Digital RF HDF5 writing.
    These are coupled because:
    1. Always decimate before writing to Digital RF
    2. Decimation filter state must be preserved across writes
    3. Digital RF requires continuous sample stream
    
    Output is ready for upload (Function 6).
    
    Design principle:
        Consumer (Function 1) doesn't care about decimation algorithms,
        filter design, or Digital RF HDF5 details. Just needs to write
        high-rate samples and get back upload-ready files.
    """
    
    @abstractmethod
    def write_decimated(
        self,
        timestamp: float,
        samples: np.ndarray,
        quality: QualityInfo,
        time_snap: Optional[TimeSnapReference] = None
    ) -> Optional[Path]:
        """
        Decimate samples and write to Digital RF format.
        
        Process:
        1. Accumulate samples until have enough for decimation
        2. Apply anti-aliasing filter
        3. Decimate to target rate (10 Hz)
        4. Write to Digital RF HDF5
        5. Embed quality metadata and time_snap reference
        
        Args:
            timestamp: UTC timestamp of first sample (time_snap corrected)
            samples: Complex IQ samples at input rate (16 kHz)
            quality: Quality metadata for this batch
            time_snap: Current time_snap reference (optional)
            
        Returns:
            Path to completed Digital RF file when ready for upload,
            None if still accumulating/writing
            
        Thread safety:
            Not thread-safe. Single writer per channel.
            
        Example:
            # Continuous writing
            completed = writer.write_decimated(
                timestamp=1699876543.0,
                samples=iq_16khz,
                quality=quality_info,
                time_snap=time_snap_ref
            )
            
            if completed:
                # File ready for upload (Function 6)
                upload_queue.queue_file(completed, metadata)
        """
        pass
    
    @abstractmethod
    def get_decimation_factor(self) -> int:
        """
        Get decimation factor.
        
        Returns:
            Decimation factor (input_rate / output_rate)
            
        Example:
            factor = writer.get_decimation_factor()
            # 16000 Hz / 10 Hz = 1600
        """
        pass
    
    @abstractmethod
    def get_input_sample_rate(self) -> int:
        """
        Get input sample rate.
        
        Returns:
            Input sample rate in Hz (typically 16000)
        """
        pass
    
    @abstractmethod
    def get_output_sample_rate(self) -> int:
        """
        Get output sample rate.
        
        Returns:
            Output sample rate in Hz (typically 10)
        """
        pass
    
    @abstractmethod
    def get_output_directory(self) -> Path:
        """
        Get Digital RF output directory.
        
        Returns:
            Path to Digital RF directory (where HDF5 files are written)
            
        Usage:
            For upload function to find files:
                drf_dir = writer.get_output_directory()
                upload_queue.monitor_directory(drf_dir)
        """
        pass
    
    @abstractmethod
    def get_samples_written(self) -> int:
        """
        Get total decimated samples written.
        
        Returns:
            Number of 10 Hz samples written to Digital RF
            
        Usage:
            Verify decimation:
                input_samples = 16000 * 60  # 1 minute
                output_samples = 10 * 60     # 1 minute decimated
                assert writer.get_samples_written() == output_samples
        """
        pass
    
    @abstractmethod
    def flush(self) -> Optional[Path]:
        """
        Flush decimation buffer and finalize current file.
        
        Forces write of any buffered samples (e.g., at shutdown).
        
        Returns:
            Path to finalized file, or None if buffer empty
            
        Usage:
            At shutdown:
                final_file = writer.flush()
                if final_file:
                    upload_queue.queue_file(final_file, metadata)
        """
        pass
    
    @abstractmethod
    def embed_quality_metadata(
        self,
        quality_summary: Dict,
        discontinuities: list
    ) -> None:
        """
        Embed quality metadata in current Digital RF file.
        
        Digital RF supports metadata channels parallel to data.
        This embeds quality metrics for scientific provenance.
        
        Args:
            quality_summary: Dict with completeness, gaps, packet loss, etc.
            discontinuities: List of discontinuity dicts
            
        Usage:
            Called by Function 1 when quality summary available:
                writer.embed_quality_metadata(
                    quality_summary={'completeness': 99.8, ...},
                    discontinuities=[gap1_dict, gap2_dict]
                )
        """
        pass
    
    @abstractmethod
    def get_current_file_metadata(self) -> Optional[FileMetadata]:
        """
        Get metadata for file currently being written.
        
        Returns:
            FileMetadata with channel info, time range, quality summary
            None if no file currently open
            
        Usage:
            Prepare upload metadata before file completion:
                metadata = writer.get_current_file_metadata()
                if metadata:
                    print(f"Current file: {metadata.start_time} to {metadata.end_time}")
        """
        pass
    
    @abstractmethod
    def verify_digital_rf_integrity(self, file_path: Path) -> bool:
        """
        Verify Digital RF file integrity.
        
        Checks:
        - File is valid HDF5
        - Digital RF structure is correct
        - Samples can be read
        - Metadata is present and valid
        
        Args:
            file_path: Path to Digital RF directory or file
            
        Returns:
            True if file is valid
            
        Usage:
            Post-write verification:
                completed = writer.write_decimated(...)
                if completed:
                    if not writer.verify_digital_rf_integrity(completed):
                        logger.error(f"Corrupt Digital RF: {completed}")
        """
        pass


class DigitalRFReader(ABC):
    """
    Interface for reading Digital RF data.
    
    Separate from writer to allow different access patterns.
    """
    
    @abstractmethod
    def read_time_range(
        self,
        start_time: float,
        end_time: float,
        channel_name: str
    ) -> np.ndarray:
        """
        Read decimated samples for a time range.
        
        Args:
            start_time: UTC start timestamp
            end_time: UTC end timestamp  
            channel_name: Channel identifier
            
        Returns:
            Decimated IQ samples (10 Hz)
            
        Usage:
            # Read 1 hour of 10 Hz data
            samples = reader.read_time_range(
                start_time=1699876800.0,
                end_time=1699880400.0,
                channel_name="WWV 5.0 MHz"
            )
            # 1 hour * 10 samples/sec = 36000 samples
        """
        pass
    
    @abstractmethod
    def read_metadata(
        self,
        channel_name: str,
        timestamp: float
    ) -> Dict:
        """
        Read quality metadata for specific time.
        
        Args:
            channel_name: Channel identifier
            timestamp: UTC timestamp
            
        Returns:
            Dict with quality summary, discontinuities, time_snap
            
        Usage:
            # Check quality for specific minute
            quality = reader.read_metadata(
                channel_name="WWV 5.0 MHz",
                timestamp=1699876800.0
            )
            print(f"Completeness: {quality['completeness_pct']}%")
        """
        pass
    
    @abstractmethod
    def get_available_time_ranges(
        self,
        channel_name: str
    ) -> list:
        """
        Get list of available continuous time ranges.
        
        Args:
            channel_name: Channel identifier
            
        Returns:
            List of (start_time, end_time) tuples
            
        Usage:
            # Find all available data
            ranges = reader.get_available_time_ranges("WWV 5.0 MHz")
            for start, end in ranges:
                duration = end - start
                print(f"Available: {duration/3600:.1f} hours")
        """
        pass


class DecimationFilter(ABC):
    """
    Interface for decimation filter implementation.
    
    Separate interface for flexibility in filter design.
    Different implementations could use scipy, custom filters, etc.
    """
    
    @abstractmethod
    def decimate(
        self,
        samples: np.ndarray,
        factor: int
    ) -> np.ndarray:
        """
        Decimate samples by given factor with anti-aliasing.
        
        Args:
            samples: Input samples
            factor: Decimation factor
            
        Returns:
            Decimated samples (len(output) = len(input) // factor)
            
        Requirements:
        - Proper anti-aliasing filter (prevent aliasing)
        - Minimal group delay (for timing accuracy)
        - Linear phase (preserve signal shape)
        - Stateful (preserve filter state across calls)
        """
        pass
    
    @abstractmethod
    def reset(self) -> None:
        """
        Reset filter state.
        
        Called at discontinuities to prevent filter ringing
        from propagating across gaps.
        """
        pass
    
    @abstractmethod
    def get_filter_delay(self) -> int:
        """
        Get filter group delay in samples.
        
        Returns:
            Group delay (number of input samples)
            
        Usage:
            Compensate timestamps for filter delay:
                delay_sec = filter.get_filter_delay() / input_rate
                corrected_time = timestamp - delay_sec
        """
        pass

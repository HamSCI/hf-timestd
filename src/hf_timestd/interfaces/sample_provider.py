"""
Sample Provider Interface (Function 1 Output)

Defines the contract for quality-analyzed sample streams.
Function 1 produces samples with time_snap correction and quality metadata.
Functions 2-5 consume this stream.
"""

from abc import ABC, abstractmethod
from typing import Optional, List, Iterator
from .data_models import (
    SampleBatch,
    TimeSnapReference,
    Discontinuity,
    QualityInfo,
)


class QualityAnalyzedSampleProvider(ABC):
    """
    Interface for Function 1: Quality & Time_Snap Analysis
    
    This is the primary producer in the hf-timestd architecture.
    It receives RTP packets, performs resequencing, gap detection,
    time_snap correction, and quality analysis.
    
    All downstream functions (2-5) consume samples from this interface.
    
    Design principle:
        Consumers don't need to know about RTP packets, sequence numbers,
        or resequencing logic. They only see quality-analyzed samples
        with corrected timestamps.
    """
    
    @abstractmethod
    def get_sample_batch(self) -> Optional[SampleBatch]:
        """
        Get next batch of quality-analyzed samples.
        
        This is a blocking call that returns when samples are available.
        Returns None when stream is closed.
        
        Returns:
            SampleBatch containing:
            - timestamp (UTC, time_snap corrected)
            - samples (complex IQ)
            - quality metadata
            - time_snap reference (if established)
            
            None if stream ended or error
            
        Thread safety:
            Multiple consumers can call this (samples are copied).
            Each consumer gets the same samples but processes independently.
        """
        pass
    
    @abstractmethod
    def get_time_snap_reference(self) -> Optional[TimeSnapReference]:
        """
        Get current time_snap reference.
        
        Returns the anchor point used for time calculation:
            utc_time = time_snap_utc + (rtp_ts - time_snap_rtp) / sample_rate
        
        Returns:
            TimeSnapReference if established, None if still using initial guess
            
        Usage:
            - Digital RF writer embeds this in metadata
            - Quality monitoring displays confidence
            - Archive files include for provenance
        """
        pass
    
    @abstractmethod
    def get_discontinuities(self, since_timestamp: float) -> List[Discontinuity]:
        """
        Get all discontinuities since a given timestamp.
        
        Critical for scientific provenance - every gap, jump, or
        time correction must be retrievable.
        
        Args:
            since_timestamp: UTC timestamp to start from
            
        Returns:
            List of Discontinuity records, sorted by timestamp
            
        Usage:
            - Archive function retrieves discontinuities for metadata
            - Digital RF writer embeds in quality log
            - Quality reports show gap timeline
        """
        pass
    
    @abstractmethod
    def get_quality_summary(self) -> QualityInfo:
        """
        Get current quality summary.
        
        Returns aggregated quality metrics for monitoring:
        - Overall completeness percentage
        - Total gap count and duration
        - Packet loss rate
        - Quality grade (A/B/C/D/F)
        
        Returns:
            QualityInfo with current statistics
            
        Usage:
            - Web dashboard displays real-time quality
            - Alerting system triggers on low quality
            - Status reports show health
        """
        pass
    
    @abstractmethod
    def register_time_snap_update_callback(self, callback) -> None:
        """
        Register callback for time_snap updates.
        
        Called when time_snap reference changes (WWV/CHU detection).
        Allows consumers to be notified of timing corrections.
        
        Args:
            callback: Function(old: TimeSnapReference, new: TimeSnapReference)
                     Called when time_snap is updated
                     
        Usage:
            - Digital RF writer can mark time_snap changes in metadata
            - Quality tracker logs time_snap corrections
            - Monitoring can alert on large timing adjustments
        """
        pass
    
    @abstractmethod
    def get_channel_info(self) -> dict:
        """
        Get channel configuration and status.
        
        Returns:
            dict with:
            - channel_name: str
            - frequency_hz: float
            - sample_rate: int
            - ssrc: int
            - is_wwv_channel: bool
            - recording_start_time: float (UTC)
            - samples_received: int
            - packets_received: int
            - packets_dropped: int
        """
        pass


class SampleBatchIterator(ABC):
    """
    Iterator interface for streaming sample batches.
    
    Alternative to get_sample_batch() for consumers that want
    to process samples in a streaming fashion.
    
    Example:
        for batch in provider.iter_batches():
            process(batch)
    """
    
    @abstractmethod
    def __iter__(self) -> Iterator[SampleBatch]:
        """
        Iterate over sample batches until stream ends.
        
        Yields:
            SampleBatch objects
            
        Usage:
            for batch in provider.iter_batches():
                archive.write_samples(
                    batch.timestamp,
                    batch.samples,
                    batch.quality
                )
        """
        pass
    
    @abstractmethod
    def __next__(self) -> SampleBatch:
        """Get next batch or raise StopIteration"""
        pass


# Convenience type for callbacks
TimeSnapUpdateCallback = callable  # (old: TimeSnapReference, new: TimeSnapReference) -> None

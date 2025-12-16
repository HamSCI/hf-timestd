#!/usr/bin/env python3
"""
Stream API - High-level interface for radiod streams

This module provides the simplest possible API for subscribing to
radiod streams. SSRC allocation is handled internally - applications
specify what they want (frequency, mode, sample rate) and receive
a handle for consuming the stream.

Example:
    from hf_timestd import subscribe_stream
    
    # Get a stream - no SSRC needed
    stream = subscribe_stream(
        radiod="radiod.local",
        frequency_hz=10.0e6,
        preset="iq",
        sample_rate=16000
    )
    
    # Use it
    print(f"Receiving on {stream.multicast_address}:{stream.port}")
    
    # Clean up
    stream.release()

For more control, use StreamManager directly:
    from hf_timestd import StreamManager
    
    manager = StreamManager("radiod.local")
    stream = manager.subscribe(frequency_hz=10.0e6, preset="iq")
"""

import logging
from typing import Dict, List, Optional

from .stream_spec import StreamSpec, StreamRequest
from .stream_handle import StreamHandle, StreamInfo
from .stream_manager import StreamManager

logger = logging.getLogger(__name__)

# Module-level manager cache (one per radiod address)
_managers: Dict[str, StreamManager] = {}


def subscribe_stream(
    radiod: str,
    frequency_hz: float,
    preset: str = "iq",
    sample_rate: int = 16000,
    agc: bool = False,
    gain: float = 0.0,
    destination: Optional[str] = None,
    description: str = ""
) -> StreamHandle:
    """
    Subscribe to a radiod stream.
    
    This is the simplest way to get a stream from radiod. Specify what
    you want (frequency, mode, sample rate) and receive a handle for
    consuming the stream. SSRC allocation is handled internally.
    
    If an identical stream already exists, you'll share it (efficient).
    Otherwise, a new stream is created in radiod.
    
    Args:
        radiod: mDNS name or address of radiod (e.g., "radiod.local")
        frequency_hz: Center frequency in Hz (e.g., 10.0e6 for 10 MHz)
        preset: Demodulation mode:
            - "iq": Raw IQ (complex samples, no demodulation)
            - "usb": Upper sideband
            - "lsb": Lower sideband  
            - "am": Amplitude modulation
            - "fm": Frequency modulation
            - "cw": Morse code
        sample_rate: Output sample rate in Hz (e.g., 16000, 12000)
        agc: Enable automatic gain control (default: False)
        gain: Manual gain in dB (used when agc=False, default: 0.0)
        destination: Multicast destination address (optional)
        description: Human-readable description for logging
        
    Returns:
        StreamHandle with:
            - multicast_address: Where to receive RTP packets
            - port: RTP port number
            - spec: The stream specification
            
    Example:
        # WWV 10 MHz IQ for hf-timestd
        stream = subscribe_stream(
            radiod="bee1-hf.local",
            frequency_hz=10.0e6,
            preset="iq",
            sample_rate=16000
        )
        
        # WSPR 20m USB for decoding
        stream = subscribe_stream(
            radiod="bee1-hf.local",
            frequency_hz=14.0956e6,
            preset="usb",
            sample_rate=12000
        )
    """
    manager = _get_manager(radiod)
    return manager.subscribe(
        frequency_hz=frequency_hz,
        preset=preset,
        sample_rate=sample_rate,
        agc=agc,
        gain=gain,
        destination=destination,
        description=description
    )


def discover_streams(radiod: str) -> List[StreamInfo]:
    """
    Discover all existing streams on a radiod instance.
    
    Args:
        radiod: mDNS name or address of radiod
        
    Returns:
        List of StreamInfo for each discovered stream
        
    Example:
        streams = discover_streams("radiod.local")
        for s in streams:
            print(f"{s.spec.frequency_hz/1e6:.4f} MHz, {s.spec.preset}")
    """
    manager = _get_manager(radiod)
    return manager.discover()


def find_stream(
    radiod: str,
    frequency_hz: float,
    preset: str,
    sample_rate: int,
    frequency_tolerance_hz: float = 100.0
) -> Optional[StreamInfo]:
    """
    Find an existing stream that matches the given parameters.
    
    Use this to check if a compatible stream already exists before
    subscribing. Useful for informational purposes.
    
    Args:
        radiod: mDNS name or address of radiod
        frequency_hz: Desired frequency in Hz
        preset: Desired demodulation mode
        sample_rate: Desired sample rate in Hz
        frequency_tolerance_hz: How close frequency must match
        
    Returns:
        StreamInfo if compatible stream found, None otherwise
    """
    manager = _get_manager(radiod)
    return manager.find_compatible(
        frequency_hz=frequency_hz,
        preset=preset,
        sample_rate=sample_rate,
        frequency_tolerance_hz=frequency_tolerance_hz
    )


def get_manager(radiod: str) -> StreamManager:
    """
    Get a StreamManager for a radiod instance.
    
    For advanced use cases that need direct manager access.
    
    Args:
        radiod: mDNS name or address of radiod
        
    Returns:
        StreamManager instance (shared for this radiod address)
    """
    return _get_manager(radiod)


def close_all():
    """
    Close all stream managers.
    
    Call this on application shutdown to clean up resources.
    """
    global _managers
    for manager in _managers.values():
        manager.close()
    _managers.clear()
    logger.info("All stream managers closed")


def _get_manager(radiod: str) -> StreamManager:
    """Get or create a StreamManager for a radiod address."""
    global _managers
    
    if radiod not in _managers:
        _managers[radiod] = StreamManager(radiod)
        logger.debug(f"Created StreamManager for {radiod}")
    
    return _managers[radiod]


# === Convenience Functions ===

def subscribe_iq(
    radiod: str,
    frequency_hz: float,
    sample_rate: int = 16000,
    **kwargs
) -> StreamHandle:
    """
    Subscribe to an IQ stream (convenience wrapper).
    
    Args:
        radiod: radiod address
        frequency_hz: Center frequency in Hz
        sample_rate: Sample rate (default: 16000)
        **kwargs: Additional arguments passed to subscribe_stream
        
    Returns:
        StreamHandle
    """
    return subscribe_stream(
        radiod=radiod,
        frequency_hz=frequency_hz,
        preset="iq",
        sample_rate=sample_rate,
        **kwargs
    )


def subscribe_usb(
    radiod: str,
    frequency_hz: float,
    sample_rate: int = 12000,
    **kwargs
) -> StreamHandle:
    """
    Subscribe to a USB (upper sideband) stream.
    
    Args:
        radiod: radiod address
        frequency_hz: Center frequency in Hz
        sample_rate: Sample rate (default: 12000)
        **kwargs: Additional arguments passed to subscribe_stream
        
    Returns:
        StreamHandle
    """
    return subscribe_stream(
        radiod=radiod,
        frequency_hz=frequency_hz,
        preset="usb",
        sample_rate=sample_rate,
        **kwargs
    )


def subscribe_am(
    radiod: str,
    frequency_hz: float,
    sample_rate: int = 12000,
    agc: bool = True,
    **kwargs
) -> StreamHandle:
    """
    Subscribe to an AM stream (with AGC by default).
    
    Args:
        radiod: radiod address
        frequency_hz: Center frequency in Hz
        sample_rate: Sample rate (default: 12000)
        agc: Enable AGC (default: True for AM)
        **kwargs: Additional arguments passed to subscribe_stream
        
    Returns:
        StreamHandle
    """
    return subscribe_stream(
        radiod=radiod,
        frequency_hz=frequency_hz,
        preset="am",
        sample_rate=sample_rate,
        agc=agc,
        **kwargs
    )


# === Batch Operations ===

def subscribe_batch(
    radiod: str,
    frequencies: List[float],
    preset: str = "iq",
    sample_rate: int = 16000,
    destination: Optional[str] = None,
    **kwargs
) -> List[StreamHandle]:
    """
    Subscribe to multiple streams with the same parameters.
    
    Efficient batch creation for applications like hf-timestd that need
    many streams with identical settings.
    
    Args:
        radiod: radiod address
        frequencies: List of frequencies in Hz
        preset: Demodulation mode (same for all)
        sample_rate: Sample rate (same for all)
        destination: Multicast destination (same for all)
        **kwargs: Additional arguments passed to subscribe_stream
        
    Returns:
        List of StreamHandles
        
    Example:
        # hf-timestd: 9 WWV/CHU channels
        streams = subscribe_batch(
            radiod="bee1-hf.local",
            frequencies=[2.5e6, 5.0e6, 10.0e6, 15.0e6, 20.0e6, 25.0e6,
                        3.33e6, 7.85e6, 14.67e6],
            preset="iq",
            sample_rate=16000,
            destination="239.1.2.101:5004"
        )
    """
    handles = []
    for freq in frequencies:
        handle = subscribe_stream(
            radiod=radiod,
            frequency_hz=freq,
            preset=preset,
            sample_rate=sample_rate,
            destination=destination,
            **kwargs
        )
        handles.append(handle)
    
    logger.info(f"Subscribed to {len(handles)} streams")
    return handles

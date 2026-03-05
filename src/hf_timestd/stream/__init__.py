"""
Stream API - High-level interface for radiod streams

This package provides an SSRC-free API for subscribing to radiod streams.
Applications specify what they want (frequency, mode, sample rate) and
the system handles SSRC allocation internally.

Example:
    from hf_timestd.stream import subscribe_stream
    
    stream = subscribe_stream(
        radiod="radiod.local",
        frequency_hz=10.0e6,
        preset="iq",
        sample_rate=16000
    )
"""

from .stream_spec import StreamSpec, StreamRequest
from .stream_handle import StreamHandle, StreamInfo
from .stream_manager import StreamManager
from .stream_api import (
    subscribe_stream,
    subscribe_iq,
    subscribe_batch,
    discover_streams,
    find_stream,
    get_manager,
    close_all,
)

__all__ = [
    # Stream specification
    "StreamSpec",
    "StreamRequest",
    # Stream handle
    "StreamHandle",
    "StreamInfo",
    # Stream manager
    "StreamManager",
    # High-level API
    "subscribe_stream",
    "subscribe_iq",
    "subscribe_batch",
    "discover_streams",
    "find_stream",
    "get_manager",
    "close_all",
]

"""
HF Time Standard Analysis (hf_timestd)

A system for receiving and analyzing HF time standard broadcasts (WWV/WWVH/CHU)
via ka9q-radio RTP streams. Produces precise timing measurements (D_clock) for
UTC alignment and clock discipline.

Key Features:
- Phase 1: Core recording of 20 kHz IQ data to raw_buffer (binary + JSON)
- Phase 2: Timing analysis - tone detection, station discrimination, D_clock
- Multi-broadcast fusion for UTC(NIST) convergence
- Chrony SHM integration for system clock discipline

Quick Start:
    from hf_timestd import subscribe_stream
    
    # Get a stream (no SSRC needed!)
    stream = subscribe_stream(
        radiod="radiod.local",
        frequency_hz=10.0e6,
        preset="iq",
        sample_rate=20000
    )
    
    print(f"Receiving on {stream.multicast_address}:{stream.port}")

See ARCHITECTURE.md for design details.

Copyright 2025
"""

__version__ = "6.8.0"
__author__ = "HF Time Standard Analysis Project"

# =============================================================================
# CORE INFRASTRUCTURE (application-agnostic)
# Located in hf_timestd/core/ package
# =============================================================================
# Core RTP infrastructure is in the 'core' subpackage
# These are re-exported for convenience
try:
    from .core import (
        RTPReceiver, RTPHeader,
        RecordingSession, SessionConfig, SessionState,
        SegmentInfo, SessionMetrics, SegmentWriter,
        PacketResequencer, RTPPacket, GapInfo,
    )
except ImportError:
    # Core subpackage may have different structure
    pass

# =============================================================================
# STREAM API (SSRC-free interface)
# Located in hf_timestd/stream/ package
# =============================================================================
from .stream import (
    StreamSpec, StreamRequest,
    StreamHandle, StreamInfo,
    StreamManager,
    subscribe_stream,
    subscribe_iq,
    subscribe_usb,
    subscribe_am,
    subscribe_batch,
    discover_streams,
    find_stream,
    get_manager,
    close_all,
)

# =============================================================================
# TIME STANDARD APPLICATION (WWV/WWVH/CHU time signals)
# Located in hf_timestd/core/ package
# Two-phase pipeline: recording + timing analysis
# =============================================================================
# Note: PipelineRecorder archived 2026-01-16 (used deprecated RTPReceiver)
# Use StreamRecorderV2 with ka9q.RadiodStream instead
try:
    from .core import CoreRecorder
except ImportError:
    pass

# =============================================================================
# WSPR APPLICATION - ARCHIVED 2026-01-16
# Demo wspr_recorder moved to archive/deprecated-wspr-demo/
# Use standalone wspr_recorder application instead
# =============================================================================

# Channel management (lower-level)
from .channel_manager import ChannelManager
from ka9q import discover_channels, ChannelInfo, RadiodControl

# ka9q timing functions (GPS_TIME/RTP_TIMESNAP support)
from ka9q import rtp_to_wallclock, parse_rtp_header

# Re-export ka9q functions for backward compatibility
discover_channels_via_control = discover_channels  # Legacy alias


__all__ = [
    # === Stream API (primary interface) ===
    "subscribe_stream",
    "subscribe_iq",
    "subscribe_usb",
    "subscribe_am",
    "subscribe_batch",
    "discover_streams",
    "find_stream",
    "get_manager",
    "close_all",
    # Stream types
    "StreamSpec",
    "StreamRequest",
    "StreamHandle",
    "StreamInfo",
    "StreamManager",
    # === Core infrastructure ===
    "RTPReceiver",
    "RTPHeader",
    "RecordingSession",
    "SessionConfig",
    "SessionState",
    "SegmentInfo",
    "SessionMetrics",
    "SegmentWriter",
    "PacketResequencer",
    "RTPPacket",
    "GapInfo",
    # === Time Standard application (two-phase pipeline) ===
    "CoreRecorder",
    # === WSPR application (archived 2026-01-16) ===
    # === Lower-level (advanced use) ===
    "ChannelManager",
    "discover_channels_via_control",
    "ChannelInfo",
    "RadiodControl",
    # Timing (from ka9q-python)
    "rtp_to_wallclock",
    "parse_rtp_header",
]

# =============================================================================
# Package structure:
#   hf_timestd/
#   ├── core/       - Time standard analysis
#   ├── stream/     - Stream API: subscribe, discover, manage
#   ├── interfaces/ - Data contracts and interfaces
#   └── wspr/       - WSPR app: 2-minute WAV recording
# =============================================================================


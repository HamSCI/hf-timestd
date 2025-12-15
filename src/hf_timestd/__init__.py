"""
HF Time Standard Analysis (hf_timestd)

A system for receiving and analyzing HF time standard broadcasts (WWV/WWVH/CHU)
via ka9q-radio RTP streams. Produces precise timing measurements (D_clock) for
UTC alignment and clock discipline.

Key Features:
- Phase 1: Core recording of 20 kHz IQ data to Digital RF archive
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

__version__ = "3.0.0"  # Major version: renamed from grape_recorder to hf_timestd
__author__ = "HF Time Standard Analysis Project"

# =============================================================================
# CORE INFRASTRUCTURE (application-agnostic)
# Located in hf_timestd/core/ package
# Note: 'core' was renamed from 'grape' - contains timing analysis modules
# =============================================================================
# Core RTP infrastructure is in the 'core' subpackage (formerly 'grape')
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
# Located in hf_timestd/core/ package (renamed from grape/)
# Two-phase pipeline: recording + timing analysis
# =============================================================================
try:
    from .core import (
        PipelineRecorder, PipelineRecorderConfig, PipelineRecorderState,
        CoreRecorder,
    )
except ImportError:
    pass

# =============================================================================
# WSPR APPLICATION (Weak Signal Propagation Reporter)
# Located in hf_timestd/wspr/ package
# =============================================================================
from .wspr import (
    WsprRecorder, WsprConfig, WsprState,
    WsprWAVWriter,
    create_wspr_recorder,
)

# Channel management (lower-level)
from .channel_manager import ChannelManager
from ka9q import discover_channels, ChannelInfo, RadiodControl

# ka9q timing functions (GPS_TIME/RTP_TIMESNAP support)
from ka9q import rtp_to_wallclock, parse_rtp_header

# Re-export ka9q functions for backward compatibility
discover_channels_via_control = discover_channels  # Legacy alias

# Note: Upload functionality moved to grape-recorder package
# See: https://github.com/mijahauan/grape-recorder

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
    "PipelineRecorder",
    "PipelineRecorderConfig",
    "PipelineRecorderState",
    "CoreRecorder",
    # === WSPR application ===
    "WsprRecorder",
    "WsprConfig",
    "WsprState",
    "WsprWAVWriter",
    "create_wspr_recorder",
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
# Package structure (Dec 14, 2025 - renamed from grape_recorder):
#   hf_timestd/
#   ├── core/       - Time standard analysis (renamed from grape/)
#   ├── stream/     - Stream API: subscribe, discover, manage
#   ├── interfaces/ - Data contracts and interfaces
#   └── wspr/       - WSPR app: 2-minute WAV recording
# =============================================================================


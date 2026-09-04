"""
HF Time Standard Analysis (hf_timestd)

A system for receiving and analyzing HF time standard broadcasts (WWV/WWVH/BPM)
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

__version__ = "6.9.0"
__author__ = "HF Time Standard Analysis Project"

# Lazy facade (PEP 562) — split plan Phase 1: the eager imports pulled
# the stream subsystem (and through it the whole recorder/engine
# surface) into every `import hf_timestd`.  Names resolve on first
# attribute access; `import hf_timestd.x.y` paths are unaffected.  The
# RTPReceiver ghost (deprecated, silently swallowed by a try/except for
# months) is dropped from __all__.

_LAZY = {
    # === Stream API (primary interface) ===
    "StreamSpec": (".stream", "StreamSpec"),
    "StreamRequest": (".stream", "StreamRequest"),
    "StreamHandle": (".stream", "StreamHandle"),
    "StreamInfo": (".stream", "StreamInfo"),
    "StreamManager": (".stream", "StreamManager"),
    "subscribe_stream": (".stream", "subscribe_stream"),
    "subscribe_iq": (".stream", "subscribe_iq"),
    "subscribe_batch": (".stream", "subscribe_batch"),
    "discover_streams": (".stream", "discover_streams"),
    "find_stream": (".stream", "find_stream"),
    "get_manager": (".stream", "get_manager"),
    "close_all": (".stream", "close_all"),
    # === Core infrastructure (re-exported from .core, itself lazy) ===
    "RTPHeader": (".core", "RTPHeader"),
    "RecordingSession": (".core", "RecordingSession"),
    "SessionConfig": (".core", "SessionConfig"),
    "SessionState": (".core", "SessionState"),
    "SegmentInfo": (".core", "SegmentInfo"),
    "SessionMetrics": (".core", "SessionMetrics"),
    "SegmentWriter": (".core", "SegmentWriter"),
    "PacketResequencer": (".core", "PacketResequencer"),
    "RTPPacket": (".core", "RTPPacket"),
    "GapInfo": (".core", "GapInfo"),
    # === Time Standard application ===
    "CoreRecorder": (".core", "CoreRecorder"),
    # === Lower-level (advanced use) ===
    "ChannelManager": (".channel_manager", "ChannelManager"),
    "discover_channels": ("ka9q", "discover_channels"),
    "discover_channels_via_control": ("ka9q", "discover_channels"),
    "ChannelInfo": ("ka9q", "ChannelInfo"),
    "RadiodControl": ("ka9q", "RadiodControl"),
    # Timing (from ka9q-python).  rtp_to_wallclock is the deprecated
    # alias, bound to rtp_to_utc so it never routes through ka9q's
    # DeprecationWarning wrapper (audit F16).
    "rtp_to_utc": ("ka9q", "rtp_to_utc"),
    "rtp_to_wallclock": ("ka9q", "rtp_to_utc"),
    "parse_rtp_header": ("ka9q", "parse_rtp_header"),
}

__all__ = sorted(_LAZY)


def __getattr__(name):
    try:
        mod, attr = _LAZY[name]
    except KeyError:
        raise AttributeError(
            f"module {__name__!r} has no attribute {name!r}") from None
    import importlib
    module = importlib.import_module(mod, __name__) if mod.startswith(".") \
        else importlib.import_module(mod)
    value = getattr(module, attr)
    globals()[name] = value      # cache: next access skips __getattr__
    return value


def __dir__():
    return sorted(set(globals()) | set(_LAZY))

# =============================================================================
# Package structure:
#   hf_timestd/
#   ├── core/       - Time standard analysis
#   ├── stream/     - Stream API: subscribe, discover, manage
#   ├── interfaces/ - Data contracts and interfaces
#   └── io/         - digital_rf_writer (rest of the data layer: hamsci-dsp)
# =============================================================================

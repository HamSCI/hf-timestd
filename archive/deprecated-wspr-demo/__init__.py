"""
WSPR Application - Weak Signal Propagation Reporter support

This package provides WSPR-specific components:
- WsprRecorder: Simple recorder for 2-minute WSPR cycles
- WsprWAVWriter: SegmentWriter for 16-bit mono WAV output

Example:
    from hf_timestd.wspr import WsprRecorder, WsprConfig
    
    config = WsprConfig(
        frequency_hz=14095600,
        output_dir=Path("wspr_output"),
    )
    recorder = WsprRecorder(config, rtp_receiver)
    recorder.start()
"""

from .wspr_recorder import WsprRecorder, WsprConfig, WsprState, create_wspr_recorder
from .wspr_wav_writer import WsprWAVWriter

__all__ = [
    "WsprRecorder",
    "WsprConfig",
    "WsprState",
    "WsprWAVWriter",
    "create_wspr_recorder",
]

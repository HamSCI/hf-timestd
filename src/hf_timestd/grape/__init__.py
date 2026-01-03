"""
GRAPE (GRAPE Recorder and Processor Engine) Module

Handles Phase 3 data products for HF time standard analysis:
- Decimation: 24/20/16 kHz → 10 Hz
- Spectrogram generation
- Digital RF packaging
- PSWS upload
"""

from .decimation import decimate_for_upload, StatefulDecimator
from .decimation_pipeline import DecimationPipeline
from .decimated_buffer import DecimatedBuffer

__all__ = [
    'decimate_for_upload',
    'StatefulDecimator',
    'DecimationPipeline',
    'DecimatedBuffer',
]

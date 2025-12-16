"""
HF Time Standard API Interfaces

Defines the contracts between core functions:
1. Quality & time_snap analysis (producer)
2. Full-bandwidth archive storage
3. WWV/WWVH/CHU tone discrimination  

Note: Functions 4-6 (decimation, Digital RF, upload) are not part of hf-timestd.

These interfaces allow testing, implementation swapping, and clear separation of concerns.
"""

# Data models (shared structures)
from .data_models import (
    # Core data structures
    SampleBatch,
    QualityInfo,
    TimeSnapReference,
    Discontinuity,
    DiscontinuityType,
    
    # Tone detection
    ToneDetectionResult,
    StationType,
    
    # Upload
    UploadTask,
    UploadStatus,
    FileMetadata,
)

# Interface definitions (abstract base classes)
from .sample_provider import (
    QualityAnalyzedSampleProvider,
    SampleBatchIterator,
)

from .archive import (
    ArchiveWriter,
    ArchiveReader,
)

from .tone_detection import (
    ToneDetector,
    MultiStationToneDetector,
)

from .upload import (
    UploadQueue,
    UploadProtocol,
)

__all__ = [
    # ===== Data Models =====
    # Core
    'SampleBatch',
    'QualityInfo', 
    'TimeSnapReference',
    'Discontinuity',
    'DiscontinuityType',
    
    # Tone detection
    'ToneDetectionResult',
    'StationType',
    
    # Upload
    'UploadTask',
    'UploadStatus',
    'FileMetadata',
    
    # ===== Interfaces =====
    # Function 1: Sample provider (producer)
    'QualityAnalyzedSampleProvider',
    'SampleBatchIterator',
    
    # Function 2: Archive storage
    'ArchiveWriter',
    'ArchiveReader',
    
    # Function 3: Tone detection
    'ToneDetector',
    'MultiStationToneDetector',
    
    # Function 6: Upload
    'UploadQueue',
    'UploadProtocol',
]

# Deprecated Core Modules

These modules have been archived as of 2026-01-16. They are superseded by newer implementations.

## Archived Files

| File | Superseded By | Reason |
|------|---------------|--------|
| `core_recorder_v1_DEPRECATED.py` | `core_recorder_v2.py` | Legacy RTP handling |
| `rtp_receiver_DEPRECATED.py` | `ka9q.RadiodStream` | Custom RTP demux replaced by ka9q-python |
| `pipeline_recorder.py` | `stream_recorder_v2.py` | Depended on deprecated RTPReceiver |
| `global_station_voter.py` | `multi_station_detector.py` | Voting approach replaced by physics-based detection |
| `station_lock_coordinator.py` | `multi_station_detector.py` | Anchor/lock approach replaced by physics-based detection |

## Migration Guide

### RTP Reception
```python
# Old (deprecated)
from hf_timestd.core.rtp_receiver_DEPRECATED import RTPReceiver
receiver = RTPReceiver('239.192.152.141', port=5004)

# New
from ka9q import RadiodStream
stream = RadiodStream(control, frequency_hz, on_samples=callback)
```

### Recording
```python
# Old (deprecated)
from hf_timestd.core import PipelineRecorder, PipelineRecorderConfig

# New
from hf_timestd.core.stream_recorder_v2 import StreamRecorderV2
```

### Station Detection
```python
# Old (deprecated)
from hf_timestd.core import GlobalStationVoter, StationLockCoordinator

# New
from hf_timestd.core import MultiStationDetector
# Backward-compat alias: GlobalStationVoter = MultiStationDetector
```

## Do Not Import

These files are preserved for historical reference only. Active code should use the replacements listed above.

Archived: 2026-01-16

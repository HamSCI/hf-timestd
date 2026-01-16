# Legacy Components

These files are from the original **generic signal recorder** design that predated the GRAPE-specific implementation.

## What's Here

- `app.py` - Generic `SignalRecorderApp` coordinator
- `recorder.py` - pcmrecord-based `StreamRecorder` (external tool)
- `processor.py` - Generic signal processor plugin architecture
- `storage.py` - Generic file storage manager
- `discovery.py` - Avahi/mDNS-based stream discovery

## Why They're Not Used

The **active implementation** (`rtp_receiver.py`, `core_recorder.py`) replaced these with:

| Legacy Component | GRAPE Replacement | Why Changed |
|------------------|-------------------|-------------|
| `recorder.py` (pcmrecord) | Direct RTP reception | Eliminates external tool dependency |
| `processor.py` (plugins) | Inline scipy decimation | Real-time processing, no separate step |
| `storage.py` (generic) | Digital RF writer | Time-indexed HDF5, HamSCI format |
| `discovery.py` (Avahi) | `control_discovery.py` | Direct radiod query, more reliable |
| `app.py` (coordinator) | `grape_recorder.py` | GRAPE-specific flow |

## Preserved for Reference

These files are kept for:
- Historical context
- Understanding design evolution
- Potential reuse for non-GRAPE projects

## Do Not Import

Active code should **not** import from this directory. Use:
- `rtp_receiver.py` - Generic RTP reception
- `core_recorder.py` - Main recording daemon
- `channel_manager.py` - Channel operations
- `control_discovery.py` - Channel discovery

See [ARCHITECTURE.md](../../../ARCHITECTURE.md) for current design.

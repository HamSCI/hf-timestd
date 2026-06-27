# Phase 1 Binary Writer Migration

**Date:** 2025-12-06  
**Issue:** HDF5/Digital RF write conflicts causing 70% data loss

## Problem

The original Phase 1 implementation used Digital RF (HDF5-based) for raw IQ storage.
This caused several critical issues:

1. **File Conflicts on Recovery**: DRF creates files at minute boundaries. When errors
   occurred and the writer was reset, it would try to write to files that already
   existed, causing cascading failures.

2. **High CPU Usage**: The sliding window monitor (FFT every 10 seconds) and DRF's
   HDF5 overhead caused 244% CPU usage for 1.44 MB/sec data rate.

3. **UDP Packet Loss**: High CPU caused UDP buffer overflows (5,640 errors per 5 sec),
   resulting in only 30% data completeness.

4. **HDF5 Library Limitations**: The Digital RF library enters an unrecoverable
   "poisoned" state after certain errors, requiring process restart.

## Solution

Replaced DRF with simple binary files, following GNU Radio's proven pattern:

### New Architecture

```
RTP Packets
    │
    ▼
┌─────────────────────────────────────────────────────┐
│  Phase 1: Binary Archive Writer                     │
│  - Append-only binary files (complex64)             │
│  - One file per chunk per channel (default 10 min)  │
│  - JSON metadata sidecar (includes file_duration_sec)│
│  - Cannot fail (OS handles append)                  │
└──────────────────────┬──────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────┐
│  Phase 2: Analytics Service                         │
│  - Reads binary via memory-map (zero-copy)          │
│  - Produces D_clock, tone detection, etc.           │
└─────────────────────────────────────────────────────┘
```

### File Structure

```
/tmp/timestd-test/raw_buffer/
  WWV_10_MHz/
    20251206/
      1765032240.bin      # 9.6 MB raw complex64
      1765032240.json     # Metadata sidecar
      1765032300.bin
      1765032300.json
      ...
```

### Binary Format

- **Format**: Raw complex64 (numpy compatible)
- **Chunk duration**: Configurable via `file_duration_sec` (default 600s = 10 min; legacy: 60s)
- **Size**: ~96 MB per 10-min chunk per channel at 24 kHz (14,400,000 samples × 8 bytes); ~9.6 MB per 1-min chunk at 20 kHz (legacy)
- **Reading**: `np.memmap(path, dtype=np.complex64, mode='r')` for zero-copy access

### Metadata Sidecar (JSON)

```json
{
  "minute_boundary": 1765032240,
  "channel_name": "WWV 10 MHz",
  "frequency_hz": 10000000,
  "sample_rate": 20000,
  "samples_written": 1200000,
  "samples_expected": 1200000,
  "completeness_pct": 100.0,
  "gap_count": 0,
  "dtype": "complex64",
  "byte_order": "little"
}
```

## Results

| Metric | Before | After |
|--------|--------|-------|
| CPU Usage | 244% | 9.8% |
| UDP Errors (5s) | 5,640 | 0 |
| Log Errors | 930 | 0 |
| Data Completeness | 30% | 100% |
| File Conflicts | Frequent | None |

## Files Changed

### New Files
- `src/hf_timestd/core/binary_archive_writer.py` - Simple binary writer

### Modified Files
- `src/hf_timestd/core/pipeline_orchestrator.py` - Use BinaryArchiveWriter
- `src/hf_timestd/core/phase2_analytics_service.py` - Read from binary raw_buffer

## Future Work

1. **Async Compression**: Compress completed minutes with zstd in background
2. **Cleanup Policy**: Delete/relocate old raw_buffer data after downstream consumers confirm retention needs

## Why Not Fix DRF?

We tried several approaches to fix the DRF writer:
- Skip-ahead on restart
- Recovery timestamps
- File cleanup
- Error retry with backoff

None worked reliably because:
1. HDF5 library enters unrecoverable states
2. File boundary conflicts are fundamental to DRF's design
3. The complexity wasn't worth it for real-time recording

The binary approach is simpler, more robust, and follows industry best practices
(GNU Radio, SDR#, etc. all use simple binary for recording, converting to other
formats offline).

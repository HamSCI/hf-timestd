# Session: 2026-03-04 RTP Timing Mismatch Diagnosis & Fix

## Problem

Minute boundaries in the raw buffer archive were ~4500 seconds ahead of UTC. Metrology could not find binary files at expected minute boundaries, causing complete measurement starvation across all channels. The fusion service received no new L1/L2 data and clock estimates went stale.

## Symptoms

- Archive files at `/dev/shm/timestd/raw_buffer/<channel>/` had `minute_boundary` values thousands of seconds in the future relative to wall-clock UTC.
- Metrology log: `No binary file found` for every expected minute.
- Fusion log: no new measurements ingested.

## Root Cause Chain

### 1. Stale radiod channels from phase-engine testing

Previous phase-engine development sessions created channels on radiod that were never cleaned up. These stale decoders shared SSRCs with hf-timestd's active channels but operated in **different RTP counter spaces** (each decoder's RTP timestamp counter starts from a different offset when created).

### 2. Global status multicast pollution

The `_timing_poll_loop` in `stream_recorder_v2.py` called `discover_channels(status_address)` at 2 Hz to refresh GPS/RTP timing snapshots. This function listens on radiod's **global status multicast**, which broadcasts status for ALL decoders across ALL clients. When two decoders share an SSRC (even on different multicast groups), `discover_channels()` returns whichever status packet arrived last during the listen window — frequently the **wrong** decoder's `rtp_timesnap`.

### 3. Corrupted GPS/RTP mapping in archive writer

The timing poll thread fed the wrong `rtp_timesnap` into `archive_writer.add_timing_snapshot()`, overwriting the correct initial mapping. Since the archive writer uses the GPS/RTP mapping to compute minute boundaries from packet RTP timestamps (`UTC = GPS_TIME + (packet_RTP - RTP_TIMESNAP) / sample_rate`), a wrong `rtp_timesnap` shifted minute boundaries by thousands of seconds.

### 4. Empty `timing_snapshots` array (pre-existing latent bug)

A race condition in the timing poll thread could leave the `timing_snapshots` array in metadata JSON empty, depriving downstream metrology of any GPS/RTP mapping at all.

## Fixes Applied

### A. Removed timing poll thread (`stream_recorder_v2.py`)

The timing poll thread was the corruption vector. It is no longer started.

**Rationale:** `discover_channels()` on the global status multicast is fundamentally unsafe for per-channel timing — it mixes status from all radiod decoders. There is no way to reliably filter by multicast group from the global status stream.

### B. Seed GPS/RTP mapping from `channel_info` (`stream_recorder_v2.py`)

The archive writer's GPS/RTP mapping is now seeded **once** from the `ChannelInfo` returned by `ensure_channel()`. This ChannelInfo comes from our dedicated channel's own status — per-client, per-decoder, authoritative.

Re-seeding happens automatically on radiod restart via the health monitor's `_create_channel()` call, which calls `ensure_channel()` again.

### C. Top-level `gps_time_ns` / `rtp_timesnap` in metadata JSON (`binary_archive_writer.py`)

Added `gps_time_ns` and `rtp_timesnap` as top-level fields in the metadata JSON, eliminating the need for the `timing_snapshots` array and its associated race condition.

### D. Updated `buffer_timing.py` to use top-level fields

`buffer_timing.py` now reads the top-level `gps_time_ns` and `rtp_timesnap` fields as the primary timing source, falling back to the `timing_snapshots` array only for backward compatibility with older files.

### E. Restarted radiod to clear stale channels

All radiod instances were restarted to purge the stale phase-engine channels that caused the SSRC collision.

## Verification

After fix deployment and service restart:

| Metric | Before Fix | After Fix |
|--------|-----------|-----------|
| Minute boundary offset from UTC | +4500s | 0s (±60s for current minute) |
| Timing source in metrology | `rtp_gps` (corrupted) | `rtp_gps` (correct) |
| `system_time` accuracy | ~4500s off | within 19μs of minute boundary |
| Metrology detections | None (starved) | Active: CHU tones, BPM ticks at 40dB SNR |
| Fusion clock estimate | Stale | `D_clock: +0.019 ms ± 1.274 ms [10 broadcasts, grade B]` |
| Chrony SHM feed | Stale | Active: `offset=+0.019ms` |

## Key Lessons

1. **Never use the global status multicast for per-channel timing.** The global status mixes status from all radiod decoders. Use the `ChannelInfo` from `ensure_channel()` — it is per-client and authoritative.

2. **Stale radiod channels are dangerous.** When testing new clients (e.g., phase-engine), always restart radiod afterward to purge leftover decoders. SSRC collisions across clients cause silent data corruption.

3. **Seed, don't poll.** The GPS/RTP mapping from a GPSDO-disciplined source is stable (sub-ppm drift). A single authoritative snapshot at channel creation is sufficient. Continuous polling from an unreliable source (global status) introduced more error than it resolved.

## Files Modified

- `src/hf_timestd/core/stream_recorder_v2.py` — removed timing poll thread, added channel_info seeding
- `src/hf_timestd/core/binary_archive_writer.py` — top-level `gps_time_ns`/`rtp_timesnap` fields (prior session)
- `src/hf_timestd/core/buffer_timing.py` — use top-level fields as primary source (prior session)

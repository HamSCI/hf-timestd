# Dependency Cascade Map

## Service Pipeline (data flows top → bottom)

```
 radiod (external)          chrony (system clock)
    │  RTP/UDP multicast        ▲
    ▼                           │ SHM 0/1
 ┌──────────────────┐     ┌────┴───────────┐
 │  core-recorder   │     │    fusion       │
 │  (9 channels)    │     │  (Kalman→chrony)│
 └────────┬─────────┘     └────────▲────────┘
          │ .bin/.json              │ L2/clock_offset
          │ /dev/shm (HOT)         │ HDF5
          ▼                        │
 ┌──────────────────┐     ┌────────┴────────┐
 │   metrology      │     │  l2-calibration │
 │  (tick detect)   │────▶│  (hw offsets)   │
 │  HOT BUFFER ONLY │     └─────────────────┘
 └────────┬─────────┘                      ▲
          │ L1, tick_phase,                │
          │ tick_timing HDF5               │
          ▼                                │
 ┌──────────────────┐                      │
 │    physics       │──────────────────────┘
 │  (TEC, dTEC)    │   reads L2/clock_offset
 └──────────────────┘
          ▲
          │ reads from cold archive
 ┌────────┴─────────┐
 │  web-api / GRAPE  │
 │  (at leisure)     │
 └──────────────────┘
```

## Real-Time vs. At-Leisure Separation

| Concern | Service | Source | Reads From | Latency Budget |
|---------|---------|--------|------------|----------------|
| UTC reconstruction | metrology | Current minute | **Hot buffer only** (`/dev/shm`) | <60s |
| L1 tick_timing, tick_phase | metrology | Current minute | **Hot buffer only** | <60s |
| L2 calibration | l2-calibration | Recent L1 HDF5 | HDF5 (disk) | ~2-5 min |
| D_clock → chrony SHM | fusion | Recent L2 HDF5 | HDF5 (disk) | ~2-5 min |
| dTEC, TEC, physics | physics | Recent + backfill | Cold archive (disk) | Minutes-hours |
| Web-API history, GRAPE | web-api | Any | Cold archive (disk) | Unbounded |

**Key principle**: Metrology never touches the cold archive.  If data isn't in
`/dev/shm`, it's already too old for real-time UTC reconstruction.  The hot→cold
archiver can move files freely without racing metrology.  Post-hoc services
(physics, web-api, GRAPE) read from cold at their leisure.

## Failure Modes and Cascade Paths

### 1. Recorder falls behind real-time
- **Trigger**: Resequencer tracking desync (large RTP gap → cap-and-continue)
- **Direct effect**: Stale `.bin` files written to `/dev/shm`
- **Cascade**:
  - Metrology reads stale data → produces stale L1 → L2 calibration stale → fusion stale
  - Fusion feeds stale D_clock to chrony SHM → chrony ignores (reach drops to 0)
  - System clock loses HF discipline (falls back to NTP/GPS)
- **Positive feedback loop**: **NONE** (recorder doesn't depend on chrony for operation)
- **Hardening applied**: Staleness guard in `BinaryArchiveWriter` drops data >120s behind wallclock. Resequencer reinitialises on gaps >5s instead of cap-and-continue.

### 2. Metrology stall (no inotify events)
- **Trigger**: inotify on tmpfs unreliable (kernel may coalesce events)
- **Direct effect**: No L1/tick_phase/tick_timing HDF5 written
- **Cascade**:
  - L2 calibration has nothing to calibrate → stalls silently
  - Fusion has no L2 data → stalls silently (D_clock frozen)
  - Physics has no L2 data → stalls silently (TEC/dTEC frozen)
  - Chrony SHM reach drops
- **Positive feedback loop**: **NONE** (metrology reads raw files, not chrony)
- **Hardening applied**: Hot-buffer-only reads eliminate the hot→cold archiver race entirely. Poll fallback scans only the hot buffer window (5 min). No backlog scan — if data has left `/dev/shm`, metrology doesn't care.

### 3. Fusion service stall (HDF5 lock contention)
- **Trigger**: Multiple services (metrology writers, L2 calibration, fusion reader, web-api readers) contend for HDF5 file locks
- **Direct effect**: Fusion blocks on `h5py.File()` open → systemd watchdog kills it
- **Cascade**:
  - Chrony SHM not updated → reach drops → system clock discipline degrades
  - If system clock drifts significantly AND fusion is restarted, the Kalman filter
    could ingest stale data with wrong timestamps
- **Positive feedback loop**: **POSSIBLE** — if fusion restart triggers backfill, and
  backfill opens many HDF5 files simultaneously, it can intensify lock contention,
  causing more timeouts and more restarts.
- **Hardening applied**: `_timed_write` with 30s timeout. `write_measurements_batch` reduces open/close cycles. `locking=False` on all h5py.File calls. Startup lookback capped at 30 min.

### 4. Physics/dTEC write storm
- **Trigger**: Per-tick HDF5 writes (~500 open/write/close per minute)
- **Direct effect**: SSD I/O saturation, HDF5 heap fragmentation, 8GB+ daily files
- **Cascade**:
  - Large files slow down web-api reads (full table scan)
  - File lock contention increases (longer writes hold locks longer)
  - Can trigger fusion stall (cascade path 3)
- **Positive feedback loop**: **YES** — larger files → slower writes → longer lock holds → more contention → fusion stall → restart → backfill creates MORE writes
- **Hardening applied**: Batch writes via `_timed_write_batch()`. ~500 writes/min → ~1 per station-channel.

### 5. radiod stream discontinuity
- **Trigger**: radiod restart, network issue, hardware glitch
- **Direct effect**: Large RTP timestamp gap
- **Cascade**: Same as path 1 if recorder doesn't handle it
- **Positive feedback loop**: **NONE** (radiod is independent)
- **Hardening applied**: Resequencer reinit on >5s gaps with diagnostic logging to distinguish "radiod sent discontinuity" from "local tracking bug"

### 6. Unbounded backfill on restart
- **Trigger**: Service restart after long outage
- **Direct effect**: Service spends minutes/hours processing old data before attending to real-time
- **Cascade**:
  - During backfill, current-minute data is not processed
  - Fusion backfill opens many HDF5 files → lock contention (path 3/4)
- **Positive feedback loop**: **YES** — crash → restart → long backfill → resource exhaustion → crash → restart
- **Hardening applied**:
  - Metrology: **no backfill at all** — hot-buffer-only reads; if data left `/dev/shm` it's gone
  - Physics fusion: 30 min startup lookback cap

## Verified: No Positive-Feedback Loops Remain

| Loop | Before | After |
|------|--------|-------|
| Backfill→contention→crash→restart→backfill | Unbounded (24h) | Metrology: none; Physics: capped 30 min |
| dTEC writes→large files→slow reads→contention→stall | ~500 writes/min | ~1 per station-channel |
| Gap→cap→track desync→repeated gaps | Cap-and-continue (infinite) | Reinit (one-shot recovery) |
| Hot→cold archiver race→retry backoff→stale reads | 3-attempt backoff per minute | Eliminated: metrology reads hot only |

## Remaining Risk: HDF5 Concurrent Access

The architecture still has N services reading/writing the same HDF5 files. All mitigations
(`locking=False`, timeouts, batch writes) are band-aids. The fundamental fix would be:
1. **Message queue**: Services communicate via a queue (Redis, ZMQ) instead of shared files
2. **Write-once readers**: Only one process writes each file; readers use SWMR
3. **Per-service output dirs**: Eliminate shared-path contention entirely

These are architectural changes for a future session.

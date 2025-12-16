# GRAPE Path Conventions - Three-Phase Pipeline

**SYNC VERSION: 2025-12-04-v2-three-phase**

This document defines the canonical path conventions that coordinate data flow between:
- **Python backend** (analytics service, core recorder)
- **JavaScript frontend** (web-ui monitoring server)

## Critical Files

| Language | File | Purpose |
|----------|------|---------|
| Python | `src/grape_recorder/paths.py` | Canonical path definitions (WRITER) |
| JavaScript | `web-ui/grape-paths.js` | Mirror path definitions (READER) |

**Rule: These files MUST stay synchronized.** When adding a new path method to Python, add the matching method to JavaScript.

---

## Three-Phase Architecture

```
{data_root}/
├── raw_archive/{CHANNEL}/           # PHASE 1: Immutable Raw Archive
│   └── {YYYYMMDD}/
│       ├── {YYYY-MM-DDTHH}/
│       │   └── rf@{ts}.h5           # 20 kHz complex64 IQ (Digital RF)
│       └── metadata/
│
├── phase2/{CHANNEL}/                # PHASE 2: Analytical Engine
│   ├── clock_offset/                # D_clock(t) time series
│   ├── carrier_analysis/            # Amplitude, phase, Doppler
│   ├── discrimination/              # WWV/WWVH per-minute results
│   ├── tone_detections/             # 1000/1200 Hz timing markers
│   └── state/
│       └── channel-status.json      # Per-channel status for web-ui
│
├── products/{CHANNEL}/              # PHASE 3: Derived Products
│   ├── decimated/                   # 10 Hz DRF with timing annotations
│   │   └── {YYYYMMDD}/              # Date-organized DRF directories
│   ├── spectrograms/                # PNG visualizations
│   │   └── {YYYYMMDD}_spectrogram.png
│   └── psws_upload/                 # PSWS format for upload
│
├── status/                          # SYSTEM-WIDE STATUS (global)
│   ├── gpsdo_status.json            # GPSDO monitor state
│   ├── timing_status.json           # Primary time reference
│   └── core-recorder-status.json    # Recording status
│
├── state/                           # SERVICE PERSISTENCE
│   └── analytics-{channel_key}.json # Per-channel analytics state
│
└── logs/                            # Application logs
```

---

## Channel Name Conventions

Channels have three representations:

| Format | Example | Usage |
|--------|---------|-------|
| Human-readable | `WWV 10 MHz` | Display, config files |
| Directory | `WWV_10_MHz` | Filesystem paths |
| Key | `wwv10` | State file names, compact IDs |

### Conversion Functions

**Python (`paths.py`):**
```python
GRAPEPaths.channel_name_to_dir("WWV 10 MHz")  # → "WWV_10_MHz"
GRAPEPaths.channel_name_to_key("WWV 10 MHz")  # → "wwv10"
```

**JavaScript (`grape-paths.js`):**
```javascript
channelNameToDir("WWV 10 MHz")   // → "WWV_10_MHz"
channelNameToKey("WWV 10 MHz")   // → "wwv10"
dirToChannelName("WWV_10_MHz")   // → "WWV 10 MHz"
```

---

## Writer/Reader Contracts

### Phase 1: Raw Archive (Core Recorder → Analytics Service)

| Writer | Reader | Path |
|--------|--------|------|
| `core_recorder.py` | `analytics_service.py` | `raw_archive/{CHANNEL}/{YYYYMMDD}/` |

**Contract:** Core recorder writes 20 kHz Digital RF. Analytics service reads for Phase 2 processing.

### Phase 2: Analytical Results (Analytics Service → Web-UI)

| Data | Python Writer | JS Reader | Path |
|------|---------------|-----------|------|
| Channel status | `analytics_service.py` | `getChannelStatusFile()` | `phase2/{CHANNEL}/state/channel-status.json` |
| Discrimination | `discrimination_csv_writers.py` | `getDiscriminationDir()` | `phase2/{CHANNEL}/discrimination/` |
| Tone detections | `analytics_service.py` | `getToneDetectionsDir()` | `phase2/{CHANNEL}/tone_detections/` |

### Phase 2: Decimated Data (Analytics Service → Spectrogram Generator)

| Data | Python Writer | Python Reader | Path |
|------|---------------|---------------|------|
| Decimated binary | `phase2_analytics_service.py` | `carrier_spectrogram.py` | `phase2/{CHANNEL}/decimated/{YYYYMMDD}.bin` |
| Decimated metadata | `phase2_analytics_service.py` | `carrier_spectrogram.py` | `phase2/{CHANNEL}/decimated/{YYYYMMDD}_meta.json` |

### Phase 3: Products (Spectrogram Generator → Web-UI)

| Data | Python Writer | JS Reader | Path |
|------|---------------|-----------|------|
| **Daily Spectrogram** | `carrier_spectrogram.py` | `/spectrograms/` endpoint | `products/{CHANNEL}/spectrograms/{YYYYMMDD}_spectrogram.png` |
| Rolling Spectrogram | `carrier_spectrogram.py` | `/spectrograms/` endpoint | `products/{CHANNEL}/spectrograms/rolling_{N}h.png` |

**⚠️ CANONICAL: There is ONE path for spectrograms. No fallbacks, no alternatives.**

### System Status (Analytics Service → Web-UI)

| Data | Python Writer | JS Reader | Path |
|------|---------------|-----------|------|
| GPSDO status | `analytics_service._write_gpsdo_status()` | `getGpsdoStatusFile()` | `status/gpsdo_status.json` |
| Timing status | `timing_metrics_writer.py` | `getTimingStatusFile()` | `status/timing_status.json` |

---

## Status File Formats

### `status/gpsdo_status.json`
```json
{
  "anchor_state": "STEADY_STATE",
  "consecutive_verifications": 47,
  "last_verification_time": 1733315400.0,
  "last_verification_error_ms": 0.05,
  "best_channel": "WWV 10 MHz",
  "best_station": "WWV",
  "holdover_since": null,
  "total_reanchors": 2,
  "verification_trend_ms": 0.03,
  "verification_history": [-0.02, -0.01, 0.05],
  "quality_flag": "LOCKED",
  "updated_at": "2025-12-04T15:30:00+00:00",
  "channel_name": "WWV 10 MHz"
}
```

### `phase2/{CHANNEL}/state/channel-status.json`
```json
{
  "channel_name": "WWV 10 MHz",
  "sample_rate": 20000,
  "time_snap": {
    "established": true,
    "utc_timestamp": 1733315400.0,
    "source": "wwv_verified",
    "confidence": 0.95
  },
  "quality_metrics": {
    "last_completeness_pct": 99.8,
    "last_packet_loss_pct": 0.1
  },
  "current_snr_db": 35.2,
  "digital_rf": {
    "last_write_time": "2025-12-04T15:30:00Z"
  },
  "updated_at": "2025-12-04T15:30:00Z"
}
```

---

## Web-UI URL Routing

| URL Pattern | Served From |
|-------------|-------------|
| `/spectrograms/{channel}/{filename}` | `products/{CHANNEL}/spectrograms/{filename}` |
| `/api/v1/gpsdo-status` | Reads `status/gpsdo_status.json` |
| `/api/v1/carrier/quality` | Reads `phase2/{CHANNEL}/state/channel-status.json` |

---

## Adding New Status Files

1. **Add Python method** to `paths.py`:
   ```python
   def get_new_status_file(self) -> Path:
       return self.get_status_dir() / 'new_status.json'
   ```

2. **Add JavaScript method** to `grape-paths.js`:
   ```javascript
   getNewStatusFile() {
       return join(this.getStatusDir(), 'new_status.json');
   }
   ```

3. **Update SYNC VERSION** in both files

4. **Write from Python** (analytics service or similar)

5. **Read from JavaScript** (monitoring server endpoint)

---

## Discovery Methods

| Method | Scans | Returns |
|--------|-------|---------|
| `discoverChannels()` | `raw_archive/` | Channels with Phase 1 data |
| `discoverPhase2Channels()` | `phase2/` | Channels with analytical results |
| `discoverProductChannels()` | `products/` | Channels with derived products |

**Important:** The web-UI uses `discoverPhase2Channels()` for carrier analysis since that's where active analytical results are stored.

---

## Sample Rate Reference

| Phase | Sample Rate | Format |
|-------|-------------|--------|
| Phase 1 (Raw) | 20 kHz | Digital RF (complex64) |
| Phase 2 (Analysis) | 20 kHz | In-memory processing |
| Phase 3 (Decimated) | 10 Hz | Digital RF (complex64) |

The decimation ratio is **2000:1** (20,000 Hz → 10 Hz).

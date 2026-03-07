# NEVER CHANGE THE FOLLOWING PRIMARY INSTRUCTION!!!

**Primary Instruction:** In this context you will perform a critical review of the HF Time Standard (hf-timestd) project, either in its entirety or in a specific component, as specified by the user. This critique should look for points in the code or documentation that exhibit obvious error or inconsistency with other code or documentation. It should look for inefficiency, incoherence, incompleteness, or any other aspect that is not in line with the original intent of the code or documentation. It should also look for obsolete, deprecated, or "zombie" code that should be removed. Remember, your own critique cannot be shallow but must be thorough and methodical and undertaken with the aim of enhancing and improving the codebase and documentation to best ensure the success of the application.

Make your criticism from the perspective of 1) a user of the system, 2) a metrologist, 3) a ionospheric scientist, and 4) a software engineer. These perspectives can differ in their priorities and interests, and your critique should reflect this. For example, a user of the system will be most interested in the accuracy and reliability of the system, while a metrologist will be most interested in the precision and traceability of the system, while an ionospheric scientist will be most interested in the ionospheric conditions and their impact on the system, and a software engineer will be most interested in the code quality, maintainability, coverage and adequacy of testing, and the resilience and robustness of the system over time and in edge cases. Ultimately, however, a consensus of these perspectives should guide your critique in service of meeting the application's objectives.

# NEVER CHANGE THE PRECEEDING PRIMARY INSTRUCTION!!!

# The following secondary instruction and information will guide your critique in this particular session (the instructions below will vary from session to session)

---

## NEXT SESSION: DIAGNOSE AND FIX THE GRAPE MODULE

**Goal:** The GRAPE daily pipeline (`grape-daily.service`) is failing every night. The decimation stage produces no output for any of the 21 channels, the quality gate aborts, and consequently `grape.html` has nothing to display (0 channels, empty selectors). Diagnose why decimation produces no output and fix it so the full pipeline (decimate → spectrogram → package → upload) succeeds.

**Deadline:** Demo-ready by approximately March 14, 2026 (HamSCI workshop).

---

## 1. Observed Symptoms

1. **`grape-daily.service`** is in **failed** state (`systemctl status grape-daily.service`)
2. **Journal output** shows every channel fails identically:
   ```
   ⚠️  CHU_7850: decimation produced no output
   ```
   Root cause visible in earlier log lines:
   ```
   WARNING - No data directory for 20260306 at /var/lib/timestd/raw_archive/WWVH_5000/20260306
   Found 0 minutes for 20260306 in WWVH 5000
   Completed WWVH 5000: 0 minutes, 0 samples
   ```
3. **Quality gate** requires ALL 21 channels to decimate successfully; since 0/21 succeed, it aborts before spectrogram/package/upload stages.
4. **`grape.html`** shows: CHANNELS: 0, empty channel/date selectors, "No decimated data found"
5. **Upload history works fine** — 40 completed uploads from earlier (Jan-Feb 2026)

## 2. Key Observation: Raw Data Exists But Is Sparse

```
/var/lib/timestd/raw_archive/          # Raw 24kHz audio per channel per day
├── CHU_3330/20260306/                  # ✓ has data for some dates
├── CHU_7850/                           # directories exist but dates vary
├── WWVH_5000/                          # ✗ missing 20260306
└── ...                                 # 21 channel directories total
```

- Raw archive has data, but **not every channel has data for every date**
- The pipeline runs for yesterday's date and expects ALL channels to have raw data
- The all-or-nothing gate (`GATE FAILED: 21 channels missing`) means **partial data is discarded**

## 3. Files to Examine

### Core Pipeline (start here)
| File | Role |
|------|------|
| `src/hf_timestd/cli.py` ~lines 360-520 | `grape daily` CLI command — orchestrates decimate → spectrogram → package → upload with quality gates |
| `src/hf_timestd/grape/decimation_pipeline.py` | `DecimationPipeline` — reads raw audio, calls `StatefulDecimator`, writes `.bin` output |
| `src/hf_timestd/grape/decimation.py` | `StatefulDecimator` — 24kHz → 10Hz decimation with anti-alias filtering |
| `src/hf_timestd/grape/raw_reader.py` | Reads raw IQ/audio files from `raw_archive/<channel>/<date>/` |

### Product Generation
| File | Role |
|------|------|
| `src/hf_timestd/grape/spectrogram.py` | Generates spectrogram PNGs from decimated `.bin` data |
| `src/hf_timestd/grape/packager.py` | Packages decimated data for HamSCI upload |
| `src/hf_timestd/grape/uploader.py` | Uploads packaged data to HamSCI servers |
| `src/hf_timestd/grape/decimated_buffer.py` | Buffer for decimated samples |

### Web Layer
| File | Role |
|------|------|
| `web-api/services/grape_service.py` | Serves channel list, spectrograms, upload history — reads from `products/` |
| `web-api/routers/grape.py` | FastAPI router, `/api/grape/*` endpoints |
| `web-api/static/grape.html` | Frontend — channel/date selectors, spectrogram viewer, upload history |

### Service Configuration
| File | Role |
|------|------|
| `/etc/systemd/system/grape-daily.service` | Runs `python3 -m hf_timestd.cli grape daily` |
| `/etc/systemd/system/grape-daily.timer` | Triggers daily at 01:01 UTC |

## 4. Data Directory Layout

```
/var/lib/timestd/
├── raw_archive/<CHANNEL>/<YYYYMMDD>/*.raw    # Input: 24kHz raw audio per minute
├── products/<CHANNEL>/
│   ├── decimated/*.bin                        # Output: 10Hz decimated (ALL EMPTY)
│   └── spectrograms/*_spectrogram.png         # Output: daily spectrograms (ALL EMPTY)
└── upload/<YYYYMMDD>/                         # Packaged data for HamSCI upload
    └── queue.json                             # Upload queue (40 completed from Jan-Feb)
```

## 5. Likely Root Causes to Investigate

1. **Raw data path mismatch** — Does `raw_reader.py` look in the right directory? The warning says `/var/lib/timestd/raw_archive/WWVH_5000/20260306` doesn't exist, but raw data may be stored differently (different naming, subdirectory structure, or the recorder writes to a different path).

2. **Channel naming mismatch** — The pipeline expects channels like `WWVH_5000` but the recorder may use a different naming convention (e.g., `WWVH_5_MHz`, `WWVH_5000_Hz`). Check what the core recorder (`timestd-core-recorder.service`) actually writes.

3. **All-or-nothing gate too strict** — The pipeline aborts if ANY channel is missing. For a station that only receives a subset of broadcasts (e.g., no BPM reception), this gate will always fail. Consider making it partial-success tolerant.

4. **Decimation writes to wrong output path** — The decimation may run but write `.bin` files somewhere other than `products/<CHANNEL>/decimated/`. Check `DecimationPipeline` output path logic.

5. **Raw file format issue** — Even for channels that DO have raw data directories (like `CHU_3330/20260306/`), the decimation still produces 0 samples. This suggests `raw_reader.py` may not be finding or parsing the raw files correctly. Check the expected file naming pattern (`????????.bin` glob) vs. what actually exists.

## 6. Diagnostic Commands

```bash
# Service status and recent logs
systemctl status grape-daily.service
sudo journalctl -u grape-daily.service --no-pager -n 100

# What raw data exists for a channel that should have data?
ls -la /var/lib/timestd/raw_archive/CHU_3330/20260306/
ls -la /var/lib/timestd/raw_archive/CHU_7850/ | head -10

# What does the products directory look like?
find /var/lib/timestd/products/ -name "decimated" -type d -exec sh -c 'echo "$1: $(ls "$1" | wc -l) files"' _ {} \;

# Run decimation manually for a single channel with verbose output
/opt/hf-timestd/venv/bin/python3 -m hf_timestd.cli grape daily --help

# Check what the recorder actually writes
ls -la /var/lib/timestd/raw_archive/CHU_3330/20260306/ | head -20

# API response
curl -s http://localhost:8000/api/grape/summary | python3 -m json.tool | head -20
```

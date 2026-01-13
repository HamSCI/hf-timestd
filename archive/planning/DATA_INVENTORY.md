# HF-TIMESTD Data Inventory and Model Assessment

**Date:** 2026-01-03  
**Purpose:** Comprehensive inventory of all data products for database migration planning and data exposure strategy  
**Objectives:**
1. Catalog all data products with complete schemas and metadata
2. Assess current data model for ClickHouse migration suitability
3. Design data exposure strategy for monitoring, metrology, and ionospheric science

---

## Executive Summary

The HF Time Standard system produces a hierarchical data pipeline (L0 → L3C) with:
- **9 data product types** across 5 processing levels
- **~50 distinct fields** per product with full ISO GUM uncertainty budgets
- **4 broadcast stations** (WWV, WWVH, CHU, BPM) across 9 frequencies
- **60-second cadence** for timing products, hourly for aggregated statistics
- **Current storage:** HDF5 files with daily rotation (~1.5 GB/day total)
- **Data volume:** ~86,400 timing measurements/day, ~216 propagation stats/day

### Key Findings

**Strengths:**
- Well-defined JSON schemas with versioning
- Comprehensive uncertainty quantification (ISO GUM compliant)
- Self-describing metadata in HDF5 format
- SWMR mode enables concurrent read access

**Opportunities:**
- Time-series database (ClickHouse) would enable faster queries across stations/frequencies
- Current file-per-channel-per-day structure limits cross-station analysis
- No unified query interface for web UI or external tools
- Metadata scattered across multiple files

---

## 1. Station Metadata Inventory

### 1.1 Broadcast Stations

| Station | Organization | Location | Coordinates | Frequencies (MHz) | Power |
|---------|-------------|----------|-------------|-------------------|-------|
| **WWV** | NIST | Fort Collins, CO, USA | 40.6807°N, 105.0407°W | 2.5, 5, 10, 15, 20, 25 | 2.5-10 kW |
| **WWVH** | NIST | Kekaha, Kauai, HI, USA | 21.9872°N, 159.7636°W | 2.5, 5, 10, 15 | 10 kW |
| **CHU** | NRC Canada | Ottawa, ON, Canada | 45.2953°N, 75.7544°W | 3.33, 7.85, 14.67 | N/A |
| **BPM** | NTSC China | Pucheng, Shaanxi, China | 34.9489°N, 109.5430°E | 2.5, 5, 10, 15 | N/A |

### 1.2 Timing Signal Characteristics

| Station | Tone Frequency | Tick Duration | Timing Offset | Modulation |
|---------|---------------|---------------|---------------|------------|
| WWV | 1000 Hz | 5 ms | 0 ms | BCD time code |
| WWVH | 1200 Hz | 5 ms | 0 ms | BCD time code |
| CHU | 1000 Hz | 300 ms (500 ms at :00) | 0 ms | FSK time code (Bell 103) |
| BPM | 1000 Hz | 10 ms (UTC), 100 ms (UT1) | -20 ms | Time code |

### 1.3 Frequency Sharing

**Shared Frequencies** (require station discrimination):
- 2.5, 5, 10, 15 MHz: WWV + WWVH + BPM

**Unique Frequencies** (single station):
- 20, 25 MHz: WWV only
- 3.33, 7.85, 14.67 MHz: CHU only

### 1.4 Broadcast Schedules

**WWV Ground Truth Minutes:** 1, 8, 16, 17, 19 (5/hour)  
**WWVH Ground Truth Minutes:** 2, 43-51 (10/hour)  
**BPM UT1 Minutes:** 25-29, 55-59 (10/hour, not usable for UTC)  
**BPM Pure Carrier Minutes:** 10-15, 40-45 (12/hour)

---

## 2. Data Product Inventory

### 2.1 Processing Levels Overview

| Level | Name | Description | Cadence | Storage Format | Retention |
|-------|------|-------------|---------|----------------|-----------|
| **L0** | Raw IQ | Complex IQ samples from SDR | Continuous | Binary (.bin.zst) | Hot buffer (RAM) |
| **L1A** | Channel Observables | Calibrated carrier/tone measurements | 60 sec | HDF5 | 30 days |
| **L1B** | BCD Timecode | Decoded BCD time information | 60 sec | HDF5 | 30 days |
| **L2** | Timing Measurements | Station-assigned UTC measurements | 60 sec | HDF5 | Permanent |
| **L3A** | TEC Estimates | Ionospheric Total Electron Content | 60 sec | HDF5 | Permanent |
| **L3A** | GNSS VTEC | GPS-derived vertical TEC | 1 sec | HDF5 + CSV | Permanent |
| **L3B** | Fusion Timing | Multi-station UTC estimate | 60 sec | HDF5 | Permanent |
| **L3C** | Propagation Stats | Aggregated mode statistics | 3600 sec | HDF5 | Permanent |

### 2.2 L0 - Raw IQ Data

**Product:** `L0_raw_iq`  
**Format:** Binary compressed (zstd level 3)  
**Location:** `/dev/shm/timestd/raw_buffer/` (hot), `/var/lib/timestd/raw_buffer/` (cold)  
**Schema:** Complex64 IQ samples at 24 kHz sample rate  
**File naming:** `{CHANNEL}_{YYYYMMDD}_{HHMMSS}.bin.zst`  
**Size:** ~6.9 MB/day/channel (compressed)  
**Channels:** 9 (SHARED_2500, SHARED_5000, SHARED_10000, SHARED_15000, WWV_20000, WWV_25000, CHU_3330, CHU_7850, CHU_14670)

**Purpose:** Raw signal archive for reprocessing and algorithm development

**Tiered Storage:**
- Hot buffer: `/dev/shm/timestd/` (RAM, 20% of available)
- Cold storage: `/var/lib/timestd/raw_buffer/` (disk)
- Automatic promotion/demotion based on age and access patterns

### 2.3 L1A - Channel Observables

**Product:** `L1A_channel_observables`  
**Schema Version:** 1.0.0  
**Location:** `/var/lib/timestd/phase2/{STATION}_{FREQ}/`  
**File naming:** `{STATION}_{FREQ}_channel_observables_{YYYYMMDD}.h5`  
**Cadence:** 60 seconds  
**Records per day:** 1,440 per channel

**Fields (23 total):**

| Field | Type | Units | Description | Valid Range |
|-------|------|-------|-------------|-------------|
| `timestamp_utc` | string | ISO8601 | Measurement timestamp | - |
| `minute_boundary` | int64 | unix epoch | Minute boundary timestamp | - |
| `rtp_timestamp` | int64 | RTP units | RTP timestamp from recorder | - |
| `carrier_power_db` | float32 | dBm | Carrier power | [-120, 0] |
| `carrier_snr_db` | float32 | dB | Signal-to-noise ratio | [-20, 60] |
| `carrier_doppler_hz` | float32 | Hz | Doppler shift | [-10, 10] |
| `doppler_std_hz` | float32 | Hz | Doppler spread | [0, 5] |
| `coherence_time_sec` | float32 | seconds | Coherent integration window | [0, 60] |
| `phase_variance_rad` | float32 | radians | Phase stability | [0, 6.28] |
| `wwv_tone_500hz_db` | float32 | dB | WWV 500 Hz tone power | - |
| `wwv_tone_600hz_db` | float32 | dB | WWV 600 Hz tone power | - |
| `wwvh_tone_1200hz_db` | float32 | dB | WWVH 1200 Hz tone power | - |
| `wwvh_tone_1500hz_db` | float32 | dB | WWVH 1500 Hz tone power | - |
| `wwv_tick_snr_db` | float32 | dB | WWV tick SNR | - |
| `wwvh_tick_snr_db` | float32 | dB | WWVH tick SNR | - |
| `chu_tone_db` | float32 | dB | CHU tone power | - |
| `chu_tick_snr_db` | float32 | dB | CHU tick SNR | - |
| `test_signal_detected` | bool | - | Test signal presence | - |
| `test_signal_snr_db` | float32 | dB | Test signal SNR | - |
| `quality_flag` | string | enum | Quality assessment | GOOD/MARGINAL/BAD/MISSING |
| `data_completeness` | float32 | fraction | Data completeness | [0, 1] |
| `processing_version` | string | - | Software version | - |

**Data Volume:** ~140 KB/day/channel (uncompressed HDF5)

### 2.4 L1B - BCD Timecode

**Product:** `L1B_bcd_timecode`  
**Schema Version:** 1.0.0  
**Location:** `/var/lib/timestd/phase2/{STATION}_{FREQ}/`  
**Cadence:** 60 seconds  
**Purpose:** Decoded BCD time information for UTC verification

**Key Fields:**
- BCD-decoded UTC time (year, day, hour, minute)
- Decode confidence and error metrics
- DUT1 offset (UT1 - UTC)
- Leap second indicators

### 2.5 L2 - Timing Measurements

**Product:** `L2_timing_measurements`  
**Schema Version:** 1.0.0  
**Location:** `/var/lib/timestd/phase2/{STATION}_{FREQ}/`  
**File naming:** `{STATION}_{FREQ}_timing_measurements_{YYYYMMDD}.h5`  
**Cadence:** 60 seconds  
**Records per day:** 1,440 per channel (9 channels = 12,960 records/day)

**Core Timing Fields:**

| Field | Type | Units | Description |
|-------|------|-------|-------------|
| `timestamp_utc` | string | ISO8601 | Measurement timestamp |
| `station` | string | enum | Station assignment (WWV/WWVH/CHU/BPM) |
| `frequency_mhz` | float32 | MHz | Carrier frequency |
| `clock_offset_ms` | float32 | ms | D_clock: observed - expected arrival |
| `uncertainty_ms` | float32 | ms | Combined standard uncertainty (u_c) |
| `expanded_uncertainty_ms` | float32 | ms | Expanded uncertainty (U = k × u_c) |
| `coverage_factor` | float32 | - | Coverage factor k (typically 2) |
| `confidence_level` | float32 | - | Confidence level (typically 0.95) |

**ISO GUM Uncertainty Budget (Type A):**

| Component | Field | Type | Description |
|-----------|-------|------|-------------|
| RTP timestamp | `u_rtp_timestamp_ms` | A | Timestamp resolution |
| Ionospheric | `u_ionospheric_ms` | A | Propagation variability |
| Multipath | `u_multipath_ms` | A | Delay spread |

**ISO GUM Uncertainty Budget (Type B):**

| Component | Field | Type | Description |
|-----------|-------|------|-------------|
| Discrimination | `u_discrimination_ms` | B | Station ID uncertainty |
| GPSDO | `u_gpsdo_ms` | B | Reference stability |
| Propagation model | `u_propagation_model_ms` | B | Model uncertainty |

**Quality Metrics:**

| Field | Type | Description |
|-------|------|-------------|
| `quality_grade` | string | A/B/C/D based on expanded uncertainty |
| `confidence` | float32 | Overall measurement confidence [0-1] |
| `quality_flag` | string | GOOD/MARGINAL/BAD/MISSING |

**Quality Grade Thresholds:**
- **Grade A:** U < 1.0 ms (excellent)
- **Grade B:** 1.0 ms ≤ U < 2.0 ms (good)
- **Grade C:** 2.0 ms ≤ U < 3.0 ms (marginal)
- **Grade D:** U ≥ 3.0 ms (poor)

**Propagation Metadata:**

| Field | Type | Description |
|-------|------|-------------|
| `propagation_delay_ms` | float32 | Estimated propagation delay |
| `propagation_mode` | string | Ray-tracing classification (1E, 2F, etc.) |
| `n_hops` | int32 | Number of ionospheric hops |
| `delay_spread_ms` | float32 | Multipath delay spread |
| `snr_db` | float32 | Signal-to-noise ratio |
| `doppler_hz` | float32 | Doppler shift |

**Traceability:**

| Field | Type | Description |
|-------|------|-------------|
| `traceability_chain` | string | Metrological traceability |
| `calibration_date` | string | Last GPSDO calibration |
| `gpsdo_locked` | bool | GPSDO lock status |
| `utc_verified` | bool | UTC verified via BCD |
| `multi_station_verified` | bool | Cross-station verification |

**Data Volume:** ~350 KB/day/channel

### 2.6 L3A - TEC Estimates

**Product:** `L3A_tec`  
**Schema Version:** 1.0.0  
**Location:** `/var/lib/timestd/phase2/science/tec/`  
**File naming:** `{STATION}_tec_{YYYYMMDD}.h5`  
**Cadence:** 60 seconds  
**Purpose:** Ionospheric Total Electron Content from multi-frequency dispersion

**Key Fields:**

| Field | Type | Units | Description |
|-------|------|-------|-------------|
| `timestamp_utc` | string | ISO8601 | Measurement timestamp |
| `station` | string | enum | Broadcast station |
| `tec_tecu` | float32 | TECU | Total Electron Content (10^16 e/m²) |
| `t_vacuum_error_ms` | float32 | ms | Vacuum propagation time (fit intercept) |
| `confidence` | float32 | [0-1] | TEC estimate confidence |
| `n_frequencies` | int32 | - | Number of frequencies used |
| `residuals_ms` | float32 | ms | RMS residuals of 1/f² fit |
| `frequencies_mhz` | string | CSV | Frequencies used in calculation |

**Validation Fields:**

| Field | Type | Description |
|-------|------|-------------|
| `vtec_tecu` | float32 | GPS VTEC for comparison (from IONEX) |
| `tec_bias_tecu` | float32 | HF TEC - GPS VTEC bias |
| `validation_flag` | string | VALIDATED/UNVALIDATED/VTEC_UNAVAILABLE/VALIDATION_FAILED |

**Physics:** TEC derived from dispersion relation: `T_obs(f) = T_vacuum + (40.3 × TEC) / f²`

**Data Volume:** ~50 KB/day/station (4 stations = 200 KB/day)

### 2.7 L3A - GNSS VTEC

**Product:** `L3A_gnss_vtec`  
**Schema Version:** 1.0.0  
**Location:** `/var/lib/timestd/data/gnss_vtec/`  
**File naming:** `gnss_vtec_{YYYYMMDD}.h5` + `gnss_vtec.csv`  
**Cadence:** 1 second  
**Purpose:** Real-time GPS-derived vertical TEC for ionospheric corrections

**Key Fields:**

| Field | Type | Units | Description |
|-------|------|-------|-------------|
| `timestamp_utc` | string | ISO8601 | Measurement timestamp |
| `unix_timestamp` | float64 | seconds | Unix epoch for efficient lookups |
| `vtec_tecu` | float32 | TECU | Vertical Total Electron Content |
| `n_satellites` | int32 | - | Number of GPS satellites used |
| `quality_flag` | string | enum | GOOD/MARGINAL/BAD |
| `min_elevation_deg` | float32 | degrees | Minimum satellite elevation |
| `dcb_corrected` | bool | - | Differential Code Bias corrected |

**Source:** ZED-F9P dual-frequency GPS receiver (192.168.0.202:9000)  
**Update Rate:** 1 Hz (86,400 records/day)  
**Data Volume:** ~2.5 MB/day (HDF5) + ~1.5 MB/day (CSV)

### 2.8 L3B - Fusion Timing

**Product:** `L3_fusion_timing`  
**Schema Version:** 1.0.0  
**Location:** `/var/lib/timestd/phase2/fusion/`  
**File naming:** `fusion_fusion_timing_{YYYYMMDD}.h5`  
**Cadence:** 60 seconds  
**Purpose:** Multi-station weighted fusion for optimal UTC estimate

**Core Fusion Fields:**

| Field | Type | Units | Description |
|-------|------|-------|-------------|
| `timestamp_utc` | string | ISO8601 | Measurement timestamp |
| `d_clock_fused_ms` | float32 | ms | Fused D_clock (calibrated, weighted) |
| `d_clock_raw_ms` | float32 | ms | Raw D_clock (unweighted, pre-calibration) |
| `uncertainty_ms` | float32 | ms | Combined uncertainty (RSS) |
| `statistical_uncertainty_ms` | float32 | ms | Measurement scatter |
| `systematic_uncertainty_ms` | float32 | ms | Calibration convergence error |
| `propagation_uncertainty_ms` | float32 | ms | Ionospheric variability |

**Station Contributions:**

| Field | Type | Description |
|-------|------|-------------|
| `n_broadcasts` | int32 | Total broadcasts in fusion |
| `n_stations` | int32 | Number of unique stations |
| `stations_used` | string | Comma-separated station list |
| `wwv_mean_ms` | float32 | Mean D_clock from WWV |
| `wwvh_mean_ms` | float32 | Mean D_clock from WWVH |
| `chu_mean_ms` | float32 | Mean D_clock from CHU |
| `bpm_mean_ms` | float32 | Mean D_clock from BPM |
| `wwv_count` | int32 | Number of WWV broadcasts |
| `wwvh_count` | int32 | Number of WWVH broadcasts |
| `chu_count` | int32 | Number of CHU broadcasts |
| `bpm_count` | int32 | Number of BPM broadcasts |

**Consistency Metrics:**

| Field | Type | Description |
|-------|------|-------------|
| `wwv_intra_std_ms` | float32 | Intra-station std dev (WWV) |
| `wwvh_intra_std_ms` | float32 | Intra-station std dev (WWVH) |
| `chu_intra_std_ms` | float32 | Intra-station std dev (CHU) |
| `bpm_intra_std_ms` | float32 | Intra-station std dev (BPM) |
| `inter_station_spread_ms` | float32 | Spread between station means |
| `consistency_flag` | string | OK/INTRA_ANOMALY/INTER_ANOMALY/DISCRIMINATION_SUSPECT |

**Global Solver:**

| Field | Type | Description |
|-------|------|-------------|
| `global_solve_verified` | bool | Global differential solve performed |
| `global_solve_consistency_ms` | float32 | Consistency metric |
| `global_solve_n_obs` | int32 | Number of observations |

**Quality Assessment:**

| Field | Type | Description |
|-------|------|-------------|
| `quality_grade` | string | A/B/C/D based on uncertainty and count |
| `kalman_state` | string | ACQUIRING/LOCKED/REACQUIRING |
| `outliers_rejected` | int32 | Outliers rejected via MAD filter |
| `calibration_applied` | bool | Per-broadcast calibration applied |
| `reference_station` | string | Reference station for calibration |

**Current Performance (2026-01-03):**
- D_clock: -0.073 ms (system 73 μs fast)
- Uncertainty: ±0.829 ms (sub-millisecond)
- Quality: Grade B
- Broadcasts: 59 stations contributing

**Data Volume:** ~120 KB/day

### 2.9 L3C - Propagation Statistics

**Product:** `L3C_propagation_stats`  
**Schema Version:** 1.0.0  
**Location:** `/var/lib/timestd/phase2/science/propagation/`  
**File naming:** `{STATION}_{FREQ}_propagation_stats_{YYYYMMDD}.h5`  
**Cadence:** 3600 seconds (hourly aggregation)  
**Purpose:** Propagation mode occurrence and MUF estimation

**Aggregation Fields:**

| Field | Type | Description |
|-------|------|-------------|
| `timestamp_utc` | string | Aggregation period end time |
| `period_start` | string | Aggregation period start time |
| `aggregation_period` | string | HOURLY/DAILY/MONTHLY |
| `station` | string | Station (or ALL for combined) |
| `frequency_mhz` | float32 | Frequency (or 0 for all) |

**Propagation Mode Probabilities:**

| Field | Type | Range | Description |
|-------|------|-------|-------------|
| `mode_1e_probability` | float32 | [0, 1] | E-layer single-hop |
| `mode_1f_probability` | float32 | [0, 1] | F-layer single-hop |
| `mode_2f_probability` | float32 | [0, 1] | F-layer two-hop |
| `mode_3f_probability` | float32 | [0, 1] | F-layer three-hop |
| `mode_gw_probability` | float32 | [0, 1] | Ground wave |
| `mode_unknown_probability` | float32 | [0, 1] | Unknown/unclassified |

**MUF Estimation:**

| Field | Type | Description |
|-------|------|-------------|
| `estimated_muf_mhz` | float32 | Maximum Usable Frequency |
| `muf_confidence` | float32 | Confidence in MUF estimate |
| `mean_snr_db` | float32 | Mean SNR during period |

**Data Quality:**

| Field | Type | Description |
|-------|------|-------------|
| `n_observations` | int32 | Number of observations in period |
| `data_completeness` | float32 | Fraction of expected observations |
| `quality_flag` | string | GOOD/MARGINAL/BAD |

**Expected Observations:**
- Hourly: 60 (1/minute)
- Daily: 1,440 (1/minute)
- Monthly: ~43,200 (1/minute)

**Data Volume:** ~30 KB/day/channel (24 records/day × 9 channels)

---

## 3. Data Storage Architecture

### 3.1 Current File Organization

```
/var/lib/timestd/
├── raw_buffer/                    # L0 - Raw IQ (tiered storage)
│   ├── SHARED_2500/
│   ├── SHARED_5000/
│   ├── SHARED_10000/
│   ├── SHARED_15000/
│   ├── WWV_20000/
│   ├── WWV_25000/
│   ├── CHU_3330/
│   ├── CHU_7850/
│   └── CHU_14670/
├── phase2/                        # L1-L3 products
│   ├── SHARED_2500/               # L1A, L2 per channel
│   │   ├── SHARED_2500_channel_observables_20260103.h5
│   │   └── SHARED_2500_timing_measurements_20260103.h5
│   ├── SHARED_5000/
│   ├── SHARED_10000/
│   ├── SHARED_15000/
│   ├── WWV_20000/
│   ├── WWV_25000/
│   ├── CHU_3330/
│   ├── CHU_7850/
│   ├── CHU_14670/
│   ├── fusion/                    # L3B - Fusion timing
│   │   └── fusion_fusion_timing_20260103.h5
│   └── science/                   # L3A, L3C - Science products
│       ├── tec/
│       │   ├── WWV_tec_20260103.h5
│       │   ├── WWVH_tec_20260103.h5
│       │   ├── CHU_tec_20260103.h5
│       │   └── BPM_tec_20260103.h5
│       └── propagation/
│           ├── WWV_10000_propagation_stats_20260103.h5
│           └── ...
├── data/
│   ├── gnss_vtec/                 # L3A - GNSS VTEC
│   │   └── gnss_vtec_20260103.h5
│   ├── gnss_vtec.csv              # Real-time CSV
│   └── state/
│       └── broadcast_calibration.json
└── logs/
```

### 3.2 HDF5 File Structure

**Individual Dataset Format** (Phase 2 schema):
```
file.h5
├── /timestamp_utc [dataset: string array]
├── /minute_boundary [dataset: int64 array]
├── /clock_offset_ms [dataset: float32 array]
├── /uncertainty_ms [dataset: float32 array]
├── ...
└── /metadata [group]
    ├── schema_version [attribute]
    ├── data_product [attribute]
    ├── channel [attribute]
    ├── processing_version [attribute]
    └── creation_date [attribute]
```

**Features:**
- SWMR (Single Writer Multiple Reader) mode for concurrent access
- Daily file rotation (YYYYMMDD)
- Individual datasets per field (efficient columnar access)
- Metadata as HDF5 attributes
- Compression: gzip level 4 (default)

### 3.3 Data Volume Summary

| Product | Cadence | Records/Day | Size/Day | Retention |
|---------|---------|-------------|----------|-----------|
| L0 Raw IQ | Continuous | - | ~62 MB (9 channels) | 7 days (hot) |
| L1A Observables | 60s | 12,960 | ~1.3 MB | 30 days |
| L2 Timing | 60s | 12,960 | ~4.5 MB | Permanent |
| L3A TEC | 60s | 5,760 | ~200 KB | Permanent |
| L3A GNSS VTEC | 1s | 86,400 | ~4 MB | Permanent |
| L3B Fusion | 60s | 1,440 | ~120 KB | Permanent |
| L3C Propagation | 3600s | 216 | ~30 KB | Permanent |
| **Total** | - | - | **~72 MB/day** | - |

**Annual Storage:** ~26 GB/year (excluding L0 raw IQ)

---

## 4. Data Access Patterns

### 4.1 Current Access Methods

**Direct File I/O:**
- Services read HDF5 files directly using `h5py`
- SWMR mode enables concurrent reads during writes
- Daily file rotation requires date-based file discovery

**Limitations:**
- No unified query interface
- Cross-station queries require opening multiple files
- Time-range queries span multiple files
- No indexing beyond timestamp arrays
- Limited filtering capabilities

### 4.2 Common Query Patterns

**Real-Time Monitoring:**
- Latest fusion estimate (last 60 seconds)
- Current system health (all channels)
- Recent quality grades and uncertainty trends

**Historical Analysis:**
- Time series for specific station/frequency (hours to months)
- Cross-station comparison at same frequency
- Quality grade distribution over time
- Propagation mode evolution (diurnal, seasonal)

**Scientific Analysis:**
- TEC validation against IONEX
- Propagation mode vs. frequency/time-of-day
- Station timing consistency (inter-station spread)
- Uncertainty budget component analysis

**Metrology:**
- Traceability chain verification
- Calibration history
- Uncertainty quantification over time
- Quality assurance metrics

### 4.3 Query Requirements for Web UI

**Dashboard (Real-Time):**
- Current UTC offset with uncertainty (1 query)
- System health status (9 channels, 1 query each)
- Recent fusion history (last hour, 1 query)
- Station contribution breakdown (1 query)

**Historical Plots:**
- Time series: station/frequency over date range
- Multi-station comparison: all stations at one frequency
- Propagation mode heatmap: frequency × time-of-day
- TEC evolution: HF vs GPS comparison

**Data Export:**
- CSV download for date range + filters
- Station/frequency/quality grade filtering
- Metadata inclusion (traceability, uncertainty)

---

## 5. ClickHouse Migration Assessment

### 5.1 Suitability Analysis

**Advantages of ClickHouse:**

✅ **Columnar Storage:** Perfect for time-series data with many fields  
✅ **Compression:** 10-100× better than HDF5 (typically 50× for time-series)  
✅ **Query Performance:** Orders of magnitude faster for aggregations  
✅ **SQL Interface:** Standard query language vs. custom HDF5 readers  
✅ **Materialized Views:** Pre-computed aggregations for dashboards  
✅ **Distributed Queries:** Can scale to multiple nodes if needed  
✅ **Time-Series Functions:** Native support for time windows, interpolation  
✅ **Partitioning:** Automatic by date, efficient for time-range queries  

**Challenges:**

⚠️ **Schema Evolution:** HDF5 is self-describing, ClickHouse requires migrations  
⚠️ **Metadata:** Need separate metadata tables or JSON columns  
⚠️ **Uncertainty Budget:** 6+ uncertainty fields per measurement (manageable)  
⚠️ **String Fields:** Station names, quality flags (use Enum or LowCardinality)  
⚠️ **Write Latency:** Batch inserts preferred (60s cadence is fine)  
⚠️ **Backup/Export:** Need strategy for long-term archival  

### 5.2 Recommended Table Schema

**L2 Timing Measurements Table:**

```sql
CREATE TABLE timing_measurements
(
    -- Timestamp (partition key)
    timestamp DateTime64(3, 'UTC'),
    date Date MATERIALIZED toDate(timestamp),
    
    -- Station/Channel
    station LowCardinality(String),
    frequency_mhz Float32,
    channel LowCardinality(String),
    
    -- Core Timing
    clock_offset_ms Float32,
    uncertainty_ms Float32,
    expanded_uncertainty_ms Float32,
    coverage_factor Float32,
    confidence_level Float32,
    
    -- Uncertainty Budget (Type A)
    u_rtp_timestamp_ms Float32,
    u_ionospheric_ms Float32,
    u_multipath_ms Float32,
    
    -- Uncertainty Budget (Type B)
    u_discrimination_ms Float32,
    u_gpsdo_ms Float32,
    u_propagation_model_ms Float32,
    
    -- Quality
    quality_grade LowCardinality(String),
    confidence Float32,
    quality_flag LowCardinality(String),
    
    -- Propagation
    propagation_delay_ms Nullable(Float32),
    propagation_mode LowCardinality(Nullable(String)),
    n_hops Nullable(UInt8),
    delay_spread_ms Nullable(Float32),
    snr_db Nullable(Float32),
    doppler_hz Nullable(Float32),
    
    -- Verification
    utc_verified Bool,
    multi_station_verified Bool,
    gpsdo_locked Bool,
    
    -- Metadata
    discrimination_method LowCardinality(String),
    discrimination_confidence Float32,
    traceability_chain String,
    processing_version LowCardinality(String),
    
    -- Indexes
    INDEX idx_station station TYPE minmax GRANULARITY 4,
    INDEX idx_quality quality_grade TYPE set(0) GRANULARITY 4
)
ENGINE = MergeTree()
PARTITION BY toYYYYMM(date)
ORDER BY (station, frequency_mhz, timestamp)
SETTINGS index_granularity = 8192;
```

**L3B Fusion Timing Table:**

```sql
CREATE TABLE fusion_timing
(
    timestamp DateTime64(3, 'UTC'),
    date Date MATERIALIZED toDate(timestamp),
    
    -- Fused Estimate
    d_clock_fused_ms Float32,
    d_clock_raw_ms Float32,
    uncertainty_ms Float32,
    statistical_uncertainty_ms Float32,
    systematic_uncertainty_ms Float32,
    propagation_uncertainty_ms Float32,
    
    -- Station Contributions
    n_broadcasts UInt16,
    n_stations UInt8,
    stations_used String,
    
    wwv_mean_ms Nullable(Float32),
    wwvh_mean_ms Nullable(Float32),
    chu_mean_ms Nullable(Float32),
    bpm_mean_ms Nullable(Float32),
    
    wwv_count UInt16,
    wwvh_count UInt16,
    chu_count UInt16,
    bpm_count UInt16,
    
    wwv_intra_std_ms Nullable(Float32),
    wwvh_intra_std_ms Nullable(Float32),
    chu_intra_std_ms Nullable(Float32),
    bpm_intra_std_ms Nullable(Float32),
    
    inter_station_spread_ms Nullable(Float32),
    
    -- Quality
    quality_grade LowCardinality(String),
    kalman_state LowCardinality(String),
    consistency_flag LowCardinality(String),
    outliers_rejected UInt16,
    
    -- Global Solver
    global_solve_verified Bool,
    global_solve_consistency_ms Nullable(Float32),
    global_solve_n_obs UInt16,
    
    -- Metadata
    calibration_applied Bool,
    reference_station LowCardinality(String),
    processing_version LowCardinality(String)
)
ENGINE = MergeTree()
PARTITION BY toYYYYMM(date)
ORDER BY timestamp
SETTINGS index_granularity = 8192;
```

**L3C Propagation Statistics Table:**

```sql
CREATE TABLE propagation_stats
(
    timestamp DateTime64(3, 'UTC'),
    date Date MATERIALIZED toDate(timestamp),
    period_start DateTime64(3, 'UTC'),
    aggregation_period LowCardinality(String),
    
    station LowCardinality(String),
    frequency_mhz Float32,
    
    -- Mode Probabilities
    mode_1e_probability Float32,
    mode_1f_probability Float32,
    mode_2f_probability Float32,
    mode_3f_probability Float32,
    mode_gw_probability Float32,
    mode_unknown_probability Float32,
    
    -- MUF
    estimated_muf_mhz Nullable(Float32),
    muf_confidence Nullable(Float32),
    mean_snr_db Nullable(Float32),
    
    -- Quality
    n_observations UInt32,
    data_completeness Float32,
    quality_flag LowCardinality(String),
    
    processing_version LowCardinality(String)
)
ENGINE = MergeTree()
PARTITION BY toYYYYMM(date)
ORDER BY (station, frequency_mhz, timestamp)
SETTINGS index_granularity = 8192;
```

### 5.3 Materialized Views for Dashboards

**Latest Fusion Estimate:**
```sql
CREATE MATERIALIZED VIEW latest_fusion_mv
ENGINE = ReplacingMergeTree()
ORDER BY timestamp
AS SELECT *
FROM fusion_timing
ORDER BY timestamp DESC
LIMIT 1;
```

**Hourly Quality Summary:**
```sql
CREATE MATERIALIZED VIEW hourly_quality_mv
ENGINE = SummingMergeTree()
PARTITION BY toYYYYMM(date)
ORDER BY (station, frequency_mhz, hour)
AS SELECT
    toStartOfHour(timestamp) AS hour,
    station,
    frequency_mhz,
    quality_grade,
    count() AS measurement_count,
    avg(uncertainty_ms) AS avg_uncertainty_ms,
    avg(snr_db) AS avg_snr_db
FROM timing_measurements
GROUP BY hour, station, frequency_mhz, quality_grade;
```

### 5.4 Migration Strategy

**Phase 1: Parallel Operation (2-4 weeks)**
- Keep HDF5 as primary storage
- Write to ClickHouse in parallel
- Validate data consistency
- Test query performance
- Develop web UI against ClickHouse

**Phase 2: ClickHouse Primary (2-4 weeks)**
- Switch web UI to ClickHouse
- Keep HDF5 as backup
- Monitor performance and stability
- Backfill historical data (optional)

**Phase 3: HDF5 Archive (ongoing)**
- HDF5 becomes long-term archive format
- ClickHouse for active queries (last 6-12 months)
- Periodic export from ClickHouse to HDF5 for archival

### 5.5 Performance Estimates

**Query Performance (ClickHouse vs HDF5):**

| Query Type | HDF5 | ClickHouse | Speedup |
|------------|------|------------|---------|
| Latest value | ~10 ms | ~1 ms | 10× |
| Hour time series | ~100 ms | ~5 ms | 20× |
| Day time series | ~1 s | ~20 ms | 50× |
| Month aggregation | ~30 s | ~100 ms | 300× |
| Cross-station query | ~5 s | ~50 ms | 100× |

**Storage Efficiency:**

| Format | Size/Day | Compression | Notes |
|--------|----------|-------------|-------|
| HDF5 (current) | ~10 MB | ~3× | gzip level 4 |
| ClickHouse | ~200 KB | ~50× | LZ4 + columnar |
| **Savings** | **98%** | - | Typical for time-series |

**Write Performance:**
- HDF5: ~1 ms per measurement (SWMR overhead)
- ClickHouse: Batch 60 measurements every 60s (~1 ms total)

---

## 6. Data Exposure Strategy

### 6.1 API Design

**RESTful API Endpoints:**

```
GET  /api/v1/fusion/latest
     → Latest fusion estimate with uncertainty

GET  /api/v1/fusion/history?start={iso8601}&end={iso8601}
     → Time series of fusion estimates

GET  /api/v1/timing/measurements?station={}&frequency={}&start={}&end={}
     → Timing measurements with filters

GET  /api/v1/timing/stations
     → List of available stations with metadata

GET  /api/v1/timing/quality?start={}&end={}
     → Quality metrics summary

GET  /api/v1/propagation/stats?station={}&frequency={}&start={}&end={}
     → Propagation mode statistics

GET  /api/v1/tec/estimates?station={}&start={}&end={}
     → TEC estimates with GPS comparison

GET  /api/v1/health/system
     → System health status (all channels)

GET  /api/v1/metadata/stations
     → Station metadata (locations, frequencies, schedules)

POST /api/v1/export/csv
     → Export data to CSV with filters
```

**WebSocket for Real-Time:**
```
WS   /api/v1/stream/fusion
     → Real-time fusion estimates (60s updates)

WS   /api/v1/stream/health
     → Real-time system health updates
```

### 6.2 Web UI Components

**Dashboard (Real-Time Monitoring):**
- UTC offset gauge with uncertainty bars
- System health matrix (9 channels × status)
- Recent fusion history (last 6 hours)
- Station contribution pie chart
- Quality grade distribution

**Historical Analysis:**
- Time series plotter (multi-station, multi-frequency)
- Propagation mode heatmap (frequency × time-of-day)
- TEC comparison (HF vs GPS)
- Uncertainty budget breakdown
- Quality metrics trends

**Station Explorer:**
- Station metadata cards (location, frequencies, power)
- Broadcast schedule visualization
- Propagation delay estimates
- Signal strength maps

**Data Export:**
- Date range selector
- Station/frequency/quality filters
- Format selection (CSV, JSON, HDF5)
- Metadata inclusion options

### 6.3 External Access

**Scientific Community:**
- Public API with rate limiting
- Data citation guidelines
- DOI assignment for datasets
- CEDAR Madrigal compatibility

**Metrology Labs:**
- Authenticated API access
- Traceability documentation
- Uncertainty budget details
- Calibration certificates

**Amateur Radio:**
- Propagation forecasts
- MUF estimates
- Real-time band conditions
- Historical propagation statistics

---

## 7. Distributed Network Architecture

### 7.1 Architecture Overview

**Two-Tier Design:**

```
┌─────────────────────────────────────────────────────────────┐
│                    CENTRAL REPOSITORY                        │
│                                                              │
│  ┌────────────────────────────────────────────────────┐    │
│  │           ClickHouse Cluster                       │    │
│  │  - Network-wide timing data                        │    │
│  │  - Cross-station analysis                          │    │
│  │  - Propagation statistics                          │    │
│  │  - Public API & Web Portal                         │    │
│  └────────────────────────────────────────────────────┘    │
│                          ▲                                   │
│                          │ HTTP/gRPC sync                    │
│                          │ (hourly/daily)                    │
└──────────────────────────┼───────────────────────────────────┘
                           │
        ┌──────────────────┼──────────────────┐
        │                  │                  │
┌───────▼────────┐  ┌──────▼───────┐  ┌──────▼───────┐
│  Station AC0G  │  │ Station XYZ  │  │ Station ABC  │
│                │  │              │  │              │
│  HDF5 Storage  │  │ HDF5 Storage │  │ HDF5 Storage │
│  FastAPI UI    │  │ FastAPI UI   │  │ FastAPI UI   │
│  Local Monitor │  │ Local Monitor│  │ Local Monitor│
└────────────────┘  └──────────────┘  └──────────────┘
```

### 7.2 Local Station Design (Current System)

**Storage: HDF5 (Keep as-is)**
- ✅ Efficient for single-station queries
- ✅ SWMR mode enables concurrent reads
- ✅ Self-describing with embedded metadata
- ✅ No additional database infrastructure needed
- ✅ Proven reliability for 24/7 operation

**Web UI: FastAPI + HDF5 Reader**
- Direct HDF5 access via `h5py` and existing `DataProductReader`
- RESTful API for local monitoring
- Real-time WebSocket updates
- Minimal resource overhead

**Performance (Local Station):**
- Latest fusion: ~1-5 ms (read last record)
- Hour time series: ~50-100 ms (single file, 60 records)
- Day time series: ~100-500 ms (single file, 1440 records)
- **Perfectly adequate for local monitoring**

### 7.3 Central Repository Design (Network-wide)

**Storage: ClickHouse**
- Aggregates data from all stations
- Enables cross-station queries (e.g., "all 10 MHz measurements")
- Network-wide propagation statistics
- Scientific analysis across geographic distribution
- Public data portal

**Data Synchronization:**
- Stations push data to central repository (hourly or daily)
- Incremental sync (only new measurements)
- Retry logic for network interruptions
- Metadata includes station ID and location

**Query Capabilities:**
- "Show timing from all stations at 10 MHz for last week"
- "Compare propagation modes across geographic locations"
- "Network-wide TEC validation against IONEX"
- "Station performance ranking by uncertainty"

### 7.4 Recommended Implementation

**Phase 1: Local Station (Immediate - 1-2 weeks)**

1. **FastAPI Web UI for Local Monitoring**
   - Implement RESTful API using existing HDF5 readers
   - Real-time dashboard (fusion, health, quality)
   - Historical plots (time series, propagation modes)
   - Data export (CSV, JSON)
   - **No ClickHouse needed at station level**

2. **API Endpoints for Local Station:**
   ```python
   GET  /api/v1/fusion/latest              # Last fusion estimate
   GET  /api/v1/fusion/history             # Time series
   GET  /api/v1/timing/measurements        # L2 data
   GET  /api/v1/health/system              # System status
   GET  /api/v1/propagation/stats          # Propagation modes
   WS   /ws/fusion                         # Real-time updates
   ```

3. **HDF5 Query Optimization:**
   - Cache file handles for current day
   - Index timestamp arrays for binary search
   - Lazy loading for large time ranges
   - Compression-aware chunking

**Phase 2: Central Repository (2-3 months)**

4. **ClickHouse Deployment**
   - Deploy at central location (not at individual stations)
   - Create tables for network-wide data
   - Set up replication for reliability

5. **Station-to-Central Sync Service**
   - Runs at each station
   - Reads HDF5 files and pushes to ClickHouse
   - Configurable sync interval (hourly/daily)
   - Handles network interruptions gracefully

6. **Network-wide API:**
   ```python
   GET  /api/v1/network/stations           # List all stations
   GET  /api/v1/network/timing             # Cross-station queries
   GET  /api/v1/network/propagation        # Network-wide stats
   GET  /api/v1/network/map                # Geographic visualization
   ```

**Phase 3: Public Portal (3-6 months)**

7. **Public Data Access**
   - Web portal for scientific community
   - Interactive maps showing station network
   - Data download with DOI assignment
   - API documentation and examples

## 8. Revised Recommendations

### 8.1 Immediate Actions (1-2 weeks)

1. **Develop FastAPI Web UI (Local Station)**
   - Use existing `DataProductReader` for HDF5 access
   - Implement core endpoints (fusion, timing, health)
   - WebSocket for real-time updates
   - Simple dashboard with plots

2. **Optimize HDF5 Access Patterns**
   - Implement file handle caching
   - Add timestamp indexing for faster queries
   - Profile common query patterns

3. **Document Local API**
   - OpenAPI/Swagger specification
   - Example queries for common use cases
   - Authentication strategy (if needed)

### 8.2 Short-Term (1-2 months)

4. **Station Sync Service**
   - Implement data export to central repository
   - Incremental sync (only new measurements)
   - Configurable sync interval
   - Network resilience (retry logic, queuing)

5. **Station Metadata Registration**
   - Register station with central repository
   - Include location, equipment, calibration info
   - Version tracking for station configuration

6. **Local Data Retention Policy**
   - Keep recent data in HDF5 (30-90 days)
   - Archive older data or rely on central repository
   - Configurable retention based on disk space

### 8.3 Long-Term (3-6 months)

7. **Central Repository Deployment**
   - ClickHouse cluster for network-wide data
   - Ingest API for station data uploads
   - Network-wide query interface

8. **Public Data Portal**
   - Web portal showing all stations
   - Geographic map of network
   - Cross-station analysis tools
   - Data download with DOI assignment

9. **Advanced Network Analytics**
   - Propagation forecasting across network
   - Station performance comparison
   - Network health monitoring
   - Automated quality assurance

### 8.4 Data Model Improvements

**Station Metadata (Central Repository):**
- Unified station registry with location, equipment, calibration
- Version control for station configuration changes
- Link measurements to station metadata via station_id

**Network-wide Traceability:**
- Track data lineage from station to central repository
- Calibration history across all stations
- Uncertainty budget aggregation for network estimates

**Quality Assurance:**
- Automated quality checks at both station and central levels
- Cross-station validation (detect outliers, calibration drift)
- Network health monitoring and alerting

---

## 9. Conclusion

The HF Time Standard system has a well-designed, hierarchical data model with comprehensive schemas and uncertainty quantification suitable for a distributed station network.

**Architecture Decision:**

**Local Stations:**
- ✅ **HDF5 storage is optimal** - efficient, reliable, self-describing
- ✅ **FastAPI web UI** - sufficient for local monitoring
- ✅ **No local ClickHouse needed** - unnecessary complexity
- Performance: 1-500ms for typical queries (perfectly adequate)

**Central Repository:**
- ✅ **ClickHouse essential** - enables network-wide analysis
- ✅ **Scales to many stations** - handles cross-station queries efficiently
- ✅ **Public API foundation** - supports scientific community access
- Performance: 50-300× faster than aggregating HDF5 files from multiple stations

**Key Strengths:**
- Rigorous schema definitions with versioning
- ISO GUM-compliant uncertainty budgets
- Self-describing HDF5 metadata
- SWMR mode for concurrent access
- Proven 24/7 reliability

**Recommended Approach:**
1. **Immediate:** FastAPI web UI for local station (HDF5 access)
2. **Short-term:** Station sync service to push data to central repository
3. **Long-term:** Central ClickHouse deployment for network-wide analysis

**Timeline:**
- Phase 1 (Local UI): 1-2 weeks
- Phase 2 (Sync Service): 1-2 months
- Phase 3 (Central Repository): 3-6 months

This two-tier architecture optimizes for both local station simplicity and network-wide scalability, supporting the three primary objectives: real-time monitoring, metrological traceability, and ionospheric science analysis across a distributed network.

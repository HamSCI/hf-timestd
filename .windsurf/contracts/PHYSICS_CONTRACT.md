# PHYSICS CONTRACT — hf-timestd

**Version:** 1.0.0
**Last Updated:** 2026-02-23
**Status:** Active — evolves with implementation

---

## 1. Goal

Ensure the physics pipeline (Phase 3) produces **scientifically valid ionospheric products** from L2 timing measurements, with honest characterization of what works, what is noise-dominated, and what is aspirational.

### Performance Objectives

- **Carrier-phase dTEC**: ≥250K records/day; ~6 mTECU/min sensitivity; primary science product
- **Differential dTEC RMS**: <0.03 TECU across all multi-frequency station pairs (verified 2026-02-20)
- **Propagation model predictions**: adaptive uncertainty from ±1.5 ms (WAM-IPE+GIRO) to ±15 ms (no model)
- **Multi-mode arrivals**: 1F, 2F, 3F, 1E evaluated per (station, frequency) with MUF and geometry checks
- **IONEX output**: written per minute from slant-to-vertical TEC mapping
- **Reanalysis**: hourly physics-constrained mode validation eliminates phantom F-layer modes at night

### Deliverable Products

| Product | Status | Records/Day | Notes |
|---------|--------|-------------|-------|
| Carrier-phase dTEC rate (`dtec_rate_tecu_per_s`) | ✅ Operational | ~250K | Primary science product |
| Per-tick dTEC time series | ✅ Operational | ~55/min/station | Full 1-second resolution |
| Differential carrier-phase TEC | ✅ Operational | Per minute | Multi-freq pairs, all GOOD quality |
| All-arrivals (multipath) | ✅ Operational | ~374/min (CHU 7.85) | Includes secondary arrivals |
| Integrated dTEC (`dtec_mean_tecu`) | ⚠️ Unanchored | Per minute | Relative only (is_anchored always False) |
| Group-delay TEC (`tec_tecu`) | ❌ Below noise floor | ~11.7K | 71% confidence < 0.5; model-limited |
| VTEC (`vtec_tecu`) | ❌ All NaN | 0 | Depends on group-delay TEC |
| `tof_kalman_ms` | ❌ Deprecated | 0 | Dead schema field, all NaN |

### Verification Steps

1. `dtec_rate_tecu_per_s` records accumulate at ~250K/day — check `phase2/science/dtec/` file sizes
2. Differential dTEC RMS < 0.03 TECU for widest frequency pairs (CHU 3.33–14.67, WWV 2.50–25.00)
3. `HFPropagationModel.predict()` returns non-zero delay and uncertainty for all 17 broadcasts
4. `IonoDataService` background thread running (check logs for "IonoDataService" or iono cache files)
5. Physics service `_processed_minutes` set prevents re-processing (no duplicate records)
6. Reanalysis logs show MUF corrections at night (real-time MUF > reanalyzed MUF)

---

## 2. Constraints

### Propagation Model Hierarchy

The `HFPropagationModel` is the **sole propagation model** throughout the pipeline (v6.7.1+). The deprecated `PhysicsPropagationModel` in `physics_propagation.py` must not be used by any new code.

| Tier | Source | Uncertainty (3σ) | Confidence |
|------|--------|-------------------|------------|
| 0 | WAM-IPE + GIRO | ±1.5 ms | 0.8 |
| 0.5 | WAM-IPE alone | ±3.0 ms | 0.6 |
| 1 | IONEX (IGS global maps) | ±3.0 ms | 0.6 |
| 2 | IRI-2020 climatology | ±4.5 ms | 0.5 |
| 3 | Parametric fallback | ±9.0 ms | 0.2 |
| — | No model | ±15.0 ms | 0.0 |

Final window blends model uncertainty with tracked observational variance, floored at ±5 ms (3σ).

### Ionospheric Group Delay Physics

```
Δτ = 40.3 × sTEC / (c × f²)
```

- At 10 MHz, 20 TECU → 0.27 ms excess delay
- At 5 MHz, 20 TECU → 1.07 ms (4× larger — 1/f² scaling)
- The dispersion signal is **sub-millisecond** while propagation model noise is **3–37 ms** — group-delay TEC is below the noise floor

### Carrier-Phase dTEC (The Viable Path)

```
dTEC/dt = −f_D × c × f / 40.3
```

- Bypasses the propagation model noise floor entirely
- Phase noise ~1 mrad/tick at 20 dB SNR → ~0.1 mTECU/s sensitivity
- 55 ticks/min reduces to ~6 mTECU integrated over one minute
- `np.unwrap()` assumes |Δφ| < π between consecutive ticks — fails if Doppler > 0.5 Hz

### Known Systematic Offsets (F2 in CRITIC_CONTEXT)

| Station | clock_offset_ms | Cause |
|---------|----------------|-------|
| CHU (all 3 freq) | −76 ms | H3E sideband filter group delay (74 ms) — **resolved** |
| WWV | +3 ms | Propagation model error |
| WWVH | +22 ms | Propagation model error |
| BPM | +38 ms | Propagation model error |

These are **not ionospheric** — they are L1 propagation model systematic errors that contaminate the TEC 1/f² fit.

### Multi-Mode Arrival Support

- `arrivals[(station, freq)]` — primary (lowest-delay feasible mode), backward-compatible
- `multi_mode_arrivals[(station, freq, mode)]` — all feasible modes with independent search windows
- `get_all_mode_arrivals(station, freq)` — returns all modes sorted by delay
- Each mode checked for: geometric feasibility, MUF constraint, minimum elevation (>3°)

### Reanalysis Constraints

- Runs hourly at `:05` past the hour, `nice 19`, `IOSchedulingClass=idle`
- Uses solar zenith angle → Chapman foF2 → oblique MUF → mode validation
- Modes above oblique MUF are **physically impossible** — hard reject, not soft penalty
- SNR gate: measurements below 12 dB are likely noise
- Negative TEC slope forced to zero (unphysical)

### Dependencies

- `IonoDataService` (background thread within metrology, not separate service)
- WAM-IPE data from NOAA S3 (`noaa-nws-wam-ipe-pds`) — currently unavailable; IRI-2020 fallback active
- GIRO ionosonde data (DIDBase API)
- `netCDF4`, `boto3` (optional deps for WAM-IPE)
- Receiver coordinates from `timestd-config.toml` `[station]` section

---

## 3. Format

### dTEC HDF5 Record

Schema: `l3_dtec_v1.json`

| Field | Type | Units | Description |
|-------|------|-------|-------------|
| `dtec_rate_tecu_per_s` | float64 | TECU/s | Mean dTEC/dt over the minute |
| `dtec_mean_tecu` | float64 | TECU | Integrated dTEC (relative if unanchored) |
| `is_anchored` | bool | — | True if anchored to group-delay TEC (always False currently) |
| `anchor_status` | string | — | `ANCHORED`, `ANCHOR_LOW_CONF`, or `NO_ANCHOR` |
| `unwrap_quality` | float64 | 0–1 | Phase unwrapping quality metric |
| `n_phase_jumps` | int | — | Number of detected phase discontinuities |

### Propagation Model Output

```python
HFPropagationModel.predict(station, frequency, utc_time) → {
    'delay_ms': float,           # Total propagation delay
    'uncertainty_3sigma_ms': float,
    'mode': str,                 # '1F', '2F', '3F', '1E'
    'confidence': float,         # 0.0–1.0
    'data_source': str,          # 'WAM-IPE+GIRO', 'IRI-2020', 'parametric', etc.
    'geometric_delay_ms': float,
    'iono_delay_ms': float,
    'elevation_angle_deg': float,
}
```

### Documentation Honesty

- `docs/PHYSICS.md` and `docs/HAMSCI_2026_WORKSHOP_ABSTRACT.md` must accurately reflect what works vs what is noise-dominated
- Status markers: ✅ (operational and validated), ⚠️ (partial/caveated), ❌ (not working/below noise floor)
- Detection limit analysis must be included: noise floor, signal at 40 TECU, SNR, verdict

### Logging

- TEC estimator: log confidence, n_frequencies, R² at INFO level
- dTEC: log `unwrap_quality < 0.8` events at WARNING
- Propagation model: log data source tier and mode selection at DEBUG
- Reanalysis: log hourly summary with MUF corrections at INFO
- Differential dTEC RMS: log at INFO with quality assessment (GOOD/MARGINAL/BAD)

---

## 4. Failure Conditions

- **Claiming group-delay TEC is operational** — it is below the noise floor (SNR 0.01–0.14); documentation must say ❌ or ⚠️
- **Using `tof_kalman_ms` in any calculation** — deprecated, all NaN
- **Using `PhysicsPropagationModel`** (deprecated) instead of `HFPropagationModel` in new code
- **Removing or weakening the reanalysis MUF constraint** — it is a hard physical constraint, not a statistical prior
- **Treating `dtec_mean_tecu` as absolute TEC** when `is_anchored=False` — it is relative only
- **Phase unwrapping across Doppler > 0.5 Hz** without detection/flagging — `np.unwrap()` silently fails
- **Mixing propagation conditions in TEC fit window** — the 5-minute aggregation window must not span mode transitions
- **Hardcoding receiver coordinates** — must come from config toml `[station]` section
- **Hardcoding elevation angle at 30°** in VTEC mapping — must use geometric elevation per path (WWV ~19°, WWVH ~7°)
- **Ignoring the CHU 74 ms systematic** — now corrected in pipeline, but any new CHU timing code must account for H3E filter delay
- **Writing duplicate records** — physics service must maintain `_processed_minutes` set to prevent re-processing
- **Full table scan of large HDF5 files** — `DataProductReader.read_time_range()` loads entire dataset; use direct tail reads for real-time consumers
- **Overstating capabilities in HamSCI abstract** — public claims must be validated against live system data

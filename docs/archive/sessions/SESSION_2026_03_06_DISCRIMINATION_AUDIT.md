# Session 2026-03-06: wwvh_discrimination.py Dead Code Audit

## Context

The user asked whether the "weighted decision" voting algorithm in `wwvh_discrimination.py`
is still necessary, given that the current architecture measures each station in parallel
using station-specific matched-filter templates (TickEdgeDetector). The answer: **the voting
logic is dead code**. The parallel measurement architecture superseded it.

## Architecture: How Station Separation Actually Works

On shared channels (2.5/5/10/15 MHz), `metrology_engine.py` lines 1193–1198 sets up
three independent measurement pipelines:

```python
station_tone_freqs = [
    ('WWV', 1000),    # 5ms tick, 800ms tone template
    ('WWVH', 1200),   # 5ms tick, 800ms tone template  
    ('BPM', 1000),    # 10ms tick, 300ms marker template
]
```

Each pipeline runs the TickEdgeDetector with its own template against the same IQ buffer.
Each produces independent D_clock, Doppler, SNR, and confidence. The physical separability:

- **WWV vs WWVH:** Different tick frequencies (1000 vs 1200 Hz) → different templates
- **WWV vs BPM:** Same 1000 Hz but different tick duration (5 vs 10 ms) and arrival time (~35 ms gap)
- **Calibration:** 14 NIST ground-truth minutes/hr refine per-station delay models via `TimingDiscriminator`

No voting or decision algorithm is needed.

## What's Still Called at Runtime

Only **3 services** from `WWVHDiscriminator` are called in production:

### 1. `detect_bcd_discrimination()` (line 2979)
- **Called from:** `metrology_engine.py:1581`
- **Purpose:** BCD (100 Hz amplitude modulation) cross-correlation for WWV vs WWVH amplitude extraction
- **Calls into:** `bcd_correlation_discrimination()` (line 2315) → `_generate_bcd_template()` (line 3051)
- **Dependencies:** `WWVBCDEncoder`, `WWVGeographicPredictor`
- **Status:** Active. Provides BCD amplitude metrics for channel characterization.

### 2. `estimate_doppler_shift_from_ticks()` (line 2069)
- **Called from:** `metrology_engine.py:1590`
- **Purpose:** Legacy per-tick phase → Doppler estimation
- **Calls into:** `extract_per_tick_phases()` (line 1891)
- **Status:** Active but **redundant**. The TickEdgeDetector now extracts Doppler directly
  from carrier phase slope (IQ mixed at tick freq, unwrapped, linear fit). This legacy
  method uses AM-envelope windowed phase extraction — a less accurate approach.
  **Candidate for removal** once confirmed that no downstream consumer depends on it
  separately from the TickEdgeDetector Doppler.

### 3. `test_signal_detector` (sub-object: `WWVTestSignalDetector`)
- **Called from:** `metrology_service.py:863` via `self.engine.discriminator.test_signal_detector.detect()`
- **Purpose:** Minutes 8/44 test signal detection (multi-tone + chirp)
- **Lives in:** `wwv_test_signal.py` (separate module), instantiated in `WWVHDiscriminator.__init__()` line 412
- **Status:** Active. Could be instantiated directly instead of through `WWVHDiscriminator`.

## Dead Code: Never Called at Runtime

### Voting/Decision Pipeline (the core "discrimination" logic)
| Method | Lines | Purpose | Status |
|--------|-------|---------|--------|
| `compute_discrimination()` | 482–681 | First-stage power ratio discrimination | **Dead** |
| `finalize_discrimination()` | 780–1287 | 12-vote weighted voting algorithm | **Dead** |
| `_cross_validate_methods()` | 3642–3881 | Inter-method agreement/disagreement | **Dead** |
| `analyze_minute_with_440hz()` | 3332–3640 | Full-minute analysis with 440 Hz | **Dead** |

### Signal Detection Methods (superseded by TickEdgeDetector)
| Method | Lines | Purpose | Status |
|--------|-------|---------|--------|
| `detect_timing_tones()` | 683–778 | 1000/1200 Hz tone detection | **Dead** — superseded by TickEdgeDetector |
| `detect_440hz_tone()` | 1289–1395 | 440 Hz tone in minutes 1/2 | **Dead** |
| `detect_500_600hz_tone()` | 1397–1546 | 500/600 Hz ground truth tones | **Dead** |
| `detect_tick_windows()` | 1548–1889 | 10-second windowed tick analysis | **Dead** — superseded by TickEdgeDetector |
| `extract_per_tick_phases()` | 1891–2067 | Per-tick IQ phase extraction | **Called** only by `estimate_doppler_shift_from_ticks()` |
| `measure_tone_powers_fft()` | 441–480 | FFT-based 1000/1200 Hz power | **Dead** |
| `estimate_doppler_shift()` | 2229–2313 | Older tick-results-based Doppler | **Dead** (different from `estimate_doppler_shift_from_ticks`) |
| `bcd_correlation_with_doppler_compensation()` | 3087–3330 | Doppler-compensated BCD | **Dead** |

### Data Structures
| Item | Status |
|------|--------|
| `DiscriminationResult` dataclass | **Partially dead** — defined here, referenced by `timing_calibrator.py` in dead method `verify_with_discrimination_result()` |
| `DiscriminationCSVWriters` | **Dead** — exported from `__init__.py` but never imported |

### Statistics/History
| Method | Lines | Status |
|--------|-------|--------|
| `get_recent_measurements()` | 3883–3885 | **Dead** |
| `get_statistics()` | 3887–3918 | **Dead** |

## Domain Knowledge That Must Be Preserved

All critical broadcast knowledge is already factored into `wwv_constants.py`, which is the
canonical source of truth (imported by 20+ modules):

- `TONE_SCHEDULE_500_600` — per-minute tone assignments
- `WWV_ONLY_TONE_MINUTES` / `WWVH_ONLY_TONE_MINUTES` — ground truth minutes
- Station coordinates (lat/lon for all 4 stations)
- `BPM_UT1_MINUTES` — BPM 100ms pulse minutes
- Physical constants, propagation bounds, frequency lists

The `timing_discrimination.py:GroundTruthSchedule` class also encodes the per-minute
station exclusivity schedule independently.

Additional knowledge embedded in `wwvh_discrimination.py` header comments (lines 1–203)
that is **not** in `wwv_constants.py`:
- BCD phase relationship (WWV = leading edge, WWVH = lagging edge)
- Voice announcement gender (WWV = male, WWVH = female)
- Cross-validation heuristics (FSS geographic matching, transient noise detection)
- Weight tuning rationale for minute-specific adjustments

These are documentation-only items that could be preserved in comments or a reference doc.

## Sub-objects Instantiated by WWVHDiscriminator.__init__()

| Object | Used at runtime? | Could be standalone? |
|--------|-----------------|---------------------|
| `WWVBCDEncoder` | Yes (via `detect_bcd_discrimination`) | Yes |
| `WWVTestSignalDetector` | Yes (via `metrology_service.py`) | Yes — already a separate module |
| `WWVGeographicPredictor` | Yes (via BCD discrimination) | Yes — already a separate module |
| `TimingDiscriminator` | Not via this class | Already standalone |

## Recommended Actions

### Safe to do now
1. **Mark dead methods** with `# DEPRECATED: not called at runtime` comments
2. **Move `test_signal_detector` instantiation** to `MetrologyEngine.__init__()` directly
   (import `WWVTestSignalDetector` from `wwv_test_signal.py`)

### Requires validation
3. **Confirm `estimate_doppler_shift_from_ticks()` is redundant** with TickEdgeDetector's
   `doppler_hz` output. If so, the last reason to call `WWVHDiscriminator` from
   `metrology_engine.py` would be `detect_bcd_discrimination()` alone.
4. **Refactor `detect_bcd_discrimination()`** into its own module (or into the BCD encoder)
   so it doesn't require instantiating the full `WWVHDiscriminator`.

### Long-term
5. **Archive `wwvh_discrimination.py`** — move to `core/legacy/` or similar. Preserve the
   header documentation (lines 1–203) as a reference for the multi-method discrimination
   rationale and broadcast schedule details.

## Presentation Updates (same session)

Updated `HAMSCI_2026_PRESENTATION.md`:
- **Deep-dive section:** Replaced "8-method weighted voting system" with "How we actually
  measure each station (parallel direct measurement)" — preserves all broadcast knowledge
  and contamination details, but correctly describes the architecture.
- **Slide 7:** Reframed as "we measure all three in parallel" with pipeline comparison table.
- **Speaker notes:** Updated to reflect direct measurement, not voting.
- **Q&A section:** Rewrote WWV/WWVH and BPM/WWV discrimination answers.
- **Takeaways:** "The system measures, it doesn't decide."

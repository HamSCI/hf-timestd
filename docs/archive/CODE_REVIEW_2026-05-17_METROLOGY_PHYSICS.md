# Code Review — Metrology & Physics Components

**Date:** 2026-05-17
**Scope:** `hf-timestd` metrology pipeline (~17 kLOC) and physics pipeline (~11 kLOC), plus user-facing documentation.
**Method:** Seven parallel expert reviews (signal/timing core; fusion & clock discipline; arrival prediction; TEC chain; propagation/iono models; physics services & science; documentation), each reading the assigned files in full against the `.windsurf` contracts and `METROLOGY_PHYSICS_SPLIT.md`. A sample of high-impact findings was then spot-checked against the source.

**Caveat — read before acting:** Two findings the sub-reviews rated *Critical* were demoted after spot-check (see §1.1). Every finding below cites `file:line`; **confirm each against current source before remediation** — line numbers drift, and a few methodological calls were re-adjudicated here. Severities in this document are the adjudicated values, not the raw sub-review values.

---

## 1. Executive summary

The two pipelines are ambitious and, in many places, genuinely sophisticated — quadrature matched filtering, MAD ensembles, CLEAN deconvolution, carrier-phase dTEC, a tiered ionospheric-model hierarchy, ISO-GUM uncertainty budgets. But the "stuttering, inconsistent-quality" development history shows clearly: the codebase is a layered accretion in which **later design decisions were not propagated back through earlier layers**, contracts have drifted from code in *both* directions, and the same physics (hop geometry, SNR, obliquity, the 40.3 dispersion constant) is reimplemented several times with divergent conventions.

Headline conclusions:

- **The metrology timing chain is correct in its arithmetic but unsound in its premises in two places** — the edge detector correlates a pure-sinusoid template against an AM-demodulated envelope (template/domain mismatch), and three different "SNR" definitions coexist so the contract's "≥10 dB" target is ambiguous.
- **The fusion layer has a genuine clock-latch-class defect** (discontinuity-filter reference variables update on different conditions and desynchronise) and a **dual-Kalman architecture that is not actually independent** — defeating the TSL1/TSL2 cross-check the design relies on.
- **The physics pipeline is still coupled to the real-time metrology critical path** (`timestd-physics.service` is `Type=notify` with `Requires=` on an L2 metrology service) — a direct violation of the `METROLOGY_PHYSICS_SPLIT` mandate.
- **Several advertised capabilities are dead on arrival**: `IonosphericModel.update_calibration_from_ionogram` crashes on first call; `RaytraceEngine` is fully built but never wired into the propagation model; `TIDDetector` writes nothing and the Web API reads a directory nothing populates; `PropagationEngine`'s "IRI tier" is a literal `pass`.
- **Scientific honesty is mostly good but uneven**: `PHYSICS.md` is exemplary; `tec_estimator.py` presents group-delay TEC as an operational product and reports R² as "confidence" (high confidence on noise), contradicting the contract's own honesty rule.
- **Documentation is unusually thorough but internally inconsistent** — five different version strings, contradictory BPM and HDF5-concurrency descriptions, and one badly stale document (`SCIENTIFIC_CAPABILITIES.md`) that *understates* what the instrument now does.

Counts (adjudicated): **9 Critical, 41 High, ~55 Medium, ~35 Low.** The Critical/High set is concentrated in `multi_broadcast_fusion.py`, `tick_*` detectors, `tid_detector.py`, `ionospheric_model.py`, and the physics service wiring.

### 1.1 Severity corrections vs the raw sub-reviews

| Claim | Raw rating | Adjudicated | Reason |
|---|---|---|---|
| `tick_edge_detector.py` front-edge "incoherent" `+half_template`/`−half_template` | Critical | **Low** | The two terms cancel; the net result `front_edge = region_start + peak_idx + sub_offset` is correct and the comment is now self-consistent. Real issue is redundancy/fragility only. |
| `propagation_model.py` MUF "formula inverted" | Critical | **Medium** | `foF2/sin(elev)` *is* the standard flat-Earth secant law: `sec(90°−elev) = 1/sin(elev)`. Not inverted. The real issue is the flat-Earth approximation (curvature ignored), which is a Medium accuracy item. |

All other Critical/High findings survived spot-check (chrony SHM struct size, `_climatological_fallback` static-method bug, `tec_estimator` R²-confidence, `carrier_tec` unwrap check on raw phase, `TickMatchedFilter` still instantiated, fusion uncertainty clobber).

---

## 2. Cross-cutting systemic issues

These are root causes; fixing them resolves many individual findings at once.

**S1 — Contract drift, both directions.** `.windsurf/contracts/*` (dated 2026-02-23) no longer match the code. Code is *behind* the contract: `arrival_pattern_matrix.validate_detection()` is still a binary gate, not the likelihood weight `METROLOGY_PHYSICS_SPLIT` mandates; `TickMatchedFilter` is still instantiated and run by `metrology_engine` though the contract calls it removed. Code is *ahead* of the contract: GNSS-VTEC anchoring is implemented (`is_anchored` can be `True`) but `PHYSICS_CONTRACT` still says "always False". **The contracts must be refreshed against code, then the code aligned to the refreshed contract** — currently neither is authoritative.

**S2 — Geometry reimplemented inconsistently.** Hop geometry exists in at least four places: spherical (`arrival_pattern_matrix._spherical_hop_path`, `propagation_model._evaluate_mode`) and flat-Earth (`propagation_mode_solver._hop_geometry`, `propagation_engine._estimate_geometric`, `tec_geometry.calculate_elevation_angle`). Great-circle/midpoint helpers exist in ≥4 modules. They produce **different delays and elevations for the same path** — for a 7000 km WWVH path the flat-vs-spherical divergence is several percent (tens of ms). Consolidate onto one spherical-geometry utility module.

**S3 — Uncertainty computed everywhere, propagated nowhere.** The L2 ISO-GUM budget, the per-broadcast Kalman `kalman_uncertainty_ms`, the L3 Kalman covariance, and an RSS budget in `fuse()` are four separate uncertainty estimates. The number finally written to Chrony (`result.uncertainty_ms`) is the least filter-aware of them, and the L2 GUM components are dropped at the L1→L3 join. `np.polyfit(cov=True)` is requested in `tec_estimator` and discarded. The instrument knows its uncertainty far better than it reports it.

**S4 — Three incompatible "SNR" definitions.** `tick_edge_detector` uses peak/median-of-Rayleigh; `tick_matched_filter` uses peak/std-of-Rayleigh; `metrology_engine` uses an FFT power ratio. These differ by 1–5 dB. The contract's "≥10 dB SNR" target and every cross-module SNR comparison are therefore ambiguous. Pick one definition (peak / Rayleigh-σ, σ = median/1.177), document it, retune thresholds.

**S5 — Swallowed exceptions at DEBUG.** `arrival_pattern_matrix`, `tec_estimator`, `propagation_model`, `metrology_service`, `l2_calibration_service`, `physics_service`, `ionospheric_reanalysis` all catch broad `Exception` and log at DEBUG or below. `METROLOGY_CONTRACT §4` explicitly forbids this ("causes invisible data starvation"). It currently hides at least three real bugs (`_extract_scalar` wrong class, `_climatological_fallback` static-method call, `update_calibration_from_ionogram` crash). Narrow the excepts; log unexpected failures at WARNING with `exc_info`.

**S6 — Physics not decoupled from the real-time path.** `METROLOGY_PHYSICS_SPLIT §"Interface"` and action item 4 require physics to be strictly asynchronous/batch. In practice `timestd-physics.service` has `Requires=timestd-l2-calibration.service` + `Type=notify` + `WatchdogSec=120`, and an `ExecStartPre` `chown -R` over the whole `phase2` tree. A physics crash-loop chowns live L2 metrology files and can race SWMR writers.

**S7 — Magic numbers without derivation.** Pervasive: Kalman `Q`/`R` values, grade/mode weights, the 1.15× ionospheric overhead, the 0.5 E-layer delay factor, the 20 ms divergence cap, `u_iono = 0.3·√n`, the `1.4826` MAD factor used inconsistently, period bands, `min_correlation = 0.6`. The contracts demand documentation that "exceeds pythonic completeness with references". Most of these have a plausible comment but no traceable source.

**S8 — Two pipelines, one set of stale station coordinates.** `arrival_pattern_matrix` hard-codes `STATION_LOCATIONS`; `propagation_mode_solver` imports from `wwv_constants`; `tec_geometry` and `tec_validator` each hard-code their own — and disagree (`tec_validator` places **BPM in Shanghai**, ~600 km from its true Pucheng site). `wwv_constants.py` is documented as the single source of truth; make it so.

---

## 3. Metrology — findings

### 3.1 Critical

**M-C1 — Dual Kalman TSL1/TSL2 is not independent.** `multi_broadcast_fusion.py:4028, 5201, 5271-5276, 5457`. `fuse()` is called twice per cycle (`force_l1_only` True then False), but the per-broadcast Kalmans (`_apply_broadcast_kalmans`, line 3382) and the convergence/drift-window state are **shared** and mutated by both calls. The L2 call sees filters that already absorbed the minute's data on the L1 call. The two SHM feeds are statistically coupled; chrony combining them underestimates error. The contract lists "Shared Kalman state between TSL1 and TSL2" as a failure condition. *Fix:* run the per-broadcast Kalmans once per cycle, or hold two independent filter sets keyed by feed; make convergence state per-feed.

**M-C2 — Discontinuity-filter reference variables desynchronise → clock-latch class bug.** `multi_broadcast_fusion.py:5402-5409, 5496-5515`. `last_chrony_d_clock` is advanced even on rejected updates (good, per contract) but `last_chrony_update_time` is set **only** on the success path. The 5-minute recovery reset keys off `last_chrony_update_time`. Under sustained rejection the timer freezes, every cycle then logs "Resetting discontinuity check" and nulls `last_chrony_d_clock`; under brief rejection after a recent success the recovery never fires. The two variables update on different conditions. *Fix:* advance `last_chrony_update_time` together with `last_chrony_d_clock` every cycle a result exists, or drop the time-based reset entirely.

**M-C3 — `ChronySHM.update` can leave the sequence count permanently odd.** `chrony_shm.py:284-389`. The NTP SHM protocol writes count-odd, body, count-even. If an exception fires between the two writes (line 359 area), `self.count` and the segment are left odd; chronyd then ignores the refclock indefinitely. The `except` only logs. *Fix:* in the `except` path force the count even and rewrite bytes 4–7; consider `msync` ordering for the mmap fallback.

### 3.2 High

**M-H1 — Edge-detector template/domain mismatch.** `tick_edge_detector.py:317-348`, fed from `metrology_engine.py:1207`. The matched-filter templates are pure sinusoids `sin/cos(2πft)`, but the input `audio_signal` is `|IQ| − mean` — an AM-demodulated *envelope*. The correlation peak position then depends on modulation depth / envelope shape (demonstrated 0–2 ms shift). Sibling module `tick_matched_filter._build_am_templates` recognises this and builds an envelope template — the two modules disagree on the correct template. *Fix:* decide the domain deliberately — either matched-filter raw IQ with a complex `exp(j2πft)` template, or use an envelope-shaped template on the AM-demod input; validate peak-to-onset by simulation against the exact `|IQ|−mean` signal.

**M-H2 — `TickMatchedFilter` instantiated and run though the contract calls it removed.** `metrology_engine.py:45, 245-246, 339-376` (confirmed: four `TickMatchedFilter` instances built and `process_minute` called every minute). `METROLOGY_CONTRACT §4` lists "Using any module other than `TickEdgeDetector` to write `tick_timing`" as a failure condition and says the A/B comparison was removed — yet the A/B `comparison_tracker`/`pll_decoders` machinery is fully present. *Fix:* decide authoritatively; if `TickEdgeDetector` is the sole timing source, strip `TickMatchedFilter`'s timing outputs and the A/B path, else fix the contract.

**M-H3 — CHU 74 ms H3E offset: contract and code describe two different physical things.** `tick_edge_detector.py:565-568`, `metrology_engine.py:1306-1331`. The code treats 74 ms as a CHU *transmit-side* onset delay; the contract phrases it as a *receiver* group-delay correction. One interpretation double-counts (148 ms) on every CHU measurement. The constant is also duplicated as a literal in two files and absent from `wwv_constants.py`. *Fix:* reconcile contract wording with the (more credible) `metrology_engine` evidence chain; define `CHU_H3E_GROUP_DELAY_SEC` once in `wwv_constants.py`.

**M-H4 — CLEAN deconvolution PSF is built from the wrong signal.** `tick_edge_detector.py:350-366`. The point-spread function is `correlate(sin, template)` — a raw-sinusoid autocorrelation — but `_clean_deconvolve` subtracts it from the correlation of *bandpass-filtered AM-demod audio*. Wrong PSF shape → residual sidelobes detected as spurious secondary arrivals → inflated `multipath_spread` → wrongly degraded `physics_confidence`. *Fix:* build the PSF by running the exact detection chain on a synthetic single arrival.

**M-H5 — SNR-weighted ensemble inconsistent with its own uncertainty.** `tick_edge_detector.py:741-753`. The central estimate is SNR-weighted (`10^(snr/20)`) but `ensemble_uncertainty_ms` is the *unweighted* MAD ÷ √n. Effective sample size of a weighted estimator is `(Σw)²/Σw² < n`, so the headline `d_clock_uncertainty_ms` is understated — making the fusion Kalman over-trust noisy minutes. *Fix:* unweighted median+MAD, or weighted MAD ÷ √N_eff; document the estimator.

**M-H6 — `TickMatchedFilter.process_minute` assumes the buffer starts on a minute boundary.** `tick_matched_filter.py:1092, 1120-1131`. Window slices use `start_sec * sample_rate` while `_detect_minute_marker` correctly uses `buffer_timing.utc_to_sample`. RTP buffers do not start on minute boundaries, so the tick windows and the marker disagree about where second 0 is. *Fix:* derive every window range from `buffer_timing.utc_to_sample`.

**M-H7 — AM-envelope matched-filter template is a bare window function.** `tick_matched_filter.py:397-411`. `_build_am_templates` computes a tone then discards it: `template = window.copy()`. Correlating a flat rectangle against a rectangular pulse yields a flat-topped correlation plateau — parabolic interpolation on a flat top is meaningless, so the claimed 0.02–1.0 ms uncertainty is unachievable. *Fix:* use the true expected envelope shape, or detect the onset edge; replace the step-function `uncertainty_ms` lookup tables with a Cramér-Rao estimate.

**M-H8 — `TickPLLDecoder` mixes seconds and sample counts in `minute_boundary`.** `tick_pll_decoder.py:732` vs `metrology_engine.py:1771`. `metrology_engine` passes a Unix timestamp; `process_minute` does `minute_boundary * sample_rate`, yielding ~4e13. `DualStationPLL._compile_analysis` instead divides `minute_boundary` *by* `fs` assuming samples. The same name carries two quantities; at least one path is corrupt. *Fix:* make `minute_boundary` unambiguously a UTC timestamp; convert via `buffer_timing`.

**M-H9 — `TickPLLDecoder` is stateful but fed independent 1-second chunks.** `tick_pll_decoder.py:164-212, 724-736`. The PLL flywheel runs one buffer at a time over 60 separate chunks; `next_expected_tick` is an absolute index from a previous (corrupt) `buffer_start`, so `rel_expected` is garbage and lock is never achieved. Combined with M-H8 this module is non-functional as integrated, yet its output is persisted for A/B comparison. *Fix:* feed the full minute as one continuous stream, or remove the module (the contract says the A/B feature is gone).

**M-H10 — PLL reports its own correction as the clock offset.** `tick_pll_decoder.py:300-305, 598-603`. `d_clock_ms` is `mean(phase_error)` where `phase_error = actual − next_expected_tick` — the discrepancy from the PLL's *own prediction*, which converges to ~0 by construction regardless of the true UTC offset. Not referenced to any second boundary. A structurally-zero `d_clock` would look "perfect" in A/B comparison. *Fix:* measure against the UTC-second boundary from `buffer_timing`.

**M-H11 — Per-broadcast Kalman process-noise units inconsistent.** `broadcast_kalman_filter.py:306, 362`. `predict()` does `P = FPFᵀ + Q·dt`; `update()`'s embedded predict does `P = FPFᵀ + Q_adaptive` with no `dt`. Dimensionally inconsistent — silently wrong for any non-unit step. The L3 Kalman uses `q_drift = 1e-8` against a contracted `1e-12` (10⁴×). Adaptive `Q` multipliers (`snr_scale`, unbounded `innovation_scale`, `mode_scale`) compound to 30–100×, letting the filter chase noise at low SNR. *Fix:* make `Q` consistently a rate × `dt`; bound `innovation_scale`; derive and document every `q_*`.

**M-H12 — Fusion weighting double-counts SNR and is not the inverse-variance scheme it claims.** `multi_broadcast_fusion.py:2099, 2118-2272`. `uncertainty_ms` is hard-coded `1.0` for every L1 measurement, so `base_weight = 1/σ² = 1` is constant — the "inverse-variance" term contributes nothing. SNR then enters twice (`snr_scale` and SNR-boosted `confidence`). The genuine per-broadcast `kalman_uncertainty_ms` is computed and never used by `_calculate_weights`. *Fix:* declare `kalman_uncertainty_ms` on the dataclass and use it as `σ_i`; apply quality factors only as a small trust scalar.

**M-H13 — Two cascaded Kalman layers; comments contradict the code.** `multi_broadcast_fusion.py:3382, 4039, 4234-4243`. Per-broadcast Kalmans smooth, then `_kalman_update` smooths again, then a comment block declares "v6.0: WLS Fusion (No L3 Kalman) … NO temporal smoothing at this layer" — 200 lines after the L3 Kalman was called. Cascading filters violates the second filter's white-innovation assumption (optimistic covariance). *Fix:* choose one filtering layer; make the comments true.

**M-H14 — GNSS-VTEC TEC correction applied in RTP mode.** `multi_broadcast_fusion.py:3619-3707`. The HF-TEC block correctly gates on `if not self.is_rtp_authority`; the GNSS-VTEC block above it mutates `m.d_clock_ms` with **no RTP check**, injecting ionospheric model error into a D_clock the GPS+PPS reference already pinned to ~50 µs. Directly violates `METROLOGY_PHYSICS_SPLIT`. *Fix:* wrap the GNSS-VTEC `d_clock_ms` mutation in `if not self.is_rtp_authority`.

**M-H15 — Fusion uncertainty: `uncertainty = measurement_uncertainty` discards holdover/Kalman uncertainty.** `multi_broadcast_fusion.py:4459` (confirmed; comment says it is deliberate — "per-broadcast Kalmans already smoothed"). The holdover branch's `holdover_uncertainty` growth model (4287-4313) is overwritten one block later, so uncertainty sent to Chrony does not grow during dropout. *Fix:* confirm intent; if holdover is a real mode, fold its uncertainty in via RSS. Add a test asserting holdover uncertainty grows with dropout duration.

**M-H16 — Outlier rejection runs twice on inconsistent data.** `multi_broadcast_fusion.py:3403-3456` (MAD on *calibrated* values) vs `3901/2274-2316` (`_reject_outliers` MAD on *raw, uncalibrated* `d_clock_ms`). The code's own comment notes raw values carry 30–60 ms inter-broadcast offsets — exactly what defeats the raw-value MAD. The two passes use 3.5σ vs 3.0σ. *Fix:* a single outlier pass on calibrated residuals.

**M-H17 — `_reject_outliers` mixes a weighted centre with an unweighted scale.** `multi_broadcast_fusion.py:2289-2307`. Weighted median, then unweighted MAD — statistically incoherent; low-weight outliers inflate the MAD and survive. *Fix:* consistently weighted (or consistently unweighted) MAD.

**M-H18 — Divergence cap rejects real large offsets.** `multi_broadcast_fusion.py:2978-2985`. `abs(state[0]) > 20 ms` triggers a filter reset to the latest noisy measurement, `n→1`. In Fusion mode a genuine 30–50 ms starting offset is plausible; the filter then can never converge and the offset is permanently capped at 20 ms. *Fix:* detect divergence from covariance/innovation statistics, not absolute state magnitude; make any cap mode-aware.

**M-H19 — Mode-transition detector uses a pre-prediction "innovation".** `broadcast_kalman_filter.py:341, 361-372`. `detect_mode_transition` and adaptive `Q` are fed `measurement − state[0]` *before* the predict step; the true innovation (post-predict) is computed later and differs by `doppler·dt`. The filter's defences key off the wrong residual. *Fix:* predict first, derive one innovation, feed both.

**M-H20 — Kalman convergence/transition timing lost on restart.** `broadcast_kalman_filter.py:176, 345, 571-573, 680`. `time_since_mode_change` counts updates-as-minutes while `is_converged()` checks wall-clock; `load_state` restores `state/P/n` but not transition timing, so a restarted filter believes its last transition was 10000 s ago and can flip "converged" immediately. *Fix:* one time base (real timestamps); persist transition timing.

**M-H21 — chrony SHM struct packs 92 bytes into a 96-byte segment.** `chrony_shm.py:54, 339-340` (confirmed: `'@ii q i 4x q i iiii II iiiiiiii'` = 92 bytes; `SHM_SIZE = 96`; chrony's `struct shmTime` has `int dummy[10]`, the code has 8 trailing ints). Currently benign (chrony ignores `dummy[]`) but fragile, and the header docstring describes a different, older 56-byte layout. *Fix:* make the format total 96 (`dummy[10]`), derive `SHM_SIZE` from `struct.calcsize`, rewrite the docstring.

**M-H22 — `_seed_last_processed` / `_seed` swallow per-file exceptions silently.** `l2_calibration_service.py:401-402`. A corrupt newest L2 file → bare `except: continue` → `last_ts` stays 0 → a 24-hour reprocessing storm with no log. *Fix:* log at WARNING when a file fails to parse.

**M-H23 — L2 mode selection is near-circular.** `l2_calibration_service.py:596-614`. Each candidate mode's `total_delay_ms + raw_toa_ms` is fed back into `identify_mode`, which then identifies the candidate it was handed; the "best mode" is whichever self-identifies most confidently — not the mode the signal took. `propagation_delay_ms`, `n_hops`, and hence `u_iono ∝ √n_hops` are chosen by tautology. *Fix:* identify the mode once from the measured arrival, or pick the dominant mode by climatological likelihood.

### 3.3 Medium (metrology)

| ID | File:line | Finding | Fix |
|---|---|---|---|
| M-M1 | `tick_edge_detector.py:619-625` | SNR noise floor is median of a Rayleigh envelope (≈1.177σ), not σ — ~1.4 dB low and inconsistent with siblings (see S4) | Standardise SNR definition |
| M-M2 | `tick_edge_detector.py:763-781` | Doppler `polyfit` is unweighted and `np.unwrap`s phases sampled at irregular second indices (cycle-slip risk) | Weight by SNR; use absolute UTC; guard slips |
| M-M3 | `tick_matched_filter.py:608-616` | SNR uses `std` of Rayleigh envelope; 40 dB sentinel when `noise_std==0` lets artefacts pass the 8 dB gate | Standardise SNR; replace sentinel with NaN/flag |
| M-M4 | `buffer_timing.py:40-41` | `GPS_LEAP_SECONDS` captured once at import — a multi-week process crossing a leap second carries a 1 s error | Resolve per-buffer, keyed off buffer GPS time |
| M-M5 | `metrology_engine.py:475-493` | Vacuum-fallback delay = `light_time·1.15` (fabricated 15% overhead); seeds the search window | Use a 1-hop slant-range geometric fallback; import `EARTH_RADIUS_KM` |
| M-M6 | `metrology_engine.py:1156-1157` | `minute_number` recomputed from untrusted `system_time`; drives tone schedule incl. BPM UT1-vs-UTC classification | Derive from `buffer_timing.sample_to_utc(0)` |
| M-M7 | `metrology_engine.py:1492-1519` | Synthetic edge measurement round-trips through sample space and truncates `mid_sec` to a whole second — `arrival_ms` and `timing_error_ms` disagree by up to ±0.5 s | Carry `ensemble_timing_error_ms` straight through |
| M-M8 | `metrology_engine.py:611-679` | `_find_all_correlation_peaks` suppresses peaks within ±`n_template` (800 ms for the marker) — HF multipath (1–10 ms) can never be reported | Suppress only ±mainlobe width |
| M-M9 | `multi_broadcast_fusion.py:2318-2326` vs `338-340` | Calibration key formatted to 1 decimal in one path, 2 in another — CHU fractional MHz can alias/miss | One key formatter everywhere |
| M-M10 | `multi_broadcast_fusion.py:2464, 3917` | `gpsdo_locked` guard reads an attribute `BroadcastMeasurement` never has — dead protection against unlocked-GPSDO data | Propagate `gpsdo_locked` from L2, or remove the dead guard |
| M-M11 | `multi_broadcast_fusion.py:3592-3599` | Leap-second Kalman hold lasts exactly one cycle (cleared as soon as TAI-UTC is seen unchanged) | Hold a fixed 5–10 min window by timestamp |
| M-M12 | `multi_broadcast_fusion.py:4079-4087` | >5 ms D_clock jump is logged but neither rejected nor damped; `last_fused_d_clock` advances anyway | Feed jumps to the Kalman as high-`R`, or hold one cycle |
| M-M13 | `multi_broadcast_fusion.py:2962-2964` vs `4257-4262` | `kalman_converged` set by two unrelated criteria — premature lock tightens the discontinuity filter during restart settling | Single covariance-based convergence definition |
| M-M14 | `broadcast_kalman_filter.py:375` | Short-form `P=(I−KH)P` covariance update; no Joseph form, no symmetrisation, ~10⁶ updates/week | Joseph form or per-cycle symmetrise + PD assert |
| M-M15 | `broadcast_kalman_filter.py:313-337` | No NaN/Inf guard on `measurement_ms`/`snr_db`; NaN poisons `P` before the L3 NaN filter runs | Guard the entrypoint |
| M-M16 | `chrony_shm.py:157-190` | `_connect_sysv` removes + recreates a segment chronyd may be attached to → chrony reads a stale orphan forever | Fix permissions in place or fail loudly |
| M-M17 | `chrony_shm.py:247-389` | A failed `update()` does not clear `.connected`, so the reconnect path never fires | Set `.connected=False` and escalate on repeated failure |
| M-M18 | `metrology_service.py:728-864` | `all_arrivals`/`detection_attempts` written per-record (50+/min/channel) — same heap-corruption risk the contract flags for `tick_phase` | Batch the writes |
| M-M19 | `metrology_service.py:699-864` | HDF5 write failures logged at DEBUG — contract requires WARNING | Log at WARNING (rate-limited) |
| M-M20 | `metrology_service.py:500-525` | `_cleanup_processed_set` horizon uses `time.time()` while minutes are keyed by ring `head_utc` — in Fusion mode the set can grow unbounded or prune live minutes | Use ring time consistently |
| M-M21 | `l2_calibration_service.py:615-628` | Geometric-fallback `propagation_delay_ms` is pure vacuum (ignores ionospheric delay) — a several-ms *bias* modelled as zero-mean uncertainty | Add a climatological iono term, or down-weight not just inflate |
| M-M22 | `l2_calibration_service.py:669-824` | `k=2.0`, `dof=10` hard-coded; for ν=10 the 95% t-value is 2.23 — `confidence_level=0.95` is mislabelled | Compute `k` from effective DOF (Welch-Satterthwaite) |
| M-M23 | `l2_calibration_service.py:779-811` | Every uncertainty component (`u_rtp`, `u_iono=0.3√n`, `u_multipath`, `u_gpsdo`, …) asserted with a comment but no traceable source | Cite the measurement/datasheet/standard per term |
| M-M24 | `arrival_pattern_matrix.py:1103-1106` | `int()` truncation in sample conversions biases every predicted arrival ~20 µs early and narrows windows | `round()` for centre, `ceil()` for half-width |
| M-M25 | `arrival_pattern_matrix.py:357-366` vs `1104-1106` | `contains_sample` (truncated bounds) and `deviation_sigma` (float) can disagree — logged σ contradicts the accept/reject decision | Compute both from the same float quantities |
| M-M26 | `arrival_pattern_matrix.py:1105-1106` | `max_search_sample` never clamped to `SAMPLES_PER_MINUTE`; `min` clamped asymmetrically | Clamp both ends |
| M-M27 | `arrival_pattern_matrix.py:587-612` | TEC correction applied unconditionally (no RTP-mode gate) and only when positive (one-sided bias) | Gate on mode; apply for any sign |
| M-M28 | `arrival_pattern_matrix.py:796-843` | Ionospheric delay added to a vacuum geometric path through a *true* `hmF2` — virtual-vs-true height semantics unspecified; risks double-counting iono excess delay | Specify height semantics; use virtual height + no extra term, or true height + proper group-delay term |
| M-M29 | `propagation_mode_solver.py:364-406` | Flat-Earth hop geometry — disagrees with the spherical model elsewhere by several % on long paths (see S2); feeds `back_calculate_emission_time` | Spherical law of cosines |
| M-M30 | `propagation_mode_solver.py:524-628` | Tier-2 fallback emits modes with no MUF check — physically impossible modes marked `viable=True` | Compute oblique MUF, hard-reject above it |
| M-M31 | `propagation_mode_solver.py:792-802` | `back_calculate_emission_time` falls back to arbitrary `candidates[0]` (shortest delay) when no 1F2 exists, still reporting 0.6 confidence | Choose the path-appropriate mode with reduced confidence, or return UNKNOWN |
| M-M32 | `propagation_mode_solver.py:603` | E-layer iono delay scaled by an unphysical `×0.5` "less dense" fudge | Model E-region group delay properly or inflate uncertainty |
| M-M33 | `propagation_mode_solver.py:674-686` | `identify_mode` selects by uncertainty-weighted residual but reports confidence from the raw residual — different objectives; favours high-σ modes | One consistent Gaussian-likelihood metric |
| M-M34 | `propagation_mode_solver.py:716-722` | FSS hop-discrimination branch logs an upgrade then changes nothing — dead code | Implement or remove + drop the docstring claim |
| M-M35 | `propagation_mode_solver.py:809-820` | `second_aligned` confidence boost is circular — the model delay being validated determines the alignment | Treat as diagnostic unless model σ ≪ ±2 ms |

### 3.4 Low (metrology)

`tick_edge_detector.py`: half-template `+/−` round-trip is redundant — simplify (see §1.1); `STATION_TICK_DURATION_MS.get(...,999)` sentinel; `is_clean_minute` docstring describes a dedicated-channel parameter it lacks. `tick_matched_filter.py`: unused `_envelope_buffer` ("zero-allocation" comment false); `phase_rad` hard-coded 0 but documented as a measurement; unused imports. `tick_pll_decoder.py`: stale "AI Assistant"/A/B-testing header; `np.mean([])` → NaN in `_compile_analysis:613`. `buffer_timing.py`: `no_timing` returns `sample0_utc=0.0` (1970 epoch) and `metrology_engine` checks a stale `'metadata_fallback'` string; `jitter_ms`/`n_snapshots_used` advertised as quality metrics but constant. `metrology_engine.py`: `_last_*` attributes written outside the lock (re-entrancy/race); dead `_save_calibration`/`bpm_calibration`; `complex64` convention not preserved through DSP. `multi_broadcast_fusion.py`: `malloc_trim` masks an undiagnosed leak; module-header grade/mode-weight tables disagree with the code. `broadcast_kalman_filter.py`: dead `check_gpsdo_continuity`; anchor-frequency exact float equality. `chrony_shm.py`: docstring struct layout wrong; `poll 3`/`poll 6` inconsistency. `metrology_service.py`: `status.json` dumps every measurement each minute; stale "do not compute clock offset" docstring. `l2_calibration_service.py`: per-poll `*.h5` glob; deprecated `datetime.utcnow()`. `arrival_pattern_matrix.py`: stale 2-tuple usage example; exact-float frequency dict keys; undocumented parametric-height magic numbers.

---

## 4. Physics — findings

### 4.1 Critical

**P-C1 — Physics service is coupled to the real-time metrology path.** `systemd/timestd-physics.service` (`Requires=timestd-l2-calibration.service`, `After=`, `Type=notify`, `WatchdogSec=120`) + `physics_fusion_service.py` `ExecStartPre` `chown -R … /var/lib/timestd/phase2`. Violates `METROLOGY_PHYSICS_SPLIT §"Interface"` and action item 4. A physics crash-loop chowns live L2 metrology files and can race SWMR writers; the watchdog can kill physics mid-write. *Fix:* `Requires=`→`Wants=`, `Type=simple`, scope the chown to physics-owned subdirs only.

**P-C2 — `IonosphericModel.update_calibration_from_ionogram` crashes on first call.** `ionospheric_model.py:1028-1090`. References three identifiers that do not exist (`base_heights.hmF2_km` — field is `hmF2`; `self._get_grid_key` — no such method; `self.calibration_history` — attribute is `_calibration_data`) and calls `get_layer_heights(latitude, longitude, timestamp)` with arguments positionally swapped vs the real signature `(timestamp, latitude, longitude, f107)`. The highest-quality calibration anchor (ionosonde, confidence 1.0) is dead on arrival. *Fix:* rewrite against the real API; add a unit test exercising the path.

### 4.2 High

**P-H1 — `tec_estimator` presents group-delay TEC as an operational product.** `tec_estimator.py:1-37`. The module docstring describes a "Bayesian Multi-Frequency TEC Estimator" with `tec_u` as a first-class deliverable, never stating that — per `PHYSICS_CONTRACT §1/§4` and `METROLOGY_PHYSICS_SPLIT` — group-delay TEC for WWV/WWVH/CHU/BPM frequency spans is **at or below the noise floor**. The contract lists "Claiming group-delay TEC is operational" as a failure condition. *Fix:* add a prominent honesty section; cite the Δt-vs-noise-floor table; mark `tec_u` caveated.

**P-H2 — `tec_estimator` confidence is R², reporting high confidence on noise.** `tec_estimator.py:228-230` (confirmed: `confidence = min(MAX_CONFIDENCE_N2, r2)` / `max(0, min(1, r2))`). With 2–4 frequencies on a 1/f² basis, R² is near 1 even when the slope is dominated by per-station systematic offsets (CHU −76 ms etc.). R² measures fit-to-line, not detectability above noise. `polyfit(cov=True)` is requested (line 281) and discarded. *Fix:* derive confidence from slope SNR `slope/√cov[0,0]`; gate to ~0 when slope σ exceeds the slope; add `tec_uncertainty_tecu` to `TECResult`.

**P-H3 — `carrier_tec` unwrap-quality check cannot detect the failure it targets.** `carrier_tec.py:136-149` (confirmed). `dphi_raw = np.diff(carrier_phase_rad)` then wraps to (−π,π] and counts steps > π/2 — but that wrap is exactly what `np.unwrap` already assumed. When the true inter-tick phase change exceeds π (Doppler > 0.5 Hz, the contract's named failure mode), the wrapped diff is *small*, so `n_jumps` stays 0 and `unwrap_quality` reports 1.0. The detector provably cannot catch wrong-branch unwrapping. *Fix:* detect the *risk* from expected Doppler/cadence, or cross-check against an independent IQ frequency estimate; at minimum fix the misleading comment.

**P-H4 — `carrier_tec` cycle-slip "freeze" fabricates a zero rate.** `carrier_tec.py:169-200`. On `|d²φ| > 5 Hz/s` the Doppler is set to 0 ("freeze dTEC rate"), which asserts TEC is constant through the slip — a fabricated measurement integrated into the cumulative TEC. The integer-cycle jump is discarded, biasing all subsequent dTEC. *Fix:* segment the series at the slip and re-anchor, or flag cycle-slip contamination and reduce confidence; count slips into `n_phase_jumps`.

**P-H5 — `tec_estimator` discards negative-TEC epochs instead of zeroing.** `tec_estimator.py:207-218`. Returns `None` on `m_slope < 0`; the contract requires "negative TEC slope forced to zero". For true TEC near zero, noise gives a negative slope ~50% of the time, so surviving samples are biased positive. (Same defect in `physics_fusion_service.py:510-522` and `ionospheric_reanalysis.py:546-552`.) *Fix:* clamp to zero with `confidence=0` and a quality flag.

**P-H6 — `tec_estimator` no zero-frequency validation.** `tec_estimator.py:125-169`. `frequency_hz` used in `1/f²` with no finiteness/HF-band check; a zero/missing frequency yields Inf/NaN into `polyfit`. *Fix:* validate each `frequency_hz` is finite and in the HF band.

**P-H7 — `carrier_tec` integrated dTEC has no uncertainty propagation.** `carrier_tec.py:215, 349-391`. Integrated TEC is a random-walk cumulative sum; its variance grows with time but no per-sample uncertainty is propagated. `_estimate_noise_floor` returns `0.0` for short/degenerate series — reads as "perfect", not "unknown". *Fix:* propagate per-tick phase-noise variance through to a `sigma_dtec_tecu` time series; return NaN when indeterminate.

**P-H8 — `tec_geometry` elevation angle uses a flat-Earth triangle.** `tec_geometry.py:55-81`. `atan2(h_iono, half_distance)` ignores curvature; for 1000–3000 km paths it overestimates elevation, underestimating the obliquity factor and biasing VTEC high. *Fix:* spherical-Earth elevation `atan2(cos γ − R/(R+h), sin γ)`.

**P-H9 — `tec_validator` compares slant HF TEC to vertical GPS VTEC with no obliquity correction.** `tec_validator.py:124-210`. `tec_bias = hf_tec − gps_vtec` differences a slant and a vertical quantity; the reported bias is mostly the obliquity factor (×2–3 at low elevation), so `FLAG_VALIDATED` decisions are meaningless. The IPP is also a Cartesian lat/lon midpoint, not even great-circle. *Fix:* convert HF slant→vertical (or GPS vertical→slant) before comparing; use the true IPP.

**P-H10 — `vtec_mapper` polynomial fit has no regularisation or conditioning check.** `vtec_mapper.py:221-241`. `lstsq(rcond=None)` on a degree-2 2-D polynomial with clustered IPPs is ill-conditioned; the map oscillates wildly off-cluster while `rms_residual` (in-sample, at the IPPs) reports a good fit. *Fix:* Tikhonov regularisation or a smooth basis; check `cond`; mask cells outside the IPP convex hull.

**P-H11 — `iono_tomography` two-shell separation is prior-dominated.** `iono_tomography.py:195-262`. E- and F-shell obliquity factors differ only ~2–3% at the available elevations, so the data term is a shallow valley and the optimiser returns essentially the prior split — yet `tec_e_tecu`/`tec_f_tecu` are emitted as measurements. *Fix:* report posterior-vs-prior variance reduction; flag "prior-dominated" when the data does not constrain the split.

**P-H12 — `propagation_model` deprecated `PhysicsPropagationModel` still exported.** `core/__init__.py:63, 262`. `PHYSICS_CONTRACT §4` lists using it as a failure condition; it is in `__all__` with no runtime `DeprecationWarning`. Its own Tier-2 iono-delay (`physics_propagation.py:514`) is missing the `×1e16` TECU→el/m² factor — ~16 orders of magnitude too small. *Fix:* drop from `__all__`; emit `DeprecationWarning` from `__init__`.

**P-H13 — `propagation_model` 1σ/3σ uncertainty convention undocumented and self-inconsistent.** `propagation_model.py:855-868` assigns `base_ms` as 1σ (0.5/1.0/1.5/3.0/5.0) while the contract tier table and schema field `uncertainty_3sigma_ms` expect 3σ; `vacuum_fallback` hard-codes `15.0` (the 3σ value). A consumer treating the output as the wrong σ gets windows 3× too tight or loose. *Fix:* name fields by σ explicitly; populate a separate `uncertainty_3sigma_ms`.

**P-H14 — `RaytraceEngine` is fully built but never wired into `HFPropagationModel`.** `propagation_model.py` never imports `raytrace_engine`. The most accurate propagation model available (PHaRLAP ray tracing through a real Ne grid) is dead code from the live pipeline's perspective, which instead uses a `2·slant` geometric hop that ignores ray bending and in-layer retardation. *Fix:* wire `RaytraceEngine` as Tier 0 (advisory/async), at least for reanalysis — or document why it is deferred.

**P-H15 — `propagation_model` MUF uses a flat-Earth approximation.** `propagation_model.py:570-573` (re-adjudicated from Critical — see §1.1). `foF2/sin(elev)` *is* the correct flat-Earth secant law; the genuine issue is that curvature is ignored, so the layer incidence angle is wrong for oblique HF paths, mis-gating high-band short-path modes. *Fix:* `cos(i₀) = R·cos(elev)/(R+h)`, `MUF = foF2/cos(i₀)`.

**P-H16 — `ionospheric_model` hmF2 solar-activity sign is backwards.** `ionospheric_model.py:709-713`. `solar_term = −HMF2_SOLAR_FACTOR·(f107−100)` drives hmF2 *down* at solar max; observationally hmF2 *rises* with solar flux. At current solar max the parametric fallback under-predicts hmF2 by ~30–50 km → ~0.1–0.3 ms/hop geometric-delay bias. *Fix:* flip the sign; correct the comment.

**P-H17 — `ionospheric_model` IONEX file selection matches by year only.** `ionospheric_model.py:485-497`. `glob(f"*{date_str[:4]}*")` then `max(..., key=mtime)` — a query for any 2026 date returns whichever 2026 IONEX file was downloaded most recently. During reanalysis of historical minutes the VTEC can be months off. *Fix:* match the full date / day-of-year for both modern and legacy filename patterns.

**P-H18 — `ionospheric_model` re-execs the IONEX module on every cache miss.** `ionospheric_model.py:505-538`. `importlib … exec_module` of `scripts/ionex_integration.py` runs every miss; `spec.loader` used without a `None` check; `_ionex_cache_max_age` defined but never honoured (stale parsers served indefinitely); importing core logic from `scripts/` breaks a wheel install. *Fix:* move `IONEXParser` into the package; import once; honour the cache age.

**P-H19 — `iono_data_service` WAM-IPE S3 fallback URL is a directory listing.** `iono_data_service.py:492-505`. The only S3 entry appended ends in `/` — a listing URL, not a `.nc` object; `_parse_wamipe_netcdf` then tries to open XML as NetCDF and fails. The primary Tier-0 source (±1.5 ms) is structurally unreachable; the hierarchy silently collapses to IRI. *Fix:* list the prefix, parse `<Contents><Key>`, select the latest `.nc`; add `xml` to the content-type skip guard.

**P-H20 — `iono_data_service` GIRO parser guesses columns positionally.** `iono_data_service.py:799-824`. `parts[-3]=foF2, parts[-2]=hmF2` with no header/column-name validation; a swapped or extra column silently corrupts hmF2, which is blended into the propagation geometry at weight up to 1.0. *Fix:* parse the documented DIDBase header / SAO-XML by column name; range-validate units.

**P-H21 — `iono_data_service._climatological_fallback` is called as a static method but defined as an instance method.** `iono_data_service.py:1041` (`def _climatological_fallback(self, …)`), called at `propagation_model.py:477` as `IonoDataService._climatological_fallback(lat, lon, utc_time)` — `lat` is bound to `self`, the third arg is missing → `TypeError` every call (confirmed). The "canonical parametric model" is never used; the minimal inline fallback runs instead. *Fix:* decorate `@staticmethod` and drop `self` (mirror `_chapman_profile`).

**P-H22 — `iono_data_service` serves stale grids as high-confidence.** `iono_data_service.py:861-868`. `get_iono_params` uses `_current_grid` with no staleness check; if the background fetch stops, an hours-old grid is returned tagged `source="wamipe"` and assigned 0.6–0.8 confidence. *Fix:* check grid age against `WAMIPE_CACHE_MAX_AGE_S`; downgrade/fall back when stale.

**P-H23 — `propagation_engine` "IRI tier" is a literal `pass`.** `propagation_engine.py:105-119`. The class advertises a physics-based IRI-2020 tier "with ionospheric ray tracing"; the branch does nothing and falls through to the geometric model even when `preferred_method='IRI'`. The constructed `IonosphericModel`/`IonosphericDelayCalculator` are never called. *Fix:* implement the tier or delete the dead branch and the docstring claims (or make `PropagationEngine` a thin delegator to `HFPropagationModel`).

**P-H24 — `raytrace_engine` infers hop count from an array index and extrapolates hop-0 geometry.** `raytrace_engine.py:469-493`. `n_hops = k+1`; `apogee` is taken from hop 0 for every mode; `grp_factor` (hop-0 group/ground ratio) is applied to all hop counts. Multi-hop group delays are approximations of an approximation in the path meant to be authoritative. *Fix:* use PHaRLAP's per-hop output directly; verify array semantics; assert lengths.

**P-H25 — `physics_fusion_service._processed_minutes` is not persisted.** `physics_fusion_service.py:199, 1251-1338`. The dedup set is in-memory; on restart (`Restart=always`, `RestartSec=10`) the 30-minute lookback reprocesses minutes whose L3 records already exist → duplicate TEC/dTEC/L3 records. The contract forbids duplicate records. *Fix:* seed `_processed_minutes` from existing L3 files on startup, or check-before-write.

**P-H26 — TEC aggregation windows span mode transitions.** `physics_fusion_service.py:435-520` (per-minute median collapses across modes; two disagreeing "dominant mode" computations) and `ionospheric_reanalysis.py:499-542` (an **entire hour** median-collapsed into one TEC fit). The contract forbids mixing propagation conditions in the TEC fit window — a mid-window mode hop injects a multi-ms geometric step into the 1/f² fit. *Fix:* group by `(station, frequency, mode)`; never collapse across modes; reanalysis must fit ≤5-min windows.

**P-H27 — Reanalysis TEC fit uses the wrong L2 field.** `ionospheric_reanalysis.py:476-542` uses `raw_arrival_time_ms`; `physics_fusion_service._read_l2_slice:404-409` explicitly warns *not* to ("its intercept is dominated by geometric delay, not TEC") and uses `clock_offset_ms`. The two services contradict each other on the same field's meaning; one TEC product is computed from the wrong quantity. *Fix:* pin the L2 schema definition authoritatively; both services must use the geometry-removed quantity.

**P-H28 — `physics_service` does a full-day HDF5 table scan every 5 s and iterates non-channel directories.** `physics_service.py:160-176` reads the whole current UTC day per channel each 5-second loop (contract-forbidden full-table-scan); `_process_l1_files:131-135` iterates *all* `phase2` subdirs including `science`/`fusion`/`ionex`. The module is also dead (no systemd unit) and full of prototype markers. *Fix:* delete the module, or finish it (incremental tail reads, channel-detection exclusion list).

**P-H29 — `TIDDetector` produces no science products.** `tid_detector.py` has no run loop and no HDF5 writer; `_active_events`/`_completed_events` are never appended; `web-api/services/tid_service.py` reads `phase2/science/tid/`, which nothing writes. The contract lists TID detection as an L3 deliverable. *Fix:* wire `TIDDetector` into the physics service and write events, or document it as not-deployed and update the contract.

**P-H30 — `TIDDetector` has no MSTID/LSTID period-band filtering.** `tid_detector.py:223-332`. No band-pass before cross-correlation; after only linear detrending the residuals still carry diurnal/instrumental drift, which cross-correlates strongly between paths and is flagged as "TIDs". `_estimate_period` is reported but not used as a gate. *Fix:* band-pass to TID period bands (≈10–90 min); gate detection on period.

**P-H31 — `TIDDetector._cross_correlate` normalisation is not a correlation coefficient.** `tid_detector.py:392-426`. `np.correlate(...)/len(s1)` instead of dividing by the per-lag overlap count `N−|lag|`; `corr` is biased low at large lag, so the `min_correlation = 0.6` threshold is meaningless and slow LSTIDs are suppressed. *Fix:* divide by per-lag overlap, or compute Pearson r on the overlap.

**P-H32 — `TIDDetector` has no statistical significance / false-alarm control.** `tid_detector.py:223-332`. Detection is `max correlation over all path pairs > 0.6` — picking the max over many pairs guarantees inflated "best" correlation on pure noise. *Fix:* significance threshold from series length and pair count (Bonferroni/surrogate); minimum-amplitude gate.

**P-H33 — `TIDDetector` interpolates linearly across arbitrarily long data gaps.** `tid_detector.py:334-390`. `np.interp` with no gap mask fabricates smooth low-frequency features across multi-hour HF dropouts, which correlate between paths as false TIDs. *Fix:* mask interpolated regions beyond a max-gap threshold; exclude masked samples from correlation.

### 4.3 Medium (physics)

| ID | File:line | Finding | Fix |
|---|---|---|---|
| P-M1 | `tec_estimator.py:183-205` | MAD outlier rejection degenerates at N=3 (can drop below 2 → `None`); "50% contamination" comment overstated | Reject ≤1 per pass; skip for N≤3; floor σ against measurement uncertainty |
| P-M2 | `tec_estimator.py:302-304` | `_fit_wls` catches bare `Exception` → `None`, hiding bugs | Catch `LinAlgError`/`ValueError` only |
| P-M3 | `carrier_tec.py:182-200` | Doppler → finite-difference → re-integrate adds noise and a half-sample lag; gaps held flat unflagged | Compute relative TEC directly from unwrapped phase |
| P-M4 | `carrier_tec.py:289-347` | `compute_differential_dtec` differences two unanchored relative series with arbitrary offsets, then calls the result physical | Detrend/mean-remove over the common window first |
| P-M5 | `carrier_tec.py:202-213` | Anchoring applies a constant offset with no uncertainty and no epoch-tolerance check; docstring says anchor to group-delay TEC (noisy) vs contract's GNSS VTEC | Anchor to GNSS VTEC; propagate anchor uncertainty |
| P-M6 | `tec_geometry.py` / `tec_validator.py:223` | Station coords duplicated and inconsistent — **BPM placed in Shanghai vs true Pucheng** (~600 km) | Single source from `wwv_constants.py`; fix BPM |
| P-M7 | `tec_validator.py:145, 174` | VTEC range `>1.0` rejects valid deep-night low TEC; broad `except` masks IONEX failures as `VALIDATION_FAILED` | Floor to ~0.1 TECU; catch specific exceptions |
| P-M8 | `vtec_mapper.py:238-252, 315-335` | In-sample residual overstates map quality; IONEX writer grid-bounds/fill-value/`%5d` width likely non-conformant | LOO cross-validation; validate against IONEX spec |
| P-M9 | `iono_tomography.py:172-179, 360-377` | `n_hops` multiplies obliquity (assumes horizontally uniform iono over a 3000 km track); elevation/distance hard-coded 30°/1500 km when predictions absent (contract-forbidden) | Per-hop IPP geometry; require real geometry, skip paths without it |
| P-M10 | `iono_tomography.py:221-236` | Non-converged `minimize` result used unconditionally; broad `except` | Reject or down-confidence on non-convergence; the quadratic could be solved closed-form |
| P-M11 | `ionospheric_model.py:601-608, 1248` | IRI cache TTL keyed off wall clock (incoherent for reanalysis); `_estimate_vertical_tec` calls `self._extract_scalar` (wrong class) → IRI TEC tier silently never works | Key validity off the query slot; fix the `_extract_scalar` call |
| P-M12 | `ionospheric_model.py:954-984` | Calibration uses flat-triangle slant geometry while prediction uses spherical — `offset_km` conflates height error with the geometry mismatch | Use one shared spherical geometry |
| P-M13 | `propagation_model.py:436` | IRI tier hard-codes `TEC_TECU = 20.0` though IRI-2020 outputs TEC | Query TEC from IRI / `IonosphericDelayCalculator` |
| P-M14 | `propagation_model.py:903-1005` | `compute_differential_delay` attributes a 1F-vs-2F geometric path difference entirely to TEC | Difference same-mode delays only |
| P-M15 | `propagation_model.py:300-368` | `predict()` cache evicts oldest-by-simulated-time — thrashes during reanalysis of old archives | Document monotonic-time assumption; disable cache for reanalysis |
| P-M16 | `iono_data_service.py:347, 638-652, 1015-1033` | No temporal interpolation between `_previous`/`_current` grids (5-min jumps); grid not validated for monotonic coords / fill values; GIRO distance in raw degrees (dateline-wrong) | Interpolate; validate; great-circle km distance |
| P-M17 | `raytrace_engine.py:99, 256, 308-311` | `r12_idx` hard-coded 100 (ignores solar cycle); `mp.get_context('fork')` unsafe in a threaded process; per-height `np.interp` loop | Source R12 from the solar feed; use `spawn`; vectorise |
| P-M18 | `raytrace_engine.py:521-549` | `_geometric_fallback` returns straight-line vacuum delay labelled mode `"1F"` with 0° launch / 0 km apogee | Compute a real 1-hop slant path or label it clearly degraded |
| P-M19 | `propagation_engine.py:160-174` | Flat-Earth hop triangle + flat 3% iono adder; `frequency_hz` accepted and ignored (iono delay is 1/f²) | Spherical geometry; proper 40.3/f² term |
| P-M20 | `physics_fusion_service.py:262-335` | `_timed_write` leaks one daemon thread per timeout; the abandoned write still holds the HDF5 handle and can race the next write | Per-writer lock or writer recreation; bound orphan threads |
| P-M21 | `physics_fusion_service.py:372-434` | `_read_l2_slice`/`_read_tick_phase_minute` full-table-scan (contract-forbidden); only `_read_gnss_vtec` was fixed to tail-read | Apply the tail-read pattern |
| P-M22 | `physics_fusion_service.py:656-667` | F2 virtual height hard-coded 300 km with a stale "improved later" comment | Source virtual height from the model/reanalysis |
| P-M23 | `ionospheric_reanalysis.py:411-414, 448-454, 641-726` | foE = `0.3·foF2` / night 0.5 (no basis); strong over-MUF daytime signals unconditionally relabelled `'Es'`; single global path-independent MUF; ad-hoc `muf_confidence` | Use the ITU foE formula; check Es geometry; per-path MUF |
| P-M24 | `ionospheric_reanalysis.py:728-798` | `process_hour` is not idempotent — re-runs/catch-up duplicate L3C/L3A records | Check-before-write per hour/station/frequency |
| P-M25 | `physics_service.py:256-275` | Solver-computed TEC discarded (`tec_estimate=None`); `StationID[station_str]` `KeyError` silently drops measurements permanently | Populate `tec_estimate`; validate station names |
| P-M26 | `tid_detector.py:462-560` | TDOA solver uses per-station midpoints (degenerate same-station rows), discards lstsq residual; 2-path separation/direction geometry physically unjustified; confidence `×1.2` and `n_paths_correlated` hard-coded | Distinct per-path pierce points; check conditioning; real pierce-point geometry; significance-based confidence |

### 4.4 Low (physics)

`tec_estimator.py`: unused `propagation_mode` field and `field` import; SNR weight formula undocumented; no detection-limit block (contract requires one). `carrier_tec.py`: records with `carrier_phase_rad == 0.0` dropped (conflates missing/zero/failed); unused imports; no `frequency_mhz > 0` guard. `tec_geometry.py`: no lat/lon validation; `M` not capped (siblings cap at 10). `tec_validator.py`: duplicates `tec_geometry`; unaddressed TODOs. `vtec_mapper.py`: hard-coded receiver coords default (contract-forbidden); duplicated polynomial term enumeration. `iono_tomography.py`: `effective_hmF2_km` is just the fixed input; N=2 allowed (no residual DOF); `path_residuals` key collides across modes. `ionospheric_model.py`: stale "IRI-2016" docstrings; tz cache-key normalisation; dead `calculate_hf_reflection_point` with an unused `layer_height_km` param. `propagation_model.py`: `foE_MHz` never populated (E-layer MUF always 3.0); `vacuum_fallback ×1.15` unjustified; redundant `except (ImportError, Exception)`. `physics_propagation.py`: pylap API mismatch confirms it never worked. `iono_data_service.py`: dead `_save_grid_cache`; filename timestamp never parsed. `propagation_engine.py`: long-path factor `1.05 < 1.0`-ish implies superluminal. `raytrace_engine.py`: `oarr[0]` foF2-vs-NmF2 docstring/code contradiction; import-time `sys.path` mutation. `physics_fusion_service.py`: per-minute channel glob; tuple-keyed dict annotated `Dict[str, …]`; differential dTEC recomputed from scratch. `ionospheric_reanalysis.py`: fixed `FOF2_NOON_MHZ = 9.0` (ignores solar cycle); per-record write failures swallowed → hour reports `ok`. `tid_detector.py`: unbounded event lists / no active→completed lifecycle; per-call `import math/itertools`; undocumented thresholds.

---

## 5. Documentation — findings

`PHYSICS.md` is the strongest document in the set — rigorous ✅/⚠️/❌ honesty markers and good "why" exposition; treat it as the reference standard. Most cross-document conflicts should be resolved in its (and the code's) favour over the stale contracts.

### 5.1 Critical / High

- **D-C1 — `SCIENTIFIC_CAPABILITIES.md` is broadly stale and *understates* the instrument.** No version/date header; describes scintillation (S4, σφ), TID detection, and foF2 estimation as "not yet implemented" when `PHYSICS.md §4` and the code show them operational; says L3 is "CSV (current)" (removed in v5.0); gives CHU FSK tones as Bell-103 originate 1070/1270 Hz (correct CHU answer pair is 2225/2025 Hz). *Fix:* retire it (fold unique content into `PHYSICS.md`) or fully rewrite with version/date; add a "SUPERSEDED" banner meanwhile.
- **D-H1 — Version numbers inconsistent across the set.** `pyproject.toml` says **7.0.0**; README says "V6.11.0" then "v6.12"; ARCHITECTURE "V6.12.0"; METROLOGY/PHYSICS/TECHNICAL_REFERENCE "6.11.0". *Fix:* single source of truth (`pyproject.toml`); reconcile every header/footer; add a doc-version check to release.
- **D-H2 — `ARCHITECTURE.md` BPM policy is wrong.** Line 104 assigns BPM a 30% fusion weight; METROLOGY §14.5 and the code (`multi_broadcast_fusion.py:2157` `'BPM': 0.0  # EXCLUDED`) exclude BPM from fusion since 2026-02-07. *Fix:* correct to "science only, 0%".
- **D-H3 — `ARCHITECTURE.md` dead reference.** Points to `docs/design/DUAL_PURPOSE_ARCHITECTURE.md`; the actual doc is `METROLOGY_PHYSICS_SPLIT.md`. Also duplicate "Last Updated" headers and content appended after the document's apparent end. *Fix:* correct the link; clean the headers/TOC.
- **D-H4 — `TECHNICAL_REFERENCE.md` HDF5 section is pre-SWMR.** §"HDF5 Concurrent Access" describes open-write-close + `locking=False` and a changelog line "SWMR eliminated", while ARCHITECTURE/README/METROLOGY describe the current v6.10 SWMR model. A developer would follow the obsolete pattern. *Fix:* rewrite to the SWMR model; add the v6.10 changelog entry.
- **D-H5 — `METROLOGY.md §4.5/§4.6` describe unbuilt functionality as operational.** T6 BPSK-PPS injection, the authority manager, `/run/hf-timestd/authority.json` schema v1, `chronyc selectopts` gating, mDNS TXT extension — present-tense, no implementation-status marker, while ARCHITECTURE lists PPS injection as a "Future Option". *Fix:* add ✅/⚠️/❌ markers (as `PHYSICS.md` does) distinguishing built from designed.
- **D-H6 — `PHYSICS.md` group-delay-TEC honesty marker too generous.** §3.1 heading carries a ✅ umbrella over both carrier-phase dTEC *and* group-delay TEC; the contract requires ❌ for group-delay TEC (below noise floor). *Fix:* split the heading; mark group-delay TEC ❌.
- **D-H7 — Contracts have drifted and cannot be treated as ground truth.** `PHYSICS_CONTRACT` says dTEC `is_anchored` is "always False" and `tof_kalman_ms` is dead, but GNSS-VTEC anchoring is implemented and `tof_kalman_ms` is still read (`physics_fusion_service.py:409`); `METROLOGY_CONTRACT` says HDF5 uses `locking=False` (pre-SWMR). *Fix:* refresh both contracts against the code (see S1).
- **D-H8 — No single clean entry point for the dual-pipeline concept.** The clearest explanation of metrology-vs-physics is buried in the `DESIGN`-marked `METROLOGY_PHYSICS_SPLIT.md`, unlinked from README. Terminology drifts (RTP/Fusion "modes" vs Metrology/Physics "pipelines" vs A/T "levels"). *Fix:* a short "What this system does and why" at the top of README or a `docs/OVERVIEW.md`; unify terminology.

### 5.2 Medium / Low

README: FUSION accuracy table (±2–5 ms) contradicts METROLOGY §14 fused figures (0.3–1.0 ms) with no single-cycle-vs-averaged note; "eight services" membership differs across docs; stale ±0.036 ms Cramér-Rao floor (tied to the superseded tone correlator, not the edge ensemble). ARCHITECTURE: duplicated TOC numbering; SHM segment numbers inconsistent (0/1 vs 2/3). METROLOGY: channel/broadcast/fused counts (9 vs 13 vs 17) never defined; Kalman Q values (1e-12 / 1e-10 / 0.01) not labelled by state component; CHU systematic stated as both 74 and 76 ms; §13 and §14 carry redundant, 5×-divergent FUSION-accuracy tables; refid examples use three conventions (TMGR/HFSN/TSL1); L1A/L1B data levels defined four different ways across docs. PHYSICS: §4 titled "Partially Implemented" but all entries marked ✅; `<!-- LIVE: -->`/`<!-- LOGS: -->` placeholders render invisibly on GitHub leaving an empty "validation" section; §3.3 layer heights ✅ vs §10.3 "not suitable" (model-provided vs measured — not spelled out). TECHNICAL_REFERENCE: stale header/footer dates; changelog missing v6.8–v7.0; references `update-production.sh` (README uses `deploy.sh`); cites retired "L5/L6 hardware" grade taxonomy.

---

## 6. Missed opportunities (consolidated)

The methodology supports more than the code currently extracts:

1. **Proper inverse-variance fusion.** The per-broadcast `kalman_uncertainty_ms` already exists — using it as `σ_i` would make the fusion genuinely minimum-variance instead of heuristic (M-H12).
2. **End-to-end uncertainty propagation.** The L2 ISO-GUM components, `polyfit` covariance, and per-tick phase noise are all computed and discarded. Propagating them would give Chrony a correct confidence weight and give every science product a real error bar (S3, P-H2, P-H7).
3. **Mode probability distributions, not single assignments.** `METROLOGY_PHYSICS_SPLIT` calls for Bayesian per-mode posteriors; `propagation_mode_solver` and `arrival_pattern_matrix` produce a single hard mode. Multi-mode arrivals are computed in the matrix but `validate_detection` checks only the primary (M-M, M-M33).
4. **Record-all-arrivals for physics.** The marker correlator suppresses secondary peaks within ±800 ms — HF multipath (1–10 ms) is erased before it can be recorded (M-M8). The physics pipeline's whole premise ("record everything, interpret later") is undercut.
5. **Ray tracing.** `RaytraceEngine` is a complete PHaRLAP integration sitting unused (P-H14).
6. **TID science.** `TIDDetector` could yield azimuth/speed/amplitude of travelling disturbances; today it writes nothing (P-H29).
7. **Gate → likelihood weight.** Converting `validate_detection` to a smooth likelihood (action item 1 of the design doc) would remove the model-accuracy dependency from metrology correctness and let edge-case CHU detections contribute instead of being discarded.
8. **Temporal interpolation of WAM-IPE grids** — infrastructure (`_previous_grid`) exists, unused (P-M16).
9. **Carrier-phase cross-minute continuity & GNSS-anchored absolute TEC** — the precise relative dTEC could be promoted to absolute TEC via the GNSS VTEC anchor that already exists in the pipeline.

---

## 7. Recommended remediation order

**Tier 1 — correctness & safety (do first):**
1. P-C1 — decouple `timestd-physics.service` from the metrology critical path.
2. M-C2 / M-C3 — fix the discontinuity-filter desync and the chrony SHM odd-count latch (clock-latch class).
3. M-C1 / M-H13 — make TSL1/TSL2 genuinely independent; resolve the double-Kalman cascade.
4. P-C2, P-H21, P-M11 — fix the three crash/dead-path bugs (`update_calibration_from_ionogram`, `_climatological_fallback`, `_extract_scalar`); narrow the excepts hiding them (S5).
5. M-H14 — gate the GNSS-VTEC `d_clock` correction on RTP mode.

**Tier 2 — methodological soundness:**
6. S2 — consolidate hop geometry onto one spherical module (resolves M-M29, P-H8, P-M12, P-M19, parts of P-H15).
7. S4 — one SNR definition project-wide.
8. M-H1 / M-H7 — fix the matched-filter template/domain mismatch.
9. P-H1/P-H2/P-H5, P-H26/P-H27 — TEC honesty: noise-floor disclosure, slope-SNR confidence, zero-clamp negatives, mode-segmented fit windows, single field definition.
10. M-H11/M-H12/M-H19 + S3 — Kalman process-noise units, true inverse-variance weighting, end-to-end uncertainty.

**Tier 3 — cleanup & documentation:**
11. S1 / D-H7 — refresh the `.windsurf` contracts against code, then realign code (M-H2: decide `TickMatchedFilter`/PLL fate; M-M27/arrival gate→weight).
12. D-C1, D-H1–D-H8 — documentation pass: retire/rewrite `SCIENTIFIC_CAPABILITIES.md`, unify version numbers, fix BPM/HDF5/dead-reference errors, add a single entry-point overview.
13. P-H29–P-H33 — make `TIDDetector` either a real, statistically-sound science product or explicitly mark it not-deployed.
14. P-H14 — wire `RaytraceEngine` in (at least for reanalysis) or document the deferral.
15. S7 — derive and cite the magic numbers.

---

*Findings were produced by parallel expert sub-reviews and adjudicated centrally; line numbers reflect source at review time and should be re-confirmed before each fix.*

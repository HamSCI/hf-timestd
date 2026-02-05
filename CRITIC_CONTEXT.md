# NEVER CHANGE THE FOLLOWING PRIMARY INSTRUCTION!!!

**Primary Instruction:** In this context you will perform a critical review of the HF Time Standard (hf-timestd) project, either in its entirety or in a specific component, as specified by the user. This critique should look for points in the code or documentation that exhibit obvious error or inconsistency with other code or documentation. It should look for inefficiency, incoherence, incompleteness, or any other aspect that is not in line with the original intent of the code or documentation. It should also look for obsolete, deprecated, or "zombie" code that should be removed. Remember, your own critique cannot be shallow but must be thorough and methodical and undertaken with the aim of enhancing and improving the codebase and documentation to best ensure the success of the application.

Make your criticism from the perspective of 1) a user of the system, 2) a metrologist, 3) a ionospheric scientist, and 4) a software engineer. These perspectives can differ in their priorities and interests, and your critique should reflect this. For example, a user of the system will be most interested in the accuracy and reliability of the system, while a metrologist will be most interested in the precision and traceability of the system, while an ionospheric scientist will be most interested in the ionospheric conditions and their impact on the system, and a software engineer will be most interested in the code quality, maintainability, coverage and adequacy of testing,and the resilience and robustness of the system over time and in edge cases. Ultimately, however, a consensus of these perspectives should guide your critique in service of the meeting the application's objectives.

# NEVER CHANGE THE PRECEEDING PRIMARY INSTRUCTION!!!

# The following secondary instruction and information will guide your critique in this particular session (the instructions below will vary from session to session)

---

## 📋 NEXT SESSION: SYSTEMATIC OFFSET + DETECTION METHODOLOGY REVIEW

**Objective:** Address the ~70ms systematic timing offset and critically review the detection methodology for weaknesses, errors, redundancy, and improvement opportunities.

---

### Issue 1: Systematic ~70ms Timing Offset

**Observation:** CHU and WWV/WWVH detections consistently arrive 40-85ms later than expected propagation delay.

**Evidence (2026-02-05):**
- CHU_3330: ToA = +45 to +72ms (expected ~5ms)
- CHU_7850: ToA = +42 to +67ms (expected ~5ms)
- WWV/WWVH tick analysis: timing_error = +30 to +55ms

**Verified NOT the cause:**
- Buffer alignment is correct (start_rtp matches expected boundary RTP exactly)
- CHU format is correct (500ms tone at second 0, not second 1)

**Suspected cause:** Latency in radiod's GPS_TIME/RTP_TIMESNAP capture. When radiod reports the mapping, there may be a delay between sampling the RTP timestamp and sampling the GPS time.

**Questions to investigate:**
1. Where in radiod is GPS_TIME/RTP_TIMESNAP captured?
2. Is there processing delay between RTP packet arrival and GPS time sampling?
3. Can we calibrate out this systematic offset?
4. Should we measure the offset empirically and apply a correction?

---

### Issue 2: Detection Methodology Critical Review

**Goal:** Identify weaknesses, errors, redundancy, circularity, and missed opportunities in the current detection pipeline.

**Current Detection Pipeline:**

```
Raw IQ Buffer (1 minute)
    ↓
AM Demodulation (magnitude - mean)
    ↓
┌─────────────────────────────────────────────────┐
│ Path A: Minute Marker Detection                 │
│   - Matched filter (500ms CHU, 800ms WWV/WWVH)  │
│   - Correlation SNR threshold (8 dB)            │
│   - Timing tolerance (±100ms)                   │
└─────────────────────────────────────────────────┘
    ↓
┌─────────────────────────────────────────────────┐
│ Path B: Tick Analysis (per-second ticks)        │
│   - 55 windows per minute (skips 29, 30, 59)    │
│   - Mean timing offset, std, drift              │
│   - Timing tolerance (±100ms)                   │
└─────────────────────────────────────────────────┘
    ↓
Physics Validation (ArrivalPatternMatrix)
    ↓
Multi-Constraint Validation (TimingConsistencyValidator)
    ↓
L1 Metrology Measurement
```

**Questions to answer:**

1. **Redundancy:** Are Path A and Path B redundant? Do they provide independent information or just duplicate effort?

2. **Circularity:** Does the expected arrival time depend on measurements that themselves depend on expected arrival time?

3. **Template matching:** Are the matched filter templates optimal?
   - CHU uses 500ms template but transmits 300ms tones at most seconds
   - Should we use different templates for minute marker vs regular ticks?

4. **SNR thresholds:** Are the thresholds appropriate?
   - 8 dB correlation SNR for detection
   - 12 dB for BPM (higher due to shorter template)
   - Are these empirically validated?

5. **Missed opportunities:**
   - CHU FSK decoding (seconds 31-39) — currently separate, could provide timing
   - WWV/WWVH BCD decoding — fragile, but could provide absolute time
   - Phase tracking — currently computed but not used for timing
   - Doppler estimation — computed but not used for ionospheric correction

6. **Edge cases:**
   - What happens at second 29 (CHU omits), 30, 59?
   - What happens during leap seconds?
   - What happens when signal fades mid-minute?

---

### Key Files for Detection Methodology Review

| File | Purpose |
|------|---------|
| `src/hf_timestd/core/metrology_engine.py` | Main detection orchestration |
| `src/hf_timestd/core/tone_detector.py` | Matched filter detection |
| `src/hf_timestd/core/tick_matched_filter.py` | Per-second tick analysis |
| `src/hf_timestd/core/arrival_pattern_matrix.py` | Physics-based predictions |
| `src/hf_timestd/core/timing_consistency_validator.py` | Multi-constraint validation |
| `src/hf_timestd/core/wwv_constants.py` | Station parameters, propagation bounds |

---

### Recent Session Summary (2026-02-05)

**Completed:**
1. Buffer alignment fix — calculate minute boundary RTP from GPS_TIME/RTP_TIMESNAP mapping
2. CHU detection fix — CHU transmits 500ms tone at second 0 (minute marker)
3. Timing tolerance increase — 50ms → 100ms for tick analysis
4. Web-API/UI improvements — broadcast-centric HDF5 data model integration
5. Documentation — `docs/changes/SESSION_2026_02_05_CHU_DETECTION_FIX.md`

**Current status:**
- CHU_3330: 72+ measurements, SNR 37-42 dB ✓
- CHU_7850: 66+ measurements, SNR 48-56 dB ✓
- CHU_14670: Weak signal (~6 dB), awaiting better propagation
- SHARED channels: WWV/WWVH/BPM detection working

---

### Success Criteria for Next Session

- ⬚ Root cause of ~70ms systematic offset identified
- ⬚ Offset either corrected at source or calibrated out
- ⬚ Detection methodology reviewed for weaknesses
- ⬚ Any identified issues documented or fixed
- ⬚ Tests added for edge cases if needed

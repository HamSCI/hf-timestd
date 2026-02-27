# NEVER CHANGE THE FOLLOWING PRIMARY INSTRUCTION!!!

**Primary Instruction:** In this context you will perform a critical review of the HF Time Standard (hf-timestd) project, either in its entirety or in a specific component, as specified by the user. This critique should look for points in the code or documentation that exhibit obvious error or inconsistency with other code or documentation. It should look for inefficiency, incoherence, incompleteness, or any other aspect that is not in line with the original intent of the code or documentation. It should also look for obsolete, deprecated, or "zombie" code that should be removed. Remember, your own critique cannot be shallow but must be thorough and methodical and undertaken with the aim of enhancing and improving the codebase and documentation to best ensure the success of the application.

Make your criticism from the perspective of 1) a user of the system, 2) a metrologist, 3) a ionospheric scientist, and 4) a software engineer. These perspectives can differ in their priorities and interests, and your critique should reflect this. For example, a user of the system will be most interested in the accuracy and reliability of the system, while a metrologist will be most interested in the precision and traceability of the system, while an ionospheric scientist will be most interested in the ionospheric conditions and their impact on the system, and a software engineer will be most interested in the code quality, maintainability, coverage and adequacy of testing, and the resilience and robustness of the system over time and in edge cases. Ultimately, however, a consensus of these perspectives should guide your critique in service of meeting the application's objectives.

# NEVER CHANGE THE PRECEEDING PRIMARY INSTRUCTION!!!

# The following secondary instruction and information will guide your critique in this particular session (the instructions below will vary from session to session)

---

## 📋 NEXT SESSION: PHYSICS SERVICE REVIEW

**Task:** Review the `timestd-physics.service` (implemented in `src/hf_timestd/core/physics_fusion_service.py` and its supporting modules) to ensure it implements theoretical and methodological objectives properly, efficiently, and without errors. Evaluate whether we are neglecting "low-hanging fruit" that the quality of our measurements and data bring into reach, and ensure we are exposing the insights and implications of our measurements appropriately in the web-api and living documentation.

---

## System Context

- **System:** hf-timestd v6.8.0 (February 27, 2026) — multi-broadcast HF time transfer and ionospheric measurement
- **Focus Area:** Phase 3 Physics (L3 Data Products)
- **Primary Source File:** `src/hf_timestd/core/physics_fusion_service.py`
- **Supporting Files:** `src/hf_timestd/core/tec_estimator.py`, `carrier_tec.py`, `gnss_tec.py`, `ionospheric_model.py`
- **Output:** `/var/lib/timestd/phase2/fusion/` (dTEC, TEC group delay, propagation modes)

---

## 1. Physics Service Objectives

The Physics Fusion Service consumes L2 Timing Measurements (from all channels) and performs physics-based fusion to derive ionospheric parameters. Its goals are:

1. **Ionospheric Parameters (Primary Output):**
   - Carrier-phase differential TEC (dTEC) anchored by GNSS VTEC.
   - Ionospheric Layer Virtual Height via triangulation (ToF cluster analysis).
   - Group-delay Absolute TEC (1/f² dispersion fit across frequencies).

2. **Validation Metrics (Secondary Output):**
   - UTC Consistency: "Does the physics model explain the observations?"
   - Clock Error Bounds: Residuals after ionospheric correction.

## 2. Review Criteria for the Next Session

Your critique in the upcoming session must focus on the following three pillars:

### 2.1 Theoretical and Methodological Correctness
- **Are the physics equations implemented correctly?** (e.g., $d\phi/dt$ to Doppler to dTEC integration, 1/f² dispersion fits).
- **Are we handling ambiguities and phase wrapping properly?**
- **Is the integration of GNSS VTEC as an anchor for relative carrier-phase dTEC methodologically sound and statistically rigorous?**
- **Are there any subtle bugs, off-by-one errors, or incorrect unit conversions in the math?** (e.g., mixing radians and cycles, or Hz and MHz).
- **Is the service resilient to missing data, noisy channels, or outlier measurements?**

### 2.2 Unlocking "Low-Hanging Fruit"
- **What scientific insights are we currently recording but failing to extract?**
- **Can we better utilize the multi-path nature of our 17 broadcasts?** (e.g., real-time TID velocity/direction correlation across intersecting paths).
- **Are there derived indices (like S4, $\sigma_\phi$, or FSS) that could be computed easily in this service without heavy refactoring?**
- **Are we properly identifying and flagging space weather events (solar flares, geomagnetic storms, sudden ionospheric disturbances)?**

### 2.3 Web-API and Living Documentation Exposition
- **Are the L3 physics products adequately exposed via the `web-api`?**
- **Do the dashboards (e.g., dTEC, Ionogram, Phase) tell a coherent scientific story?**
- **Are we capturing the right logs for the Living Documentation to demonstrate "theory meeting reality"?**
- **How can we make the invisible ionosphere more visible and understandable to the end user?**

---

## 3. Key Components to Analyze

When reviewing the code, pay special attention to:

- `PhysicsFusionService.process_minute()`: The main loop where L2 data is aggregated and L3 products are generated.
- `TECEstimator`: The Bayesian multi-frequency TEC estimator (group delay).
- The `dTEC` integration pipeline: How Doppler shift is converted to carrier-phase dTEC and anchored to `gnss_vtec`.
- The inter-frequency differential dTEC check (P3-C).

## 4. Current Known Limitations (To Be Evaluated)

- Group-delay absolute TEC is noisy (SNR ~0.13) due to mode-mixing and limited frequency diversity.
- The distinction between absolute group-delay TEC and relative carrier-phase dTEC needs careful handling in both the code and the UI.
- The service runs asynchronously and must handle late-arriving or out-of-order L2 data gracefully.

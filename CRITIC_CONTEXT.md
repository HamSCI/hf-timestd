# CRITIC CONTEXT - Pipeline Hardening & Resilience

**DO NOT ALTER THIS HEADER. This file is for AI agent context between sessions.**

---

## Session Date: 2026-01-06 (Post-Fusion Fix)

## Primary Objective for Next Session

**HARDENING & SIMPLIFICATION (RTP → Chrony)**

**Goal**: Ideally architect the pipeline to be as theoretically and methodologically sound as possible. It must be resilient to changing conditions (propagation shifts, ionospheric storms) and robust to the presence or absence of auxiliary data (GNSS VTEC, IONEX).

**Key Focus Areas**:

1. **Theoretical Soundness**: Verify that the uncertainty budget (ISO GUM) is rigorous and does not double-count errors.
2. **Resilience**: Ensure the system degrades gracefully. If VTEC is missing, it should fall back to physics models without crashing or producing discontinuous jumps.
3. **Simplification**: Remove fragility. The interaction between "fallback" values (0.0/NaN) and "measured" values should be explicit and typed, not implicit.

---

## Current System Status

### Services

- ✅ **Analytics**: Running stable. Calibration loop restored. Writing `NaN` for non-detections.
- ✅ **Fusion**: Running stable. Robust against `NaN` inputs. Feeding Chrony.
- ✅ **Chrony Feed**: Active (`Reach` > 0, `LastRx` updating).

### Recent Fixes (Context for Hardening)

1. **NaN Handling**: We just moved from "implicit zero" to "explicit NaN" for missing data. The hardening phase should enforce this pattern strictness.
2. **Dataclass Integrity**: We fixed a missing field in `TransmissionTimeSolution`. Future refactoring should use Pydantic validators to prevent this class of error.
3. **Filter Logic**: We patched `_reject_outliers` to handle `NaN`. A more sound approach might be to use a proper statistical filter that naturally handles missing data (e.g., Kalman filter with variable measurement matrix).

---

## Strategic Questions for the Critic

1. **Uncertainty Propagation**: Are we correctly propagating the uncertainty from L2 (Tone Detection SNR) -> L3 (Fusion)? Or is Fusion re-calculating uncertainty from scratch?
2. **Fallback vs. Model**: When we use a fallback model (e.g., predicted propagation delay), do we correctly inflate the uncertainty to reflect that it is a *prediction*, not a *measurement*?
3. **Mode Identification**: Is the `TransmissionTimeSolver` robust enough to trust its mode identification (`1F` vs `2F`) for automated hardening, or do we need a "Mode Ambiguity" uncertainty term?

---

## Known Artifacts

- **Verification**: `walkthrough.md` (contains verification of the recent fix).
- **Summary**: `FUSION_FIX_SUMMARY_2026-01-06.md` (detailed root cause of the previous instability).

**END OF CONTEXT**

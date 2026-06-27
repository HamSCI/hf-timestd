# HF-TimeStd: Metrological Description

**Prepared for:** Time metrology professionals, "time nuts", and general users  
**System Version:** 7.0.0 (canonical: `pyproject.toml`)  
**Last Updated:** 2026-05-24  
**Author:** Michael James Hauan (AC0G)

> **Foundational principles**: see
> [ARCHITECTURE-FIRST-PRINCIPLES.md](ARCHITECTURE-FIRST-PRINCIPLES.md).
> The metrological story rests on the RTP sample counter as substrate.
> The T-tier hierarchy described in §4.5 of this doc grades the
> *annotation* on top of that substrate.  Chrony SHM feeds (mentioned
> below) are one *consumer* of the annotation, not the architectural
> goal.

---

## 1. Executive Summary

**hf-timestd** is a dual-purpose HF time transfer and ionospheric measurement system. It receives WWV/WWVH/CHU/BPM time signal broadcasts via a GPSDO-disciplined SDR and operates in two complementary modes:

**RTP Mode (Physics Pathway):** With GPS+PPS providing authoritative timing (~50 μs accuracy via radiod's RTP timestamps), the system uses the known transmission times and measured arrival times to **study the ionosphere**. The propagation delay residuals reveal carrier-phase differential TEC (dTEC, the primary ionospheric product, anchored by GNSS VTEC), traveling ionospheric disturbances (TIDs), and space weather effects.

**Fusion Mode (Metrology Pathway):** The system attempts to **recover UTC from the HF broadcasts alone**, using multi-broadcast fusion to solve for the local clock offset. This pathway demonstrates how closely tone analysis can reconstruct the timing authority that RTP mode provides directly. Measured accuracy: ±0.5 ms (1σ, conservative claim) **when multi-station fusion is locked and converged** (with GNSS VTEC correction); ±2–5 ms without ionospheric correction. Fusion is not always available — T3 is gated on a fresh `fusion_status.json`, and fusion can be (and currently is, in the field) unavailable, in which case the host falls back to a higher tier (T5/T6) or coasts. See Section 13 for the full error budget.

These two modes are operational shortcuts over a finer-grained **timing authority hierarchy** (T-levels) described in §4.5, which governs how the system selects, cross-checks, and falls back between UTC sources at runtime.

The system demonstrates metrological rigor through:

- **Authoritative RTP timestamps** from GPS+PPS-disciplined radiod (no pipeline offset correction needed)
- **TickEdgeDetector unified pipeline** — single source for D_clock (AM-domain front-edge ensemble), Doppler (carrier phase slope), and SNR (per-tick matched filter)
- **Real-time ionospheric propagation model** (WAM-IPE + GIRO + IRI-2020 fallback)
- **ISO GUM-compliant uncertainty budgets** with full traceability
- **Kalman-filtered fusion** with inverse variance weighting
- **Chrony SHM integration** for system clock discipline

**The Core Value Proposition:** The system serves dual purposes. In RTP mode, the ~50 μs timing accuracy from GPS+PPS enables precision ionospheric science — the HF propagation delays become the measurement, not the error. In fusion mode, the system demonstrates HF time transfer capability, using software complexity and ionospheric physics to recover UTC from broadcast signals alone.

This system treats timing as a **measurement problem** with proper uncertainty quantification, systematic error correction, and validation against physical constraints.

---

## 2. The Measurement Problem

### 2.1 What We Actually Measure

The system measures **D_clock**, the offset between the local system clock and UTC(NIST):

```
D_clock = T_system - T_UTC(NIST)
```

This is extracted from HF time signal broadcasts by solving the **transmission time equation**:

```
T_arrival = T_emission + τ_propagation + D_clock
```

Where:

- **T_arrival**: Measured tone arrival time (RTP timestamp from GPSDO-disciplined SDR)
- **T_emission**: Known transmission time (top of minute, UTC)
- **τ_propagation**: Ionospheric path delay (2-70 ms, station-dependent)
- **D_clock**: The unknown we solve for

### 2.2 The Central Challenge

The difficulty is that **τ_propagation is not constant**:

- Varies with ionospheric mode (1F, 2F, 3F hops)
- Changes diurnally (day/night propagation)
- Affected by space weather (geomagnetic storms, solar flares)
- Multipath creates 0-5 ms delay spread
- Different for each station/frequency combination

Single-broadcast methods achieve only **±5-10 ms** due to mode ambiguity. Our multi-broadcast fusion approach achieves **±0.5 ms** by:

1. **Calibrating** systematic propagation biases per station
2. **Fusing** 9-17 independent measurements with inverse variance weighting
3. **Validating** against physical constraints (cross-station consistency, continuity)

---

## 3. "Steel Ruler" Philosophy

### 3.1 Core Principle

The **"Steel Ruler"** philosophy asserts that a GPSDO provides a rigid "ruler" (precise tick rate/frequency), but this ruler is floating in time. The system's job is not to straighten the ruler (the GPSDO does that), but to use ionospheric physics to **pin the ruler's zero-point to UTC**.

In a GPSDO-disciplined system, the local clock is significantly more stable (sub-ppb stability) than the ionosphere (10-100 ppb equivalent jitter). Therefore, we must:

1. **Trust the local clock** (zero process noise)
2. **Attribute all residuals** to ionospheric path variation
3. **Clamp long-term drift** to 0.0, as the GPSDO prevents accumulation

**The Hardware Hierarchy:**

| Hardware | Frequency (Slope) | Time (Offset) | Cost |
|----------|-------------------|---------------|------|
| **GPSDO** | Excellent | Drifting (if undisciplined) | ~$162-400 |
| **Cesium Beam** | Excellent | Excellent | ~$30,000+ |
| **GPSDO + hf-timestd** | Excellent | ±0.5 ms | Software |

This system bridges the gap between a common GPSDO (which provides excellent *frequency*) and a laboratory Cesium Standard (which provides absolute *time*), using the ionosphere as the correction mechanism.

### 3.2 The Three-Layer Metrological Architecture

Understanding the fundamental difference between **Frequency Stability** (Slope) and **Time Accuracy** (Offset) is essential:

#### Layer 1: Single Broadcast — "The Floating Ruler"

A single broadcast (e.g., WWV 15 MHz) provides:

- **Capability**: Measures the stability of local clock's *tick rate* relative to the transmitter's *tick rate*
- **Limitation**: NOT anchored to UTC — the signal always arrives late by the propagation delay (τ)
- **Result**: If averaged for a year, you get a perfect line with the correct slope (frequency), but the line is shifted vertically by the average propagation delay (e.g., +8 ms)
- **You know**: *How fast* time is passing, but not *what time it is*

#### Layer 2: Single Station, Multiple Frequencies — "The Dispersion Anchor"

By adding multiple frequencies (e.g., WWV 5, 10, 15 MHz), you unlock the **dispersion calculation**:

- **Physics**: Lower frequencies are delayed MORE by the ionosphere than higher ones
- **Mechanism**: This difference allows calculation of **Total Electron Content (TEC)** along that specific path
- **Gain**: Once TEC is known, the ionospheric delay (τ_iono) can be calculated and subtracted
- **Result**: Characterizes the **Path Physics**, moves the "Floating Ruler" closer to the true UTC line

#### Layer 3: Multiple Stations (17 Broadcasts) — "The Geometry Lock"

By adding geography (WWV vs WWVH vs CHU vs BPM), you sound the ionosphere from different angles and reflection points:

- **Physics**: Different stations probe different ionospheric regions and paths
- **Gain**: Cancels localized anomalies ("Weather") — solar flares affect paths differently depending on sun angle
- **Result**: "Triangulates" the ionosphere globally, provides **Integrity** (validation)

### 3.3 Summary Table

| Component | Provides | Function |
|-----------|----------|----------|
| **GPSDO** | Slope (Rate) | Ensures the ruler is straight and rigid |
| **Multi-Frequency Dispersion** | Vertical Shift (Path Delay) | Calibrates the zero-point for each station |
| **Multi-Station Fusion** | Integrity (Validation) | Ensures the zero-point is consistent across the hemisphere |

**Key Insight**: The combined regression of 17 broadcasts doesn't just average noise — it **solves the geometry** of the ionosphere to find the true UTC origin point.

---

## 4. System Architecture

### 4.1 The Eight Services

The system is composed of eight independent systemd services:

| Service | Responsibility | Output |
|---------|---------------|--------|
| **timestd-core-recorder** | Reliable Data Capture | `/var/lib/timestd/raw_buffer/` |
| **timestd-metrology** | Signal Processing & Timing Extraction | `/var/lib/timestd/phase2/{CHANNEL}/` |
| **timestd-l2-calibration** | Geometric + Ionospheric Corrections | L2 calibrated timing (HDF5) |
| **timestd-fusion** | Multi-Broadcast Synthesis | `/var/lib/timestd/phase2/fusion/` + Chrony SHM |
| **timestd-vtec** | Ionospheric Data Acquisition | `/var/lib/timestd/data/gnss_vtec/GNSS_gnss_vtec_YYYYMMDD.h5`, `/var/lib/timestd/ionex/` |
| **timestd-physics** | Carrier-phase dTEC, group-delay TEC validation, T_iono | `/var/lib/timestd/phase2/science/tec/` |
| **timestd-web-api** | User Visualization & System API | Port 8000 |
| **timestd-radiod-monitor** | Hardware Health Monitoring | Alerts on failure |

### 4.2 Three-Phase Pipeline

```
Phase 1: Core Recorder (Immutable Archive)
  ↓ Binary IQ (.bin.zst) + JSON sidecars (24 kHz IQ)
Phase 2: Analytics (Timing Extraction)
  ↓ HDF5 L2 Timing Measurements
Phase 3: Fusion (Multi-Broadcast Synthesis)
  ↓ Chrony SHM (System Clock Discipline)
```

**Metrological Advantages:**

- **Phase 1 never drops data** during Phase 2 updates
- **Reprocessability**: Improve algorithms without re-recording
- **Independent validation**: Test analytics on archived data
- **HDF5 SWMR**: Writer keeps file open (`swmr_mode=True`), flushes after each append; readers use `swmr=True`. `h5clear -s` on every writer open handles crash recovery automatically.

### 4.3 RTP Timestamp as Authoritative Reference

**Critical Design Decision:** System time is **derived from RTP timestamps**, not vice versa.

```python
utc = gps_time_unix + (rtp_ts - rtp_timesnap) / sample_rate
```

radiod's `GPS_TIME` and `RTP_TIMESNAP` are both derived from `input_sample_index / decimation` — they are in the same counter space. No pipeline offset correction is needed. The timestamps are authoritative, providing ~50 μs accuracy to UTC via GPS+PPS.

**Why this matters:**

- **Sample count integrity**: Gaps are unambiguous (RTP timestamp jumps)
- **No time stretching**: Never adjust sample count to fit wall clock
- **Authoritative timing**: RTP timestamps from ka9q-radio carry GPS+PPS time through the decimation pipeline
- **No calibration needed**: GPS_TIME/RTP_TIMESNAP mapping is direct — no wall-clock calibration bias
- **Reprocessable**: Raw data can be reanalyzed with improved algorithms

**Prerequisites for "authoritative"**: the ~50 μs claim presumes the ADC is GPSDO-disciplined (A-level A1 in §4.5) and RTP_TIMESNAP is fresh. Under A0 (free-running TCXO) the RTP tick rate drifts at ~±5 ppm, and under a stale RTP_TIMESNAP the origin carries whatever system-clock error existed at the last snapshot. In both cases RTP timestamps remain *sample-accurate* (gaps and sequencing are still unambiguous) but are no longer UTC-authoritative at the μs scale. The T-level hierarchy in §4.5 makes this explicit.

### 4.4 Data Levels

| Level | Description | Content |
|-------|-------------|---------|
| **L0** | Binary IQ Archive | Raw IQ samples (.bin.zst + JSON sidecars) |
| **L1A** | Tone Detections | Channel observables, SNR, BCD |
| **L1B** | BCD Timecode | Decoded time information |
| **L2** | Timing Measurements | D_clock + ISO GUM uncertainty |
| **L3** | Fused Timing | Kalman filtered, multi-broadcast |

### 4.5 Timing Authority Hierarchy (A- and T-Levels)

> **Implementation-status legend** (matches the markers used in
> `docs/PHYSICS.md`):
> - ✅ Operational in the deployed code today.
> - ⚠️ Partial: the building blocks exist but some pieces of the policy / wiring described here are not yet running.
> - ❌ Designed but not implemented; the schema/policy is recorded here as the contract a future implementation must satisfy.
>
> Status of the §4.5 / §4.6 content as of 2026-05-24:
>
> | Component | Status | Notes |
> |---|---|---|
> | A-level (ADC timebase) ranking | ✅ | Reported by `radiod` / `core_recorder`. |
> | T0–T2, T4 levels (chrony/NTP-based) | ✅ | Conventional chrony plumbing; behaviour matches the table. |
> | T3 (Fusion HF-derived UTC) | ✅ | Produced by `multi_broadcast_fusion`. **Availability is gated on a fresh `fusion_status.json`**; fusion can be (and currently is, in the field) unavailable, in which case T3 is not offered and the manager selects a higher tier (T5/T6) or coasts. The ±0.5 ms (1σ) figure holds only when multi-station fusion is locked and converged. |
> | T5 (USB-delivered GPS+PPS) | ✅ | LBE-1421 USB-NMEA is consumed by hf-timestd for second-of-day disambiguation (alongside T6) and as the standalone source when T6 is unavailable. Precision is USB-bus-jitter floored at µs-to-ms class. **Upgrade path:** wire the TS-1 PPS OUT jack to a host GPIO / RS232 input with kernel PPS-API support — that adds a *second* ns-class path alongside T6 (for continuous §8 chain-delay cross-validation), but does not promote T5 itself, since the T5 definition is the USB transport. |
> | **T6 BPSK-PPS injection / detection** | ✅ | TS-1 HF-injected BPSK PPS coupled into the RX path, decoded sample-precise from the IQ stream (HPPS matched-filter; the HFPS diff calibrator is wired but disabled by default). Live on bee1. The chrony-facade calibration has known weaknesses (one-shot disambig is sensitive to host-clock state at calibration moment — see [TIMING-PIPELINE-WIRING.md](TIMING-PIPELINE-WIRING.md) and the chrony-tuning notes); the **annotation product** (per-sample tier + offset + uncertainty) is operational and is the deployed best tier. |
> | Authority manager + `/run/hf-timestd/authority.json` v1 | ✅ | `AuthoritySnapshotStore` + `AuthorityManager` live; per-cycle records persisted to `/var/lib/timestd/authority_history.db`. |
> | `chronyc selectopts` runtime gating | ✅ | `ChronyRefclockGate` is wired into the AuthorityRunner and invoked every tick (`AuthorityManager._apply_chrony_gate`); enabled per `[timing.authority_manager.chrony_gate]` (`dry_run` available for staged rollout). |
> | mDNS TXT-record extension | ✅ | `MdnsFusionAdvertiser` is wired into the AuthorityRunner and applied every tick (`AuthorityManager._apply_mdns_advertiser`); enabled per `[timing.authority_manager.mdns]` (`dry_run` logs the TXT without forking avahi). |
>
> Treat the rest of §4.5 / §4.6 as the **contract** an authority
> manager must satisfy when it's built, not as a description of
> live behaviour.

> **Note:** The T-level hierarchy defined here is a **distinct axis** from the data-product levels L0–L3 in §4.4. L-levels describe *what a product is*. T-levels describe *how well the system knows UTC* at a given moment. A host has exactly one active T-level; it produces L0–L3 data products at whatever T-level is currently active. The two hierarchies are orthogonal.

A hf-timestd host's UTC alignment has two independent axes: the **ADC timebase** (hardware property) and the **UTC alignment source** (how RTP timestamps map to wall-clock UTC). Each is ranked separately, and the authority manager selects among valid (A, T) combinations at runtime.

#### Axis A — ADC Timebase

| Level | Hardware | Effect on RTP timestamps |
|-------|----------|-------------------------|
| **A1** | RX888 ADC governed by external GPSDO | Rate is GPS-locked (ppb stability); RTP tick spacing is authoritative |
| **A0** | ADC free-running on local TCXO | Rate drifts at ~±5 ppm; RTP tick spacing unreliable over long windows |

A1 is a hard prerequisite for T5 and T1. For other T-levels it is a quality multiplier, not a gate — hf-timestd can produce useful Fusion output even under A0.

#### Axis T — UTC Alignment Source

Ranked from highest authority (most accurate, most independent of external state) to lowest:

| T-level | Source | Hard prereq | (A1, T) uncertainty | (A0, T) uncertainty |
|---|---|---|---|---|
| **T6** | hf-timestd detects TS-1 HF-injected BPSK-PPS in the RX path (sample-precise from the IQ stream) | TS-1 injector present + detection lock + §8 chain-delay calibrated | ~ns (post-§8) | ~tens of μs (per-tick; drifts between ticks at TCXO rate) |
| **T5** | GPS+PPS delivered over USB to the radiod host (LBE-1421 USB-NMEA, optionally USB-PPS on the same channel); consumed for second-of-day disambig under T6, or as the standalone source when T6 is unavailable | A1 + LBE-1421 USB connected to host | ~µs–few ms (USB-bus-jitter floored) | *not available* |
| **T4** | system clock chronyed to LAN GPS+PPS timeserver via NTP | reachable GPS-backed peer | ~100 μs – few ms | ~1–5 ms (adds TCXO drift between syncs) |
| **T3** | hf-timestd recovers UTC from WWV/WWVH/CHU tick Fusion | ≥2 stations detected + ionospheric model | ~0.5–2 ms | ~5–10 ms |
| **T2** | system clock chronyed to public NTP via WAN | internet reachability; stratum ≤3 | ~1–50 ms | ~5–50 ms (NTP dominates; TCXO negligible at this scale) |
| **T1** | A1 only — ADC rate locked but no UTC discipline beyond last RTP_TIMESNAP | A1 | const offset at snapshot + 0 drift | *not available* |
| **T0** | free-running system clock, no GPSDO | none | *not available* | unbounded |

"Not available" entries are structural: (A0, T5) and (A0, T1) are by-definition invalid — those T-levels are *defined* by GPSDO presence. (A1, T0) collapses to T1, because A1 alone is a timing authority worth naming.

#### The RTP-reference labeling invariant

Data products across the HamSCI suite — hf-timestd's own services, wspr-recorder, psk-recorder, and future clients — label samples using **RTP time from radiod as the authoritative reference**, optionally corrected by a published Fusion offset:

```
label_utc = rtp_time + rtp_to_utc_offset_ns
```

This is a hard invariant for **data-label production** and for the T6 anchor-offset publication: clients do **not** consult their own system clock for data labeling, and chrony is not load-bearing for data labels. Note the scope: the absolute "feed = rtp_time + offset, never the system clock" framing applies to those label/publication paths. The *legacy* Fusion→chrony SHM (`FUSE`) discipline path is different — for clock STEERING it feeds `system_time − D_clock` into chrony's SHM, because that path's job is to correct the host system clock, not to label data. The invariant below governs the data-label path. The rationale is multi-host coherence: a single radiod serves RTP to many client hosts, each of which may have its own chrony state and drift independently. Labeling with RTP-time plus a shared offset means all clients agree on labels by construction, regardless of per-host clock state. The 2026-04-20 incident — system clock drift of ~107 s mislabeling WSPR WAV files — is structurally impossible under this invariant, because system-clock drift does not propagate into labels.

**The offset is always applied, regardless of T-level.** Its magnitude and uncertainty tell the provenance story; the client code path is uniform:

- **T5, T4**: radiod's RTP_TIMESNAP is derived from a GPS-disciplined system clock (USB-delivered GPS+PPS for T5, LAN GPS+PPS via NTP for T4). RTP-time is inherently µs- to ms-accurate UTC. The published offset is near zero with µs/ms uncertainty; applying it is a no-op.
- **T6**: hf-timestd's detection of TS-1 HF-injected BPSK-PPS in the RX path produces an ns-level (post-§8) RTP→UTC offset **independent of radiod's host clock source**. Measured near-zero if radiod already has GPS-disciplined system time (cross-check confirmation), non-zero if it does not (active correction). Supersedes RTP-inherent accuracy at the ns level either way.
- **T3–T0**: hf-timestd's HF time-station tick analysis produces an offset with ms–seconds uncertainty. RTP origin is limited by radiod's available reference (stale NTP, free-running TCXO, or no reference at all), and the Fusion offset is the primary UTC correction.

Because the offset is applied uniformly, no client branches on T-level. The authority manager publishes `(offset, σ, T_level, …)` as one tuple; clients read and apply. T-level is provenance metadata for sidecar recording and operator surfacing, not a control flow gate in consumer code.

At T0 with no HF signals reaching the receiver and no other source, the published offset may be unavailable. Clients then record RTP-time only and stamp the sidecar with an explicit `no_utc_alignment_available` flag. They do **not** substitute the system clock.

##### Multi-radiod stations — one governor radiod

At stations with more than one radiod, "RTP time" is ambiguous — each radiod has its own `RTP_TIMESNAP` anchor derived from its own host's clock at snapshot time. The Fusion offset is computed from one specific radiod: the one hf-timestd reads IQ from. That radiod is the **governor**, and the published offset is relative to its RTP timebase.

Clients that subscribe to the governor radiod apply the offset directly. Clients that subscribe to a *different* radiod on the same station still apply the offset, but inherit the clock-skew between radiod hosts as additional uncertainty. The governor's identifier — `[ka9q].status_address` from hf-timestd's own config — is published in `authority.json` as `governor_radiod` so consumers can record it in sidecars alongside their own `client_radiod`, making the pair traceable through later analysis.

**Operator assumption**: multi-radiod stations should have all radiod hosts chrony-synced to a shared time source (typically the station's GPS timeserver) so `delta_host_clocks` stays within ms. This is standard station practice; the invariant as stated holds under that assumption and degrades gracefully (with added ms-level uncertainty) as host-clock synchrony degrades. Radiod-host timing is the operator's responsibility — outside sigmond's scope.

#### T-Level Classification

**T6 and T3 share an architecture.** Both are hf-timestd's payload-signal offset products — a known-timed signal is detected in the audio and correlated against RTP time. They differ only in signal: T6's TS-1-injected BPSK-PPS is a clean *local* signal with no propagation-medium variability, only the (calibrable, §8) analog chain delay (ns-class once §8 is locked); T3's *received* multi-hop HF ticks have larger, partially-modeled ionospheric delay (~ms). Both are independent of the system clock entirely and survive arbitrary system-clock drift as long as A holds.

**T5 / T4 / T2 are system-clock disciplines.** They align the system clock itself, which hf-timestd then trusts as a proxy for UTC. T5 (USB-delivered LBE-1421 GPS+PPS) is the lowest-jitter of the three but is still bounded by USB bus scheduling, putting it well below T6's RF/ADC path in absolute precision. Silent failure here — peer unreachable, drift accumulating — only becomes visible when something external cross-checks. This is the failure mode behind the 2026-04-20 incident, where T4 (192.168.1.80) became unreachable after a DHCP reassignment and the system drifted ~107 s over ~32 hours without alarm.

**T1 is a degraded holdover, not a steady operating point.** When T2 and above all fail but A1 still holds, RTP timestamps remain rate-accurate; their UTC origin is whatever RTP_TIMESNAP was at the last good sync, with no drift (because A1 is perfect rate-wise). T1 is the "coasting on the GPSDO" state.

**T0 is terminal.** No GPSDO, no NTP, no Fusion. Data products produced at T0 must be marked as such and excluded from shared products (fusion input, SHM output, science archives).

#### Selection, Cross-Check, and Transition Rules

The authority manager selects the **highest available** T-level per decision period. "Available" means the level's health probe has passed within the probe window:

- **T6 probe**: hf-timestd reports ≥ N BPSK-PPS detections in the last minute with q95 < threshold.
- **T5/T4/T2 probe**: `chronyc -n -c sources` shows the relevant source healthy (state `*`/`+`) **and** with `reach` ≠ 0 (a healthy state on a zero reach register is a transient/bug and is rejected). An optional per-tier `max_error_ms` ceiling rejects a source whose last-sample error margin exceeds the tier limit ("RMS within tier limit"); it is **off by default**, because the cross-check layer (the σ-widening / `TIMING_DISAGREEMENT` path above) already catches a witness whose offset has drifted — so "offset stable" is adjudicated there rather than in this single-sample probe, and an unconditional ceiling would only risk dropping noisy-but-usable witnesses and shrinking cross-check coverage.
- **T3 probe**: hf-timestd reports ≥ 2 stations with tick detections in the last minute and Kalman innovation within bounds. Zero detections → T3 unavailable (the Saturday failure's missing alarm).
- **T1 probe**: A1 present AND last RTP_TIMESNAP within freshness window.

When multiple levels are simultaneously healthy, the higher wins as **active** and the lower remain **witnesses** for cross-check. Disagreement between active and a witness beyond threshold raises `TIMING_DISAGREEMENT`. The response is graded — a single noisy lower witness must **not** demote a higher-precision tier on its own say-so:

- **Single witness disagrees** → raise `TIMING_DISAGREEMENT`, keep the active tier, but **widen the published `sigma_ns`** to cover the discrepancy. The offset is still served (the higher tier is the more likely-correct one), but at honestly-reduced confidence so no consumer trusts a contested value at full precision.
- **Majority (≥ 2 witnesses) agree with each other and disagree with active** → active is the outlier; **downgrade** to the highest-ranked agreeing witness.
- **Asymmetric T3 ↔ T2 gross delta** (> 1 s) → Fusion being wildly wrong vs WAN NTP is a hardware/detection bug; force T3 down regardless of the normal cross-check math.

A resolved downgrade needs no uncertainty widening — the adjudicated tier is trusted. Thresholds are per-pair floors on the `3·√(σ_a² + σ_b²)` combined CI (subject to empirical tuning on live data):

| Pair | Threshold | Rationale |
|---|---|---|
| T6 ↔ T5 | 5 ms | T6 is ns-class; T5 is µs-to-ms-class (USB jitter floored). Threshold sized to T5's combined uncertainty — alarms only on disagreement beyond what USB transport explains. |
| T3 ↔ T4 | 2 ms | T3 worst-case meets T4 typical |
| T3 ↔ T2 | 5 ms | T2 NTP-level tolerance |

**Two T6 quantities — what is cross-checked vs what is published.** T6 (and T5-direct) carry *two distinct, both anchor-relative* numbers, and the manager uses each for a different purpose:

- The **cross-check witness quantity** is the anchor's absolute UTC error vs an independent truth — for T6 the sub-second residual of the anchor-predicted PPS firing against the integer second a real BPSK-PPS fires on (`core_recorder`'s `local_minus_source_ns`, despite the legacy name a purely anchor-derived value, *not* a system-clock reading); for T5-direct the anchor-vs-NMEA-GPS disagreement (`anchor_offset_ns`). Because both are anchor-vs-truth residuals, the **T6 ↔ T5 comparison is like-for-like** — that is why this pair is the load-bearing cross-check.
- The **published** offset (`rtp_to_utc_offset_ns`) is a *different* anchor-relative number: the anchor-vs-host-clock bridge consumers apply to convert RTP → UTC (`anchor_utc_ns − rtp_to_wallclock(anchor_rtp)`). It can be large (the host clock is far from the anchor) while the cross-check residual is sub-µs (the anchor is dead-on PPS truth). The published `sigma_ns` (`max(MF jitter, |residual|, floor)`) bounds the anchor's absolute UTC error, which is precisely the residual label error left after applying the published offset — so the σ is honest for the published value even though it is derived from the cross-check residual.

Consequence for the witness math (frame-aware cross-check): each probe tags its offset with a **reference frame** — `rtp` (anchor-vs-truth, measured in the RTP stream, system-clock-independent: **T6, T5-direct, and T3/Fusion**, which share the same payload-signal-vs-RTP architecture) or `sysclock` (chrony's `local_clock − source`: **T5-via-refclock, T4, T2**). The chrony (`sysclock`) offsets are commensurate with an `rtp`-frame active tier's residual only while the SHM feed keeps the system clock disciplined to the anchor. So when a **GPS-disciplined `rtp` tier (T6/T5)** is active, a `sysclock` witness that disagrees is treated as **advisory only** — surfaced (with an `:advisory` flag suffix) but it does **not** widen the published `sigma_ns` or contribute to a downgrade, because the difference then reflects system-clock drift rather than an error in the published anchor offset. Same-frame (`rtp`) witnesses still fully cross-check T6/T5. **Fusion (T3) is deliberately *not* shielded** — it is `rtp`-frame but ms-class and fallible (it ranks below T4), so an agreeing `sysclock` majority can still downgrade a broken Fusion, which is the wanted safety net. The asymmetric T3 ↔ T2 gross-error rule (> 1 s) is likewise unaffected. The anchor-frame T6 ↔ T5 pair remains the primary arbiter.

Transitions are logged and stamped in sidecars:

```json
{"t_level_active": "T3", "a_level": "A1",
 "t_level_witnesses": ["T2"], "disagreement_ms": 0.8,
 "t_level_previous": "T4", "t_level_transition_utc": "2026-04-22T16:13:44Z"}
```

**Upgrade hysteresis:** a level must pass its probe for N consecutive windows (default N=3, ~3 min for one-minute windows) before it can become active, to prevent flapping.

**Downgrade is immediate:** a failed probe disables the level at the next authority decision. No hysteresis on failure — the whole point is to stop trusting a broken source as soon as we notice.

**Demote-on-breach (T6→T5), ON by default (Phase 2C).** Distinct from the cross-check above: when T6's own drift monitor reports a *sustained breach* (the RTP anchor has drifted far enough that T6's SHM feed would mislead chrony) for `demote_on_breach_min_cycles` consecutive ticks (default 3) **and** T5 is available past hysteresis, the manager hands the active cycle to T5 and stamps a `demote-on-breach:T6->T5` flag. This is the **default** as of the Phase 2C cutover; operators opt out with `[timing.authority_manager.t6] demote_on_breach = false` (which still maintains the breach counter for telemetry). The trigger is conservative — a *sustained* breach over `min_cycles` consecutive ticks, and only when T5 is healthy past hysteresis — and it composes with the cross-check σ-widening below (which independently degrades a *contested* T6 offset's confidence).

**Disagreement-flag vocabulary.** `disagreement_flags` in `authority.json` is a list of strings drawn from a fixed set; consumers should match by prefix:

| Flag | Meaning |
|---|---|
| `TIMING_DISAGREEMENT` | The active tier was kept despite an unresolved cross-check disagreement; the published `sigma_ns` has been widened to cover it (the canonical "trust this offset less" alarm). |
| `<A><->​<B>:<Δ>ms><thr>ms` | A pairwise cross-check between tiers A and B exceeded the threshold (e.g. `T6<->T5:6.0ms>5.0ms`). |
| `…:advisory` (suffix) | The disagreement is cross-frame against a GPS-disciplined rtp tier (T6/T5) — surfaced for operators but it did **not** widen sigma or drive a downgrade (system-clock drift, not an anchor error). See the frame note above. |
| `majority-downgrade:<from>-><to>` | ≥ 2 mutually-agreeing witnesses outvoted the active tier; it was demoted to the highest-ranked agreeing witness. |
| `asymmetric-T3-T2:<Δ>ms><thr>ms` | Fusion (T3) disagreed with WAN NTP (T2) by > 1 s — a gross detection bug; T3 was forced down. |
| `demote-on-breach:T6->T5` | T6's drift monitor reported a sustained breach and demote-on-breach is enabled (see above). |
| `chrony-rejected-<refid>:state=<s>` | chrony has rejected (falseticker/unselectable) the SHM segment hf-timestd feeds for the active tier (V7 self-feedback check). |
| `chrony-missing-<refid>` | the SHM refid hf-timestd feeds isn't present in chrony's source list (chrony not configured to consume it, or no first sample yet). Informational on hosts without the SHM refclock wired. |

#### Published Authority State (schema v1)

Every hf-timestd host continuously publishes its authority state at `/run/hf-timestd/authority.json`. This file is the single published contract between the authority manager and every consumer (sidecar writers, chrony SHM feeder, mDNS advertiser, wspr-recorder, psk-recorder, LAN peers, sigmond watchdog).

```json
{
  "schema": "v1",
  "utc_published": "2026-04-22T16:13:44.123456Z",
  "a_level": "A1",
  "t_level_active": "T3",
  "t_level_available": ["T3", "T2"],
  "t_level_witnesses": ["T2"],
  "rtp_to_utc_offset_ns": 812345,
  "sigma_ns": 940000,
  "stations_contributing": ["WWV", "CHU"],
  "last_transition_utc": "2026-04-22T16:13:44Z",
  "disagreement_flags": [],
  "governor_radiod": "bee1-hf-status.local"
}
```

`governor_radiod` is optional and additive within schema v1 — legacy consumers that don't know the field simply ignore it. When present it names the radiod whose RTP timebase the offset is computed against (see multi-radiod clarification in §4.5.1).

**Freshness rule.** Consumers treat the state as valid only if `utc_published` is within a freshness window (default 60 s). Beyond that, the offset is "unavailable" and clients fall back to RTP-time-only labeling with `authority_stale=true` stamped in the sidecar. No client ever substitutes the system clock for UTC labeling, even when the offset is stale.

**Atomic write.** The authority manager writes this file via write-to-temp + rename so consumers never observe a partial state.

**Single-writer / coupling rule.** The authority manager's main loop is the single gate for three outputs: (1) writing `authority.json`, (2) writing chrony SHM segments, (3) refreshing the mDNS advertisement subprocess. If the manager loop hangs or crashes, all three signals decay in lockstep:

- `authority.json` ages past its freshness window within 60 s → local consumers see "authority stale."
- Chrony SHM refclock stops receiving fresh timestamps → `chronyc sources` shows `reach=0` within ~5 polling intervals.
- mDNS advertisement is not refreshed → avahi ages the record out within ~120 s → LAN consumers see the service disappear.

Because sigmond's `smd lan-fusion-watch` polls chrony reach and browses `_ntp._udp`, both of the cross-host signals naturally disappear when the manager hangs — there is no separate heartbeat file to maintain. The coupling rule guarantees that "signals look healthy" cannot diverge from "the manager is actually running."

**Schema versioning.** The `schema` field is gated at the consumer; unknown versions are treated as unavailable. Future additions bump the version; consumers gate on `schema ∈ {v1, v2, …}`.

#### Sidecar History

Each L-level sidecar records the full T-level history covering the data product's time range, not just the active level at file start. This preserves forensic traceability through mid-file transitions:

```json
{
  "authority_history": [
    {"utc": "2026-04-22T16:00:00Z", "a": "A1", "t": "T4", "q95_ms": 1.2},
    {"utc": "2026-04-22T16:13:44Z", "a": "A1", "t": "T3", "q95_ms": 0.8, "reason": "T4 peer unreachable"}
  ]
}
```

History is bulkier than the single-value encoding but essential for reprocessing — downstream analysis needs to know at what authority each sample was taken.

#### Relationship to "RTP Mode" and "Fusion Mode"

The §1 and §4.3 shortcuts map onto the T-level space — but with the RTP-reference invariant in force, "mode" is no longer about *whether* RTP or Fusion is trusted. RTP is always the label reference. What changes between modes is **whether the published Fusion offset is a no-op or an active correction**:

- **RTP Mode** ≈ (A1, T5) or (A1, T4). RTP-time is inherently μs/ms UTC because radiod's clock source is GPS-disciplined. The published Fusion offset is near zero with low uncertainty; applying it is a no-op. hf-timestd's focus in this mode is physics products (dTEC, TIDs) — timing is a solved problem and Fusion runs mainly as a cross-check witness.
- **Fusion Mode** ≈ (A1, T6) or (A0/A1, T3–T0). RTP origin is imperfect, missing, or drifting; the published Fusion offset carries real correction. hf-timestd's primary contribution in this mode is the offset itself. SHM output disciplines chrony as a secondary benefit, useful for logs and external NTP serving, but irrelevant to data labeling.

The `[timing] authority =` key in `timestd-config.toml` becomes a *preference* (which T-level to prefer when multiple are simultaneously available), not a hard mode switch. Runtime authority — and therefore whether the offset is a no-op or a correction — is determined by the manager's probes and cross-checks. `authority = "auto"` is the default: always pick the highest available level, let the uniform client contract handle the rest.

### 4.6 Relationship to Chrony and Standard NTP Practice

The authority hierarchy in §4.5 **extends** — it does not replace — the established chrony/NTP framework. Every mechanism we use is defined by existing standards (RFC 5905 for NTP, RFC 6763 for DNS-SD, chrony's standard `refclock SHM` driver). What's novel is the **separation of data-label authority from system-clock authority**, enforced by the RTP-reference invariant. This subsection maps our architecture to standard practice, names the chrony conventions we rely on, and declares the one application-level extension (mDNS TXT metadata schema).

#### Chrony's role — disciplined clock, not data label

Chrony retains its classical role of disciplining the host system clock from the best available time sources. It consumes:

- The Fusion-produced RTP→UTC offset via the standard **SHM refclock** driver (`FUSE` on segment 1, `HPPS` on segment 2 in the dual-feed configuration of §13.4; SHM 0 retired 2026-05-23).
- Configured NTP peers — WAN NTP, LAN GPS+PPS servers, other LAN hf-timestd hosts serving Fusion — via standard `server`/`peer` directives.

Chrony then:

- **Outputs** a disciplined system clock for host-level uses: syslog timestamps, UI wall-clock displays, cron, journald, anything outside the HamSCI suite's data pipeline.
- **Optionally serves** NTP to the LAN via the `allow` directive, at whatever stratum the upstream source quality implies.

**Chrony is not load-bearing for data labels.** Under the RTP-reference invariant (§4.5), every data-product timestamp is `rtp_time + rtp_to_utc_offset_ns`. Chrony may drift, fail, lose all sources, or be wildly wrong — data labels remain correct because they travel a different path. Chrony's health becomes an operational concern for the host, not a correctness concern for the science data.

This is the structural fix for the 2026-04-20 failure mode: when chrony lost all usable sources and the system clock drifted ~107 s, the wspr-recorder WAV labels followed. Under the RTP-reference invariant, the same chrony outage would leave data labels untouched — only the operator's wall clock and syslog timestamps would go wrong.

#### Host-clock-vs-UTC watchdog — `timestd-clock-monitor`

`timestd-clock-monitor.service`/`.timer` (commit 75b8a45) runs `scripts/check-clock-health.sh --auto-makestep` every 30 s (timer `OnCalendar` `*:*:00/30`), as root. It detects a free-running chrony (Reference ID `00000000`), a leap status other than Normal, a root dispersion > 0.5 s, or a reachable source disagreeing by > 1.0 s, then — when chrony has a selectable source — issues a cooldown-guarded `chronyc makestep` to step the system clock back onto UTC immediately.

This is the **host-clock-vs-UTC** watchdog, and is DISTINCT from `timestd-chrony-monitor`, which only watches the FUSE/HPPS SHM segments' reach (whether hf-timestd's own refclock feed is alive). `timestd-clock-monitor` was added after a 2026-06 field incident: the host's USB-GPS dropped off the bus and chrony free-ran ~6 s, darkening FT8/FT4 decoding on the co-hosted recorders. It guards the operator wall clock and syslog/journal timestamps — not the data labels, which remain correct via the RTP-reference invariant above.

#### hf-timestd as a LAN NTP server — standard mechanisms only

When authority.json reports T3 or T6 active and non-stale, an hf-timestd host functions as a standard NTP server for its LAN, using entirely unextended mechanisms:

- **Chrony SHM refclock** driver — RFC-compatible, unchanged from existing hf-timestd installations.
- **`allow <cidr>`** directive (RFC 5905 / chrony-standard NTP serving). Installed as a drop-in at `/etc/chrony/conf.d/10-hf-timestd-serve.conf`; activated at runtime via `chronyc allow` without requiring a chrony restart (per §4.5's LAN NTP service rules).
- **Standard UDP 123** for NTP queries. No custom clients, no protocol extensions.

From a consumer's perspective — including non-HamSCI hosts, hobbyist NTP clients, embedded devices — this is indistinguishable from any other NTP server whose upstream is a local reference clock. Classical NTP source-selection logic (Marzullo intersection + combine) applies unchanged.

#### Stratum, refid, precision — install-time convention; noselect — runtime-mutable

Chrony exposes runtime control of a source's **select policy** via `chronyc selectopts <refid> ±noselect` (and related flags). It does **not** expose runtime setters for `refid`, `stratum`, or the config-time `precision` — those are fixed per refclock line in chrony.conf at install time. The authority manager therefore splits the §4.6 policy into two layers:

**Install-time convention** (static per deployment — operator chooses once via the chrony.conf drop-in):

| Expected best T-level on this host | Recommended stratum | refid | Rationale |
|---|---|---|---|
| T6 (BPSK-PPS injection available) | 1 | `HPPS` | Tightest payload offset (BPSK-PPS, SHM 2); stratum 1 substantiated |
| T3 multi-station, A1 host | 1 | `FUSE` | Multi-broadcast Fusion (SHM 1) with GPSDO rate; ~ms |
| T3 only, A0 host | 2 | `FUSE` | Without GPSDO, claim one stratum lower |
| T1 coast (rare primary) | 3 | `FUSE` | Not authoritative; keep discoverable but visibly lower-ranked |

The live refids are 4-char ASCII `HPPS` (HF BPSK-PPS, SHM 2) and `FUSE` (HF multi-broadcast Fusion, SHM 1), per `config/chrony-timestd-refclocks.conf`. These are more informative than the legacy `TMGR` (see §7.3) when diagnosing with `chronyc sources -v`. The legacy SHM 0 raw-metrology feed (`TMGR`/`TSL1`) was retired 2026-05-23. The authority manager is refid-agnostic and reads the configured value.

**Per-sample precision** (dynamic, via the SHM segment): Fusion already publishes `precision_l1` / `precision_l2` per cycle based on the current uncertainty (see `multi_broadcast_fusion.py` SHM update logic). This reflects the authority's current quality without needing to restart chrony.

**Runtime gating** (authority-manager-driven, via `chronyc selectopts`):

| Active T-level | Gate action | Effect |
|---|---|---|
| T6 or T3 | `-noselect` | Refclock offered as upstream; may be used to discipline the local clock and served to LAN peers |
| T5 / T4 / T2 / T1 / T0 or no active | `+noselect` | Refclock visible in `chronyc sources` for diagnostics; not used for discipline and not served to LAN |

The gate fires only on T-level transitions — steady state makes no `chronyc` calls. This gives us the critical safety property from §4.5: **if Fusion breaks, we stop offering our refclock as an authoritative source within one authority cycle, regardless of the static stratum**. A Fusion host that has lost its HF signals cannot silently poison consumers on the LAN.

Dynamic stratum / refid mutation would require either multiple pre-configured refclock lines with different stratum values (switched via selectopts) or a chrony upstream feature that does not currently exist. Operators who want this behavior today can install multiple refclock lines (e.g., one at stratum 1 `HFSN` and one at stratum 2 `HFSN2` with the same SHM unit) and extend the gate to toggle between them; the current implementation supports a single refid and treats stratum as install-time.

#### mDNS advertisement — RFC 6763 with a documented TXT extension

Service discovery uses the IANA-registered `_ntp._udp` DNS-SD type (RFC 6763), advertised via `avahi-publish-service`. PTR and SRV records follow RFC 6763 exactly; any compliant mDNS client sees a standard NTP service.

RFC 6763 §6 permits application-defined `key=value` pairs in TXT records and there is no standard schema for `_ntp._udp` TXT. We define a versioned schema for HamSCI-aware consumers to prefer hf-timestd hosts based on Fusion quality:

```
schema=v1
source=fusion
host=<hostname>
A=A1
T=T3
q95_ms=0.8
stations=WWV,CHU
disagreement=none
```

- Consumers gate on `schema ∈ {v1, …}`; unknown versions treat the record as plain NTP.
- Non-HamSCI consumers ignore TXT entirely and use us as a standard NTP server. This graceful-degradation property is explicit.
- Future schema bumps add fields; old consumers continue to work.

#### What the authority manager does NOT do

To keep the scope clean and interoperability intact:

- **Does not replace chrony.** Chrony remains the system-clock disciplinarian and the NTP server implementation.
- **Does not replace NTP.** LAN time distribution uses standard NTP on UDP 123 between chrony peers.
- **Does not invent a service discovery protocol.** Standard `_ntp._udp` mDNS with documented TXT extensions.
- **Does not expose a new client-facing network API.** HamSCI consumers read `authority.json` locally (schema v1, §4.5); classical UNIX apps read the disciplined system clock from chrony; neither is a new interface.
- **Does not claim stratum it cannot substantiate.** Stratum is set per T-level per the table above; chrony's own source-selection handles the rest.

The authority manager is a **policy layer** on top of standard mechanisms. It chooses which T-level is active, keeps the chrony SHM refclock's stratum/refid/precision consistent with that choice, publishes the offset tuple for in-suite clients, and gates mDNS advertisements on live Fusion health. All underlying transport and discovery is standard.

---

## 5. Physics Models

### 5.1 Signal Reception (9 Channels, 17 Broadcasts)

**Configuration:**

- **WWV** (Fort Collins, CO): 2.5, 5, 10, 15, 20, 25 MHz (6 frequencies)
- **WWVH** (Kauai, HI): 2.5, 5, 10, 15 MHz (4 frequencies, shared with WWV)
- **CHU** (Ottawa, Canada): 3.33, 7.85, 14.67 MHz (3 frequencies)
- **BPM** (Pucheng, China): 2.5, 5, 10, 15 MHz (4 frequencies, experimental)

**Receiver:**

- **SDR**: ka9q-radio (Phil Karn's software-defined radio)
- **Reference**: GPSDO-disciplined sampling clock
- **Sample Rate**: 24 kHz IQ per channel
- **Format**: Binary IQ archive (.bin.zst + JSON metadata sidecars)

### 5.2 Tick Edge Detection (TickEdgeDetector — v6.8+)

**Primary Timing Source:**

The `TickEdgeDetector` is the single source for all three `tick_timing` observables:

- **D_clock** (ms): AM-domain front-edge ensemble timing, UTC-referenced via `buffer_timing`
- **Doppler** (Hz): Carrier phase slope across the minute from IQ-domain extraction
- **SNR** (dB): Per-tick matched filter signal-to-noise ratio

**Tick Templates (per station):**

| Station | Tone Freq | Tick Duration | Template Samples (24 kHz) |
|---------|-----------|---------------|--------------------------|
| **WWV** | 1000 Hz | 5.0 ms | 120 |
| **WWVH** | 1200 Hz | 5.0 ms | 120 |
| **CHU** | 1000 Hz | 300 ms | 7200 |
| **BPM** | 1000 Hz | 10 ms | 240 |

**Detection Method (inspired by ntpd refclock_wwv.c Type 36 driver [Mills, "refclock_wwv — NIST Modem Time Services (Type 36)," ntpd reference clock driver]):**

1. **Quadrature Matched Filter:**
   - Generates I/Q template for the exact tick shape (e.g., 5 cycles of 1000 Hz for WWV)
   - Phase-invariant detection via envelope of complex correlation
   - 800–1400 Hz bandpass rejects 100 Hz BCD, 440/500/600 Hz audio tones
   - Processing gain: ~21 dB per tick (120 samples at 24 kHz)

2. **Front-Edge Back-Calculation:**
   - Correlation peak corresponds to the CENTER of the tick pulse
   - The on-time marker is the LEADING EDGE [NIST Special Publication 432, "NIST Time and Frequency Services"]
   - Subtract half the tick duration from peak position to recover front edge
   - Sub-sample parabolic interpolation (~5 μs precision)

3. **Carrier Phase Extraction (IQ Domain):**
   - At each detected tick, mix raw IQ samples at the tone frequency over the tick duration
   - Take the angle of the mean phasor → carrier phase at that tick
   - Phase progression across the minute encodes Doppler shift

4. **Ensemble Combination (57 ticks/minute):**
   - SNR-weighted robust median of all detected per-second ticks
   - Outlier rejection via MAD (Median Absolute Deviation)
   - Typical: 50–57 valid ticks per minute per station
   - Effective processing gain: 21 + 10×log10(57) ≈ 38.6 dB

5. **Doppler from Phase Slope:**
   - Unwrap carrier phase across detected ticks
   - Linear fit: slope (rad/s) / (2π) = Doppler frequency shift (Hz)
   - Requires ≥5 detected ticks spanning ≥5 seconds for meaningful fit
   - Uncertainty from linear fit covariance

6. **Intermodulation Awareness:**
   - Audio tone schedule determines "clean" vs "contaminated" minutes
   - WWV silent minutes: {29, 43–51, 59}; WWVH silent: {0, 8–10, 14–19, 30}
   - When one station's audio tone is silent, the other's ticks are intermod-free
   - Clean minutes yield higher detection confidence

**Implementation:** `src/hf_timestd/core/tick_edge_detector.py`

**Precision**: ±0.008 ms ensemble uncertainty (CHU at 20+ dB SNR), ±0.5–2 ms typical (WWV/WWVH)

### 5.3 Station Discrimination (Shared Frequencies)

On 2.5, 5, 10, 15 MHz, **WWV and WWVH transmit simultaneously**. Misidentification causes **3-8 ms systematic error**.

**Discrimination Methods:**

1. **BCD Correlation (Primary)** — 100 Hz subcarrier cross-correlation
2. **Tone Power Ratio** — WWV: 1000 Hz strong; WWVH: 1200 Hz strong
3. **Station ID Tones** — WWV: 500/600 Hz; WWVH: 600/1500 Hz
4. **Cross-Frequency Guidance** — Exploits ionospheric coherence

**Validation:**

- Inter-station D_clock consistency (< 1 ms spread required)
- D_clock continuity (jumps > 5 ms flagged)
- GPSDO lock status check

### 5.4 Real-Time Ionospheric Propagation Model (v6.7)

The `HFPropagationModel` computes frequency-dependent ionospheric delay using a three-tier data hierarchy:

```
HFPropagationModel.predict(station, frequency, utc_time)
    ├── IonoDataService.get_iono_params()
    │       ├── WAM-IPE grid (NOAA S3/NOMADS)     ← Tier 1: Real-time 3D model
    │       ├── GIRO ionosonde corrections          ← Tier 1.5: Ground-truth hmF2/foF2
    │       ├── IRI-2020 climatology                ← Tier 2: Monthly median model
    │       └── Parametric fallback                 ← Tier 3: Diurnal/seasonal formula
    ├── _evaluate_mode() × [1F, 2F, 3F, 1E]
    │       ├── Geometric feasibility check
    │       ├── MUF check (freq vs foF2/sec(i))
    │       ├── Spherical Earth path length
    │       └── Ionospheric group delay
    │               ├── Ne(h) numerical integration  ← When profile available
    │               └── TEC-based: 40.3·sTEC/(c·f²)  ← Fallback
    └── _estimate_uncertainty()
```

**Ionospheric Group Delay Physics** (the 40.3 m³/s² group-delay constant [Davies, *Ionospheric Radio*; ITU-R Recommendation TF.460-6]):

```
Δτ = (40.3 / c) × ∫ Ne(s) ds / f²  =  40.3 × sTEC / (c × f²)
```

For a vertical TEC of 20 TECU at 10 MHz, the excess delay is ~0.27 ms. At 5 MHz, it's ~1.07 ms (4× larger).

**Multi-Mode Predictions:**

For each (station, frequency) pair, the model evaluates four propagation modes:

| Mode | Description | Typical Distance |
|------|-------------|-----------------|
| **1F** | Single F-layer hop | < 3000 km |
| **2F** | Two F-layer hops | 3000–6000 km |
| **3F** | Three F-layer hops | > 6000 km |
| **1E** | Single E-layer hop (daytime) | < 2000 km |

Each mode is checked for geometric feasibility, MUF constraint, and minimum elevation (>3°).

**Adaptive Uncertainty:**

| Data Source | 3σ Uncertainty | Confidence |
|-------------|---------------|------------|
| WAM-IPE + GIRO | ±1.5 ms | 0.8 |
| WAM-IPE alone | ±3.0 ms | 0.6 |
| IRI-2020 | ±4.5 ms | 0.5 |
| Parametric fallback | ±9.0 ms | 0.2 |
| No model | ±15.0 ms | 0.0 |

The final window blends model uncertainty with tracked observational variance, floored at ±5 ms (3σ).

**Self-Consistency Check:**

Multi-frequency differential delay validates model TEC predictions:

```
Δτ(f1,f2) = τ(f1) - τ(f2) = 40.3 × sTEC × (1/f1² - 1/f2²) / c
```

If observed differential delay disagrees with predicted by >1 ms RMS, the model flags an inconsistency.

**Great-Circle Path TEC Sampling (v6.7.1):** TEC is sampled along the great-circle path using spherical trigonometry, ensuring accurate TEC integration for long paths (e.g., BPM at 11,504 km).

**Altitude-Dependent Obliquity Mapping (v6.7.1):** `M(h) = 1 / sqrt(1 - (R·cos(e) / (R + h))²)` replaces the simpler `1/sin(e)` approximation. This is the standard single-layer obliquity (thin-shell) mapping function [Bust and Mitchell, "History, current state, and future directions of ionospheric imaging," *Rev. Geophys.*, 46, RG1003, 2008].

**Propagation Delay Bounds:**

- WWV: 4–12 ms
- WWVH: 15–30 ms
- CHU: 6–15 ms
- BPM: 40–70 ms

Delays outside bounds have plausibility reduced by 70%.

#### Optional: Local GNSS-VTEC Enhancement

When a dual-frequency GNSS receiver (e.g., u-blox ZED-F9P) is available, the system can measure **local vertical TEC in real-time** (~1 minute latency vs 1–2 hours for IONEX maps). The `timestd-vtec` service polls the receiver and writes to `/var/lib/timestd/data/gnss_vtec/GNSS_gnss_vtec_YYYYMMDD.h5`.

**Key Files:**

| File | Purpose |
|------|---------|
| `src/hf_timestd/core/propagation_model.py` | `HFPropagationModel` — delay prediction, multi-mode, self-consistency |
| `src/hf_timestd/core/raytrace_engine.py` | `RaytraceEngine` — PHaRLAP 2D ray tracing with spatially varying IRI grid |
| `src/hf_timestd/core/iono_data_service.py` | `IonoDataService` — WAM-IPE/GIRO fetch, cache, great-circle TEC sampling |
| `src/hf_timestd/core/arrival_pattern_matrix.py` | `ArrivalPatternMatrix` — integrates model into arrival predictions |

### 5.5 Adaptive Search Windows

**Phase Progression:**

| Phase | Window | Criteria |
|-------|--------|----------|
| **Bootstrap** | ±50 ms | Initial (RTP timestamps are authoritative, no wall-clock bias) |
| **Provisional** | ±5-15 ms | 10+ detections, 2+ stations, D_clock σ < 1 ms |
| **Calibrated** | ±2-5 ms | 30+ detections, 60 min span, RTP variance < 50² |

**Arrival Tolerance:** ±100 ms (validates detected tone arrivals against expected propagation delay)

**Key Insight:** GPSDO is the foundation. Stations are periodic calibration checks, not the primary reference.

#### Physics-Driven Adaptive Windows (v6.11)

The static phase progression above is augmented (v6.11) by a per-station adaptive window driven by the `HFPropagationModel`:

```
σ_physics = model uncertainty (1σ)
σ_utc     = FusionTimingState UTC uncertainty / 3  (Fusion mode only)
σ_total   = √(σ_physics² + σ_utc²)
search_window = 3 × σ_total   (clamped to [5 ms, 200 ms])
```

The `BroadcastWindowState` in `ArrivalPatternMatrix` tracks observed propagation variance per (station, frequency) and blends it with the model uncertainty.  Three safeguards prevent pathological narrowing:

| Safeguard | Trigger | Action |
|-----------|---------|--------|
| **Staleness decay** | >5 min since last detection | Exponential widening toward model+initial |
| **Miss counter** | 5 consecutive minutes with no detection | Full reset to initial uncertainty |
| **Model floor** | Tracked < model uncertainty | Only allowed when confidence ≥ 0.95 and ≥ 30 observations |

**Implementation:** `src/hf_timestd/core/arrival_pattern_matrix.py` (`BroadcastWindowState`), `src/hf_timestd/core/metrology_engine.py` (`process_minute`)

---

## 6. Uncertainty Budget (ISO GUM)

### 6.1 Multi-Broadcast Fusion

**Calibration Model:**

```python
calibration_offset = -mean(D_clock_station)
```

**Update Method:** Exponential Moving Average (α = max(0.5, 20/n_samples); 0.5 is the steady-state floor)

**Inverse Variance Weighting:**

```python
w = 1 / (uncertainty_ms²)
d_clock_fused = Σ(w_i × d_clock_i) / Σ(w_i)
```

**Why this matters:**

- **Statistically optimal** for combining independent measurements
- **ISO GUM best practice** for inverse-variance weighting (GUM-S1) [BIPM/ISO, "Evaluation of measurement data — Guide to the expression of uncertainty in measurement (GUM)," JCGM 100:2008]
- Measurements with 0.5 ms uncertainty get 4x weight vs 1.0 ms uncertainty

### 6.2 Kalman Filtering (LEGACY offset-tracking Kalman)

> **Note:** This table documents the **legacy** single-offset clock tracker,
> `clock_convergence.KalmanClockTracker` ([tof offset, drift] state). The
> v6.0 hierarchical architecture (§12.7) removed the single offset-Kalman
> from the L3 fusion layer. The **live** fusion path uses the per-frequency
> `BroadcastKalmanFilter` ([tof_ms, doppler] state, §12.3) followed by WLS at
> L3 (§12.5) — see §12 for the deployed hierarchical filters.

**Steel Ruler Parameters** (`KalmanClockTracker` code defaults):

| Parameter | Value | Description |
|-----------|-------|-------------|
| **Initial P (Offset)** | `initial_uncertainty_ms=5.0` (P = 5.0² = 25 ms²) | Moderate initial trust |
| **Initial P (Drift)** | (5.0/10)² = 0.25 ms² | High trust in factory calibration |
| **Q (Offset)** | `process_noise_offset_ms=1e-6` (near-zero stability) | The clock does not wander |
| **Q (Drift)** | `process_noise_drift_ms_per_min=1e-7` | Drift is negligible |
| **R (Measurement)** | `measurement_noise_ms=30.0` | High measurement noise (ionospheric) |

**Drift Clamping:** `drift_ms_per_min` is forced to `0.0` after convergence.

### 6.3 Error Sources

**Type A (Statistical):**

- Weighted standard error of fusion
- Reduces as √N with more broadcasts

**Type B (Systematic):**

| Source | Uncertainty | Notes |
|--------|-------------|-------|
| Tone detection (Cramér-Rao) | ±0.036-0.9 ms | SNR-dependent (v6.2) |
| Multipath delay spread | ±0.5-2.5 ms | Inflates uncertainty when detected |
| Doppler bias (uncorrected) | ±0.1-2 ms | Now corrected in v6.2 |
| Propagation model (IONEX) | ±1-2 ms | |
| Propagation model (IRI) | ±2-5 ms | |
| RTP jitter | ±0.1 ms | |
| GPSDO stability | ±0.001 ms | Negligible |

**Combined Uncertainty (RSS):**

```
u_combined = √(u_cramer_rao² + u_multipath² + u_propagation² + u_systematic²)
```

**v6.2 Enhancement:** The `ToneDetectionResult` now includes `timing_uncertainty_ms` computed from the Cramér-Rao bound, which is inflated when multipath is detected. This provides rigorous per-measurement uncertainty for downstream fusion.

### 6.4 Typical Values

| Condition | Uncertainty (1σ) |
|-----------|------------------|
| Single broadcast | ±3.6 ms |
| 13 broadcasts fused | ±1.0 ms |
| With calibration | **±0.5 ms** |

**Coverage Factors:**

- 68% (1σ): ±0.5 ms
- 95% (2σ): ±1.0 ms
- 99% (3σ): ±1.5 ms

---

## 7. Verification Procedures

### 7.1 Verify Baseline Stability

The most critical check is ensuring the `D_clock` baseline is **horizontal**, not "walking".

1. **Open Web UI:** Go to Metrology Dashboard
2. **Check Slope:** The fused `D_clock` line should be flat over 24 hours
3. **Verify Fusion Log:**

   ```bash
   journalctl -u timestd-fusion -n 50 | grep "Steel Ruler"
   # Expected Output:
   # INFO:__main__:Steel Ruler: Baseline is STABLE (drift = 0.0 ms/min)
   ```

### 7.2 Verify Latency

Ensure latency is low to allow real-time Chrony discipline.

1. **Run Verification:** `scripts/verify_pipeline.sh`
2. **Check Phase 2 Latency:** Should be < 90 seconds for active channels
3. **Check Phase 3 Latency:** Should be < 120 seconds

### 7.3 Chrony Discipline

Verify the system is actively steering the kernel clock.

```bash
chronyc tracking
# Look for:
# Ref ID        : 46555345 (FUSE)   # or 48505053 (HPPS) when the BPSK-PPS feed is the selected source
# Stratum       : 1 (if treating HF as primary) or >1
# Last offset   : +0.000xxxx seconds (sub-millisecond)
# RMS offset    : 0.000xxxx seconds
# Frequency     : x.xxx ppm (should be stable)
```

**Chrony SHM Configuration** (live `config/chrony-timestd-refclocks.conf`):

```
refclock SHM 1 refid FUSE poll 4 precision 1e-4   # T3 multi-broadcast fusion (L2 calibrated)
refclock SHM 2 refid HPPS poll 0 precision 5e-6   # T6 BPSK-PPS matched filter
```

> SHM 0 — the legacy raw-metrology L1 feed (formerly `refid TMGR` / `TSL1`) — was retired 2026-05-23 and is no longer published. The diff-detector calibrator (HFPS) is config-gated on SHM 3.

- Fusion calculates D_clock every **8 seconds**
- Chrony polls every **16 seconds**
- Expected reach: 87.5% (7 out of 8 polls successful)

### 7.4 Ionospheric "Weather"

If `D_clock` values are jumping significantly (> 2-3 ms), this is likely ionospheric weather (storms, TIDs), not clock failure.

- **Check Propagation Analysis:** Look for similar jumps across *multiple frequencies*
- **Global Differential:** If 10 MHz and 15 MHz both jump +2ms, it's a layer height change

### 7.5 Quality Grades

| Grade | Uncertainty | Criteria |
|-------|-------------|----------|
| **A** | ±0.5 ms | 30+ detections, 60 min span, RTP variance < 50², calibrated, inter-station < 1 ms |
| **B** | ±1.0 ms | 10+ detections, provisional phase |
| **C** | ±2.0 ms | Bootstrap phase, limited validation |
| **D/F** | > 2.0 ms | Insufficient data or validation failures |

---

## 8. Troubleshooting

### 8.1 "Walking" Baseline (Non-Zero Slope)

If the baseline starts tilting:

1. **Check GPSDO Lock:** Ensure the physical clock is actually locked
2. **Force Reset:**

   ```bash
   sudo systemctl stop timestd-fusion
   # Edit /var/lib/timestd/state/broadcast_calibration.json
   # Set "drift_ms_per_min": 0.0 for all stations.
   sudo systemctl start timestd-fusion
   ```

### 8.2 "Stale TEC" Warning

If `verify_pipeline.sh` reports stale TEC:

1. **Check Night/Day:** TEC requires multi-frequency data. At night, MUF drops, and we often lose higher bands (15/20/25 MHz), making TEC calculation impossible.
2. **Action:** Wait for sunrise or check `timestd-physics` logs.

### 8.3 WWV 20/25 MHz STALE Measurements

Not a software bug—HF propagation on higher bands is poor during night/early morning. These are single-broadcast anchor channels (no WWVH overlap), critical for bootstrap. Measurements resume when propagation improves.

---

## 9. Data Products

### 9.1 Raw Archive: Binary IQ + JSON Sidecars

- **Format:** `.bin.zst` (zstd-compressed binary) + `.json` metadata sidecar per chunk
- **Chunk duration:** Configurable via `file_duration_sec` (default 600s = 10 minutes). Legacy 1-minute files are still supported by the reader.
- **Structure:**
  - Binary file: `sample_rate × file_duration_sec` complex64 IQ samples per chunk (e.g., 14,400,000 at 24 kHz / 600s; 1,440,000 per minute within each chunk)
  - JSON sidecar: RTP timestamps, gap info, system time, quality metrics, `file_duration_sec`
- **Metadata:** Start RTP timestamp, start system time, sample rate (24 kHz), center frequency, gap count, `file_duration_sec`
- **Size:** ~2-3 GB/day/channel

> **Note:** Digital RF (MIT Haystack) is used only for GRAPE DRF packaging/upload, not for raw recording.

### 9.2 Tick Timing: L2 HDF5 (Primary Timing Product)

**Schema:** `l2_tick_timing_v1.json` (schema version 2.0.0, processing version 5.0.0)

All fields sourced from `TickEdgeDetector`:

| Field | Type | Description |
|-------|------|-------------|
| `d_clock_ms` | float | Ensemble timing residual from expected arrival (ms) |
| `d_clock_uncertainty_ms` | float | MAD of per-tick timing errors (ms) |
| `d_clock_source` | string | Always `edge_ensemble` |
| `doppler_hz` | float | Carrier phase slope across the minute (Hz) |
| `doppler_uncertainty_hz` | float | Doppler uncertainty from linear fit (Hz) |
| `mean_snr_db` | float | Mean per-tick matched filter SNR (dB) |
| `valid_windows` | int | Number of ticks with valid detections |
| `total_windows` | int | Total seconds attempted |
| `ensemble_n_edges` | int | Number of tick edges used in ensemble |
| `n_clean` | int | Ticks from intermod-free minutes |

### 9.3 Fusion Output: HDF5 L3

- **Structure:**
  - `/fused_solution`: Time series of weighted mean offset
  - `/residuals`: Per-station residuals from the mean
  - `/calibration`: Current calibration state for each station
- **Chrony SHM updates** for clock discipline
- **Allan deviation tracking**

### 9.4 Science Products

- TEC estimates (multi-frequency 1/f² fit)
- Propagation mode statistics
- Sporadic-E events
- Space weather correlations
- Doppler time series (ionospheric motion)

---

## 10. Limitations and Caveats

### 10.1 Fundamental Limits

**Ionospheric Variability:**

- Propagation delay varies by ±1-3 ms over minutes
- Cannot be eliminated, only averaged
- Dominates uncertainty budget

**Mode Ambiguity:**

- Cannot always distinguish 1F vs 2F propagation
- Multipath creates 0-5 ms delay spread
- Mitigated by calibration, not eliminated

**Single Receiver:**

- No spatial resolution
- Cannot determine propagation direction
- Not suitable for ionospheric tomography

### 10.2 Operating Conditions

| Condition | Performance |
|-----------|-------------|
| Quiet to moderate (Kp < 5) | Best |
| Ionospheric storms (Kp > 5) | Degraded |
| Solar flares (X-ray absorption) | Degraded |
| Propagation blackouts | Failure (holdover mode) |

### 10.3 What This System Does NOT Do

- **Not a frequency standard:** Disciplines system clock, not a standalone oscillator
- **Not better than GPS:** GPS achieves ±10 ns; this achieves ±0.5 ms (demonstrates the capability to derive precision timing from frequency-standard hardware)
- **Not ionospheric tomography:** Single receiver, limited spatial resolution

---

## 11. Conclusion

### 11.1 What We Claim

- **±0.5 ms (1σ) to UTC(NIST)** with proper uncertainty, when multi-station fusion is locked and converged
- **ISO GUM-compliant** methodology
- **Physics-validated** measurements
- **Production-grade** reliability

### 11.2 What We Don't Claim

- Better than GPS (we're 50,000x worse)
- Ionospheric tomography (single receiver)
- Absolute timing better than ±0.5 ms

### 11.3 The Bottom Line

This is not an amateur "time sync" project. This is an **instrument of synthetic metrology** for HF time transfer with:

- Proper uncertainty quantification
- Physics-based validation
- Systematic error correction
- Multi-broadcast redundancy
- ISO GUM compliance

**For a time nut:** This extracts maximum utility from your existing GPSDO. It transforms a device that only disciplines *frequency* into a system that disciplines *time*, providing a second, physics-based opinion on UTC that validates your GPS solution.

**For a metrologist:** This is traceable to UTC(NIST), has a complete uncertainty budget, and follows ISO GUM best practices.

**For a skeptic:** The code is open source, the physics is documented, and the limitations are honestly stated. Verify it yourself.

---

## 12. Hierarchical Estimation Architecture (v6.0)

### 12.1 Motivation: Why Hierarchical?

The original architecture (v5.x) used a **single Kalman filter at the fusion layer**. This caused:

1. **Restart variance**: The fused D_clock settled to different values on each service restart
2. **False smoothing**: Ionospheric variations were smoothed away, hiding science
3. **Single point of failure**: All state concentrated in one filter

The revised architecture (v6.0) distributes filtering to where it is **physically justified**:

| Layer | Method | Physical Justification |
|-------|--------|------------------------|
| Per-Broadcast | Kalman filter | Ionosphere has temporal continuity |
| Per-Station | TEC 1/f² fit | Multi-frequency dispersion is physics |
| Multi-Station | Weighted Least Squares | Optimal linear combination, no temporal smoothing |

### 12.2 The Estimation Problem

**Quantity of interest:** `offset_to_UTC = T_local - T_UTC(NIST)`

**Observation model:** For each broadcast i:
```
D_clock_i = T_arrival_i - ToF_i - T_transmit_i
          = offset_to_UTC + ε_i
```

Where:
- `T_arrival_i` — measured precisely by GPSDO (known)
- `ToF_i` — ionospheric path delay (nuisance parameter, varies)
- `T_transmit_i` — scheduled transmission time (known by definition)
- `ε_i` — residual error (ionospheric + noise)

**Key insight:** All 17 broadcasts share the **same unknown** (offset_to_UTC). The ionospheric paths are **nuisance parameters** we must estimate to extract the quantity of interest.

### 12.3 Layer 1: Per-Broadcast Kalman Filter

**Purpose:** Track ionospheric path dynamics for each of the 17 broadcasts.

**State vector:**
```
x = [ToF_ms, dToF_dt]
```

**Physical model:**
- The ionosphere is a continuous medium — it cannot teleport
- ToF changes are bounded by ionospheric dynamics (~1-5 ms/minute max)
- A Kalman filter here models **real physics**

**Process noise (tuned per frequency):**
- Low frequencies (2.5-5 MHz): Higher Q (E-layer volatility)
- High frequencies (15-25 MHz): Lower Q (F-layer stability)

**Measurement noise:** SNR-dependent (high SNR → trust measurement)

**Implementation:** `src/hf_timestd/core/broadcast_kalman_filter.py`

**What this achieves:**
- Rejects detection glitches (false peaks, multipath)
- Preserves real ionospheric dynamics (science signal)
- Provides smoothed ToF with uncertainty for downstream processing

### 12.4 Layer 2: Per-Station TEC Estimation

**Purpose:** Validate multi-frequency consistency and estimate ionospheric TEC.

**Physics:** The ionospheric group delay follows:
```
τ_iono(f) = K × TEC / f²
where K = 40.3 m³/s²
```

For a given path, **TEC is the same** for all frequencies. Only the delay differs by 1/f².

**Algorithm:** Weighted Least Squares regression on ToF vs 1/f²:
```
ToF(f) = ToF_geometric + k/f²

Solve for:
  - ToF_geometric (ionosphere-free delay)
  - k (proportional to TEC)
```

**Implementation:** `src/hf_timestd/core/tec_estimator.py`

**Metrological Constraints:**

The HF-derived TEC estimator provides validation and science products, but **does not directly modify D_clock values** because mode mixing can corrupt the 1/f² fit.

**What HF TEC achieves:**
- **Validates** that multi-frequency measurements follow 1/f² physics
- **Boosts confidence** for measurements with good TEC fit (R² > 0.9)
- **Reduces confidence** for measurements with poor TEC fit
- **Produces TEC science products** for ionospheric research
- **Detects mode changes** when 1/f² relationship breaks

### 12.4.1 GNSS VTEC Ionospheric Correction (v6.1)

**NEW in v6.1:** When local GNSS VTEC is available, the fusion layer applies a **direct ionospheric correction** to D_clock measurements.

**Physics Basis:**

The propagation model computes D_clock using a **modeled** TEC value (from IRI-2020, IONEX, or parametric fallback). GNSS VTEC provides a **direct measurement** of the actual ionospheric electron content. The correction is:

```
D_clock_corrected = D_clock + Δiono
where Δiono = K × (TEC_model - TEC_gnss) × n_hops × obliquity / f²
      K = 1.344 ms·MHz²/TECU (ionospheric delay constant)
```

**Why this is metrologically justified:**

1. **GNSS VTEC is a direct measurement** — dual-frequency GPS receivers measure TEC to ±1-2 TECU accuracy
2. **The 1/f² physics is well-established** — ionospheric group delay follows this dispersion relation exactly
3. **We're correcting model error** — not adding new uncertainty, but removing systematic bias

**TEC Source Hierarchy:**

| Priority | Source | Latency | Accuracy | Usage |
|----------|--------|---------|----------|-------|
| 1 | Local GNSS VTEC | ~1s | ±1-2 TECU | Direct D_clock correction |
| 2 | IONEX maps | 2 hours | ±2-5 TECU | Propagation model (Tier 1.5) |
| 3 | IRI-2020 | Climatology | ±5-10 TECU | Propagation model (Tier 1) |
| 4 | Parametric | Climatology | ±10-20 TECU | Propagation model (Tier 2) |

**Implementation:** `src/hf_timestd/core/multi_broadcast_fusion.py` (GNSS VTEC correction block)

**Typical Correction Magnitude:**

For ΔTEC = 10 TECU, f = 10 MHz, 1 hop, obliquity = 1.5:
```
Δiono = 1.344 × 10 × 1 × 1.5 / 100 = 0.20 ms
```

For ΔTEC = 30 TECU, f = 5 MHz, 1 hop, obliquity = 1.5:
```
Δiono = 1.344 × 30 × 1 × 1.5 / 25 = 2.42 ms
```

**Uncertainty Floor with GNSS TEC:**

With proper GNSS VTEC correction, the theoretical uncertainty floor is:
- **Per-station:** ~0.1-0.2 ms (geometric path becomes calibratable constant)
- **Multi-station WLS:** ~0.05-0.1 ms

**Example:**
```
WWV 5 MHz:  ToF = 35.2 ms
WWV 10 MHz: ToF = 33.8 ms  
WWV 15 MHz: ToF = 33.4 ms

Fit: ToF = 33.1 ms + 530/(f_MHz)²
R² = 0.98 (good fit)

Result: Measurements validated, confidence boosted 15%
TEC estimate = 25 TECU (science product)
```

### 12.5 Layer 3: Multi-Station Weighted Least Squares

**Purpose:** Combine per-station D_clock estimates into a single offset_to_UTC.

**Method:** Best Linear Unbiased Estimator (BLUE); inverse-variance weighting is the minimum-variance linear unbiased combination per the Gauss–Markov theorem [Kay, *Fundamentals of Statistical Signal Processing: Estimation Theory*]:
```
offset_to_UTC = Σ(w_i × D_clock_i) / Σ(w_i)
where w_i = 1/σ_i²
```

**Why NOT a Kalman filter here?**

A Kalman filter at L3 would model `offset_to_UTC` as having **process noise** — implying the offset drifts randomly. But:

1. The GPSDO doesn't drift randomly — it has deterministic (tiny) drift
2. Any apparent "drift" in the fused offset is actually **ionospheric bias** leaking through
3. A Kalman would **mask** this bias instead of **estimating** it

**Cross-station validation:**
- Compute per-station residuals from the weighted mean
- If a station systematically deviates → flag for calibration review
- Detect ionospheric gradients (science signal, not error)

**Implementation:** `src/hf_timestd/core/multi_broadcast_fusion.py` (method: `_weighted_least_squares_fusion`)

### 12.6 State Persistence

**Per-broadcast state:** Each of the 17 Kalman filters persists its state:
```json
{
  "WWV_5000": {"tof_ms": 33.8, "dtof_dt": 0.001, "P": [[0.1, 0], [0, 0.001]], "n_updates": 1234},
  "WWV_10000": {"tof_ms": 33.4, "dtof_dt": 0.002, "P": [[0.08, 0], [0, 0.001]], "n_updates": 1189},
  ...
}
```

**File:** `/var/lib/timestd/state/broadcast_kalman_state.json`

**Restart behavior:**
- Each broadcast resumes from its last known ToF
- No single point of state that can drift
- Fusion is instantaneous (WLS), not path-dependent

### 12.7 Comparison: Old vs New Architecture

| Aspect | v5.x (Single L3 Kalman) | v6.0 (Hierarchical) |
|--------|-------------------------|---------------------|
| **Filtering location** | Fusion layer only | Per-broadcast |
| **State persistence** | Single Kalman state | 17 independent states |
| **Restart behavior** | Variance depends on trust decay | Deterministic from per-broadcast state |
| **Ionospheric bias** | Smoothed away | Removed by TEC fit |
| **Science preservation** | Variations hidden | Variations preserved |
| **Fusion method** | Kalman (temporal) | WLS (instantaneous) |

### 12.8 Code References

| Component | File | Key Method |
|-----------|------|------------|
| Per-Broadcast Kalman | `core/broadcast_kalman_filter.py` | `BroadcastKalmanFilter.update()` |
| TEC Estimation | `core/tec_estimator.py` | `TECEstimator.estimate_tec()` |
| WLS Fusion | `core/multi_broadcast_fusion.py` | `_weighted_least_squares_fusion()` |
| State Persistence | `core/multi_broadcast_fusion.py` | `_save_broadcast_kalman_state()` |

---

## 13. FUSION Mode Accuracy Analysis

### 13.1 Motivating Rationale

The system serves a dual purpose. In **RTP mode** (with GPSDO), the authoritative timing comes from GPS+PPS via radiod, and the metrology pipeline functions as a testbed for refining detection algorithms, calibration models, and ionospheric corrections against a known-good reference. This refinement directly serves the second purpose: **FUSION mode**, where GPS, GPSDO, or even network access may be unavailable, and the system must derive UTC solely from HF time standard receptions.

FUSION mode addresses real operational scenarios:
- **Remote/off-grid installations** without GPS coverage
- **Disaster/emergency situations** where GPS and network infrastructure are disrupted
- **Intentional GPS denial** (jamming, spoofing) in contested environments
- **Backup timing** when primary GNSS disciplining fails

### 13.2 Error Budget

In FUSION mode, the timing chain is:

```
UTC(NIST/NRC) → HF transmitter → Ionosphere → Receiver → ADC → Detection → D_clock
```

| Source | Magnitude | Notes |
|--------|-----------|-------|
| **Transmitter timing** | < 1 µs | WWV/WWVH/CHU traceable to UTC(NIST)/UTC(NRC) |
| **Ionospheric propagation** | 3–15 ms variation | Dominant error. Diurnal, seasonal, solar cycle |
| **Multipath/mode structure** | 1–5 ms | Multiple ionospheric modes arrive at different times |
| **ADC clock accuracy** | 0.1–10 ppm | TCXO: 1–2 ppm. Cheap crystal: 10–50 ppm |
| **TickEdgeDetector** | 0.008–2 ms | Ensemble of 50–57 ticks/minute, sub-sample interpolation |
| **NTP initial sync** | 1–50 ms | Depends on network path |

### 13.3 Expected FUSION Mode Accuracy

| Configuration | Expected Accuracy | Time to Lock |
|--------------|-------------------|--------------|
| Multi-station + TCXO + NTP | **±2–5 ms** steady-state | 2–3 min |
| Multi-station + TCXO, no network | **±2–5 ms** steady-state | 5–10 min |
| Single station + TCXO | **±5–15 ms** | 2–3 min |
| Multi-station + cheap crystal | **±2–5 ms** (if freq lock works) | 5–10 min |

The ionosphere is the dominant error in all cases. Oscillator quality affects **time to lock** and **holdover during outages**, but not steady-state accuracy once locked.

### 13.4 Dual Chrony Feed Architecture (v6.5.1)

| Feed | SHM Unit | Source | Purpose |
|------|----------|--------|---------|
| **FUSE** | 1 | L2 Kalman (physics model) | T3 multi-broadcast fusion with full ionospheric correction via propagation model |
| **HPPS** | 2 | BPSK-PPS matched filter (TS-1) | T6 HF-injected PPS — bypasses fusion uncertainty, BPSK quantization-limited floor |

> The legacy raw-metrology L1 feed (SHM 0, formerly `TSL1`) was retired 2026-05-23; the live config (`config/chrony-timestd-refclocks.conf`) publishes `FUSE` on SHM 1 (refid `FUSE`) and `HPPS` on SHM 2 (refid `HPPS`). Chrony prefers HPPS (sub-µs) over FUSE (sub-ms) when both are available.

Each feed has its own independent state. HPPS shows lower jitter and better accuracy (sees the BPSK quantization floor, ~14 ns std once locked); FUSE carries the ionospheric-corrected multi-broadcast fusion (±0.3–1.0 ms).

---

## 14. Timing Authority Levels: Achievable Uncertainty Analysis

> **Note on taxonomy.** An earlier revision of this section classified the system along a single linear scale L1–L6. That scale has been superseded by the two-axis A/T hierarchy defined in §4.5. The achievable uncertainty analysis below is written in A/T terms and references §4.5 throughout; the old L-scale is retired and should not be used in new discussion.

The uncertainty hf-timestd delivers depends on the active (A, T) pair from §4.5, the ionospheric conditions during the measurement window, and the fusion averaging duration. This section characterizes what each T-level *delivers in practice* — single-cycle and fused uncertainty, the primary limiter, and the quality-grade implications. §4.5 defines the hierarchy and the runtime selection / cross-check rules; §14 gives the practical numbers.

### 14.1 Error Source Taxonomy

| # | Error Source | Symbol | Description |
|---|-------------|--------|-------------|
| 1 | **Transmitter timing** | σ_tx | UTC(NIST/NRC) to RF emission |
| 2 | **Ionospheric propagation** | σ_iono | Path delay variation (dominant for HF) |
| 3 | **Multipath/mode structure** | σ_mode | Multiple ionospheric modes |
| 4 | **Detection algorithm** | σ_det | TickEdgeDetector ensemble + sub-sample interpolation |
| 5 | **ADC sample clock** | σ_adc | Rate stability of the RX888 ADC timebase (Axis A) |
| 6 | **RTP-to-UTC mapping** | σ_rtp | Uncertainty of the published Fusion offset at the active T-level (Axis T) |
| 7 | **Timing authority** | σ_auth | Composite: which (A, T) is active at measurement time |

Sources 1–4 are **irreducible** (physics/algorithm). Sources 5–7 are **configuration-dependent** and collapse onto the two §4.5 axes: σ_adc tracks Axis A; σ_rtp tracks Axis T; σ_auth is the composite tag the authority manager attaches to each published offset. Under the §4.5 RTP-reference labeling invariant, data-labeling error in downstream products is σ_rtp alone — σ_adc manifests only as coherence-loss within a single fusion window at A0.

### 14.2 Irreducible Error Sources

- **σ_tx < 0.001 ms**: WWV/WWVH traceable to UTC(NIST) with < 1 µs. Negligible.
- **σ_iono = 3–15 ms**: Dominant error. Diurnal, seasonal, solar cycle, geomagnetic.
- **σ_mode = 1–5 ms**: Multiple propagation modes (1F2, 2F2, 1E) arrive at different times.
- **σ_det ≈ 0.05 ms**: TickEdgeDetector ensemble of 50–57 ticks achieves ~38.6 dB processing gain. Negligible compared to σ_iono.

### 14.3 Per-T-Level Uncertainty

Each row summarizes the **published Fusion offset's uncertainty** at one T-level — i.e., the σ tag attached to `rtp_to_utc_offset_ns` in `authority.json`. For T5/T4/T2, the offset is near zero (no correction; RTP is externally disciplined) and the σ characterizes how well that zero is known. For T3/T6, the offset is an active correction hf-timestd produces, and the σ characterizes Fusion's self-assessment. "Single cycle" is one fusion interval (default 8 s); "Fused 10 min" is steady-state after ~75 cycles of continuous coverage. The (A1) and (A0) columns apply where §4.5 allows both A-levels; `—` indicates §4.5 gates the combination as structurally unavailable.

| T-level | Source (recap from §4.5) | σ single cycle (A1) | σ fused 10 min (A1) | σ single cycle (A0) | σ fused 10 min (A0) | Primary limiter |
|---|---|---|---|---|---|---|
| **T6** | TS-1 HF-injected BPSK-PPS via RX-888 ADC | ~ns (post-§8) | ~ns | ~tens μs per detection | ~100 μs¹ | §8 chain-delay calibration stability; BPSK-PPS SNR + coverage |
| **T5** | LBE-1421 USB-delivered GPS+PPS → system clock | ~µs–few ms | ~µs (chrony averaging) | — | — | USB bus jitter + chrony PLL |
| **T4** | LAN GPS timeserver via NTP → system clock | 100 μs – few ms | ~300 μs | 1–5 ms | ~1.5 ms | LAN NTP jitter (+ TCXO drift between syncs at A0) |
| **T3** | HF tick Fusion of WWV/WWVH/CHU | 3–15 ms | 0.3–1.0 ms | 5–20 ms | 1–3 ms | σ_iono single-cycle; A-level × window length for fused |
| **T2** | Public NTP via WAN → system clock | 1–50 ms | ~10 ms | 5–50 ms | ~15 ms | NTP wander dominates; A-level invisible at this scale |
| **T1** | GPSDO-coast from last good RTP_TIMESNAP | const offset + ~0 drift² | — (not applicable) | — | — | Snapshot age + accumulated A1 rate error (ppb × hours) |
| **T0** | *(no UTC alignment available)* | — (offset unavailable) | — | — | — | Terminal — data tagged `no_utc_alignment_available` |

¹ T6 at A0 is re-anchored at each BPSK-PPS detection but drifts at TCXO rate (~5 ppm) between detections. Fused uncertainty assumes continuous coverage; a detection gap of length Δt inflates σ by ~5 ppm × Δt.

² T1 has no ongoing UTC measurement — the offset is whatever it was at the last T ≥ 2 sync. At A1, rate is perfect, so the offset does not drift; only snapshot freshness matters. Uncertainty grows with coast time only via residual A1 rate error, which is sub-ms for coasts of hours.

**Measured performance at A1/T5** (March 17–18, 2026, prior to TS-1/T6 integration): ±0.3 ms (1σ) fused uncertainty with GNSS VTEC correction applied; ±0.7 ms (1σ) with raw L1-only data products (no L2 calibration). Consistent with the A1/T3 10-minute fused prediction above, since the HF Fusion witness at T5 operates under the same σ_iono floor that gates T3. Post-TS-1 deployment (2026-05-23, HPPS @ ±1 ns σ=1 ns) the deployed best tier on bee1 is T6; these T5 numbers now characterize the fallback path rather than the steady-state operating point.

### 14.4 Key Insights

1. **σ_iono is the single-cycle floor at T3.** 3–15 ms per cycle is ionospheric physics; no hardware or algorithm change moves it. Fusion averaging pulls 10-minute σ ≈ 10× below the single-cycle number because independent iono realizations across ~75 cycles de-correlate.
2. **Grade A (σ < 0.5 ms) is reachable at T3/A1 with enough averaging** — the 10-minute number lands in grade A under clean conditions. Without A1 (ppb rate stability), the same averaging can't close the gap because cycle-to-cycle coherence degrades.
3. **A1 is the rate anchor, not the zero-point.** A1 alone gives T1 (rate perfect, zero unknown). The zero-point always comes from Axis T; A1 just keeps measurements within a fusion window coherent.
4. **At T2-scale σ, the A-level is noise.** NTP wander (~10 ms) dominates so thoroughly that TCXO drift over an 8-second cycle (~5 ppm × 8 s ≈ 40 μs) is invisible. This is why §4.5 lists similar uncertainty for (A1, T2) and (A0, T2).
5. **T3 and T6 degrade gracefully under A0.** Unlike T5 and T1, which §4.5 structurally gates on A1, the hf-timestd-derived levels survive GPSDO loss — the authority manager shifts from (A1, T3) to (A0, T3) without a T-level transition, only a σ inflation in the published offset. Operators see this as a widened sigma_ns in authority.json, not a failed probe.
6. **Cross-check confirms provenance, not precision.** §4.5's disagreement thresholds (T6↔T5 @ 5 ms, T3↔T4 @ 2 ms, T3↔T2 @ 5 ms) raise `TIMING_DISAGREEMENT` when the active level's published offset diverges from a witness; they do not reduce the σ quoted above. The σ of the active level is whatever Axis T's source delivers; cross-check is an alarm on "is the active level still meaningful," not a noise reduction. The T6↔T5 threshold is sized to T5's USB-jitter floor, not to T6's precision — T6 itself reaches ns-class but the cross-check fires only when disagreement exceeds what T5's transport can explain.
7. **Default operating point is (A1, T6) when the TS-1 path is healthy**, with (A1, T5) as the immediate fallback via the same LBE-1421's USB-NMEA. Subsequent downgrades: (A1, T3) active with (A1, T2) witness on LAN-GPS failure, and further to (A1, T2) active (no Fusion) if Fusion also loses lock. Each downgrade widens σ and is recorded in the `authority_history` sidecar entries (§4.5) so downstream reprocessing can tag samples by their σ at acquisition time.

### 14.5 Station Priority Policy

| Station | Role | Rationale |
|---------|------|----------|
| **CHU** | Reference | Unique frequencies, FSK-verified timing; coarse UTC producer for bootstrap |
| **WWV** | Primary | Closest U.S. station, best SNR for the typical receiver latitude |
| **WWVH** | Primary | Independent Pacific path, cross-validates WWV |
| **BPM** | Science only — excluded from fusion | 11,000 km trans-Pacific path introduces 18–36 ms cross-station disagreement that dominates fusion uncertainty. Since 2026-02-07, BPM measurements are removed from the fusion pipeline in `MultiBroadcastFusion.fuse()` (`BPM exclusion: removed N BPM measurements from fusion (kept for science)`). Per-broadcast Kalmans still track BPM for ionospheric science products (TEC, propagation mode identification). |

---

## 15. Metrological Validation (v6.2)

This section describes procedures for validating hf-timestd performance against external references and theoretical predictions.

### 15.1 FUSE vs HPPS Comparison

The dual Chrony feed architecture (FUSE and HPPS) provides built-in validation: the multi-broadcast fusion feed cross-checked against the BPSK-PPS feed.

<!-- LIVE: l1-l2-comparison -->

> **Historical note:** earlier revisions paired an L1 raw-metrology feed (`TSL1`, SHM 0) against an L2 calibrated feed (`TSL2`, SHM 1). SHM 0 / the L1 raw feed was retired 2026-05-23; the live feeds are now `FUSE` (SHM 1, the L2-calibrated multi-broadcast fusion) and `HPPS` (SHM 2, the TS-1 BPSK-PPS). The L1-vs-L2 comparison discussion below is retained for the propagation-correction-quality reasoning it illustrates.

**What FUSE and HPPS Represent:**

| Feed | SHM | Data Source | Processing | Typical Uncertainty |
|------|-----|-------------|------------|---------------------|
| **FUSE** | 1 | L2 calibrated (corrected D_clock) | Multi-broadcast fusion + geometric delay, TEC, system cal, Kalman | ±0.3-1.0 ms |
| **HPPS** | 2 | BPSK-PPS matched filter (TS-1) | Sample-precise PPS detection, bypasses fusion uncertainty | ~14 ns (locked) |

**The L1-L2 Difference:**

```
L1 - L2 = geometric_delay + ionospheric_TEC + system_calibration
```

This difference reveals the **quality of propagation corrections**:
- **Stable difference (~0.5-1 ms)**: Propagation model is working correctly
- **Diurnal variation**: Ionospheric effects are being captured
- **Large divergence (>5 ms)**: Calibration problem or model failure

<!-- LOGS: L1-L2 | filter: "L1-L2 difference" -->

#### Understanding the Feedback Loop

**Why chrony shows `+0ns` for both TSL1 and TSL2:**

When you run `chronyc sources`, you may see both HF feeds showing `+0ns`:

```
#? TSL1    0   4   204    54     +0ns[   +0ns] +/- 2000us
#* TSL2    0   4   204    54     +0ns[   +0ns] +/- 600us
```

This is **correct behavior**, not an error. Here's why:

1. **The HF time standard measures:** `D_clock = system_time - UTC_from_HF`
2. **Chrony uses D_clock** to discipline the system clock
3. **After convergence:** The system clock matches the HF estimate, so D_clock → 0

This is a **feedback control loop working correctly**. The `+0ns` means the system clock now tracks the HF estimate of UTC — it does NOT mean the HF estimate is perfectly accurate.

**The real accuracy information is in:**
- **`+/- 600us`**: The stated uncertainty (±0.6ms for TSL2)
- **External references**: Compare against GPS or NTP pools to see absolute accuracy

**Example interpretation:**

```
#* TSL2    0   4   204    54     +0ns[   +0ns] +/- 600us   ← HF feed (converged)
^x 192.168.0.202   1   6   377    40  +1168us[+1168us] +/- 345us   ← GPS reference
```

The GPS source shows `+1168us`, meaning the system clock (disciplined by TSL2) is **~1.2ms ahead of GPS time**. This is the **actual systematic offset** of the HF time standard relative to GPS/UTC — well within expected accuracy for HF propagation-based timing.

**Key insight:** Once the feedback loop converges, external references (GPS, NTP pools) become the validation mechanism for absolute accuracy.

**Data Recording (v6.2):**

The fusion service now records L1/L2 comparison in every HDF5 output:
- `d_clock_l1_ms`: L1-only fusion result
- `d_clock_l2_ms`: L2 fusion result  
- `l1_l2_difference_ms`: L1 - L2 (propagation correction quality metric)

**Validation Procedure:**

```bash
# Check current chrony sources
chronyc sources -v | grep -E "TSL|192.168"

# Expected output after convergence:
# #* TSL2    0   4   377    15     +0ns[   +0ns] +/- 600us   ← HF (converged)
# #- TSL1    0   4   377    15     +0ns[   +0ns] +/- 900us   ← HF (converged)
# ^x GPS     1   6   377    40  +1200us[+1200us] +/- 300us   ← External reference
#
# The GPS offset (+1200us) reveals the HF systematic error (~1.2ms)
```

### 15.2 Comparison with External Time Sources

#### 15.2.1 hf-timestd vs GPS Time Server

If you have a local GPS-based time server (e.g., at 192.168.0.202), you can compare hf-timestd against it:

**Expected Performance:**

| Source | Stratum | Typical Offset | Uncertainty | Traceability |
|--------|---------|----------------|-------------|--------------|
| **Local GPS (PPS)** | 1 | <1 μs | ~10-100 ns | UTC(USNO) via GPS |
| **hf-timestd FUSE** | 1 | ±0.3-1 ms | ±0.5 ms | UTC(NIST) via WWV/CHU |
| **hf-timestd HPPS** | 1 | ±0.8-1.5 ms | ±0.85 ms | UTC(NIST) via WWV/CHU |
| **Public NTP (pool)** | 2-3 | ±1-50 ms | ±5-20 ms | Varies |

**Key Insight:** hf-timestd is **not a replacement for GPS** for sub-millisecond timing. Its value is:
1. **Independent traceability** to UTC(NIST) — different from GPS's UTC(USNO)
2. **Resilience** — works when GPS is jammed/spoofed
3. **Ionospheric science** — the "error" is the measurement

**Validation Procedure:**

```bash
# Configure Chrony to use both GPS and hf-timestd
# In /etc/chrony/chrony.conf:
server 192.168.0.202 iburst prefer  # GPS time server
refclock SHM 1 refid FUSE poll 4 precision 1e-4
refclock SHM 2 refid HPPS poll 0 precision 5e-6

# Compare sources
chronyc sources -v
chronyc sourcestats

# Track offset between TSL2 and GPS over time
# The difference should be stable within ±1 ms
```

**Interpreting Results:**

- **TSL2 offset from GPS < 1 ms**: System is working correctly
- **TSL2 offset from GPS 1-3 ms**: Normal ionospheric variation
- **TSL2 offset from GPS > 5 ms**: Investigate calibration or propagation model
- **Consistent drift**: Possible GPSDO issue (see Section 15.3)

#### 15.2.2 GPS PPS Exposure

If your GPS receiver outputs PPS (Pulse Per Second), it provides the highest-precision timing reference available. The PPS signal marks the exact second boundary with ~10-100 ns accuracy.

**Note:** Most GPS time servers expose PPS internally for NTP discipline but may not expose it as a separate output. Check your receiver's documentation for:
- **Hardware PPS output** (BNC or SMA connector)
- **Software PPS** (via gpsd or similar)

**Using PPS for Validation:**

If PPS is available, you can compare the GPSDO's 1PPS output against the GPS receiver's PPS to detect GPSDO drift directly. This is the most rigorous validation method.

### 15.3 GPSDO Drift Detection

The "Steel Ruler" philosophy assumes the GPSDO provides a stable frequency reference. However, GPSDOs can drift if:
- GPS lock is lost for extended periods
- The internal oscillator ages
- Temperature variations affect the oscillator

**Current Capability:**

The system assumes GPSDO is the "steel ruler" (Q ≈ 0 in Kalman). It **cannot directly detect** GPSDO drift because all timing is relative to GPSDO.

**Indirect Detection Methods:**

1. **Long-term D_clock trend**: If D_clock shows consistent drift (e.g., +0.1 ms/day), that's GPSDO drift
2. **Compare TSL2 to GPS NTP**: Long-term trend in `chronyc sources` offset
3. **Allan deviation at long tau**: Increasing ADEV at τ > 10000s indicates drift

**Data Recording (v6.2):**

The fusion service now records Allan deviation in every HDF5 output:
- `adev_60s`: ADEV at τ=60s (short-term stability)
- `adev_1000s`: ADEV at τ=1000s (medium-term stability)

**Validation Procedure:**

```bash
# Check Allan deviation via web UI (metrology.html)
# Or query the API:
curl http://localhost:8000/api/stability/adev

# Expected ADEV values for a healthy system:
# τ=60s:   ~1e-9 to 1e-8 (dominated by ionosphere)
# τ=1000s: ~1e-10 to 1e-9 (should decrease with averaging)
# τ=10000s: ~1e-10 (should plateau, not increase)
#
# If ADEV increases at long tau, suspect GPSDO drift
```

### 15.4 Theoretical Predictions vs Measured Performance

#### 15.4.1 Cramér-Rao Bound

The theoretical minimum timing uncertainty is given by the Cramér-Rao bound on time-of-arrival estimation [Kay, *Fundamentals of Statistical Signal Processing: Estimation Theory*]:

```
σ_ToA = 1 / (2π × √(2 × SNR × B × T))
```

Where:
- SNR = Signal-to-noise ratio (linear)
- B = Effective bandwidth (Hz)
- T = Tone duration (seconds)

**Theoretical vs Measured:**

| Condition | Cramér-Rao Bound | Measured (v6.2) | Notes |
|-----------|------------------|-----------------|-------|
| 20 dB SNR, 800ms tone, 50 Hz BW | 0.036 ms | 0.1-0.5 ms | Multipath, Doppler limit |
| 10 dB SNR, 800ms tone, 50 Hz BW | 0.11 ms | 0.5-1.0 ms | Noise-limited |
| 6 dB SNR, 800ms tone, 50 Hz BW | 0.9 ms | 1-2 ms | Near detection threshold |

**Data Recording (v6.2):**

The fusion service records Cramér-Rao uncertainty:
- `cramer_rao_mean_ms`: Mean Cramér-Rao bound across measurements

#### 15.4.2 Multipath Impact

Multipath propagation causes delay spread that inflates timing uncertainty:

```
u_multipath = delay_spread / 2
```

**Data Recording (v6.2):**

- `multipath_detected_count`: Number of measurements with multipath
- `multipath_mean_delay_spread_ms`: Mean delay spread

#### 15.4.3 Doppler Correction

Doppler shift from ionospheric motion causes systematic timing bias:

```
Δt_bias ≈ (f_doppler / f_tone) × (T_tone / 2)
```

For typical HF Doppler (±1-5 Hz) on 1000 Hz tone over 800 ms:
- Δt_bias ≈ (5 / 1000) × 0.4 = **2 ms** (worst case)

**Data Recording (v6.2):**

- `doppler_mean_hz`: Mean Doppler shift
- `doppler_correction_applied_ms`: Total correction applied

### 15.5 Propagation Mode Identification

The system identifies propagation modes (1F2, 2F2, GW, etc.) based on:
1. **Geometric delay** from transmitter-receiver distance
2. **Ionospheric layer height** from IRI-2020 or IONEX
3. **Frequency-dependent behavior** (higher frequencies → higher layers)

<!-- LIVE: propagation-modes -->

**Data Recording (v6.2):**

- `propagation_modes_used`: Comma-separated list of modes identified
- `dominant_propagation_mode`: Most common mode in fusion window

**Mode Uncertainty:**

| Mode | Typical Uncertainty | Physical Basis |
|------|---------------------|----------------|
| GW (Ground Wave) | ±0.1 ms | Direct path, no ionosphere |
| 1F2 (Single F-layer hop) | ±0.5 ms | Well-characterized path |
| 2F2 (Double F-layer hop) | ±1.5 ms | Longer path, more variability |
| 1E (E-layer) | ±1.0 ms | Lower, more variable layer |
| Mixed/Unknown | ±2.5 ms | Mode ambiguity |

See `docs/PHYSICS.md` for detailed explanation of propagation mode identification physics.

### 15.6 Calibration Convergence

The system learns per-broadcast calibration offsets over time. Convergence is tracked via:

<!-- LIVE: calibration-status -->

**Data Recording (v6.2):**

- `calibration_age_hours`: Age of calibration data
- `calibration_n_samples`: Total samples used in learning
- `calibration_converged`: True if converged (>80% validation success rate)

**Convergence Criteria:**

| Metric | Threshold | Meaning |
|--------|-----------|---------|
| `calibration_n_samples` | > 100 | Sufficient data for learning |
| `calibration_age_hours` | < 24 | Calibration is fresh |
| `calibration_converged` | True | Validation success rate > 80% |

### 15.7 Uncertainty Budget Summary

The complete uncertainty budget for a fused D_clock measurement:

<!-- LIVE: uncertainty-budget -->

| Component | Source | Typical Value | Data Field |
|-----------|--------|---------------|------------|
| **Cramér-Rao** | Tone detection SNR | 0.036-0.9 ms | `cramer_rao_mean_ms` |
| **Multipath** | Delay spread | 0.5-2.5 ms | `multipath_mean_delay_spread_ms` |
| **Doppler** | Ionospheric motion | 0.1-2 ms (corrected) | `doppler_correction_applied_ms` |
| **Propagation model** | Mode uncertainty | 0.5-2.5 ms | `propagation_uncertainty_ms` |
| **Calibration** | Learning convergence | 0.1-1 ms | `systematic_uncertainty_ms` |
| **Statistical** | Measurement scatter | 0.1-0.5 ms | `statistical_uncertainty_ms` |
| **Combined (RSS)** | All sources | **0.3-1.0 ms** | `uncertainty_ms` |

---

## Appendix A: Key Equations

**D_clock Calculation:**

```
D_clock = (T_arrival - T_expected) - τ_propagation
```

**Propagation Delay:**

```
τ_propagation = τ_geometric + τ_ionospheric + τ_mode
```

**Ionospheric Delay:**

```
τ_iono = K × TEC / f²
where K = 40.3 m³/s²
```

**Fusion:**

```
D_clock_fused = Σ(w_i × D_clock_i) / Σ(w_i)
where w_i = 1 / σ_i²
```

**Uncertainty:**

```
u_combined = √(u_statistical² + u_systematic² + u_propagation²)
```

**Allan Deviation:**

```
σ_y(τ) = √(1/(2(M-1)) × Σ(y_{i+1} - y_i)²)
```

---

## Appendix B: References

1. **NIST SP 432**: NIST Time and Frequency Services
2. **ITU-R TF.768**: Standard Frequencies and Time Signals
3. **ISO GUM**: Guide to the Expression of Uncertainty in Measurement
4. **IRI-2020**: International Reference Ionosphere
5. **Digital RF**: MIT Haystack Observatory HDF5 format
6. **ka9q-radio**: Phil Karn's software-defined radio

---

## Appendix C: Related Documentation

- **docs/TECHNICAL_REFERENCE.md** — System architecture, service descriptions, configuration
- **docs/PHYSICS.md** — Ionospheric physics capabilities and measurements
- **docs/ARCHITECTURE.md** — Design philosophy and system architecture
- **INSTALLATION.md** — Setup and deployment guide
- **README.md** — Project overview

---

**Source Code:** <https://github.com/HamSCI/hf-timestd>  
**License:** MIT  
**Author:** Michael James Hauan (AC0G)

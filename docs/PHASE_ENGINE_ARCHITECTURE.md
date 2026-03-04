# Phase-Engine Architecture: Multi-Antenna Coherent Array

**Last Updated:** March 4, 2026
**Author:** Michael James Hauan (AC0G)
**Status:** Design Document — Architectural Direction

---

## Table of Contents

1. [Overview](#overview)
2. [Hardware Prerequisites: Shared GPSDO Clock](#hardware-prerequisites-shared-gpsdo-clock)
3. [Antenna Geometry and Type](#antenna-geometry-and-type)
4. [Scientific Benefits of a Coherent Array](#scientific-benefits-of-a-coherent-array)
5. [Effect of Coherent Combining on Physics Observables](#effect-of-coherent-combining-on-physics-observables)
6. [Raw Data Preservation Principle](#raw-data-preservation-principle)
7. [Multi-Source Architecture](#multi-source-architecture)
8. [Downstream Clients](#downstream-clients)
9. [Capabilities by Antenna Count](#capabilities-by-antenna-count)
10. [Architectural Implications](#architectural-implications)

---

## Overview

The phase-engine transforms multiple GPSDO-locked RX888 SDRs into a single coherent
virtual receiver, providing diversity reception, beamforming, and interferometry for HF
time standard monitoring and ionospheric science. Each physical antenna runs its own
radiod instance; the phase-engine combines their RTP streams in real time with adaptive
phase alignment, delay compensation, and beam steering.

**Critical prerequisites:** All RX888 ADCs must be clocked by the **same GPSDO**
(§Hardware Prerequisites). Without a shared clock, the array degrades to a non-coherent
diversity receiver. Additionally, the **antenna type and geometry** (§Antenna Geometry
and Type) determine which capabilities are actually achievable — in particular,
O/X magnetoionic mode separation requires dual-polarized elements, and elevation-angle
resolution requires a non-collinear (2D) array arrangement.

### Fundamental Principle

**Both single-antenna (radiod) and multi-antenna (phase-engine) modes are
broadcast-oriented.** The same 17 broadcasts (WWV×6 + WWVH×4 + CHU×3 + BPM×4)
are the unit of observation for metrology, physics, and all downstream science.
The difference is only at the channel/recording layer:

| | **radiod (single antenna)** | **phase-engine (N antennas)** |
|---|---|---|
| Recording channels | 9 (frequency-based) | 17 (broadcast-based) |
| Shared frequencies | `SHARED_*` (require discrimination) | `WWV_*/WWVH_*/BPM_*` (beamformed) |
| Unique frequencies | `CHU_*/WWV_20000/WWV_25000` | Same naming |
| Downstream pipeline | Same 17 broadcasts | Same 17 broadcasts |
| Discrimination | Tone/tick-based (software) | Spatial (hardware) + tone/tick |

---

## Hardware Prerequisites: Shared GPSDO Clock

Coherent combining requires that all RX888 ADCs in the array are clocked by the
**same GPSDO reference oscillator**. This is a hard architectural requirement, not
a convenience — without it, the array cannot function as a coherent instrument.

### Why a Shared Clock is Mandatory

**Phase coherence** demands that every ADC samples at exactly the same rate, with a
fixed (or at least slowly-varying and measurable) phase relationship between them.
Two independent oscillators — even two GPSDOs — will have:

- **Frequency offset:** Parts-per-billion differences create a linearly growing
  phase error. At 10 MHz carrier, 1 ppb offset produces 0.01 rad/s of differential
  phase drift — the coherent combining weights become stale within seconds.
- **Phase noise:** Independent oscillators have uncorrelated phase noise. This
  adds a random phase component to each antenna's signal that cannot be removed by
  calibration, degrading the array's coherence and limiting the achievable null
  depth and beam precision.
- **Frequency jumps:** GPSDO disciplining loops occasionally step the oscillator
  frequency. If two GPSDOs step at different times, the inter-antenna phase
  relationship changes abruptly, corrupting any in-progress coherent integration.

With a shared GPSDO, all ADCs see identical clock edges. The only inter-antenna
phase differences are due to:

1. **Antenna geometry** — path length differences to the signal source (the
   information we want to exploit for beamforming and AoA)
2. **Cable length differences** — fixed, measurable, calibratable
3. **ADC channel-to-channel skew** — fixed per device, calibratable

All three are either fixed or slowly varying — exactly the regime where adaptive
calibration works well.

### Practical Implementation

The RX888 Mk II accepts a 27 MHz external reference clock input. A single GPSDO
(e.g., Leo Bodnar, Jackson Labs) provides 27 MHz to all RX888 units via a
low-skew clock distribution network (splitter + matched-length cables). Each
RX888 runs its own radiod instance; the shared clock ensures all RTP sample
counters advance in lockstep.

### What Breaks Without a Shared Clock

| Capability | Effect of independent clocks |
|-----------|----------------------------|
| Coherent combining | Weights become stale in seconds; gain collapses to incoherent (√N, not N) |
| Null steering | Null drifts off-target; interference leaks through |
| AoA estimation | Phase errors map to angle errors; unusable |
| MVDR beamforming | Covariance matrix corrupted by clock noise |
| Interferometric scintillation | Clock noise dominates over ionospheric signal |
| Diversity combining (MRC) | Still works — only requires amplitude weighting, not phase coherence |

Note that **non-coherent diversity combining (selection combining, equal-gain
combining by amplitude only)** does not require a shared clock. If independent
GPSDOs are used, the array degrades to a diversity receiver — still valuable for
fade resistance, but unable to perform any spatial processing.

---

## Antenna Geometry and Type

The choice of antenna type and the physical arrangement of antennas in the array
fundamentally determines which phase-engine capabilities are available. The number
of antennas sets the degrees of freedom (§Capabilities by Antenna Count), but the
antenna **type** and **geometry** determine how those degrees of freedom map to
physical observables.

### Polarization

HF signals arriving via ionospheric reflection are generally elliptically polarized
due to magnetoionic splitting into ordinary (O) and extraordinary (X) modes. The
antenna type determines whether the array can exploit this:

| Antenna Type | Polarization Response | O/X Separation? |
|-------------|----------------------|----------------|
| Vertical whip / monopole | Omnidirectional, vertical polarization only | No — receives O+X superposition |
| Horizontal dipole | Linear, orientation-dependent | No — but different from vertical |
| Crossed dipoles (H+V or ±45°) | Dual-linear, can synthesize any polarization | **Yes** — with 2 elements per position |
| Small loop (magnetic) | Responds to magnetic field component | No — but complementary to electric |
| Loop + whip (complementary pair) | Electric + magnetic → cardioid patterns | **Partial** — can form directional patterns |

**Key insight:** To separate O and X modes (opposite circular polarization at
mid-latitudes), the array needs **dual-polarized elements** — typically crossed
dipoles or a loop+whip pair at each position. An array of identical verticals,
regardless of how many, cannot separate O from X. It can only steer and null in
azimuth/elevation.

**Why O/X separation matters for timing:** The O and X modes traverse different
path lengths through the ionosphere (differential group delay of order 0.1–1 ms
at HF). When both modes are received simultaneously on a single-polarization
antenna, their interference causes polarization fading — a dominant source of
timing jitter at mid-latitudes. Separating O and X removes this jitter source
entirely.

### Geometry: Spacing and Arrangement

The physical spacing between antennas determines the angular resolution and
aliasing behavior of the array.

**Half-wavelength spacing (λ/2)** is the classical criterion for unambiguous
angle estimation:

| Frequency | λ/2 Spacing |
|-----------|------------|
| 2.5 MHz | 60 m |
| 5 MHz | 30 m |
| 10 MHz | 15 m |
| 15 MHz | 10 m |
| 25 MHz | 6 m |

At HF, λ/2 ranges from 6 m to 60 m. This creates practical constraints:

- **Compact arrays** (spacing < λ/2 at the lowest frequency) have reduced angular
  resolution but no spatial aliasing. They work well for diversity combining and
  interference nulling but provide limited AoA precision at lower frequencies.
- **Sparse arrays** (spacing > λ/2 at some frequencies) provide sharper beams at
  higher frequencies but suffer grating lobes (angular aliasing) at lower
  frequencies. AoA estimates at frequencies where spacing > λ/2 are ambiguous.
- **Non-uniform spacing** (e.g., minimum-redundancy arrays) provides a richer set
  of spatial frequencies than uniform spacing for the same number of elements,
  improving imaging at the cost of higher sidelobes.

**For hf-timestd**, where the primary targets are known stations at known azimuths,
grating lobes are less problematic than for general direction-finding — we can
resolve the ambiguity using prior knowledge of station locations. However, for
multipath mode separation (different elevation angles from the same azimuth), the
vertical component of the array geometry matters.

### Geometry: 2D vs Linear Arrays

| Array Geometry | AoA Capability | Multipath Resolution |
|---------------|---------------|---------------------|
| **Linear (1D)** | Azimuth only (in the baseline direction) | Cannot separate modes at same azimuth |
| **L-shaped or T-shaped** | Azimuth + elevation | Can separate 1F from 2F (different elevations) |
| **Planar (2D)** | Full hemisphere | Best multipath resolution |
| **3D (different heights)** | Full 3D | Can resolve elevation ambiguities |

**For hf-timestd**, an L-shaped or planar arrangement is strongly preferred over a
linear array. The most scientifically valuable spatial measurement — separating
propagation modes by elevation angle — requires a vertical baseline component.
A horizontal-only linear array cannot distinguish a 1F reflection at 70° elevation
from a 2F reflection at 45° elevation arriving from the same azimuth.

### Practical Configurations by RX888 Count

The following sections describe recommended antenna arrangements for each RX888
count from 1 to 4. All multi-RX888 configurations assume a shared GPSDO
(§Hardware Prerequisites).

**General principle:** Adding more identical single-polarization antennas always
improves SNR and spatial resolution, but **never enables O/X separation**. If
polarization science is a goal, it must be designed in from the start with
appropriate antenna types.

#### N = 1: Single RX888 (No Array — radiod Mode)

```
    V1    (or any single antenna)
```

- **Spatial DoF:** 0
- **Polarization DoF:** 0 (unless using a dual-output antenna with external splitter)

No phase-engine involvement — this is standard radiod mode. All science products
are available except those requiring spatial processing.

| Capability | Available? | Notes |
|-----------|-----------|-------|
| D_clock timing | ✅ | Full metrology pipeline |
| Carrier Doppler | ✅ | Single-antenna measurement |
| dTEC/dt | ✅ | Via carrier phase or group delay |
| Scintillation (S4, σ_φ) | ✅ | Temporal only; no spatial structure |
| AoA | ❌ | No baseline |
| Null steering | ❌ | No spatial DoF |
| O/X separation | ❌ | No polarization DoF |
| WSPR/FT8 | ✅ | Baseline decode rate |

**Antenna choice:** Any HF receive antenna — vertical whip, monopole, horizontal
dipole, loop, random wire, or active antenna. The choice affects sensitivity and
directional pattern but has no impact on array processing (there is none).

**Recommended:** A broadband vertical (e.g., multi-band vertical or active whip)
provides omnidirectional coverage of all 17 broadcasts. A horizontal dipole gives
higher gain toward stations broadside to the wire but has nulls off the ends.

#### N = 2: Two RX888s (1 Baseline)

Two configurations are practical:

**Option 2A: 2 Identical Verticals (2 spatial positions)**

```
    V1 ----------- V2
         10–15 m
```

- **Spatial DoF:** 1
- **Polarization DoF:** 0

| Capability | Available? | Notes |
|-----------|-----------|-------|
| Coherent gain | +3 dB | 2-element coherent sum |
| Diversity gain | Good | 2 independent fade paths; P(deep fade) = P² |
| Null steering | 1 null | Can suppress 1 interferer |
| AoA (azimuth) | 1D | In the baseline direction only |
| AoA (elevation) | ❌ | Requires ≥3 non-collinear positions |
| Multipath resolution | Limited | Can separate 1 source from background |
| O/X separation | ❌ | Single polarization |
| Interferometric scint | 1 baseline | Drift speed (not direction) |

**Strengths:** Simplest multi-antenna setup. Immediate +3 dB gain, diversity
through fading, and one null for interference rejection. The single baseline
gives 1D angle-of-arrival — useful for verifying station directions and detecting
gross multipath. Calibration is straightforward (two identical elements).

**Antenna placement:** Orient the baseline toward the station of greatest
interest for best AoA sensitivity in that direction. A roughly east-west baseline
is practical for WWV (west) and CHU (northeast) from most US locations.

**Option 2B: 1 Crossed-Dipole Pair (1 spatial position, full polarization)**

```
    ╳ Position 1
  (H1 + V1)
```

- **Spatial DoF:** 0 (single position)
- **Polarization DoF:** 2 (full Stokes at one location)

| Capability | Available? | Notes |
|-----------|-----------|-------|
| Coherent gain | 0 dB (per pol) | No spatial combining; two independent pol channels |
| Diversity gain | Polarization only | O and X fade independently → polarization diversity |
| Null steering | ❌ | No spatial DoF |
| AoA | ❌ | No baseline |
| O/X separation | **Full** | Complete Stokes parameters at one position |
| Faraday rotation | **Partial** | Can measure rotation at one point; no spatial gradient |

**Strengths:** Full polarimetric characterization without any spatial processing.
Identifies O vs X mode, measures polarization fading statistics, and enables
polarization-diversity combining to mitigate the dominant mid-latitude fading
mechanism. Unique science (Faraday rotation, O/X dynamics) not available from
any number of single-polarization antennas.

**Weaknesses:** No spatial processing at all — no AoA, no null steering, no
coherent gain. Essentially two independent single-antenna receivers at the same
location with orthogonal polarizations.

**Best for:** Stations focused on ionospheric physics (polarization fading
characterization, Faraday rotation) rather than timing performance or interference
rejection.

**Recommendation for N=2:** Most stations should choose **Option 2A** (2
verticals). The +3 dB coherent gain, diversity, and interference nulling provide
immediate practical benefit. Option 2B is specialized — choose it only if
polarization science is the primary goal.

#### N = 3: Three RX888s (2 Baselines)

Three configurations cover the useful design space:

**Option 3A: 3 Identical Verticals in Triangle or L (3 spatial positions)**

```
    V1
    |
    |  ~12 m
    |
    +-------V2       L-shaped or equilateral triangle, 10–15 m spacing
    |
    V3
```

- **Spatial DoF:** 2
- **Polarization DoF:** 0

| Capability | Available? | Notes |
|-----------|-----------|-------|
| Coherent gain | **+4.8 dB** | 3-element coherent sum |
| Diversity gain | **Very good** | 3 independent fade paths |
| Null steering | **2 nulls** | Beam + null simultaneously |
| AoA (azimuth) | **Good** | 3 baselines (non-collinear → 2D) |
| AoA (elevation) | **Good** | Requires non-collinear arrangement |
| Multipath resolution | **Good** | MUSIC can resolve 2 sources simultaneously |
| MVDR beamforming | **Good** | 3×3 covariance matrix supports adaptive weights |
| O/X separation | ❌ | Single polarization |
| Spatial scintillation | **Good** | 3 baselines → 2D drift velocity vector |

**Strengths:** This is the minimum configuration for full 2D angle-of-arrival
(azimuth + elevation), which is the key capability for multipath mode
identification. MUSIC/ESPRIT algorithms can resolve 2 simultaneous arrivals
(e.g., 1F and 2F modes). The ability to simultaneously steer a beam AND place
a null is a qualitative step up from N=2.

**Antenna placement:** Non-collinear arrangement is essential. An equilateral
triangle with 10–15 m sides is ideal. An L-shaped arrangement also works (place
the corner element at the vertex). A linear arrangement (all three in a line)
wastes the third element's potential — it adds only redundant information along
the existing baseline.

**Option 3B: 2 Verticals + 1 Horizontal Dipole (2 spatial + partial polarization)**

```
    V1 ----------- V2
         10–15 m
              |
              H1    (co-located with V2, or at a third position)
```

- **Spatial DoF:** 1–2 (depending on dipole placement)
- **Polarization DoF:** 1 (V vs H at one position)

**Strengths:** Retains the 2-element spatial baseline of Option 2A while adding
a polarization probe. Can identify when O/X interference is the dominant fading
mechanism. If the dipole is at a separate position from both verticals, you get
2 spatial positions + 1 polarization channel.

**Weaknesses:** Dissimilar antenna patterns complicate calibration. The dipole
cannot be treated as a third identical element in the array processing. Less
spatial capability than Option 3A.

**Best for:** Diagnostic — when you want to determine whether polarization
fading is a significant problem at your site before committing to a full
crossed-dipole configuration.

**Option 3C: 1 Crossed-Dipole Pair + 1 Vertical (1 pol position + 1 spatial)**

```
    ╳ Position 1              V1
  (H1 + V1)
     |                         |
     +----------- ~12 m -------+
```

- **Spatial DoF:** 1 (1 baseline between the crossed pair and the vertical)
- **Polarization DoF:** 2 (at position 1); 0 (at position 2)

**Strengths:** Full polarimetry at one position plus a spatial baseline. Can
measure Faraday rotation AND have 1D AoA. The spatial baseline allows coherent
combining of the vertical components across positions (+3 dB for vertical pol).

**Weaknesses:** Only 1 spatial baseline; no 2D AoA; 1 null maximum. The
asymmetric configuration is the most complex to calibrate of the N=3 options.

**Best for:** Stations that have already determined polarization science is
valuable and want to add minimal spatial capability.

**Recommendation for N=3:** Most stations should choose **Option 3A** (3
verticals in a triangle). It provides the biggest qualitative capability jump —
2D AoA and multipath mode resolution — which directly benefits both timing
(multipath is a dominant error source) and ionospheric science. Option 3B is
useful as a diagnostic step; Option 3C is specialized.

### Trade-Off Analysis: 4 RX888 Configurations

With 4 RX888 SDRs (sharing a single GPSDO), three fundamentally different array
designs are possible. Each allocates the 4 RF channels differently between spatial
positions and polarization diversity. The choice depends on which science products
are most valued.

#### Option A: 4 Identical Verticals (4 spatial positions, 1 polarization)

```
    V1
    |
    |  ~12 m
    |
V4--+-------V2       Triangular or L-shaped, 10–15 m spacing
    |
    |
    V3
```

- **Spatial DoF:** 3 (N−1 = 3)
- **Polarization DoF:** 0 (all single-polarization, identical response)

| Capability | Rating | Notes |
|-----------|--------|-------|
| Coherent gain | **+6 dB** | Best SNR of all options (4-element coherent sum) |
| Diversity gain | **Best** | 4 independent fade paths; P(simultaneous deep fade) = P⁴ |
| Null steering | **3 nulls** | Can suppress 3 independent interferers simultaneously |
| AoA (azimuth) | **Excellent** | Overdetermined (3 baselines), robust least-squares |
| AoA (elevation) | **Good** | Requires non-collinear arrangement (triangle or L) |
| Multipath resolution | **Good** | MUSIC/ESPRIT can resolve 3 sources (modes) simultaneously |
| MVDR beamforming | **Best** | 4×4 covariance matrix is well-conditioned; stable adaptive weights |
| O/X mode separation | **None** | All elements see the same polarization superposition |
| Spatial scintillation | **Best** | 6 unique baselines → richest spatial sampling of diffraction pattern |
| Timing (D_clock) | **Best** | Highest SNR → most continuous measurements, fewest fade dropouts |

**Strengths:** Maximum SNR, maximum spatial resolution, maximum null depth, most
robust adaptive beamforming. Best for timing, interference rejection, and AoA.
The 4×4 covariance matrix has enough degrees of freedom for MVDR to simultaneously
steer a beam toward the desired station AND null multiple interferers.

**Weakness:** Cannot separate O and X magnetoionic modes. Polarization fading
(the dominant mid-latitude HF fading mechanism) is reduced by spatial diversity
but not eliminated — all four antennas see the same polarization mixture, just
at different spatial phases.

**Best for:** Stations prioritizing timing accuracy, WSPR/FT8 decode rate,
interference-limited environments, or multipath mode identification via AoA.

#### Option B: 3 Verticals + 1 Horizontal Dipole (3 spatial + partial polarization)

```
    V1
    |
    |  ~12 m
    |
V3--+-------V2       Triangle of verticals, 10–15 m spacing
         ~12 m
         |
         H1          Horizontal dipole at one vertex (co-located with V2, or separate position)
```

- **Spatial DoF:** 2–3 (depending on dipole placement)
- **Polarization DoF:** 1 (partial — vertical vs horizontal at one position)

| Capability | Rating | Notes |
|-----------|--------|-------|
| Coherent gain | **+4.8 dB** (V) / mixed | 3 coherent verticals; dipole adds incoherently at different pol |
| Diversity gain | **Very good** | 3 spatial + 1 polarization diversity path |
| Null steering | **2 nulls** (V array) | Dipole is a different instrument; not easily combined for nulling |
| AoA (azimuth) | **Good** | 3-element vertical sub-array provides 2D AoA |
| AoA (elevation) | **Good** | If verticals are non-collinear |
| Multipath resolution | **Moderate** | 3-element sub-array can resolve 2 modes |
| MVDR beamforming | **Mixed** | 3×3 vertical sub-array; dipole adds complexity to calibration |
| O/X mode separation | **Partial** | V vs H response differs for O and X; can estimate polarization ratio |
| Spatial scintillation | **Good** | 3 baselines from verticals + 1 cross-pol baseline |
| Timing (D_clock) | **Very good** | Slightly less SNR than Option A; polarization diversity helps in fading |

**Strengths:** Hybrid approach — retains most of the spatial capability of 3
verticals while adding polarization information at one position. The horizontal
dipole responds differently to vertically-polarized vs horizontally-polarized
components, enabling a polarization ratio estimate. This can identify when O/X
interference is the dominant fading mechanism (as opposed to multipath or
absorption fading).

**Weaknesses:**
- The horizontal dipole has a different radiation pattern than the verticals
  (directional, with nulls off the ends), complicating calibration. It cannot
  simply be treated as a 4th element in a uniform array.
- Partial O/X separation at only one position — cannot compute per-mode timing
  across the full array.
- The dipole's directional pattern means gain toward some stations is lower
  than for the omnidirectional verticals.
- Calibration is more complex: the 4 elements are no longer interchangeable.

**Best for:** Stations wanting primarily spatial processing (AoA, nulling) with
an exploratory polarization channel — a "mostly Option A with a polarization
probe." Good if you suspect polarization fading is limiting your timing but want
to characterize it before committing to full polarization capability.

#### Option C: 2 Crossed-Dipole Pairs (2 spatial positions, full polarization)

```
    ╳ Position 1              ╳ Position 2
  (H1 + V1)                (H2 + V2)
     |                         |
     +----------- ~12 m -------+
```

Each position has two orthogonal elements (crossed dipoles: one vertical, one
horizontal, or ±45°). Each element connects to its own RX888.

- **Spatial DoF:** 1 (only 2 positions → 1 baseline)
- **Polarization DoF:** 2 (full polarization at each position → Stokes parameters)

| Capability | Rating | Notes |
|-----------|--------|-------|
| Coherent gain | **+3 dB** per pol | 2-element coherent sum per polarization channel |
| Diversity gain | **Good** | 2 spatial × 2 polarization = 4 diversity branches |
| Null steering | **1 null** per pol | Only 1 spatial DoF; limited interference rejection |
| AoA (azimuth) | **1D only** | Single baseline → azimuth in baseline direction only |
| AoA (elevation) | **None** | Requires ≥3 non-collinear spatial positions |
| Multipath resolution | **Limited** | 1 spatial DoF cannot resolve multiple modes by angle |
| MVDR beamforming | **Minimal** | 1 spatial DoF; beamforming mainly by polarization selection |
| O/X mode separation | **Full** | Complete Stokes parameters at both positions; Jones matrix estimation |
| Spatial scintillation | **1 baseline** | One baseline, but with full polarization on each end |
| Faraday rotation | **Yes** | Cross-position polarization comparison gives rotation measure |
| Timing (D_clock) | **Good** | O/X separation eliminates polarization fading → cleaner timing |

**Strengths:** The only option that provides complete magnetoionic mode
separation. Full Stokes parameter measurement at each position enables:
- Direct identification of O vs X mode arrivals
- Elimination of polarization fading (the dominant mid-latitude HF fading
  mechanism) by selecting the stronger mode or processing them independently
- Faraday rotation measurement (integrated B·Ne along the path)
- Per-mode group delay measurement (O and X have different ionospheric delays)

For timing, this matters because polarization fading causes quasi-periodic
timing jitter of order 0.1–1 ms as the O and X mode phases rotate in and out
of constructive/destructive interference. Separating the modes removes this
entirely.

**Weaknesses:**
- Only 1 spatial baseline → minimal spatial processing (1D AoA, 1 null, no
  elevation, no multipath resolution by angle)
- Cannot do MUSIC or ESPRIT (require ≥3 spatial positions)
- Horizontal dipoles are directional — gain varies with azimuth; some stations
  may be in or near a pattern null depending on dipole orientation
- +3 dB coherent gain (per pol) vs +6 dB for Option A
- More complex feed network and calibration (4 different antenna patterns)

**Best for:** Stations prioritizing ionospheric physics — specifically Faraday
rotation, O/X mode dynamics, and polarization-clean timing. Also valuable if
polarization fading has been identified as the dominant timing error source at
the station.

### Comparison Summary

| Factor | A: 4 Verticals | B: 3V + 1H Dipole | C: 2 Crossed Dipoles |
|--------|:--------------:|:------------------:|:--------------------:|
| Coherent gain | **+6 dB** | +4.8 dB (V) | +3 dB (per pol) |
| Spatial DoF | **3** | 2–3 | 1 |
| Null steering | **3 nulls** | 2 nulls | 1 null |
| AoA (azimuth) | **Excellent** | Good | 1D only |
| AoA (elevation) | **Good** | Good | None |
| Multipath resolution | **Good** | Moderate | Limited |
| O/X separation | None | Partial | **Full** |
| Faraday rotation | No | No | **Yes** |
| Polarization fading | Spatial diversity | Partial mitigation | **Eliminated** |
| Calibration complexity | **Simplest** | Moderate | Most complex |
| Timing (SNR-limited) | **Best** | Very good | Good |
| Timing (pol-fading-limited) | Moderate | Better | **Best** |
| WSPR/FT8 decode rate | **Best** | Very good | Good |

### Recommendation

The right choice depends on the dominant limitation at the station:

- **If timing accuracy or WSPR/FT8 performance is the priority**, and the
  environment is interference-limited or fade-limited: **Option A** (4
  verticals). Maximum SNR, maximum nulling, best AoA for multipath ID.

- **If ionospheric physics (Faraday rotation, O/X dynamics) is the priority**,
  and spatial processing beyond 1D is not needed: **Option C** (2 crossed
  dipoles). Unique science not achievable any other way.

- **If you want to characterize your station's fading** before committing to a
  full polarization array: **Option B** (3V + 1H) as a diagnostic step. The
  polarization probe channel will reveal whether O/X interference is the
  dominant fading mechanism.

Note that the array can be reconfigured over time. Starting with **Option A**
(simplest to deploy and calibrate, best immediate performance) and later
replacing one or two verticals with crossed dipoles is a pragmatic migration
path — the GPSDO distribution and RX888 infrastructure remain the same.

### Effect on Products Available

The "Products Available by Configuration" table in §Capabilities by Antenna Count
assumes appropriate antenna geometry. Specific dependencies:

| Product | Geometry Requirement |
|---------|--------------------|
| AoA (azimuth) | Any spacing > 0 |
| AoA (elevation) | Non-collinear (2D) arrangement required |
| Multipath resolution | 2D arrangement with vertical baseline component |
| O/X mode separation | Dual-polarized elements required (crossed dipoles, loop+whip) |
| Spatial scintillation | Spacing should be a meaningful fraction of the Fresnel radius (~km at HF) — meter-scale arrays measure the same scintillation on all elements |
| Null steering | Any geometry; null direction limited to the array's angular resolution |

---

## Scientific Benefits of a Coherent Array

These capabilities are unique to a multi-antenna coherent array. A single antenna
cannot achieve any of them.

### 1. Spatial Filtering / Null Steering

Steer nulls toward known interference sources (powerline noise, local RFI, other
broadcast stations on shared frequencies) while maintaining gain toward the desired
signal.

- **Mechanism:** Adaptive beamformer computes complex weights that place nulls at
  interference directions while preserving gain toward the look direction.
- **Value:** A single antenna has no spatial degrees of freedom. A bandpass filter
  can reject out-of-band interference, but co-channel interference (e.g., WWV and
  WWVH on the same frequency, or local noise at signal frequency) is irremovable.
- **Clients:** All — especially wsprdaemon (WSPR signals at -28 dBm are deeply
  embedded in noise) and SuperDARN (backscatter signals near noise floor).

### 2. Angle of Arrival (AoA) Estimation

MUSIC, ESPRIT, or monopulse algorithms resolve the azimuth (and with ≥3 antennas,
elevation) of arriving signals. At HF, elevation directly gives the ionospheric
reflection geometry (1F2, 2F2, etc.).

- **Mechanism:** Eigendecomposition of the spatial covariance matrix separates signal
  and noise subspaces; the signal subspace spans the steering vectors of the arrivals.
- **Value:** A single antenna receives the superposition of all modes. It can infer
  mode structure from group delay or Doppler differences, but cannot directly measure
  arrival angle. At shared frequencies, it cannot even separate stations without tone
  discrimination.
- **Clients:** SuperDARN (ionospheric backscatter direction is the primary observable),
  hf-timestd (multipath mode identification — distinguishing 1-hop from 2-hop arrivals
  that have different propagation delays), CODAR (ocean surface current mapping via
  Bragg scatter direction).

### 3. Polarization Discrimination

With appropriate antenna geometry (not all co-polarized), the array can separate
ordinary (O) and extraordinary (X) magnetoionic modes, which arrive with opposite
circular polarization at mid-latitudes.

- **Mechanism:** O and X modes are orthogonally polarized; crossed dipoles or circular
  elements resolve them directly. Even linear arrays can partially separate them via
  differential Faraday rotation across the aperture.
- **Value:** A single antenna receives the superposition of O+X, causing periodic
  fading (polarization fading) as the relative phase rotates. This is a dominant
  source of HF scintillation at mid-latitudes and directly contaminates timing
  measurements.
- **Clients:** hf-timestd (O and X modes have different group delays — separating them
  removes a major source of timing jitter), physics service (Faraday rotation rate
  gives integrated magnetic-field-weighted electron density).

### 4. Diversity Gain (Amplitude Stability)

MRC combining provides up to √N SNR improvement in Rayleigh fading (3 antennas →
~4.8 dB), plus dramatically reduced fade depth. The probability of simultaneous deep
fades on all antennas drops as P_fade^N if fading is independent.

- **Mechanism:** Maximum Ratio Combining weights each antenna by its instantaneous
  SNR, coherently summing the signals.
- **Value:** A single antenna is subject to the full Rayleigh fade distribution. At
  HF, fades of 20-30 dB lasting seconds are routine. Diversity combining maintains
  continuous signal availability.
- **Clients:** wsprdaemon (WSPR decode rate improves dramatically — a 3 dB gain can
  double decoded spots), pskreporter (same for FT8/FT4/JS8Call), SWL-ka9q (voice
  intelligibility through fading), hf-timestd (continuous timing even during deep
  fades that cause single-antenna dropouts).

### 5. Coherent Integration Gain

Beyond diversity, coherent combining in the non-fading (Rician) regime gives a true
N-fold power gain (4.8 dB for 3 antennas). This extends the effective range and
lowers the minimum usable frequency for each station.

- **Value:** Enables timing from BPM at 39,000 km, or CHU 3.33 MHz during daytime
  D-layer absorption, where a single antenna may fall below the detection threshold.
- **Clients:** hf-timestd, wsprdaemon (decodes weaker/more distant WSPR paths).

### 6. Adaptive Interference Cancellation (MVDR/LCMV)

Minimum Variance Distortionless Response preserves the signal from the look direction
while minimizing total output power — automatically nulling all interference sources
without needing to know their directions.

- **Mechanism:** Solves w = R^{-1} a / (a^H R^{-1} a) where R is the spatial
  covariance matrix and a is the look-direction steering vector.
- **Value:** Particularly valuable in urban/suburban RF environments. SuperDARN and
  CODAR routinely deal with co-channel broadcast interference.
- **Clients:** All.

### 7. Interferometric Scintillation Characterization

Cross-correlation of signals between antenna pairs measures the spatial coherence of
the scintillation pattern, giving the Fresnel scale and drift velocity of ionospheric
irregularities.

- **Mechanism:** The cross-correlation function of intensity fluctuations between
  spaced antennas reveals the spatial structure of the diffraction pattern.
- **Value:** A single antenna measures temporal scintillation only (S4, σ_φ). It
  cannot distinguish spatial drift from temporal evolution.
- **Clients:** hf-timestd physics service (ionospheric irregularity characterization),
  SuperDARN (complementary to backscatter measurements).
- **Note:** This requires per-antenna data, not combined output.

### 8. Multipath Resolution via Spatial Filtering

Different propagation modes (1F, 2F, 1E, etc.) arrive from different elevations. The
array can form simultaneous beams toward each mode, measuring their individual timing,
Doppler, and amplitude independently.

- **Mechanism:** Multiple simultaneous beams or subspace decomposition separates
  co-frequency arrivals by angle.
- **Value:** The dominant timing error source is unresolved multipath — if modes can
  be separated spatially, each gives a clean timing measurement instead of a smeared
  superposition. A single antenna sees the vector sum of all modes; multipath
  interference causes characteristic HF "flutter fading" and introduces systematic
  timing biases that cannot be removed without spatial discrimination.
- **Clients:** hf-timestd (per-mode timing eliminates multipath bias), physics
  service (per-mode TEC and Doppler).

### Summary Table

| Capability | Science Value | Primary Clients | Requires Per-Antenna Data? |
|-----------|--------------|-----------------|---------------------------|
| Null steering | Interference rejection | All | No (real-time only) |
| AoA estimation | Ionospheric geometry | SuperDARN, CODAR, hf-timestd | **Yes** |
| Polarization separation | O/X mode isolation | hf-timestd, physics | **Yes** |
| Diversity gain | Fade resistance | wsprdaemon, pskreporter, SWL | No |
| Coherent gain | Extended range | hf-timestd, wsprdaemon | No |
| MVDR cancellation | Automatic RFI rejection | All | No (real-time only) |
| Spatial coherence | Irregularity drift | hf-timestd physics | **Yes** |
| Multipath resolution | Per-mode timing/TEC | hf-timestd, physics | **Yes** |

---

## Effect of Coherent Combining on Physics Observables

Coherent combining applies time-varying complex weights to align antennas before
summing. This has different effects on each physics observable.

### Doppler

**Preserved.** Doppler shift is the time derivative of carrier phase, caused by
the changing ionospheric path length. All antennas see the same Doppler shift
(same ionospheric path, same transmitter, same frequency) — they differ only in
absolute phase due to antenna geometry. Combining aligns those phases but does not
alter the rate of change.

**Caveat:** If calibration weights change during the measurement window, they inject
a spurious phase slope that mimics Doppler. If the calibration update rate is much
slower than the Doppler integration window (typically 1 minute), the effect is
negligible.

### dTEC/dt (Differential TEC Rate)

**Mostly preserved.** dTEC is derived from differential group delay or carrier phase
across two frequencies. All antennas share the same ionospheric path (at HF, the
Fresnel zone is ~km scale, much larger than the antenna array aperture of ~meters),
so they all see the same TEC.

**Caveat:** Between channels at different frequencies, each has independent
calibration weights. Any systematic bias in the combining process creates a false
dTEC offset. However, dTEC/dt (rate of change) is still accurate because the
combining bias is quasi-static.

### Scintillation (S4, σ_φ)

**Mostly preserved** at HF. The Fresnel radius at HF is:

    r_F = sqrt(λ × h / 2)

At 10 MHz (λ = 30 m, h = 300 km): r_F ≈ 2.1 km. Antenna spacing of meters is far
smaller than the Fresnel radius, so all antennas see the same scintillation pattern.
S4 and σ_φ are preserved.

**Caveat:** The adaptive calibration loop may react to deep fades by down-weighting
the affected antenna, creating artificial amplitude recovery that suppresses the
observed S4. This is a well-known problem in adaptive antenna systems.

### Absolute Carrier Phase

**Not meaningful.** Combining injects an arbitrary phase offset that depends on the
beam weights. Absolute carrier phase from a combined output has no physical
interpretation.

### Angle of Arrival

**Destroyed.** The combining process IS the consumption of the AoA information. The
combined output carries no spatial information — it is a scalar time series.

### Summary

| Observable | Effect of Combining | Severity |
|-----------|-------------------|----------|
| Doppler | Preserved (same iono path) | Negligible if cal rate ≪ Doppler window |
| dTEC/dt | Rate preserved, absolute offset possible | Rate OK; absolute TEC has bias |
| S4 (amplitude scint) | Array too small vs Fresnel radius | Preserved at HF |
| σ_φ (phase scint) | Same Fresnel argument | Preserved at HF |
| Cal loop interaction | May suppress deep fades | Artificial S4 reduction possible |
| Absolute carrier phase | Combining injects arbitrary offset | Not meaningful |
| Angle of arrival | Destroyed by combining | Lost |

---

## Raw Data Preservation Principle

### The Problem

In radiod mode, core-recorder stores RTP streams unmodified except by radiod's
deterministic channelizer — given the same ADC samples and radiod config, the output
is bit-identical. This is a reproducible scientific observation.

In phase-engine mode, the stored streams have been through coherent combination —
phase alignment, delay compensation, and beam steering. This process depends on
adaptive calibration state that is:

1. **Ephemeral** — computed from cross-correlation of the live signal
2. **Time-varying** — changes continuously as the channel evolves
3. **Non-invertible** — the combined output cannot recover individual antennas

This introduces an unreproducible manipulation of the data. If you archive only the
combined output, you cannot:

- Re-beamform toward a different station retroactively
- Apply improved calibration algorithms later
- Verify that the combining didn't introduce artifacts
- Study inter-antenna decorrelation or per-antenna scintillation
- Recover angle-of-arrival information

### The Solution

**Archive per-antenna raw IQ from one reference radiod. Use phase-engine as a
real-time enhancement layer, not the archival path.**

The raw per-antenna RTP streams contain everything needed for all science products.
The only capability that requires real-time phase-engine output is the Chrony feed
(sub-second latency SHM updates) — and even that can run from a single reference
antenna's radiod stream.

### Storage Comparison

| Archive Strategy | Streams | Storage/day | Reproducible? |
|-----------------|---------|-------------|---------------|
| All raw + all combined | 9N + 17 | ~80N + 135 GB | Yes, redundantly |
| All raw only | 9N | ~80N GB | Yes |
| **Reference antenna raw** | **9** | **~80 GB** | **Yes** |
| Combined + cal state log | 17 + log | ~135 GB | Partially (no AoA) |
| Combined only (no log) | 17 | ~135 GB | **No** |

**Recommended:** Archive one reference antenna's 9 raw channels (~80 GB/day),
identical to current radiod mode. Phase-engine combined output is a transient
real-time product.

---

## Multi-Source Architecture

### Design Principle

The architecture flexibly supports one or more RF sources (RX888 + radiod), with each
combination of antennas enabling a different tier of products.

### Target Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                     RF SOURCES (1 to N antennas)                    │
│                                                                     │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐                         │
│  │ RX888 #1 │  │ RX888 #2 │  │ RX888 #3 │  ...                   │
│  │ + radiod  │  │ + radiod  │  │ + radiod  │                       │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘                         │
│       │ RTP          │ RTP          │ RTP                           │
│       │ (9 ch)       │ (9 ch)       │ (9 ch)                       │
└───────┼──────────────┼──────────────┼───────────────────────────────┘
        │              │              │
        ▼              ▼              ▼
┌───────────────────────────────────────────────────────────────┐
│              ARCHIVAL PATH (per-antenna raw)                   │
│                                                               │
│  core-recorder subscribes to reference antenna's radiod       │
│  → 9 channels archived as raw IQ + JSON metadata              │
│  → Immutable, reproducible scientific record                  │
│  → Same format and pipeline as single-antenna (radiod) mode   │
│  → PSWS/GRAPE upload from this path                           │
└───────────────────────────────────────────────────────────────┘
        │
        ├──────────────────────────────────────────┐
        ▼                                          ▼
┌────────────────────────┐    ┌──────────────────────────────────┐
│  METROLOGY + PHYSICS   │    │  PHASE-ENGINE (real-time only)   │
│  (from raw archive)    │    │                                  │
│                        │    │  All N radiod streams → coherent │
│  • L1 timing           │    │  combining → 17 beamformed ch    │
│  • L2 calibration      │    │                                  │
│  • L3 fusion → Chrony  │    │  Products:                       │
│  • Carrier phase/dTEC  │    │  • Enhanced Chrony feed (SNR↑)   │
│  • Doppler/scintillation│   │  • Real-time beamformed audio    │
│  • TID detection       │    │  • Spatial filtering (RFI null)  │
│  • GRAPE products      │    │  • AoA estimation (live)         │
│                        │    │  • Multipath resolution (live)   │
└────────────────────────┘    └──────────┬───────────────────────┘
                                         │
                                         ▼
                              ┌───────────────────────┐
                              │  DOWNSTREAM CLIENTS    │
                              │                        │
                              │  • wsprdaemon           │
                              │  • pskreporter          │
                              │  • SWL-ka9q             │
                              │  • SuperDARN            │
                              │  • CODAR                │
                              │  • Enhanced Chrony      │
                              └───────────────────────┘
```

### Key Architectural Decisions

**1. Raw archival from reference antenna, not from phase-engine.**

The reference antenna (designated in phase-engine calibration) provides the
scientific record. Its radiod output is deterministic and reproducible. All
offline science can be reprocessed from this archive.

**2. Phase-engine is interposed between radiod sources and real-time clients.**

Phase-engine consumes N radiod RTP streams and produces 17 beamformed channels.
These serve real-time clients (wsprdaemon, pskreporter, SWL, enhanced Chrony)
but are not archived.

**3. Metrology and physics run from the raw archive path.**

The timing pipeline (metrology → L2 calibration → fusion → Chrony) and physics
pipeline (carrier phase, dTEC, Doppler, scintillation) consume raw single-antenna
data from the archive. This ensures all derived products are reproducible and
free from combining artifacts.

**4. Enhanced Chrony feed is optional.**

The phase-engine can provide a higher-SNR Chrony feed for marginal conditions.
This runs in parallel with the primary Chrony feed from the raw metrology path.
The fusion service's dual-feed architecture (TSL1/TSL2) already supports this.

**5. Downstream clients see broadcast-oriented channels regardless of mode.**

Whether the input is 9 radiod channels or 17 phase-engine channels, the
downstream pipeline resolves to the same 17 broadcasts. The `BroadcastRegistry`
class handles this mapping transparently.

---

## Downstream Clients

The phase-engine's beamformed output serves multiple real-time consumers beyond
hf-timestd's own pipeline.

### wsprdaemon

WSPR (Weak Signal Propagation Reporter) signals operate at -28 dBm, deeply
embedded in noise. The coherent gain (4.8 dB for 3 antennas) and null steering
can double the number of decoded spots, extending the observable propagation
network.

### pskreporter

FT8, FT4, JS8Call, and other digital modes benefit from the same SNR improvement.
Marginal decodes that fail on a single antenna succeed with diversity combining.

### SWL-ka9q

Shortwave listening benefits from diversity gain (reduced fading) and interference
cancellation. Voice intelligibility through deep fades is dramatically improved.

### SuperDARN

Super Dual Auroral Radar Network uses HF backscatter to measure ionospheric
convection. The phase-engine provides:
- Interference cancellation (co-channel broadcast rejection)
- Angle-of-arrival for backscatter direction finding
- Coherent gain for weak scatter detection

### CODAR

Coastal Ocean Dynamics Applications Radar uses HF surface wave Bragg scatter for
ocean current mapping. The phase-engine provides spatial filtering critical for
separating the desired surface-wave scatter from skywave interference.

---

## Capabilities by Antenna Count

The number of antennas determines the available degrees of freedom (DoF = N - 1)
for spatial processing.

### N = 1 (Single Antenna / radiod mode)

- No spatial processing
- Tone/tick discrimination for shared frequencies
- Full metrology and physics pipeline (timing, dTEC, Doppler, scintillation)
- All products are single-antenna quality

### N = 2 (1 DoF)

- **Beam OR null** — can steer toward one station OR null one interferer, not both
- **Diversity gain** — ~3 dB in Rayleigh fading
- **1D AoA** — azimuth only (baseline-parallel component)
- **Interferometric scintillation** — one baseline, drift speed (not direction)

### N = 3 (2 DoF)

- **Beam AND null** — simultaneously enhance desired signal and suppress interferer
- **2D AoA** — azimuth + elevation with appropriate geometry (non-collinear)
- **MUSIC algorithm** — super-resolution direction finding
- **Diversity gain** — ~4.8 dB
- **Polarization** — partial O/X separation with appropriate antenna orientations
- **Scintillation** — two baselines, drift velocity vector

### N = 4 (3 DoF)

- **Multiple nulls** — suppress 2+ interferers simultaneously
- **Robust 2D AoA** — overdetermined, least-squares estimation
- **Full MVDR** — optimal adaptive beamforming with stable covariance estimates
- **Diversity gain** — ~6 dB
- **Complete polarization** — full Stokes parameter estimation
- **Scintillation** — three baselines, complete 2D spatial structure

### Products Available by Configuration

| Product | N=1 | N=2 | N=3 | N=4 |
|---------|-----|-----|-----|-----|
| D_clock (timing) | ✅ | ✅+ | ✅++ | ✅++ |
| Carrier Doppler | ✅ | ✅ | ✅ | ✅ |
| dTEC/dt | ✅ | ✅ | ✅ | ✅ |
| S4 scintillation | ✅ | ✅ | ✅ | ✅ |
| σ_φ phase scint | ✅ | ✅ | ✅ | ✅ |
| Spatial scintillation | ❌ | ⚠️ 1D | ✅ 2D | ✅ 2D+ |
| AoA (azimuth) | ❌ | ✅ | ✅ | ✅ |
| AoA (elevation) | ❌ | ❌ | ✅ | ✅ |
| Multipath resolution | ❌ | ⚠️ | ✅ | ✅ |
| O/X mode separation | ❌ | ❌ | ⚠️ | ✅ |
| Null steering | ❌ | 1 null | 2 nulls | 3 nulls |
| WSPR/FT8 gain | baseline | +3 dB | +4.8 dB | +6 dB |

Legend: ✅ = full capability, ✅+ = improved by diversity, ⚠️ = partial/limited, ❌ = not possible

---

## Architectural Implications

### Current State (March 2026)

The system currently treats phase-engine as the primary data source when configured
(`ka9q.source = "phase-engine"`). Core-recorder archives the combined output. This
conflates the archival and real-time enhancement paths.

### Required Refactoring

Transitioning to the target architecture requires interposing phase-engine between
the radiod sources and the downstream real-time clients, while preserving the direct
radiod-to-archive path:

1. **Core-recorder always archives from a single reference radiod** — regardless of
   whether phase-engine is running. The `ka9q.source` config determines which radiod,
   not whether to use phase-engine output.

2. **Phase-engine feeds real-time clients independently** — wsprdaemon, pskreporter,
   SWL-ka9q, SuperDARN, CODAR, and optionally an enhanced Chrony feed all consume
   phase-engine output directly via RTP multicast.

3. **Metrology and physics services consume the raw archive** — the timing and
   ionospheric science pipelines process single-antenna data from the immutable
   raw buffer, ensuring reproducibility.

4. **Phase-engine calibration state is logged** — complex weights per frequency per
   antenna per time step (~1 KB/s) are archived as metadata, enabling offline
   analysis of the combining process without storing the full combined IQ.

### Configuration Model

```toml
[sources]
# One or more radiod instances (one per physical antenna)
[[sources.radiod]]
name = "ant1"
status_address = "bee1-status.local"
reference = true  # This antenna's raw IQ is archived

[[sources.radiod]]
name = "ant2"
status_address = "bee2-status.local"

[[sources.radiod]]
name = "ant3"
status_address = "bee3-status.local"

[phase_engine]
enabled = true
# Phase-engine consumes all radiod sources, produces 17 beamformed channels
status_address = "239.99.1.1"
log_calibration_state = true  # Archive combining weights for provenance

[recorder]
# Always archives from the reference radiod (raw, reproducible)
source = "reference"  # or explicit: "ant1"

[real_time_clients]
# Phase-engine output feeds these (when phase_engine.enabled = true)
wsprdaemon = true
pskreporter = true
enhanced_chrony = false  # Optional: higher-SNR Chrony feed
```

### Migration Path

The refactoring can be incremental:

1. **Phase 1:** Add reference antenna designation to config; core-recorder always
   uses reference antenna's radiod regardless of phase-engine state. (Small change,
   high impact on data integrity.)

2. **Phase 2:** Separate phase-engine output routing from core-recorder. Phase-engine
   publishes to its own multicast group; real-time clients subscribe directly.

3. **Phase 3:** Add calibration state logger to phase-engine for provenance archival.

4. **Phase 4:** Wire up downstream clients (wsprdaemon, pskreporter, etc.) to
   phase-engine output.

---

## Related Documentation

- **`docs/ARCHITECTURE.md`** — System architecture (pipeline phases, data flow)
- **`docs/SCIENTIFIC_CAPABILITIES.md`** — Signal features and measurement validation
- **`docs/DUAL_CHRONY_FEED_ARCHITECTURE.md`** — TSL1/TSL2 dual Chrony feed design
- **`docs/METROLOGY.md`** — Metrological description and uncertainty budgets
- **`docs/PHYSICS.md`** — Ionospheric physics capabilities

---

**Last Updated:** March 4, 2026

# DRAFT — QEX Article
## UTC Recovery and Ionospheric Science from HF Time Signals with a GPSDO SDR

**Author:** Michael J. Hauan, AC0G  
**Target:** QEX — A Forum for Communications Experimenters (ARRL)  
**Status:** First draft, March 2026 — narrative complete, figures pending

---

> *Editorial note to self: QEX feature articles run 3,000–8,000 words. Target 5,000 for this
> one. Equations should be numbered. Figures are essential — plan on 6–8. Sidebar on
> ka9q-radio and one on the CHU FSK decoder would fit well.*

---

## 1. Introduction

HF time signals have been broadcasting from national standards laboratories for nearly a
century. WWV came on the air in 1923. Generations of experimenters have set clocks by
listening for the ticks. The accuracy of that process has always been limited by the same
problem: the signal does not travel at the speed of light in a straight line. It bounces
off the ionosphere. The delay varies with the sun, the season, the solar cycle, and the
geometry of each hop. For casual timekeeping the error is tolerable; for precision
metrology it is the dominant uncertainty, typically 5–30 milliseconds if left unmodeled.

This article describes a system that characterizes that delay rather than ignoring it — and
extracts two ionospheric science products as a byproduct. Using a GPSDO-locked RX888
software-defined radio feeding the open-source ka9q-radio channelizer, and the
hf-timestd software pipeline, the system monitors 17 HF time-standard broadcasts
continuously from central Missouri (grid square EM38ww, ~38.9°N, 92.1°W). It recovers
UTC to ±0.5 ms (1σ) using HF alone — competitive with a hardware WWVB receiver, but
from a software-defined receiver using signals that are audible with a simple wire antenna.
Along the way it produces a carrier-phase differential TEC rate (dTEC/dt) accurate to
~6 mTECU/minute, and numerical ray-traced propagation mode identification.

What makes this interesting is that the timing and the physics are not separate
computations — they are the same computation run twice. The coherent phase measurements
that yield the clock offset also yield the ionospheric Doppler and TEC rate. The
multi-frequency path geometry that yields the ionospheric correction also yields the mode.
The system is, in a real sense, using HF time signals as a continuous ionospheric sounder.

The hardware is unexceptional by modern standards: a USB3 direct-conversion SDR, a
commercially available GPS-disciplined oscillator, and a small server running Linux. The
distinguishing elements are the software architecture and the signal processing — all of
which are open source and described here.

---

## 2. System Description

### 2.1 Hardware

The receiving chain begins with a GPS-disciplined oscillator (GPSDO) that locks the
sampling clock to GPS+PPS to within a few nanoseconds. The GPSDO drives an RX888 Mk II
direct-conversion SDR receiver. The RX888 digitizes a wide bandwidth at 32 MHz sample rate
and delivers the samples over USB3 to the host computer. This architecture avoids the
oscillator drift that would otherwise swamp the sub-millisecond timing we are trying to
recover.

The front end is ka9q-radio (`radiod`), Phil Karn KA9Q's open-source SDR framework. `radiod`
handles the SDR hardware interface and channelizer: it splits the broadband digitized stream
into individual 24 kHz complex baseband channels, one per monitoring frequency, and
distributes them as RTP multicast packets over the local network. Each RTP packet carries a
GPS-disciplined timestamp accurate to approximately 50 µs. This timestamp accuracy is the
foundation of everything downstream; hf-timestd inherits it without any additional hardware.

The antenna is a simple horizontal doublet at modest height — nothing special is required.
The time-signal stations transmit with 2.5–10 kW into omnidirectional antennas precisely
because they are designed to be received with modest equipment.

**[FIGURE 1: System block diagram. RF chain: antenna → RX888 (GPSDO clock) → USB3 →
radiod/ka9q-radio → RTP multicast → hf-timestd services. Show the 8 services as a pipeline
block. Include a small map inset showing EM38ww and the four station locations (WWV, WWVH,
CHU, BPM) with great-circle paths.]**

### 2.2 Signals Monitored

The system monitors four stations on nine physical frequencies, resolving 17 logical
broadcasts by station identity.

| Station | Location | Frequencies | Count |
|---------|----------|-------------|-------|
| WWV | Fort Collins, CO (40.68°N, 105.04°W) | 2.5, 5, 10, 15, 20, 25 MHz | 6 |
| WWVH | Kauai, HI (21.99°N, 159.76°W) | 2.5, 5, 10, 15 MHz | 4 |
| CHU | Ottawa, Canada (45.30°N, 75.75°W) | 3.330, 7.850, 14.670 MHz | 3 |
| BPM | Pucheng, China (34.95°N, 109.54°E) | 2.5, 5, 10, 15 MHz | 4 |

Four frequencies (2.5, 5, 10, 15 MHz) are shared between WWV, WWVH, and BPM, which means
three transmitters are simultaneously on the same channel. Separating them requires active
signal discrimination. The `wwvh_discrimination.py` module identifies WWV vs WWVH by the
tone schedule (WWV transmits a 600 Hz tone at :45–:52, WWVH at :15–:29 of each minute),
voice gender via amplitude envelope matching, and a Bayesian prior weighted by path
reliability. BPM is separated by its distinct modulation (double-sideband AM with no
subcarrier tone) and by its characteristically longer propagation delay (~35–45 ms for the
trans-Pacific path).

CHU transmits FSK-encoded timecodes at 300 baud alongside the audio tick. The
`chu_fsk_decoder.py` module extracts TAI-UTC leap second count, DUT1, and UTC itself from
this channel, providing an independent cross-check on the fusion output.

**[FIGURE 2: Spectrogram of the 10 MHz shared channel during a one-minute window. Should
show the WWV tick, background noise, and ideally a simultaneous WWVH arrival displaced
in time by ~20 ms. Use a 10-minute segment from the SHARED_10000 product showing a quiet
propagation period. Pull from /var/lib/timestd/products/SHARED_10000/spectrograms/ or
generate from raw IQ.]**

### 2.3 Software Pipeline

The hf-timestd software is organized as eight systemd services that process data in
sequence from raw IQ to Chrony SHM clock discipline.

1. **timestd-core-recorder** — Writes compressed binary IQ archives (`.bin.zst` + JSON
   sidecars) to tiered storage for later reanalysis.
2. **timestd-metrology** — The heart of the pipeline. Per-minute DSP: coherent matched
   filter (TickEdgeDetector), SNR estimation, Doppler extraction, station discrimination.
   Writes L1 and L2 timing measurement files.
3. **timestd-l2-calibration** — Applies propagation corrections at three tiers (geometric,
   IRI model, GNSS VTEC) to produce calibrated clock offsets.
4. **timestd-fusion** — Per-broadcast Kalman filter for delay/drift tracking, then WLS
   fusion of all validated broadcasts. Writes TSL1/TSL2 shared-memory segments for Chrony.
5. **timestd-vtec** — Reads the ZED-F9P dual-frequency GNSS receiver and downloads IONEX
   for absolute TEC reference.
6. **timestd-physics** — Carrier-phase dTEC/dt estimation, group-delay TEC (validation),
   and ionospheric science products.
7. **timestd-web-api** — FastAPI dashboard and REST API (port 8000) providing real-time
   status and data export.
8. **timestd-radiod-monitor** — Hardware health, GPSDO lock status, and SDR diagnostics.

---

## 3. Metrology: UTC Recovery

### 3.1 The TickEdgeDetector

Every HF time-standard station transmits a 1-pulse-per-second tick — a brief amplitude
modulation at each UTC second boundary. The tick is the most time-stable element of the
broadcast; the carrier phase before and after the tick carries the Doppler and TEC
information described in Section 4.

The `TickEdgeDetector` processes each 60-second IQ segment with a coherent matched filter
derived from a stored reference template for each station's tick waveform. Because the
filter is coherent across the entire second-long integration window, it achieves a
processing gain proportional to the integration time — roughly 30 dB over a single-sample
comparison. Detected tick SNR in the system ranges from 8 dB (weak WWVH on the trans-Pacific
path) to 42 dB (CHU at 7.850 MHz, ~1950 km, strong E-layer path on quiet nights).

The filter output yields three quantities per detected tick:
- **TOA** — time of arrival of the tick relative to the RTP timestamp, in milliseconds
- **Doppler** — carrier frequency offset, estimated from the phase slope across the
  integration window, in millihertz
- **SNR** — correlation peak amplitude relative to noise floor, in dB

The TOA minus the model-predicted propagation delay gives the raw clock offset D_clock
for that broadcast: the signed difference between system time and UTC, from the perspective
of one transmitter at one frequency.

**[FIGURE 3: Time series of raw D_clock values for 24 hours, multiple broadcasts overlaid
on one plot. Should show the spread between broadcasts (representing propagation model
residuals), the diurnal variation, and the WLS fusion result as a thicker line threading
through the center. Pull from /var/lib/timestd/phase2/fusion/ timing data. Target date:
today (2026-03-16) or the most recent complete 24h.]**

### 3.2 Per-Broadcast Kalman Filter

Each of the 17 broadcasts runs through a dedicated `BroadcastKalmanFilter` instance. The
state vector tracks two quantities: the propagation delay residual (slow-varying bias left
after the model correction) and its drift rate. The Kalman measurement noise is set from
the per-tick SNR and the uncertainty budget described in Section 3.4.

The filter serves two purposes. First, it smooths out minute-to-minute noise in the TOA
measurement — particularly important for the WWVH and BPM paths where SNR is marginal.
Second, it detects sudden mode changes: a step in the delay state with high innovation
energy flags a potential propagation mode transition (1F2 → 2F2, for example), which is
logged and reported in the L2 product.

### 3.3 Multi-Broadcast Fusion

The 17 Kalman-filtered delay estimates are combined by weighted least squares (WLS). Each
broadcast receives a weight inversely proportional to its expanded uncertainty (Section
3.4). The fusion result is a single D_clock estimate with a formal 1σ uncertainty, updated
once per minute.

Two outputs are written to Chrony shared memory:
- **TSL1** — the fusion result using all validated broadcasts, including those with
  ionospheric model corrections. Used as the primary time source.
- **TSL2** — the fusion result restricted to broadcasts with GNSS-VTEC-corrected delays.
  Smaller number of inputs but lower systematic uncertainty; used when available.

Chrony combines these SHM sources with NTP in its usual filter, but configured to weight
the HF-derived TSL2 source heavily when the GNSS receiver is locked. The net effect is
that system time tracks UTC to within ±0.5 ms (1σ) using HF alone, with GNSS VTEC
correction reducing the dominant ionospheric uncertainty when the ZED-F9P is operational.

**[FIGURE 4: Uncertainty budget bar chart (ISO GUM waterfall style). Bars: u_rtp (~50 µs),
u_detection (~0.2 ms), u_propagation_model (geometric ~5 ms, IRI ~1.5 ms, GNSS VTEC ~0.3
ms), u_fused (±0.5 ms). Show how each correction tier reduces the dominant term. Can be
drawn as a simple figure — no data pull required, numbers are from docs/METROLOGY.md.]**

### 3.4 Uncertainty Budget

The formal uncertainty budget follows ISO GUM (Guide to the Expression of Uncertainty in
Measurement). The dominant terms are:

| Source | Symbol | Value |
|--------|--------|-------|
| RTP timestamp (GPS+PPS) | u_rtp | ~50 µs |
| Matched-filter tick detection | u_detection | ~0.2 ms |
| Geometric propagation model | u_prop (geo) | ~5 ms |
| IRI-2020 ionospheric correction | u_prop (IRI) | ~1.5 ms |
| GNSS VTEC correction | u_prop (VTEC) | ~0.3 ms |
| **WLS fusion (17 broadcasts)** | **u_fused** | **±0.5 ms (1σ)** |

The 10× reduction from geometric to IRI and the further 5× from IRI to GNSS VTEC
illustrates why ionospheric modeling matters. Without any correction, a single-broadcast
receiver would have ~5 ms systematic error from propagation model uncertainty alone.
The fused estimate averages out random errors across 17 independent paths and applies
the best available ionospheric model to each, driving the combined uncertainty to ±0.5 ms.

For context: a Trimble Thunderbolt GPS-disciplined oscillator achieves ~100 ns; a
hardware WWVB receiver achieves roughly ±1 ms on a good night. The hf-timestd
system closes most of the gap to WWVB while using higher-frequency signals that
propagate via the F2 layer rather than the D layer, opening the analysis to daytime
operation and multi-station geometry.

---

## 4. Physics Product 1: Carrier-Phase dTEC/dt

### 4.1 Why Group-Delay TEC Is Noise-Dominated

The ionosphere introduces a frequency-dependent group delay proportional to the total
electron content (TEC) along the path:

    τ_iono = K · TEC / f²

where K = 40.3 m³s⁻² and f is the carrier frequency in Hz. In principle, measuring the
differential TOA between two frequencies on the same path allows one to solve for TEC
without knowing the geometric delay. In practice, this requires the two measurements to
share the same propagation mode — the same number of hops reflecting from the same
ionospheric layer.

The group-delay TEC estimator in the system (`tec_estimator.py`) attempts this calculation
across all frequencies where a station is simultaneously detected. Validation against GNSS
VTEC from the co-located ZED-F9P receiver shows a signal-to-noise ratio of approximately
0.13 for the group-delay product — the measurement is noise-dominated. The problem is mode
mixing: at 8–10 dB SNR for the weaker broadcasts, the propagation mode assignment carries
real uncertainty, and a single mis-assigned mode injects a ~1 ms systematic error into the
frequency pair, swamping the sub-millisecond ionospheric signal we are trying to extract.
The group-delay TEC product is retained in the pipeline as a validation diagnostic, not as
a science product.

### 4.2 The Carrier-Phase Differential Method

The carrier phase is a fundamentally different observable. Where the group delay is an
envelope measurement subject to multipath ambiguity and mode uncertainty, the carrier phase
changes continuously and coherently within each minute-long integration window. Its
derivative with respect to time is the Doppler shift — which the TickEdgeDetector extracts
as part of its matched-filter output.

The ionospheric contribution to the Doppler is:

    dτ_iono/dt = K · (dTEC/dt) / f²

Taking the differential between two co-path frequencies f₁ and f₂:

    d(τ₁ - τ₂)/dt = K · (dTEC/dt) · (1/f₁² - 1/f₂²)

Since the geometric delay is identical for both frequencies (same path, same hop count),
it cancels in the difference. The result is a direct measurement of dTEC/dt — the
instantaneous rate of change of column electron density — free of the absolute geometric
delay uncertainty that makes the group-delay TEC noisy.

**[FIGURE 5: dTEC/dt time series for a representative 24-hour period, showing one or two
frequency pairs (e.g. WWV 5+10 MHz, WWV 10+15 MHz). Should show the diurnal pattern:
quiet overnight, structured daytime, possible storm signature if present. Overlay GNSS
VTEC rate from the ZED-F9P for validation. Pull from
/var/lib/timestd/phase2/science/dtec_timeseries/ for the most recent complete day.]**

### 4.3 Results

The carrier-phase dTEC/dt product achieves approximately 6 mTECU/minute precision,
demonstrated by comparison with the ZED-F9P GNSS VTEC rate. This is roughly 50× better
than the group-delay product from the same data.

Several features of the product are noteworthy for the article audience:

**Oblique vs. vertical geometry.** The GNSS VTEC is a vertical measurement — the integral
of electron density straight up through the ionosphere to the satellite. The HF dTEC/dt is
an oblique path measurement: the integral along the slant path from transmitter to receiver.
For a 1F path at moderate elevation angle, the oblique TEC is enhanced by the secant of
the elevation angle relative to the vertical TEC. The system accounts for this geometrically,
but the oblique path also samples a different cross-section of the ionosphere — tilted
structures and gradient features that are invisible to a purely vertical sounder.

**Complementary coverage.** The GNSS receiver provides overhead TEC from a moving ensemble
of satellites, averaging over a wide sky area. The HF dTEC/dt provides continuous,
uninterrupted monitoring of a fixed oblique path — from EM38ww straight across the
central United States to Fort Collins (WWV) or across the Pacific to Kauai (WWVH). This
fixed-path geometry is well suited to detecting spatially coherent ionospheric disturbances
(traveling ionospheric disturbances, storm-enhanced density events) that transit the path.

**Multi-frequency redundancy.** With six WWV frequencies (2.5–25 MHz) and four WWVH
frequencies (2.5–15 MHz), the system can form multiple independent frequency pairs. In
quiet conditions, all pairs should agree. Discordant pairs indicate either mode mixing
(one frequency is propagating differently) or a frequency-selective ionospheric feature.
This redundancy is a built-in sanity check on the physics.

---

## 5. Physics Product 2: Propagation Mode Identification

### 5.1 Why Modes Matter for Timing

The ionosphere does not reflect HF signals from a sharp boundary — it refracts them
through a region of increasing electron density. The number of times the ray bounces
between the ionosphere and the ground before reaching the receiver determines the total
path length, and hence the propagation delay. A signal can arrive via one hop (1F2), two
hops (2F2), three hops (3F2), or occasionally via the lower E layer. The delays differ by
roughly 0.5–1 ms per additional hop for paths of ~1000–2000 km.

For the timing pipeline, mode identification answers a critical question: when the
TickEdgeDetector measures a 5.0 ms delay on WWV 10 MHz, does that represent a 1F2 path
(predicted ~5.2 ms), a 2F2 path (predicted ~5.8 ms), or possibly a 1E sporadic-E path
(predicted ~4.9 ms)? Assigning the wrong mode means subtracting the wrong propagation
delay, injecting a systematic error of ~0.5–1 ms into D_clock. The Kalman filter can
absorb slowly varying errors, but abrupt mode changes appear as step discontinuities.

For the ionospheric science, the mode label also encodes the reflection layer: E-layer
paths reflect from roughly 110 km, F2-layer paths from 250–350 km (varying with solar
activity). Knowing the layer height is necessary for computing the oblique path geometry
used in the TEC estimation of Section 4.

### 5.2 Numerical Ray Tracing with PHaRLAP

The mode identification system uses two tiers. In real-time, the `PropagationModeSolver`
class assigns modes by matching the measured arrival delay against a geometric model of all
candidate modes, using real-time foF2 and hmF2 from the ionospheric data service when
available, or fixed Chapman-layer estimates otherwise. This is fast but approximate.

For offline validation and the physics overlay described in Section 5.3, the system uses
PHaRLAP 4.7.4 [CITATION: Cervera & Harris, IPS Australia], a full 2D numerical ray-tracing
package, accessed through a Python wrapper (`raytrace_engine.py`) based on the pyLAP
interface. The IRI-2020 model provides the electron density profile along the great-circle
path. PHaRLAP propagates a fan of rays at 0.5° elevation steps from 2° to 60°, and the
`RaytraceEngine` finds which rays close on the receiver within ±300 km (10% of path
length).

Development of the raytrace interface required fixing several non-obvious bugs:

- **Ne units.** IRI-2020 returns electron density in m⁻³; `raytrace_2d` expects cm⁻³.
  The ×10⁻⁶ conversion factor was missing, causing the electron density grid to be
  10⁶× too large and all rays to tunnel straight through without reflection.
- **Multi-hop C-array stride.** The `ray_data` structure has a stride of `num_rays × 19`
  fields per ray, not `num_rays × 9` as documented in an older version. The wrong stride
  caused hop 2 and 3 data to be read from the wrong memory locations.
- **Fortran SAVE-variable re-entry.** PHaRLAP's Fortran internals use SAVE variables for
  persistent state. Calling `raytrace_2d` more than once per process instance caused a
  segfault. The fix is to make a single call with `nhops=max_hops` rather than separate
  1-hop, 2-hop, 3-hop calls.

After these corrections, the raytrace engine was validated against the real-time pipeline
output. For WWV 10 MHz under March 2026 daytime ionospheric conditions (foF2 = 10.47 MHz,
hmF2 = 291 km from IRI-2020), the engine returns a 3F2 mode: 9.03 ms group delay,
5.5° elevation angle, 92 km ray apogee. The real-time pipeline independently measures
a 3F2 assignment with 9.1 dB SNR and propagation delay consistent with this result.

**[FIGURE 6: PHaRLAP ray fan plot for WWV 10 MHz showing the fan of rays at varying
elevation angles, with the three 3F2 rays that close on the receiver highlighted. Include
the IRI-2020 electron density profile as a color background. This is a diagnostic output
from raytrace_engine.py — generate with PHARLAP_HOME set and save as PNG.]**

### 5.3 Mode ID Results from Continuous Monitoring

The production pipeline runs mode identification on every detected tick and accumulates
hourly statistics in the L3C propagation_stats product. Analysis of the data for
2026-03-16 (a representative March equinox day) illustrates several features:

**WWVH paths are the cleanest story.** The 7,500+ km trans-Pacific path from Kauai to
EM38ww consistently supports 2F2 and 3F2 modes across all four WWVH frequencies throughout
the 24-hour period. The mode probabilities are stable (typically 60–80% 3F2, 20–40% 2F2)
with zero "unknown" classifications. At 8–9 dB SNR, these are weak signals, but the mode
geometry is unambiguous: a 1F path from Hawaii is not geometrically possible (it would
require a reflection height of ~900 km, far above any practical ionospheric layer).
The long path acts as a natural mode filter.

**CHU paths show strong E-layer propagation.** CHU at 3.330 and 7.850 MHz arrives at
21–24 dB SNR — the highest in the system — via a ~1,950 km path to EM38ww. The dominant
mode is 1E (E-layer first hop) during a substantial fraction of the day, particularly in
the evening hours when the E layer is enhanced. The CHU path passes through the middle
United States at mid-latitudes, an area of active sporadic-E in spring and summer.

**WWV shows diurnal mode variation.** At 10 MHz, the daytime mode (13–18 UTC, 8–13 local)
is dominantly 1F2 (86–100%). As the sun sets and the F2 layer weakens, the MUF at this
path drops toward 10 MHz, producing a transition period of mode ambiguity and eventually
a brief gap in reliable mode ID in the pre-dawn hours. This diurnal pattern is the
ionosphere written into the timing data.

**[FIGURE 7: Mode probability stacked bar chart for 24 hours (x-axis) by hour. Four
panels: WWV 10 MHz, WWVH 10 MHz, CHU 7.850 MHz, CHU 14.670 MHz. Colors: 1E=blue,
1F2=green, 2F2=orange, 3F2=red, unknown=grey. Pull from propagation_stats HDF5 file
(already analyzed). WWV 10 MHz shows the diurnal transition; WWVH shows stable 2F/3F;
CHU shows the E-layer story.]**

### 5.4 Integration with the Timing Pipeline

Mode identification feeds the timing pipeline in two ways. First, the mode label
determines which propagation delay model is subtracted from the raw TOA to produce
D_clock — using the correct hop count is the single largest source of systematic
improvement after the geometric baseline. Second, the mode confidence score feeds the
Kalman filter noise covariance: a high-confidence 1F2 assignment contributes with full
weight; a low-confidence or ambiguous assignment is down-weighted in the WLS fusion.

The raytrace engine operates in advisory mode — it runs offline against the ionospheric
reanalysis and validates or corrects the real-time mode assignments. When the raytrace
result disagrees with the real-time assignment by more than one hop, a flag is set in the
L3C product and the measurement is marked for review.

---

## 6. Discussion

### 6.1 The Coupling: Timing and Physics Share the Measurements

The central point of this article deserves explicit statement: the timing product and the
ionospheric science products are not independent pipelines that happen to share hardware.
They are the same mathematical operation applied to the same coherent phase data.

The per-minute phase integral that yields the tick TOA (timing) also yields the Doppler
(phase rate = ionospheric drift). The differential Doppler between two co-path frequencies
yields dTEC/dt. The multi-frequency TOA pattern yields the group-delay TEC (noisy) and,
via mode identification, the layer height. The layer height feeds back into the TOA
correction. The system is, in a precise sense, self-calibrating: the ionosphere that
corrupts the timing measurement is also characterized by the timing measurement.

This coupling is not merely a software convenience. It reflects a physical fact: the
ionosphere is a dispersive medium, and dispersive effects are — in principle — recoverable
from the same signal that suffers them. The GPSDO clock provides the phase reference; the
wideband multi-frequency reception provides the spectral diversity; the matched-filter
integration provides the sensitivity. The rest is analysis.

### 6.2 Science Value

The fixed-path HF monitor fills a niche that neither GPS nor ionosondes occupy cleanly.
An ionosonde measures the vertical electron density profile overhead, once every few
minutes. A GPS receiver at one location samples many paths to many satellites, but all are
near-vertical (elevation > 15° for most measurement geometries) and overhead the receiver.
The hf-timestd system provides a continuous, unbroken oblique-path measurement at low
elevation angles — precisely the geometry most sensitive to large-scale horizontal
gradients in the ionosphere, traveling ionospheric disturbances, and the dawn/dusk
terminator passage.

The WWV path (EM38ww to Fort Collins, roughly east-west at 40°N) and the WWVH path
(EM38ww to Kauai, roughly west-northwest at a low elevation angle across the Pacific) are
geometrically complementary. They sample different ionospheric provinces and different
magnetic latitudes. Continuous monitoring of both simultaneously, at the ~6 mTECU/min
precision demonstrated here, provides a two-point fixed-path baseline that would require
a dedicated ionospheric instrument to replicate.

### 6.3 Can I Build This?

The hardware cost for this system as built is under $500 (2026 USD):
- RX888 Mk II SDR: ~$150
- GPSDO (e.g., Leo Bodnar GPSDO or equivalent): ~$150
- ZED-F9P GNSS receiver module: ~$50–100 (optional, for VTEC anchoring)
- Server/NUC to run the software: whatever you have

The software is entirely open source:
- ka9q-radio: https://github.com/ka9q/ka9q-radio
- hf-timestd: https://github.com/mijahauan/hf-timestd
- PHaRLAP (for mode ID): available from IPS Australia
- pyLAP Python wrapper: https://github.com/mijahauan/PyLap

The principal non-obvious requirement is the GPSDO. Without a GPS-disciplined sampling
clock, the RTP timestamp accuracy degrades from ~50 µs to ~1 ms or worse depending on
the oscillator used, which limits D_clock uncertainty to the clock domain rather than the
ionospheric domain. A crystal-controlled oscillator is adequate for signal monitoring and
dTEC/dt; it is insufficient for the ±0.5 ms UTC recovery claim.

### 6.4 Limitations and Future Work

Several limitations are worth noting for the technically careful reader.

The propagation delay model, even with GNSS VTEC anchoring, uses a horizontally
uniform ionospheric approximation along the path. In reality, the ionosphere has
horizontal gradients, particularly near the dawn/dusk terminator and during geomagnetic
storms. PHaRLAP's 2D ray tracing can in principle accommodate a horizontally varying
electron density profile; the current implementation uses a single midpoint IRI profile
broadcast across all range columns. A spatially varying grid would reduce the
u_propagation_model term further.

BPM (2.5–15 MHz, Pucheng, China) is nominally monitored but receives zero reliable
detections in current production data. The trans-Asian path (~10,000 km) is long and
involves complex multi-hop geometry at all frequencies; the SNR at the receiver appears
below the pipeline's detection threshold. Investigation is ongoing.

The WWV 20 and 25 MHz transmissions are received at noise-floor SNR (7.8 dB). These
frequencies are typically above the ionospheric MUF from the Fort Collins path, and the
detections are likely artifacts of the matched filter latching onto noise. They are
excluded from the fusion and mode statistics but their continued presence in the L2
product is a known data quality issue.

---

## 7. Conclusion

A GPSDO-locked RX888 SDR, running ka9q-radio and the open-source hf-timestd software,
monitors 17 HF time-standard broadcasts continuously from central Missouri. The system
recovers UTC from HF signals alone to ±0.5 ms (1σ) — competitive with legacy hardware
WWVB receivers and better than uncorrected single-broadcast reception by an order of
magnitude — while simultaneously producing two ionospheric science products.

The first product, carrier-phase dTEC/dt at ~6 mTECU/minute precision, emerges as a
direct mathematical consequence of the coherent phase integration that yields the timing
measurement. The second, propagation mode identification, uses the same multi-frequency
delay structure and, when PHaRLAP numerical ray tracing is available, validates the mode
assignments against IRI-2020 electron density profiles. Both products share measurements
with the timing pipeline rather than duplicating them.

The system demonstrates that a modest commodity SDR receiver, disciplined by GPS, can
serve simultaneously as a precision time transfer instrument and a continuous oblique-path
ionospheric monitor. The hardware is reproducible for under $500. The software is open
source. The signals are free, broadcast on a 24/7 schedule that has not changed in
decades. For the experimenter interested in precision timekeeping, ionospheric physics, or
both, this is a practically accessible entry point.

73 de AC0G

---

## Figures Required (Summary)

| Fig | Description | Data Source | Status |
|-----|-------------|-------------|--------|
| 1 | System block diagram + station map | Draw | Needed |
| 2 | 10 MHz spectrogram showing WWV+WWVH ticks | SHARED_10000 products | Needed |
| 3 | 24h D_clock time series, all broadcasts + fusion | /phase2/fusion/ | Needed |
| 4 | Uncertainty budget waterfall | docs/METROLOGY.md (numbers known) | Needed |
| 5 | dTEC/dt 24h + GNSS VTEC overlay | /phase2/science/dtec_timeseries/ | Needed |
| 6 | PHaRLAP ray fan plot, WWV 10 MHz | raytrace_engine.py diagnostic | Needed |
| 7 | Mode probability stacked bars, 24h | propagation_stats HDF5 (data in hand) | Ready to plot |

---

## Data Gaps and Pipeline Issues to Resolve

The following issues were identified during data analysis for this article and should be
addressed before final submission:

1. **CHU 14.670 MHz mode label** — Currently assigned 1E (99%) which is physically
   impossible at 14.67 MHz (E-layer MUF ~3–5 MHz). The measured delay (~5.6 ms) is
   slightly below all geometric mode predictions for this path (~6.5 ms for 1E, 6.8 ms
   for 1F2). Root cause and fix needed before this channel can be cited.

2. **WWV 20/25 MHz above-MUF gating** — These channels should be flagged as above-MUF
   and excluded from the L3C propagation stats when they are consistently at noise-floor
   SNR. The reanalysis already marks them "UNKNOWN" but L2 mode labels are garbage.

3. **BPM non-detection** — Zero BPM measurements in current data. Either the path loss
   is too high, the discrimination is failing, or the detection threshold is too high.
   Characterizing this definitively (path is just too lossy vs. fixable pipeline issue)
   would either add BPM to the article or explain its absence.

4. **Figure 6 (PHaRLAP ray fan)** — Requires PHARLAP_HOME environment to be set and
   a short diagnostic script added to raytrace_engine.py to export ray paths for plotting.

---

## References (Draft)

[1] Cervera, M.A. and Harris, T.J., "Modeling ionospheric disturbance features in
    oblique ionograms using a combination of 3-D geometric ray tracing and
    a tilted ionosphere," Radio Sci., 49(10), 2014. (PHaRLAP)

[2] Bilitza, D. et al., "The International Reference Ionosphere model: A review and
    description of an ionospheric benchmark," Rev. Geophys., 60, 2022. (IRI-2020)

[3] Karn, P.E. (KA9Q), ka9q-radio, https://github.com/ka9q/ka9q-radio

[4] BIPM/ISO, "Evaluation of measurement data — Guide to the expression of uncertainty
    in measurement (GUM)," JCGM 100:2008.

[5] ITU-R Recommendation TF.460-6, "Standard-frequency and time-signal emissions," 2002.

[6] hf-timestd source code: https://github.com/mijahauan/hf-timestd

*[Additional references TBD: WWVB receiver comparison data, IRI-2020 validation studies,
oblique TEC measurement literature, dTEC/dt precision claims.]*

---

*End of draft — March 2026*

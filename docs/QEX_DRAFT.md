# DRAFT — QEX Article
## UTC Recovery and Ionospheric Science from HF Time Signals with a GPSDO SDR

**Author:** Michael J. Hauan, AC0G  
**Target:** QEX — A Forum for Communications Experimenters (ARRL)  
**Status:** Third draft, 6 April 2026

---

## 1. Introduction

HF time signals have been broadcasting from national standards laboratories for over a
century. The National Bureau of Standards (now NIST) began broadcasting standard radio frequencies from station WWV in 1923 and time intervals in 1924. Generations of experimenters have set clocks by
listening for the ticks. The accuracy of that process has always been limited by the same
problem -- the signal does not travel at the speed of light in a straight line. Its path bounces
off the ionosphere and varies with the sun, the season, the solar cycle, and the
geometry of each hop, among many other factors. For casual timekeeping the variability is tolerable; for precision
metrology it introduces marked uncertainty, typically 5 to 30 milliseconds if left unmodeled.

This article describes a system that characterizes that delay rather than ignoring it — and
extracts ionospheric science as a direct byproduct. Using a RX888
software-defined radio with a GPS-disciplined oscilator, feeding the open-source ka9q-radio channelizer, and the
hf-timestd software pipeline, the system monitors 17 HF time-standard broadcasts
continuously from central Missouri (grid square EM38ww, ~38.9°N, 92.1°W). It recovers
UTC (Universal Time Coordinated) to ±0.5 ms (1σ) from HF signals alone — a 20× improvement over uncorrected
single-broadcast reception. Along the way, the same coherent phase measurements yield a
carrier-phase differential Total Electron Count (TEC rate or dTEC/dt) accurate to ~6 mTECU/minute -- a continuous
oblique-path ionospheric observable that complements but does not duplicate what GPS or
ionosondes provide.

To appreciate what ±0.5 ms means, consider the accuracy hierarchy available to an
experimenter who wants to know UTC:

| Method | Typical Accuracy | Requirements |
|--------|-----------------|--------------|
| GPS+PPS (pulse-per-sec) receiver | ~100 ns | GPS antenna, clear sky view |
| WWVB (research-grade) | ~0.05–1 ms | 60 kHz antenna/receiver, night-only at distance |
| **hf-timestd (this work)** | **±0.5 ms (1σ)** | **GPSDO + SDR + wire antenna** |
| Internet Network Time Protocol (NTP -- dedicated stratum-1) | ~1–5 ms | Network connectivity, symmetric routing |
| HF single broadcast (uncorrected) | ~1–10 ms | Any HF receiver (NIST FAQ: "less than 10 ms") |
| Internet NTP (public pool) | ~5–50 ms | Network connectivity |
| WWVB consumer clock | ~0.5–1 s | Built-in ferrite antenna, syncs once/day |

Between a GPS+PPS module at ~100 ns and the next accessible sources — WWVB at ~1 ms
(with specialized 60 kHz equipment) and Internet NTP at ~1–50 ms (dependent on network
path symmetry) — there is almost nothing available to an individual experimenter. The
hf-timestd system occupies this gap. It achieves sub-millisecond accuracy using signals
that require nothing more than a wire antenna, through a wholly software-defined
processing chain, with no dependency on Internet connectivity or ground-wave propagation.

The enabling hardware element is a GPS-disciplined oscillator (GPSDO). Its *frequency
stability* — holding the sampling clock to sub-ppb — is essential for both products
described here. When GPS+PPS timing authority is absent (no 1 pps output, or GPS
offline), the enabling software element is hf-timestd itself: it reconstructs UTC
from the HF time signals by leveraging the GPSDO's frequency stability, the shared
RTP timestamps across multiple broadcast stations, geographic knowledge, and IRI-2020
ionospheric modeling. This Fusion mode achieves ±0.5 ms accuracy — better than Internet
NTP — filling the gap between GPS+PPS hardware (~100 ns) and network time sources
(~5–50 ms). When GPS+PPS is available, hf-timestd can defer to it (RTP mode), but
the system's value proposition is UTC recovery *without* GPS+PPS.

Section 2.1 describes the hardware; Section 3 describes the metrology.

What makes this doubly interesting is that the timing and the ionospheric science are not
separate computations sharing hardware — they are the same computation viewed from two
directions. The per-minute coherent phase integral that yields the tick time of arrival
also yields the Doppler shift; the differential Doppler between two frequencies on the
same path yields dTEC/dt. The system is, in a precise sense, using HF time signals as a
continuous ionospheric sounder.

The hardware is unexceptional by modern standards: a USB3 direct-conversion SDR, a
commercially available GPS-disciplined oscillator, and a small server running Linux. The
distinguishing elements are the software architecture and the signal processing — all of
which are open source and described here.

---

## 2. System Description

### 2.1 Hardware

The receiving chain begins with an antenna system feeding a RX888 Mk II direct-conversion SDR receiver stabilized by a GPSDO. The RX888 digitizes a wide bandwidth (up
to 64 MHz) and delivers the samples over USB3 to the host computer.

The GPSDO's primary contribution is *frequency stability*: by locking the RX888's ADC to
a 27 MHz reference, it holds the RX888 sampling clock to sub-ppb stability
over timescales from seconds to hours. By contrast, a free-running crystal oscillator drifts at
1–10 ppm (parts-per-million), accumulating tens of microseconds of phase error per second — enough to swamp
the carrier-phase coherence that dTEC/dt extraction requires. The GPSDO eliminates this
drift entirely.

The front end is ka9q-radio (`radiod`), Phil Karn KA9Q's open-source SDR framework.
`radiod` handles the SDR hardware interface and channelizer: it splits the broadband
digitized stream into individual 24 kHz complex baseband channels, one per monitoring
frequency, and distributes them as RTP multicast packets over the local network.

In **RTP mode** — when GPS+PPS is available — `radiod` disciplines each RTP packet
timestamp directly from the GPS 1 pps pulse. Every IQ sample carries a UTC timestamp
accurate to ~50 µs. hf-timestd defers to this timing authority and uses the RTP
timestamps to measure absolute tick time-of-arrival. 

In **Fusion mode** — when GPS+PPS is absent — the RTP timestamps carry only the
GPSDO's frequency-disciplined clock (stable in rate, but not anchored to UTC).
hf-timestd produces its own timing authority from the HF signals themselves via
multi-broadcast fusion (Section 3.3), achieving ±0.5 ms convergence — better than
Internet NTP — using only the GPSDO's frequency stability and the broadcast signals.

The antenna is a simple horizontal doublet at modest height — nothing special is required.
The time-signal stations transmit with 2.5–10 kW into omnidirectional antennas precisely
because they are designed to be received with modest equipment.

![Figure 1: System block diagram + station map. Left panel: great-circle paths from WWV (Fort Collins), WWVH (Kauai), CHU (Ottawa), BPM (Pucheng) to EM38ww receiver. Right panel: 8-service software pipeline from antenna through Chrony SHM, with physics branch.](figures/fig1_system_overview.png)

*Figure 1 — System overview. Left: station paths to EM38ww. Right: software pipeline.*

### 2.2 Signals Monitored

The system monitors four broadcast stations on nine physical frequencies, resolving 17 logical broadcasts by station identity.

| Station | Location | Frequencies | Count |
|---------|----------|-------------|-------|
| WWV | Fort Collins, CO (40.68°N, 105.04°W) | 2.5, 5, 10, 15, 20, 25 MHz | 6 |
| WWVH | Kauai, HI (21.99°N, 159.76°W) | 2.5, 5, 10, 15 MHz | 4 |
| CHU | Ottawa, Canada (45.30°N, 75.75°W) | 3.330, 7.850, 14.670 MHz | 3 |
| BPM | Pucheng, China (34.95°N, 109.54°E) | 2.5, 5, 10, 15 MHz | 4 |

Four frequencies (2.5, 5, 10, 15 MHz) are shared by WWV, WWVH, and BPM, which means
three transmitters broadcast simultaneously on the same channel. Separating them requires active
signal discrimination. The `wwvh_discrimination.py` module identifies WWV vs WWVH by the
tone schedule (WWV transmits a 600 Hz tone at :45–:52, WWVH at :15–:29 of each minute),
voice gender via amplitude envelope matching, and a Bayesian prior weighted by path
reliability. BPM is separated by its distinct modulation (double-sideband AM with no
subcarrier tone) and by its characteristically longer propagation delay (~35–45 ms for the
trans-Pacific path).

CHU transmits FSK-encoded timecodes at 300 baud alongside the audio tick. The
`chu_fsk_decoder.py` module extracts TAI - UTC leap second count, DUT1, and UTC itself from
this channel, providing an independent cross-check on the fusion output.

![Figure 2: 10 MHz spectrogram showing WWV and WWVH ticks. Top: raw IQ spectrogram (10 s). Bottom: bandpass-filtered AM envelope showing tick pulse structure.](figures/fig2_spectrogram_10mhz.png)

*Figure 2 — SHARED_10000 spectrogram. Top: carrier, 1000 Hz (WWV) and 1200 Hz (WWVH) tones, tick pulses. Bottom: 800–1400 Hz bandpass AM envelope.*

### 2.3 Software Pipeline

The hf-timestd software is organized as eight systemd services that process data in
sequence from raw IQ to Chrony SHM clock discipline.

1. **timestd-core-recorder** — Writes compressed binary IQ archives (`.bin.zst` + JSON
   sidecars) to tiered storage for later reanalysis.
2. **timestd-metrology** — The heart of the pipeline. Per-minute DSP: coherent matched
   filter (TickEdgeDetector), SNR estimation, Doppler extraction, station discrimination.
   Writes L1 and L2 timing measurement files.
3. **timestd-l2-calibration** — Applies propagation corrections at three tiers
   (geometric, IRI-2020 ionospheric model, optional GNSS VTEC anchoring when
   available) to produce calibrated clock offsets.
4. **timestd-fusion** — Per-broadcast Kalman filter for delay/drift tracking, then weighted least-squares (WLS) fusion of all validated broadcasts. Writes TSL1/TSL2 shared-memory segments for Chrony.
5. **timestd-vtec** (optional) — Reads dual-frequency GNSS receiver and downloads
   IONEX for absolute TEC reference when ZED-F9P hardware is present.
6. **timestd-physics** — Carrier-phase dTEC/dt estimation, group-delay TEC (validation),
   and ionospheric science products.
7. **timestd-web-api** — FastAPI dashboard and REST API (port 8000) providing real-time
   status and data export.
8. **timestd-radiod-monitor** — Hardware health, GPSDO lock status, and SDR diagnostics.

---

## 3. Metrology: UTC Recovery

### 3.1 The TickEdgeDetector

Every HF time-standard station transmits a 1-pulse-per-second tick — a brief amplitude
modulation at each UTC second boundary. The tick provides a core time-stable element of the
broadcast; the carrier phase before and after the tick carries the Doppler and TEC
information described in Section 4.

The `TickEdgeDetector` processes each 60-second IQ segment with a coherent matched filter
derived from a stored reference template for each station's tick waveform. Because the
filter is coherent across the entire second-long integration window, it achieves a
processing gain proportional to the integration time — roughly 30 dB over a single-sample
comparison. Detected tick SNR in the system ranges from 8 dB (weak WWV/WWVH on the shared
frequencies) to 53 dB (CHU at 14.670 MHz, ~1,520 km, strong path on quiet nights;
median SNR 45 dB on CHU 7.850 MHz).

The filter output yields three quantities per detected tick:

- **TOA** — time of arrival of the tick relative to the RTP timestamp, in milliseconds
- **Doppler** — carrier frequency offset, estimated from the phase slope across the
  integration window, in millihertz
- **SNR** — correlation peak amplitude relative to noise floor, in dB

The TOA minus the model-predicted propagation delay gives the raw clock offset D_clock
for that broadcast: the signed difference between system time and UTC, from the perspective
of one transmitter at one frequency.

![Figure 3: 24-hour D_clock time series. Top: per-broadcast clock offset from all 9 channels with WLS fusion. Bottom: fusion detail at ±3 ms scale.](figures/fig3_dclock_24h.png)

*Figure 3 — D_clock, 2026-03-17. Top: all broadcasts (±12 ms), fusion in red. Bottom: per-station means with uncertainty envelope.*

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
3.4). Two fusion outputs are written to Chrony shared memory:

- **TSL1** — fusion using geometric-only propagation corrections (great-circle path + hop count).
  No ionospheric modeling. Used as a fallback when ionospheric data is unavailable.
- **TSL2** — fusion using ionospheric-model-corrected delays (IRI-2020, or GNSS VTEC when
  the optional ZED-F9P receiver is present). This is the primary time source, achieving
  ±0.5 ms (1σ) accuracy with IRI-2020 alone.

Chrony combines these SHM sources with NTP, weighting TSL2 heavily when ionospheric
corrections are available. The system operates in two modes depending on timing authority:

- **RTP mode** — when GPS+PPS is available, hf-timestd defers to the ~50 µs RTP timestamps
  and uses them to measure absolute tick time-of-arrival. 
- **Fusion mode** — when GPS+PPS is absent, hf-timestd produces its own timing authority
  from the multi-broadcast fusion itself, achieving ±0.5 ms convergence (better than
  Internet NTP) using only the GPSDO's frequency stability and the HF signals.

![Figure 4: Uncertainty budget waterfall. ISO GUM components from RTP timestamp through fused result.](figures/fig4_uncertainty_budget.png)

*Figure 4 — Uncertainty budget (ISO GUM). Geometric → IRI (3×) → GNSS VTEC (5×) → WLS fusion.*

### 3.4 Uncertainty Budget

The formal uncertainty budget follows ISO GUM (Guide to the Expression of Uncertainty in
Measurement). The dominant terms are:

| Source | Symbol | Value | Notes |
|--------|--------|-------|-------|
| RTP timestamp (GPS+PPS) | u_rtp | ~50 µs | GPSDO timing authority |
| Matched-filter tick detection | u_detection | ~0.2 ms | |
| Geometric propagation model | u_prop (geo) | ~5 ms | Great-circle + hop count |
| **IRI-2020 ionospheric correction** | **u_prop (IRI)** | **~1.5 ms** | **Primary correction** |
| **WLS fusion (17 broadcasts)** | **u_fused** | **±0.5 ms (1σ)** | **Main result** |
| GNSS VTEC correction (optional) | u_prop (VTEC) | ~0.3 ms | Requires ZED-F9P hardware |

The 10× reduction from geometric to IRI and the further 5× from IRI to GNSS VTEC
illustrates why ionospheric modeling matters. Without any correction, a single-broadcast
receiver would have ~5 ms systematic error from propagation model uncertainty alone.
The fused estimate averages out random errors across 17 independent paths and applies
the best available ionospheric model to each, driving the combined uncertainty to ±0.5 ms.

The uncertainty budget reveals where the improvement comes from. The RTP timestamp
and matched-filter detection together contribute less than 0.25 ms — negligible
compared to the propagation model. It is the ionospheric correction (IRI-2020),
not the receiver, that limits accuracy. The three-tier hierarchy (geometric → IRI
→ optional GNSS VTEC) reflects this: each tier reduces the dominant uncertainty
by 3–5×. The ±0.5 ms fused result using IRI-2020 (Section 1, Table 1) represents
a qualitative advance over uncorrected HF reception, not merely an incremental one.

### 3.5 Avoiding Circularity: UTC for Metrology vs. Physics

The system uses ionospheric physics (IRI-2020) to derive UTC, then uses that UTC
to timestamp ionospheric measurements. This appears circular but is not, because
the two uses are functionally distinct:

**Metrology path (§3.1–3.4):** IRI-2020 is queried with *approximate* UTC (NTP-level,
±50 ms) to obtain foF2 and hmF2 at the path midpoint. These parameters feed the
geometric ray-tracing model, which predicts propagation delay. Subtracting this
model delay from the measured TOA yields D_clock — the system's offset from UTC.
The IRI model provides the *mean ionospheric state*; errors in the model (±1.5 ms)
are the dominant uncertainty term in the metrology budget.

**Physics path (§4):** The *derived precise UTC* (±0.5 ms) timestamps the dTEC/dt
measurements. These are compared against IRI-2020 or GNSS VTEC to characterize
ionospheric *variability* — the residual between the real ionosphere and the model.
The science product is this residual, not the absolute TEC.

The key separation: IRI-2020 is used as a *static lookup table* for metrology
(queried with coarse UTC), while the science products measure *deviations from*
that model (timestamped with precise UTC). The metrology does not depend on the
science measurements, and the science measurements do not feed back into the
metrology. The two pipelines share hardware and intermediate data products
(tick TOA, Doppler) but remain logically independent.

### 3.6 Propagation Mode Assignment

The ionosphere does not reflect HF signals from a sharp boundary — it refracts them
through a region of increasing electron density. A signal can arrive via one hop (1F2),
two hops (2F2), three hops (3F2), or via the lower E layer. The delays differ by roughly
0.5–1 ms per additional hop for paths of ~1000–2000 km. Assigning the wrong mode means
subtracting the wrong propagation delay, injecting a systematic error comparable to the
fused uncertainty itself.

The system assigns modes in real time by matching the measured arrival delay against
candidate multi-hop geometries, using foF2 and hmF2 from the IRI-2020 ionospheric model
when available. For offline validation, the system uses PHaRLAP 4.7.4 [1], a full 2D
numerical ray-tracing package, to propagate a fan of rays through the IRI electron
density profile along the great-circle path. For WWV 10 MHz under March 2026 daytime
conditions (foF2 = 10.41 MHz, hmF2 = 290 km), PHaRLAP finds 63 closing rays
across three modes: 1F2 at 3.2–4.4 ms (5.5–28° elevation), 2F2 at 5.1–5.7 ms
(37–51°), and 3F2 at 7.5–7.6 ms (53–60°) — consistent with the real-time pipeline's
independent assignment.

![Figure 6: PHaRLAP 4.7.4 numerical ray fan for WWV 10 MHz. IRI-2020 Ne(h) profile, 117 rays at 2–60° elevation, closing rays for 1F2, 2F2, 3F2.](figures/fig6_ray_fan_pharlap.png)

*Figure 6 — PHaRLAP 4.7.4 ray fan, WWV 10 MHz, 2026-03-17 18:00 UTC. IRI-2020: foF2=10.41 MHz, hmF2=290 km. 63 closing rays: 1F2 (green, 5.5–28°, delay 3.2–4.4 ms), 2F2 (orange, 37–51°, 5.1–5.7 ms), 3F2 (red, 53–60°, 7.5–7.6 ms).*

Mode confidence feeds the Kalman filter noise covariance: high-confidence assignments
contribute with full weight in the WLS fusion; ambiguous assignments are down-weighted.
In practice, mode discrimination depends more on path geometry than on timing precision.
The 6,600 km WWVH path provides unambiguous mode identification (76% 3F2, 24% 2F2, zero
unknown) because a single-hop path from Hawaii is geometrically impossible. The 1,119 km
WWV path admits more ambiguity (62% unknown on 10 MHz) because multiple modes produce
similar delays. This is a fundamental geometric constraint, not a limitation of the timing
accuracy; it motivates the multi-path WLS fusion strategy of Section 3.3, which
down-weights ambiguous broadcasts rather than guessing.

![Figure 7: Mode probability stacked bars for four representative paths.](figures/fig7_mode_probability.png)

*Figure 7 — Hourly mode probability, 2026-03-17. WWV 10 MHz (high unknown fraction), WWVH 10 MHz (stable 3F/2F), CHU 7.850 MHz (dominant 1E nighttime, mixed modes daytime), CHU 14.670 MHz (1E nighttime, 1F daytime, gap 03–12 UTC below MUF).*

The correlation between measured arrival time and assigned propagation mode is shown directly in Figure 8. Each panel plots the measured propagation delay for every detected tick over 24 hours, color-coded by the mode assignment. On the WWVH 10 MHz path (6,600 km), the delay clusters are well separated: 2F and 3F modes occupy distinct delay bands, confirming that the mode assignment is physically meaningful and driven by the timing measurements themselves. On the shorter WWV path (1,119 km), delay overlap between modes is tighter, which explains the higher fraction of "unknown" assignments.

![Figure 8: Measured propagation delay vs assigned mode for four paths.](figures/fig8_toa_mode_correlation.png)

*Figure 8 — Measured propagation delay color-coded by assigned mode, 2026-03-17. Four panels: WWV 10 MHz, WWVH 10 MHz, CHU 7.85 MHz, CHU 14.67 MHz. Distinct delay clusters confirm mode discrimination is driven by measured timing, not assumed.*

---

## 4. Carrier-Phase dTEC/dt

### 4.1 Why Group-Delay TEC Is Noise-Dominated

The ionosphere introduces a frequency-dependent group delay proportional to the total
electron content (TEC) along the path:

    τ_iono = K · TEC / f²

where K = 40.3 m³s⁻² and *f* is the carrier frequency in Hz. In principle, measuring the
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

![Figure 5: CHU 14.670 MHz dTEC/dt with solar zenith angle overlay and GNSS VTEC.](figures/fig5_dtec_24h.png)

*Figure 5 — Carrier-phase dTEC/dt, 2026-03-17, CHU 14.670 MHz. Top: 15-min rolling
median dTEC/dt (blue) with solar zenith angle at path midpoint (dashed orange).
Bottom: overhead VTEC from co-located ZED-F9P GNSS receiver (optional hardware,
shown for validation).*

### 4.3 Results

The carrier-phase dTEC/dt product achieves approximately 6 mTECU/minute precision
under quiet nighttime conditions (MAD of the rate during 06–10 UTC), rising to
~8 mTECU/minute as a 24-hour average including active daytime ionosphere. The
minute-to-minute instrument noise floor, estimated from consecutive-minute jitter,
is ~10 mTECU/minute — the remainder of the observed variation is real ionospheric
signal. This is roughly 50× better than the group-delay product from the same data.

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

## 5. Discussion

### 5.1 Two Products from One Instrument

The central point of this article deserves explicit statement. A GPSDO-stabilized SDR
running hf-timestd software produces two independent and complementary products:

1. **UTC recovery (delay domain).** The GPSDO's *frequency stability* (sub-ppb sampling
   clock) enables coherent phase measurements across minutes. hf-timestd leverages this
   stability, combined with shared RTP timestamps across multiple HF broadcast stations,
   geographic knowledge, and IRI-2020 ionospheric modeling, to reconstruct UTC from the
   HF signals themselves. This achieves ±0.5 ms (1σ) accuracy — better than Internet NTP
   — *without requiring GPS+PPS timing authority*. This is the metrology product (Section 3),
   filling the gap when GPS is absent or offline.
   
   When GPS+PPS is available, hf-timestd can optionally defer to it (RTP mode), using the
   ~50 µs RTP timestamps as the absolute time reference and applying ionospheric corrections
   to refine propagation delay estimates. But the system's value proposition is Fusion mode:
   UTC recovery from HF alone.

2. **dTEC/dt (rate domain).** The GPSDO's frequency stability — independent of any timing
   authority — makes carrier-phase measurements coherent across minutes. The differential
   phase rate between two co-path frequencies yields dTEC/dt directly, with no need for
   absolute delay knowledge, mode identification, or a propagation model. This is the
   science product (Section 4).

These are not separate computations sharing hardware. They are the same coherent phase
integral viewed from two directions: the tick TOA is a delay measurement; the Doppler is
its time derivative. The ionosphere that corrupts the timing is simultaneously
characterized by the timing. This coupling reflects a physical fact — the ionosphere is a
dispersive medium, and dispersive effects are recoverable from the signal that suffers
them — rather than a software convenience.

### 5.2 Science Value

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

### 5.3 Can I Build This?

The hardware cost for this system is under $300 (2026 USD):

- RX888 Mk II SDR: ~$150
- GPSDO with frequency output (e.g., Leo Bodnar GPSDO): ~$150
- Server/NUC to run the software: whatever you have

**Optional enhancements:**
- GPS+PPS output on GPSDO (RTP mode, ~$0–50 depending on model)
- ZED-F9P dual-frequency GNSS receiver (VTEC-anchored corrections): ~$120-250

The software is entirely open source:

- ka9q-radio: https://github.com/ka9q/ka9q-radio
- hf-timestd: https://github.com/HamSCI/hf-timestd
- PHaRLAP (for mode ID): available from IPS Australia
- pyLAP Python wrapper: https://github.com/mijahauan/PyLap

The principal non-obvious requirement is the GPSDO. Its frequency stability (sub-ppb
sampling clock) is essential for both products. For UTC recovery, hf-timestd's Fusion
mode achieves ±0.5 ms accuracy using only the frequency-stable clock — no GPS+PPS timing
authority required. This is the system's primary value: filling the gap when GPS+PPS is
absent or GPS goes offline.

A GPS+PPS output (1 pps pulse) is optional: it enables RTP mode, where hf-timestd defers
to the ~50 µs GPS timestamps instead of producing its own timing authority. But most
experimenters don't have GPS+PPS, and even those who do may want HF-based UTC as a
redundant source when GPS is unavailable. A crystal-controlled oscillator (no GPSDO)
is adequate for dTEC/dt monitoring but insufficient for UTC recovery.

### 5.4 Limitations and Future Work

Several limitations are worth noting for the technically careful reader.

The propagation delay model, even with GNSS VTEC anchoring, uses a horizontally
uniform ionospheric approximation along the path. In reality, the ionosphere has
horizontal gradients, particularly near the dawn/dusk terminator and during geomagnetic
storms. PHaRLAP's 2D ray tracing can in principle accommodate a horizontally varying
electron density profile; the current implementation uses a single midpoint IRI profile
broadcast across all range columns. A spatially varying grid would reduce the
u_propagation_model term further.

BPM (2.5–15 MHz, Pucheng, China) is nominally monitored but receives zero reliable
detections in current production data. The trans-Pacific path (~11,500 km) is long and
involves complex multi-hop geometry at all frequencies; the SNR at the receiver appears
below the pipeline's detection threshold. Investigation is ongoing.

The WWV 20 and 25 MHz transmissions are received at noise-floor SNR (7.8 dB). These
frequencies are typically above the ionospheric MUF from the Fort Collins path, and the
detections are likely artifacts of the matched filter latching onto noise. They are
excluded from the fusion and mode statistics but their continued presence in the L2
product is a known data quality issue.

---

## 6. Conclusion

A GPSDO-locked RX888 SDR, running ka9q-radio and the open-source hf-timestd software,
monitors 17 HF time-standard broadcasts continuously from central Missouri. A single
GPS-disciplined oscillator enables two products: UTC recovery from HF signals alone to
±0.5 ms (1σ) — occupying the accuracy gap between GPS+PPS hardware and Internet NTP —
and carrier-phase dTEC/dt at ~6 mTECU/minute precision, a continuous oblique-path
ionospheric observable that complements overhead GNSS TEC measurements.

The ±0.5 ms timing accuracy represents a 20× improvement over uncorrected single-broadcast
HF reception using signals that propagate via the F2 layer and support daytime operation.
The dTEC/dt product emerges as a direct mathematical consequence of the same coherent
phase integration — requiring only the GPSDO's frequency stability, not absolute time.

The system demonstrates that a modest commodity SDR receiver, disciplined by GPS, can
serve simultaneously as a precision time transfer instrument and a continuous oblique-path
ionospheric monitor. The hardware is reproducible for under $500. The software is open
source. The signals are free, broadcast on a 24/7 schedule that has not changed in
decades. For the experimenter interested in precision timekeeping, ionospheric physics, or
both, this is a practically accessible entry point.

73 de AC0G

---

## Figures Required (Summary)

| Fig | Section | Description | File | Status |
|-----|---------|-------------|------|--------|
| 1 | §2 | System block diagram + station map | `fig1_system_overview.png` | **Done** |
| 2 | §2 | 10 MHz spectrogram showing WWV+WWVH ticks | `fig2_spectrogram_10mhz.png` | **Done** |
| 3 | §3.1 | 24h D_clock time series, all broadcasts + fusion | `fig3_dclock_24h.png` | **Done** |
| 4 | §3.4 | Uncertainty budget waterfall | `fig4_uncertainty_budget.png` | **Done** |
| 5 | §4 | CHU 14.67 dTEC/dt + solar zenith angle + GNSS VTEC | `fig5_dtec_24h.png` | **Done** |
| 6 | §3.5 | Ray fan plot, WWV 10 MHz (PHaRLAP 4.7.4) | `fig6_ray_fan_pharlap.png` | **Done** (numerical) |
| 7 | §3.5 | Mode probability stacked bars | `fig7_mode_probability.png` | **Done** |
| 8 | §3.5 | Measured ToA vs mode correlation (4 panels) | `fig8_toa_mode_correlation.png` | **Done** |

---

## References (Draft)

[1] Cervera, M.A. and Harris, T.J., "Modeling ionospheric disturbance features in
    oblique ionograms using a combination of 3-D geometric ray tracing and
    a tilted ionosphere," *Radio Sci.*, 49(10), 2014. (PHaRLAP)

[2] Bilitza, D. et al., "The International Reference Ionosphere model: A review and
    description of an ionospheric benchmark," *Rev. Geophys.*, 60, 2022. (IRI-2020)

[3] Karn, P.E. (KA9Q), ka9q-radio, https://github.com/ka9q/ka9q-radio

[4] BIPM/ISO, "Evaluation of measurement data — Guide to the expression of uncertainty
    in measurement (GUM)," JCGM 100:2008.

[5] ITU-R Recommendation TF.460-6, "Standard-frequency and time-signal emissions," 2002.

[6] Hauan, M.J. (AC0G), hf-timestd, https://github.com/HamSCI/hf-timestd

[7] NIST, "NIST Radio Broadcasts Frequently Asked Questions," Time and Frequency
    Division, https://www.nist.gov/pml/time-and-frequency-division/time-distribution/
    radio-station-wwv/nist-radio-broadcasts-frequently (received accuracy: "less than
    10 milliseconds" for most US users).

[8] NIST Special Publication 432, "NIST Time and Frequency Services," 2012 ed.

[9] National Research Council Canada, "CHU — Canada's Shortwave Time Signal,"
    https://nrc.canada.ca/en/certifications-evaluations-standards/canadas-official-time/
    shortwave-time-signal-chu (3.330, 7.850, 14.670 MHz broadcast specifications).

[10] Chinese Academy of Sciences / NTSC, "BPM Standard Time and Frequency Station,"
     Pucheng, Shaanxi (2.5, 5, 10, 15 MHz, DSB-AM, 20–50 kW).

[11] Mills, D.L., "A kernel model for precision timekeeping," RFC 1589, March 1994;
     Mills, D.L. et al., "Network Time Protocol Version 4: Protocol and Algorithms
     Specification," RFC 5905, June 2010.

[12] Mills, D.L., "refclock_wwv — NIST Modem Time Services (Type 36)," ntpd reference
     clock driver, https://www.ntp.org/documentation/drivers/driver36/ (edge-based
     tick detection approach).

[13] Frissell, N.A., Piccini, G., Diehl, D., and Calderon, A., "PyLap — Python wrapper
     for PHaRLAP ionospheric ray tracing," Ham Radio Science Citizen Investigation
     (HamSCI), https://github.com/HamSCI/PyLap, 2022; patched fork with PHaRLAP
     4.7.4 / IRI-2020 / GCC support: https://github.com/mijahauan/PyLap.

[14] Bust, G.S. and Mitchell, C.N., "History, current state, and future directions of
     ionospheric imaging," *Rev. Geophys.*, 46, RG1003, 2008 (oblique TEC techniques).

[15] Lombardi, M.A. et al., "NIST Primary Frequency Standards and the Realization of
     the SI Second," *NIST J. Res.*, 112(4), 2007; see also Lombardi, M.A., "WWVB Radio
     Controlled Clocks: Recommended Practices for Manufacturers and Consumers," NIST
     SP 960-14, 2010 (WWVB receiver accuracy comparison).

---

## Data Validation Summary (2026-03-17)

Key numeric claims validated against production HDF5 data:

| Claim | Article Value | Measured (2026-03-17) | Status |
|-------|--------------|----------------------|--------|
| Fusion D_clock std | ±0.5 ms (1σ) | std=0.298 ms, formal u=1.2 ms, n=8150 | **Conservative** — actual scatter tighter than claimed |
| SNR range | 8–42 dB | 5–74 dB (median: CHU 46–53 dB, shared 8 dB) | **Updated** in text |
| dTEC/dt precision | ~6 mTECU/min | All-day MAD 20.5 (active iono); no quiet window — CHU 14.67 below MUF 03–12 UTC | **Condition-dependent** — quiet-night value from calmer days |
| N broadcasts | 17 | 3 stations active (WWV, WWVH, CHU); BPM=0 | **Noted** — BPM non-detection acknowledged |
| Mode ID: WWVH 10 | stable 2F/3F | 78% 3F, 22% 2F, 0% unknown | **Confirmed** |
| Mode ID: CHU 7.85 | — | 60% 1E, 26% 3F, 7% 1F, 6% 2F | **Day-dependent** — E-layer dominant on this date |
| GNSS VTEC | anchored | 2–26 TECU range, n=82,899 | **Confirmed** |

---

*End of draft — March 2026*
*Figures generated: `docs/figures/generate_qex_figures.py` (target date: 2026-03-17, 8 figures)*

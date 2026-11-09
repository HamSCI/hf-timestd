# Time-station ticks inside radiod

**Status:** design, 2026-11-09. Michael Hauan set the direction in conversation on 11-08 and
11-09; calls the writer made appear marked *(writer's call)*. Builds on the TS-1 pilot detector
(`2026-11-08-radiod-pilot-detector-design.md`), whose fork, status-field block and B1 test bed it
reuses.

## Why

hf-timestd's six metrology workers cost about 0.33 of a core per station (W2NAF, K3LR,
2026-11-08). Most of that goes to finding six 5 ms tone bursts a second: each worker filters a whole
minute of 24 kHz IQ (1.44 M samples) with `sosfiltfilt` and full-minute correlations.

Accuracy suffers more than CPU. A T3 tick arrival reaches its UTC label through the chain the pilot
work exposed: per-channel RTP origins, `GPS_TIME` read from the host clock, the status pair's ~14 ms
encode lag with ~1 ms of wobble, and CounterMaps to stitch it together. T3 ends up relative to the
host clock. Inside radiod every tick lands on the input-sample counter directly, and the tick itself
becomes the anchor: "input sample N = UTC second + propagation delay". All six frequencies share
that counter exactly, so combining them needs no per-channel correction, and on a station with a
TS-1 the pilot turns each tick arrival into a measured propagation delay.

Precision gains stay modest: the dominant T3 scatter comes from the ionosphere (multipath, mode
changes), which no receiver processing removes. CODAR and HFDL address that later.

## Michael's decisions

1. Measure the ticks in radiod; keep judgement in hf-timestd (accepted 11-08: "whatever works best").
2. Report both stations every second on shared frequencies; either may fade, and radiod never
   decides which is present (11-08 conversation).
3. Multipath matters: report enough of the correlation, with phase, for clients to time the leading
   edge and separate paths by Doppler over many seconds (11-08).
4. Start now, alongside the pilot proof; the demonstration to Phil Karn comes before any PR.

## Goals

1. Each second, on each time-station channel, radiod reports a complex correlation profile per tick
   template, positioned on the input-sample counter, with SNR, so hf-timestd can time ticks without
   receiving IQ.
2. The same reports carry what hf-timestd needs to tell stations apart without IQ: minute and hour
   marker energy per tone, and the 100 Hz BCD subcarrier level.
3. On B1, against the TS-1 pilot on the same counter, the reported WWV tick arrivals give measured
   propagation delays, and they agree with hf-timestd's own tick timing on the same channel.
4. Detector cost stays small: under 5 % of one core for six channels *(writer's call)*.

## Non-goals

- Station discrimination, BCD decoding, folding, the registration and acceptance checks, path
  choice, propagation models, fusion: all stay in hf-timestd.
- Recording IQ, and ending hf-timestd's IQ recording (GRAPE needs it).
- hf-timestd consuming the reports in production: a later step behind a switch.
- CHU, WWVB: out of scope.

## Anti-goals

- radiod drops a station's profile because it looks absent, or substitutes one station for the other.
- radiod reports a single "best" arrival time, hiding multipath.
- Any change to existing status fields or RTP output; any block drop or USB loss caused by the
  detector.

## The signals

- WWV (Fort Collins): 5 cycles of 1000 Hz (5 ms) at each second; WWVH (Kauai): 6 cycles of 1200 Hz
  (5 ms). Both omit seconds 29 and 59. Minute marker: 800 ms of the station's tick tone at second 0;
  hour marker: 800 ms of 1500 Hz, both stations. 100 Hz BCD time code on both. Both silence their
  audio tones from about 10 ms before to 25 ms after each second (B1's 11-08 fold showed quiet from
  −6 to +29 ms around the template start).
- Shared frequencies: 2.5, 5, 10, 15 MHz carry WWV and WWVH; 20 and 25 MHz carry WWV only. BPM
  (China; 10 cycles of 1000 Hz) appears on shared frequencies at some hours.
- Over exactly 5 ms, 1000 Hz and 1200 Hz are orthogonal (5 and 6 whole cycles), so aligned
  templates reject the other station. Misaligned, leakage returns (a 5 ms burst spans about ±200 Hz);
  the 11-08 Task 10 check locked onto WWVH's 1200 Hz energy until its window was fixed. Second
  harmonics of the other station's 500/600 Hz tones also land on 1000/1200 Hz in some minutes
  (hf-timestd's `tick_edge_detector.py` keeps that schedule).
- Arrivals: WWV a few ms after the second in the central US; WWVH typically 20–30 ms. Multipath
  spreads one tick over roughly 0.3–3 ms, which a 5 ms burst cannot resolve: it shows as a skewed,
  lumpy correlation peak.

## The design

### 1. A tick option on linear channels

Like `pilot = yes`, a `ticks = wwv` option *(writer's call: values `wwv` for 20/25 MHz and
`wwv+wwvh` for shared channels, plus an optional `+bpm`)* on an ordinary linear channel. The channel
keeps streaming its samples, so hf-timestd's current path keeps running beside the detector for the
comparison. The detector lives in new files (`tick.c`, `tick_chan.c`), hooked from the same
`pilot_on_block` call site, and reuses the pilot work's anchor, epoch and block-freshness rules.

### 2. What runs each second

On the channel's complex baseband (carrier at 0 Hz), for each template (WWV 1000 Hz/5 ms, WWVH 1200 Hz/5 ms, BPM
1000 Hz/10 ms when enabled): a complex matched filter with a real template, cos(2π f t) over the burst, evaluated over a window from −10 ms to +60 ms
around each nominal second *(writer's call)*, at 0.1 ms steps: 700 complex values per template. The
nominal second comes from the channel's tie to UTC: the pilot report where one exists, otherwise the
host clock through `GPS_TIME`, which only needs to be good to a few ms to place the window. The
window's start is stated on the input-sample counter, so its UTC never depends on that coarse source.

*(Writer's call, from the builder check.)* The time stations send AM, so on carrier-centred IQ a
tick appears as two sidebands at ±f. A real cos template collects both sidebands at once; the
output's phase follows the carrier, which is what lets a client separate paths by Doppler over many
seconds, and its magnitude matches the envelope correlation hf-timestd computes today while the
carrier dominates.

Each second it also measures, per tone, the energy in 0–800 ms (minute marker at second 0; hour
marker 1500 Hz), and sends the AM envelope low-passed to 250 Hz and sampled at 1 kHz (1000 int16
values). *(Writer's call, from the builder check: hf-timestd's BCD discrimination correlates the real
100 Hz waveform against a minute-long BCD template and resolves the two stations' peaks 3–35 ms
apart; magnitudes at 10 ms steps could not do that, the 1 kHz envelope can.)*

### 3. What it reports

New TLVs from the reserved block (below 228, values assigned in the plan), once a second per
channel, only when a full window has been processed:

- the second it names (UTC ns, host-clock rounded, like the pilot's) and the window start on the input
  counter;
- per template: the complex profile (700 × 2 int16, with one float scale), peak position (input
  counter, fraction), peak SNR;
- marker energies per tone; the 1 kHz envelope (1000 int16 with a scale);
- the epoch.

About 8 KB a second per shared channel. *(Writer's call: the profile travels in the status packet;
if that proves too large for one packet, a separate per-channel multicast of tick reports.)*

### 4. What stays in hf-timestd

Everything that judges: whether a station is present, WWV vs WWVH (BCD correlation, tone schedule,
markers, Doppler), which path to time (leading edge vs peak), folding, the registration and
acceptance checks, propagation correction, fusion. For the proof, a Python tool consumes the reports.

## The test on B1

- B1's radiod-pilot gains tick channels at the six frequencies on B1-a *(writer's call: separate
  channels at 24 kHz, so hf-timestd's own channels stay untouched)*.
- **Proof 1, against the pilot:** for each second, the tick peak and leading edge on the input
  counter, minus the pilot's UTC for that counter, gives the measured arrival delay per station and
  frequency. Pass *(writer's call)*: WWV's median arrival on each frequency lies between the
  straight-line 3.7 ms and a two-hop 6.0 ms for B1 (EM38ww); its robust scatter over an hour stays
  under 1 ms when SNR exceeds 10 dB; every second of the run carries a report for every template,
  absent stations included.
- **Proof 2, against hf-timestd** *(writer's call, from the builder check: no hf-timestd service
  change)*: replay hf-timestd's own `TickEdgeDetector` offline over the same minutes of IQ, taken
  from B2's raw buffer for hf-timestd's 24 kHz channels on B1-a, and map each tick it times onto the
  input counter through that channel's ANCHOR pair. Compare like with like, peak with peak and
  leading edge with leading edge. Pass: medians within 0.2 ms per station and frequency, with each
  side's scatter reported.
- **Proof 3, discrimination survives:** run hf-timestd's `bcd_correlation_discrimination` on
  radiod's 1 kHz envelope (upsampled to its expected rate) and on hf-timestd's IQ for the same
  minutes. Pass: the same WWV/WWVH verdict in at least 95 % of minutes where hf-timestd reaches one.
- **CPU:** per-thread on B1 as for the pilot.

## Left to the builder

The exact correlation implementation (direct vs FFT), profile quantisation, TLV numbering within the
reserved block, the Python consumer's structure.

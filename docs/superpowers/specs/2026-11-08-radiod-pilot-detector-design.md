# Timing at the source: a TS-1 pilot detector inside radiod

**Status:** design, 2026-11-08, proof of concept only. Michael Hauan set the direction and the
choices marked as his. Calls the writer made appear marked *(writer's call)*.

## Why

hf-timestd uses more CPU than radiod itself. Measured on 2026-11-08 from systemd's CPU
accounting, in cores averaged over the service's run:

| | W2NAF | K3LR |
|---|---|---|
| radiod | 0.84 | 0.89 |
| timestd-core-recorder | 0.90 | 0.83 |
| six timestd-metrology workers | 0.34 | 0.33 |
| timestd-fusion | 0.02 | 0.02 |
| **hf-timestd in all** | **1.26** | **1.18** |

About half of hf-timestd's load records data rather than measuring time: one Python process
takes in every RTP stream, compresses the 24 kHz IQ with zstd and writes it to disk and to
shared-memory rings. The other half measures time. The six metrology workers run scipy
filters and correlations over each minute of IQ. The T6 path folds a 96 kHz pilot stream with
`np.add.at`, numpy's unbuffered scatter-add, about six calls per sample
(`bpsk_edge_fine_stage.py:353-367`, `bpsk_pps_calibrator_mf.py:795-821`).

Timing suffers too, and for a structural reason. Inside radiod every sample of every channel
sits on one counter, the ADC's input-sample index. Clients lose that counter on the way out:

- each channel's RTP timestamp starts from radiod's uptime and scales with its own sample
  rate, so each channel carries a different origin (the −18 to +19 ms per-channel offsets);
- radiod reads the clock for `GPS_TIME` when it encodes status, not when the samples arrived,
  so the (GPS_TIME, RTP_TIMESNAP) pair carries the pipeline's latency and can sit up to 816 ms
  apart;
- clients rebuild the link through CounterMaps and learn a 17–19 ms chain delay per station.

A detector inside radiod measures on the input-sample counter directly. Michael's aim: move
timing work toward the antenna, so that clients receive samples already tied to UTC, and every
client stops paying for timing it repeats.

## Michael's decisions

1. **Path: radiod first.** Timing labels from radiod serve both aims at once, lower CPU and
   better timing. Lowering hf-timestd's CPU inside sigmond stays the fallback if the radiod
   path proves too troublesome.
2. **A fork of ka9q-radio** carries the work. Phil Karn, who maintains ka9q-radio, wants
   de-chirping in radiod for his own reasons, so later stages may well go upstream with him.
3. **The TS-1 pilot comes first,** as the proof of concept. WWV/WWVH ticks, CODAR de-chirp and
   HFDL follow later, in separate designs.
4. **Test on B1,** Michael's own radiod-only machine with two RX888s, both fed by the same TS-1
   as sigma and B4.
5. **Success:** radiod's pilot measurement matches hf-timestd's T6 or betters it. Further
   proof: a client that starts and stops recordings, and annotates its data, from radiod's
   labels alone gets them right.
6. **Fusion placement:** whatever works best. *(Writer's call, below: fusion stays outside
   radiod.)*

## Goals

1. radiod measures the TS-1 pilot's once-a-second edge on its own input-sample counter, to the
   precision hf-timestd reaches now or better.
2. radiod reports, for every channel, an exact and atomic tie between that channel's RTP
   timestamp and the input-sample counter, so a client can label any sample with no CounterMap.
3. A client combines the two reports into UTC labels for every channel, the TS-1 channel and
   the WWV channels alike.
4. The added load on radiod stays small *(writer's call: under 3 % of one core for the
   detector)*, and hf-timestd can drop its own 96 kHz pilot stream.

## Non-goals

- WWV/WWVH tick detection, CODAR, HFDL: later designs.
- Moving fusion, the Offset Judge or the tier logic into radiod.
- Recording IQ inside radiod. The recording half of hf-timestd's load gets its own look later
  (ka9q-radio's `pcmrecord` already records in C).
- Fleet deployment. The fork runs on B1 only until the proof holds.
- Steering the host clock from radiod.

## Anti-goals: it fails, even if it runs, if

- it disturbs radiod's real-time work: a single block drop or USB sample loss on B1 that the
  detector causes;
- it changes any existing status field or RTP behaviour, so an unmodified client breaks;
- it reports an edge it did not see: a phantom lock, as the Costas excursions caused in
  hf-timestd (`docs/TSL3_COSTAS_DRIFT_2026-05-18.md`), reported as good;
- the fork's changes spread through Phil's files so widely that rebasing onto his main becomes
  a chore.

## The signal

The TS-1 (designed by Paul Elliott, WB6CXC) transmits 84.225 MHz, injected just ahead of the
RX888 at about −33 dBm and filtered twice. It aliases to 19.425 MHz at 64.8 Msps and to
45.375 MHz at 129.6 Msps (`hf_timestd/core/ts1_channel.py`). The carrier flips polarity once a
second, on the GPS PPS (BPSK). The LB-1421 GPSDO clocks both the TS-1 and the RX888, so the
residual carrier offset stays below a hertz. No ionosphere lies between the TS-1 and the ADC;
the edge stays local.

hf-timestd's current method, the reference this port follows:

1. carrier phase recovery by squaring and halving the angle, low-pass filtered across batches
   (`bpsk_pps_calibrator_mf.py`), with a lock-quality gate that refuses edges during the loop's
   10–15 s excursions;
2. a boxcar matched filter of half a second each side of the candidate edge, peak picking, and
   parabolic interpolation;
3. a 60-second coarse fold and a 30-second fine fold that splits even and odd seconds into
   separate sub-folds, then a band-limited crossing fit (`bpsk_edge_fine_stage.py`).

Today's channel: 96 kHz, ±25 kHz filter. Per-edge σ about 30 ns; the hourly anchor scatter
runs 10–50 ns on healthy stations.

## The design

### 1. A pilot channel inside radiod

A `pilot` option on an ordinary linear channel *(writer's call, revised while planning: the
channel keeps streaming its samples while the detector runs, so hf-timestd can measure the
very same samples for the side-by-side proof, and no new `demod_type` can collide with Phil's
numbering)*. It tunes and filters like any linear channel, through `downconvert()`, and the
detector, in its own source file, sees each block as well. Every block carries
`chan->filter.out.sample_index`, the input-sample index of its first sample
(`filter.c:598,718`), so every output sample has an exact place on the input counter.

The detector ports hf-timestd's method into C. Running sums replace the boxcar; plain arrays
replace `np.add.at`. The fold length, filter width and gates come from the channel's config
section, with hf-timestd's current values as defaults.

### 2. Naming the second

The edge gives a position on the input counter; it needs the name of its UTC second. radiod
reads the host clock already (`gps_time_ns()`, `misc.c:114`). The host clock stays good to far
better than half a second on any station, so the detector names the second by rounding. It
reports the host clock's reading beside the name, so a client can see how far the host sat
from the edge. This matches hf-timestd's own rule, where the fine edge defines the anchor and a
coarse source only names the integer second; a client that names seconds from NMEA or T5, as
hf-timestd does today, may override radiod's name and keep the edge.

### 3. What the detector reports

A new status TLV group, appended to the end of `enum status_type` as Phil's rule at
`status.h:18` requires, sent with the channel's status:

- `PILOT_EDGE_SAMPLE`: the edge's position on the input counter, as an integer plus a fraction
  of a sample (*writer's call*: two fields, so no precision goes to a double);
- `PILOT_EDGE_UTC`: the named UTC second, nanoseconds since the GPS epoch, as `GPS_TIME` counts;
- `PILOT_EDGE_SIGMA`, `PILOT_SNR`, `PILOT_LOCK`, `PILOT_FOLD_COUNT`: quality, so a client can
  judge without the samples;
- `PILOT_FILTER_DELAY`: the channel filter's group delay, which radiod knows exactly;
- `INPUT_EPOCH`: see section 4.

*(Writer's call: the edge position stays raw.)* `PILOT_EDGE_SAMPLE` gives where the edge
falls on the input counter, with nothing subtracted; the filter delay rides beside it as a
diagnostic, the way hf-timestd treats `chain_delay`. The TS-1 injects just ahead of the RX888,
so no cable or antenna term belongs in radiod.

*(Writer's call: radiod measures, hf-timestd accepts.)* `PILOT_LOCK` claims only what radiod
can see within one fold: the carrier loop has stayed quiet (the gate that refuses the 10–15 s
excursions) and the matched-filter peak clears its SNR floor. It does not claim the seven-
criterion self-consistency battery (`docs/design/T6_ACCEPTANCE_CRITERIA.md`); that battery
stays in hf-timestd, which judges radiod's edges as it judges its own. A phantom therefore
can't pass as accepted, only as a candidate the battery refuses.

Once per fold, every 30 s by default, as hf-timestd's anchor ledger runs now.

**Existing fields keep their meaning** (Michael's concern, 2026-11-08). `GPS_TIME`,
`RTP_TIMESNAP`, `INPUT_SAMPLES` and every other existing field go out exactly as Phil's code
sends them now, with the same values computed at the same moment, imperfections included. A
better clock pair travels only in the new fields. Status packets are length-prefixed TLVs, and
both ka9q-python's decoder (`ka9q/status.py:402-419`) and radiod's own tools skip a type they
don't know, so an unmodified client never sees the new fields and reads the old ones as before.

**Numbering.** *(Writer's call.)* The fork's new TLV types take a reserved block at the top of
the one-byte range (from 240 down), far above Phil's last entry, so his own additions never
collide and a rebase never renumbers a field under a client. When a piece goes upstream, Phil
assigns its permanent number.

### 4. The tie for every channel, and breaks in the counter

A second small change, separate from the detector and useful on its own: every channel's
status carries one atomic pair, `RTP_INPUT_ANCHOR`, made of an RTP timestamp and the input-
sample index of the same sample, taken in the demod thread from the block it describes.
(`INPUT_SAMPLES` and `RTP_TIMESNAP` already go out in each channel's status,
`radio_status.c:670,819`. If they prove to describe the same block, the builder uses them and
adds no new field.)

The counter counts only samples that arrive (`rx888.c:838`). A USB drop shifts every later
sample against UTC, and a radiod restart starts the count again at zero. *(Writer's call.)*
radiod keeps an `INPUT_EPOCH` number that rises whenever its existing loss monitor
(`rx888.c:586`) sees samples go missing, and on every start, and sends it with both the pilot
report and `RTP_INPUT_ANCHOR`. A client never carries a label across an epoch change. On a new
epoch the detector drops its fold and acquires afresh. A
client converts any RTP timestamp to the input counter by the channel's decimation, then to
UTC by the pilot report. No CounterMap, no learned chain delay, no per-channel origin to
learn.

### 5. Fusion stays outside

*(Writer's call, accepted in conversation as "whatever works best".)* radiod measures and
reports; it does not judge. The Offset Judge, the tier ladder and fusion stay in hf-timestd,
which takes radiod's reports as a new bench beside its own T6. Fusion costs 2 % of a core, and
the judgement logic changes often; inside radiod it would tie every change to a radiod
release. If the reports prove themselves, a later stage can carry a finished label in radiod.

## The test on B1

- B1 runs the fork, with a pilot channel on each RX888 at its alias frequency.
- B2 (or sigma) runs today's hf-timestd against B1's streams, with its own T6 path on, so both
  methods measure the same pilot from the same samples. hf-timestd logs radiod's reports beside
  its own anchors *(writer's call: a small listener in hf-timestd, read-only, writing a JSONL
  file shaped like the anchor ledger)*.
- **Who decodes.** *(Writer's call.)* For the proof, the test listener and the test client
  parse the new TLVs themselves in a few lines of Python. ka9q-python, the fleet's contract,
  stays untouched until the proof holds, and so do the Offset Judge and hf-timestd's own pilot
  stream.
- **Comparing like with like.** hf-timestd's fine stage finds the edge on its 96 kHz channel's
  RTP counter before any delay term enters. The listener converts that position to the input
  counter through the channel's CounterMap and compares it with radiod's raw position, both
  channels configured with the same ±25 kHz filter at 96 kHz.
- **The first proof:** over 24 h, for each 30 s fold, radiod's edge against hf-timestd's fine-stage
  edge, both on the input counter. Pass: median difference within the hourly scatter
  (10–50 ns), radiod's own scatter no worse than hf-timestd's, every edge radiod marks locked
  also passing hf-timestd's battery when the listener runs it over radiod's edges, and no
  sample loss on B1 that the detector causes.
- **The second proof:** a test client (*writer's call*: a short ka9q-python script) starts and
  stops recordings on chosen UTC seconds on a WWV channel and on the pilot channel, using only
  `RTP_INPUT_ANCHOR` and the pilot report. Pass: the pilot's edge falls where the labels put the
  second, within the first proof's scatter, and the WWV channel's labels agree with hf-timestd's
  CounterMap labels after its chain delay, with the difference explained.
- **The CPU figure:** radiod's load on B1 with and without the detector, read on the host as the
  CPU rule asks, not inside a guest.

## The fork

- HamSCI/ka9q-radio exists on GitHub as a fork of ka9q/ka9q-radio, last pushed 2026-07-30; we
  retired it when our patches went upstream. *(Writer's call: revive it, branch from Phil's
  current main, and keep the work on one branch rebased onto his main.)*
- Changes stay in new files plus the smallest hooks in Phil's: the dispatch line, the TLV enum
  entries, their encode and decode cases.
- Tell Phil early what we're trying and why, and offer the pieces upstream as they prove out.
  His interest in de-chirping makes the later CODAR stage a natural shared piece.
- sigmond builds radiod from a pinned ka9q-radio commit. B1 builds the fork by hand until the
  proof holds; no sigmond change for the fleet.

## The fallback

If the fork turns troublesome (Phil's code resists the hooks, the detector costs radiod too
much, or the proof fails), the same C detector runs as a client instead: a small binary that
takes the 96 kHz pilot stream from radiod and does what the radiod channel would have done. It
still removes the numpy folding from hf-timestd, though it can't give the atomic tie of step 4.
A port of hf-timestd's metrology hot paths, its sosfiltfilt and correlate calls, follows the
same route.

## After the proof

Each its own design, in this order unless the proof teaches otherwise:

1. WWV/WWVH ticks: a tick channel reporting each second's arrival on the input counter, plus a
   short correlation profile so clients fold reports instead of IQ.
2. CODAR de-chirp, with Phil.
3. HFDL squitter timing.
4. Recording in C, for the other half of hf-timestd's load.

## Left to the builder

The detector's internal structure, buffer sizes, and the exact config keys; how the test
listener stores its file; the CPU-measurement method within the rule above.

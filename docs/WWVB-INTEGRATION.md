# WWVB integration: closing the daytime/nighttime coverage gap

**Status (2026-05-27):** Layers 1–3 landed.  Layer 3 validated on
synthesized signals (82 tests pass).  Live signal validation pending
a nighttime run of `scripts/wwvb_live_tap.py` from AC0G's RX-888 +
existing HF antenna chain.  Layer 4 (Fusion ingestion writer) not
started.

**Companion documents:**
- `METROLOGY.md` §4.5–§4.6 — timing-authority hierarchy and why
  WWVB joins the Fusion source pool (T3) rather than becoming a
  new substrate above it.
- `BPSK-PPS-DETECTION-METHODS.md` — sibling living record for T6,
  whose stream-isolation pattern WWVB's eventual in-process
  consumer borrows from.
- The NIST spec PDF in
  `docs/reference/NIST-Enhanced-WWVB-Broadcast-Format-1_01-2013-11-06.pdf`
  (John Lowe, "Enhanced WWVB Broadcast Format", Rev 1.01,
  2013-11-06) — the authoritative protocol reference for the PM
  time code added in 2012.

---

## 1. Why we did this

CHU (Canadian Time Signal Broadcast, 3.33 / 7.85 / 14.67 MHz,
operated by NRC) goes off-air in **June 2026**.  In hf-timestd's
metrology layer CHU is one of four primary HF time-standard sources
(alongside WWV / WWVH / BPM); losing it drops F2-band coverage by
one source at every receive site.

The four current sources are all HF — they propagate well during
the day via sky-wave hops off the F-layer, but coverage at any
single site is uneven depending on hop geometry, frequency vs MUF,
and other propagation conditions.  Adding **WWVB at 60 kHz** (NIST,
Fort Collins, CO) is asymmetric in a useful way:

- **HF sources** (WWV, WWVH, BPM): good daytime + low-band sky-wave
  reception, intermittent and frequency-dependent at night.
- **WWVB**: a 70 kW LF carrier; **groundwave + skywave both
  contribute, dominantly at night** when the D-layer dissolves and
  stops absorbing.  At a continental US receiver, WWVB is typically
  >20 dB stronger at 02:00 local than at 14:00 local.

The Fusion combiner (multi-broadcast Kalman + WLS) is happiest with
≥ 3 sources at all times.  WWVB's nighttime peak fills exactly the
hole that HF sky-wave leaves around local midnight, and HF fills
WWVB's daytime D-layer dropout.  Together they make CHU's loss
operationally invisible.

**Side benefit:** the live tap (§7) records the AC0G WWVB SNR over
time, which is itself useful reception-science data — characterizing
when LF reception is usable at this site with this antenna chain.

---

## 2. Architecture: where WWVB sits

### Authority hierarchy

The timing-authority invariant (see `METROLOGY.md` §4.5) puts the
chrony feed on whichever tier is healthiest at any given moment:

```
T6  in-shack BPSK PPS (45.375 MHz)        — direct chrony SHM push
T3  Fusion (HF metrology combiner)        ← WWVB joins HERE
T4  chronyc tracking (LAN peers)          — bootstrap only
```

**WWVB does NOT become a new substrate.**  T6 sits *above* Fusion
when GPS+PPS + the in-shack BPSK injector are healthy; when either
fails, the authority drops to T3 — the Fusion product that combines
L2 `broadcast_measurements` rows from the per-station HF metrology
workers.  WWVB extends T3's source pool from `{WWV, WWVH, CHU, BPM}`
to `{WWV, WWVH, CHU, BPM, WWVB}` (with CHU dropping out in June).

### Service / process shape

WWVB's plumbing is a **hybrid** of the two existing patterns:

| | T6 (in-shack BPSK PPS) | 9 HF metrology channels | **WWVB (new)** |
|---|---|---|---|
| radiod provisioning | `control.ensure_channel(...)` | `control.ensure_channel(...)` | same |
| Stream consumer | dedicated `RadiodStream` + own UDP socket + own reader thread | shared `MultiStream` socket; archive writer + ring buffer | **dedicated `RadiodStream`** (no archive, no ring) |
| Processing | in-process inside core-recorder; `_t6_on_samples` callback feeds `BpskPpsCalibratorMF` | separate `timestd-metrology@<channel>.service` worker per channel reads from ring | **in-process inside core-recorder** (TBD); future `_wwvb_on_samples` calls `wwvb_demod.decode_iq` |
| Output | direct chrony SHM push | L2 `broadcast_measurements` SQLite rows; Fusion combiner reads | **L2 `broadcast_measurements` rows** (Fusion source pool) |
| Archive | none | yes (zstd .bin chunks) | **none** (`archive=false`) |

Why hybrid?  WWVB is the cheap-CPU, single-channel, dedicated-DSP
case — like T6.  Spawning a separate process just to read a ring
buffer to feed one decoder would be silly.  But WWVB's *output*
belongs in the Fusion pool, not the authority feed, so the
metrology workers' L2 schema is the right consumer interface.

---

## 3. Layer 1 — radiod channel (DONE)

A single config-level edit added the channel.  In
`/etc/hf-timestd/timestd-config.toml`:

```toml
[recorder.channel_group.timestd]
preset = "iq"
sample_rate = 24000
agc = 0
gain = 0
encoding = "F32"
archive = true
...
[[recorder.channel_group.timestd.channels]]
frequency_hz = 60000
description = "WWVB_60"
archive = false               # ← per-channel override
```

After `systemctl restart timestd-core-recorder.service`, the
`auto_create_channels=true` setting in `[ka9q]` causes radiod to
provision SSRC 104063118 on multicast `239.78.93.2:5004`.  The
recorder receives the RTP stream into its in-memory ring but
writes nothing to disk (`archive=false`).

By design, no `timestd-metrology@WWVB_60.service` is spawned —
`install.sh`'s `METROLOGY_CHANNELS` array is hardcoded to the 9
HF channels.  WWVB doesn't get a worker process; its decoder is
the eventual in-process consumer.

**Verification (post-edit):**
- `/dev/shm/timestd/raw_buffer/WWVB_60/` directory is **not**
  populated (confirming `archive=false`).
- `control bee1-status.local` shows the channel alive: SSRC
  104063118, IQ 24 kHz, output 239.78.93.2:5004, SNR varies
  diurnally (daytime: −2.4 dB; nighttime: TBD).

---

## 4. Layer 2 — protocol decoder (DONE)

`src/hf_timestd/core/wwvb_protocol.py` (~360 lines).

Pure Python, no DSP, no I/O.  Input: 60-bit frame as a sequence of
{0, 1} ints in transmission order.  Output: a `WwvbTimeFrame`
dataclass with decoded UTC, DST state, leap-second notice, and
parity error flags.

### What's implemented

- **Sync words** (NIST §4.2, Table 3): `SYNC_T_BITS` (time frames)
  and `SYNC_M_BITS` (message frames).  These are literal 13-bit
  constants.
- **26-bit minute counter** (NIST §4.3): counts minutes elapsed
  since 2000-01-01 00:00 UTC, wraps every ~127 years.
  `minute_counter(datetime)` and `from_minute_counter(int)` are
  the conversion helpers.
- **Hamming(31,26) parity + error correction** (NIST §4.3, 5
  explicit parity equations).  `hamming_parity()` computes the
  parity bits; `hamming_decode()` returns `(corrected_time_word,
  errors_detected)` where `errors_detected` is 0 (clean), 1
  (single-bit error corrected), or 2 (multi-bit error detected).
- **Frame bit allocation** (NIST §4, Table 1): the 60-bit PM
  frame includes sync_T at positions 0–12, the 31-bit Hamming
  codeword scattered across positions 13–18, 20–28, 30–38, 40–46,
  the repeated time[0] LSB at position 19 (NIST §4.3), reserved
  bits at 29 and 39, DST/leap-second code at 47/48/50–52,
  notice bit at 49, DST-next code at 53–58, and a literal 0 at 59.
- **DST state decode** (NIST §4.4, Table 5): one of
  `NOT_IN_EFFECT`, `STARTING_TODAY`, `IN_EFFECT`, `ENDING_TODAY`.
- **Leap-second notice** (NIST §4.4, Table 6): `NONE`, `NEGATIVE`,
  `POSITIVE`.
- **DST-next advance** (NIST §4.6, Table 8): raw 6-bit code passed
  through — full table-8 interpretation is left to the consumer
  (most operational consumers only need to know "is there a
  transition coming and when").
- **`sync_score()` correlation helper** for the upstream framing
  layer.

### What's NOT implemented (and why)

- **NIST §7 extended (6-minute) symbols** — 124 PRBS sequences
  derived from a 7-stage LFSR with `g(x) = x⁷ + x⁶ + x⁵ + x² + 1`.
  These broadcast every half-hour at XX:10 and XX:40 UTC with
  ~10× link-budget gain (360-bit sequence vs 60-bit frame) and
  would let us decode at much lower SNR.  Deferred: the PDF
  text-extraction dropped bits from the spec's reference Sequence
  #1 / #2 listings, so we don't have a clean validation source
  for the LFSR convention.  Implement when a clean source is
  available.
- **NIST §5 message frames** (`SYNC_M`, 42-bit payload).  Carries
  emergency notices and operational messages, not time.  Skipped
  pending operational need.
- **AM/PWM legacy time code** — still needed as the *gating
  envelope* per NIST §2.2 (which the DSP layer in §5 handles),
  but the AM data bits themselves (BCD time with markers) are
  redundant with the PM time word and not parsed.

### Test coverage

`tests/test_wwvb_protocol.py`: **72 tests pass**, including the
NIST §6 worked example end-to-end:

- Date: 2012-07-04 17:30:00 UTC
- Minute counter: 6,578,970
- Parity bits: {1, 0, 0, 1, 0} (time_par[4]..time_par[0])
- DST in effect, no leap second
- dst_next code: 27 (Table-8 row 37: 1st Sunday of November,
  02:00 AM local → 01:00 AM local)
- notice bit: 1

The encoder produces the bit pattern; the decoder recovers all
fields.

### Known limitations captured in tests

Hamming(31,26) is a **perfect code** (`n = 2^r − 1 = 31`, `r = 5`):
there are exactly 31 distinct non-zero 5-bit syndromes, one per
single-bit-error position (5 parity + 26 data).  No syndrome is
spare for double-error detection, so every double-bit data error
mis-corrects to a single-bit-error syndrome at some other
position.  This is a property of the code, not a decoder bug — the
NIST claim of "detect up to 2 errors" only holds if a higher layer
adds cross-checks (e.g., the redundant time[0] copy at frame
position 19, or a monotonicity sanity check against the prior
minute).  The test
`test_double_data_bit_errors_always_mis_correct` documents this
explicitly so any future "detect 2 errors" claim must update the
test consciously.

---

## 5. Layer 3 — DSP decoder (DONE; live-validated against synthesized only)

`src/hf_timestd/core/wwvb_demod.py` (~360 lines after cleanup).

Pure-numpy / scipy DSP.  Input: complex64 IQ array, centered on
the 60 kHz channel (as radiod delivers).  Output: a `DemodResult`
with carrier offset, per-second IQ, per-second bits, and a list of
`DetectedFrame` (one per minute boundary located).

### Pipeline stages

```
IQ → carrier offset estimate → carrier correction
   → AM envelope smoothing
   → second-boundary detection (with backward extrapolation)
   → per-second mean IQ over guaranteed-high-amp window
   → BPSK bit clustering (polarity-resolving)
   → sync correlation (sync_T, max 1 error)
   → 60-bit frame extraction → wwvb_protocol.parse_time_frame()
```

### 5.1 Carrier offset estimator — `estimate_carrier_offset()`

WWVB on the 24 kHz IQ stream is nominally at 0 Hz (radiod centers
the channel on the requested frequency), but receiver clock error
(~1 ppm × 60 kHz ≈ 60 mHz) adds a small residual offset.  Over
60 s of integration even 0.01 Hz of uncorrected offset rotates the
phase by > 0.6 cycles and scrambles every bit.

**Direct FFT does not work** on antipodal BPSK — the carrier is
*suppressed* (the ±1 modulation cancels the carrier in expectation)
and energy is spread across the spectrum.

**Solution: square the IQ first.**  Squaring `±1·exp(j2πf·t)`
gives `+1·exp(j4πf·t)` — the BPSK modulation is removed and the
carrier appears at `2f`.  The peak of `|FFT(iq²)|` then reads `2f`,
which we divide by 2.  With 60 s of IQ we get 17 mHz frequency
resolution — far tighter than RX-888 TCXO drift.

### 5.2 AM envelope — `amplitude_envelope()`

A short (~5 ms) boxcar moving average over `|IQ|`.  Suppresses
sample-level noise while preserving the 200/500/800 ms-class
amplitude transitions.

### 5.3 Second-boundary detection — `find_second_boundaries()`

WWVB drops carrier amplitude at the start of every second for
200 ms (AM "0"), 500 ms (AM "1"), or 800 ms (marker).  The
**leading falling edge** is the on-time mark per NIST §2.2.

We find falling-edge crossings of the envelope below
`0.7 × median(envelope)`, enforce a refractory window of 0.85 s to
suppress noise duplicates, and then **extrapolate backward**: when
the signal starts inside an AM-low window (sample 0 is in the
low-amp phase), there's no falling edge at sample 0 to detect, so
the bit stream gets shifted by one relative to sync_T.  Fix: after
we have ≥ 2 visible falling edges we infer the 1-Hz period (median
spacing) and project backward to sample 0.  Crucial — without it
sync_T never matches.

### 5.4 Per-second PM bit extraction — `extract_pm_bits()`

For each second, average the complex IQ over `[850 ms, 990 ms]`
after the second boundary.  Per NIST §2.2, "receivers extract
[phase] only from the high amplitude portion of the symbol" — the
window we use is always high-amp regardless of AM bit value
(markers extend the AM-low window out to 800 ms; we start at 850 ms
to clear the transient).

Returns a per-second mean IQ array (complex64).

### 5.5 BPSK bit clustering — `phases_to_bits()`

The absolute phase reference is unknown.  We resolve it by squaring
the mean IQ (removes BPSK), taking the angle of the mean of the
squared values (collapses to one cluster), dividing by 2, and using
that as the rotation reference.  Bits are then `real(rotated) < 0`.

Polarity ambiguity (whole stream might be inverted) is resolved at
the sync-correlation step in §5.6.

### 5.6 Sync correlation — `find_sync_positions()`

Sliding 13-bit window against sync_T with up to 1-bit error
tolerance.  Evaluates both upright and inverted; if inverted hits
dominate, the upstream code (`decode_iq`) flips the whole bit
stream once before frame extraction.

### Test coverage

`tests/test_wwvb_demod.py`: **10 tests pass** covering each stage
individually plus end-to-end at clean SNR, with residual carrier
offset, and at +20 dB SNR.  All use synthesized signal (no chunk
files).

### What's NOT done (the limits of the MVP)

- **Costas-loop refinement.**  The FFT-only carrier estimator is
  accurate for stable signal but not for sky-wave phase wandering
  over many minutes.  When the live tap (§7) reports `frames=0`
  with strong `mean|iq|`, this is the upgrade.
- **AM-bit value decoding.**  We use the AM envelope only for
  second-boundary timing; the 200/500/800 ms duration that encodes
  the legacy AM time bit is unused.  Adding it would let us
  cross-check the PM time word against the legacy AM time word per
  minute (catches Hamming mis-corrections).
- **Chain-delay calibration.**  Sky-wave propagation delay
  (~3–10 ms, diurnally varying with terminator transitions) is
  the chain-delay analog of T6's fixed value.  Currently the
  decoder emits the bare minute UTC; an absolute timing tier
  needs a calibrated propagation-delay model (likely a learning
  pass during initial deployment, GPS-anchored).
- **Wider-window phase averaging when AM "0" is detected.**  When
  we know the AM bit was "0" (high-amp window is the full 800 ms),
  we could average over `[250, 990] ms` instead of `[850, 990] ms`
  — ~4× more samples, ~6 dB more processing gain on those seconds.
  Optimization; not needed for first cut.

---

## 6. The "no archive" principle

WWVB never gets archived to disk.  Not for production, not for
"testing," not for "characterization."  This is the same operational
discipline T6 follows.

Rationale (paraphrasing the user statement of 2026-05-27):

> WWVB serves the metrology, same as T6 — using the payload of RTP
> streams to align samples with UTC.  If GPS+PPS, and hence T6 (the
> HF PPS), disappear, we have HF time-standard signals from WWVB,
> WWV, WWVH, and possibly BPM to keep accurate timing going until
> GPS+PPS return.

If we ever need IQ for debugging, the path is to use the live tap
(§7) or to subscribe to the running multicast and inspect on the
fly — not to flip `archive=true` even temporarily.

This principle is enforced at three levels:

1. **Config:** `archive = false` on the WWVB_60 channel entry
   in `/etc/hf-timestd/timestd-config.toml` (verified by the
   recorder code at `core_recorder_v2.py:1022–1027`: when False,
   "core-recorder still receives the stream … but writes no IQ
   data to cold storage").
2. **Code:** the protocol module (`wwvb_protocol.py`) and DSP
   module (`wwvb_demod.py`) have no chunk-file dependencies; no
   `load_chunk()` helper, no zstd imports.  Tests are entirely
   synthesized-signal driven.
3. **Tooling:** the validation tap (`scripts/wwvb_live_tap.py`)
   streams directly from radiod multicast, never touches disk.

---

## 7. Operational tooling: the live tap

`scripts/wwvb_live_tap.py` (~190 lines).

A development / validation tool that mirrors the eventual
production consumer's input plumbing — dedicated `RadiodStream`
(same shape as `_start_t6_stream()` for the same reasons:
isolation from any shared-stream socket-buffer pressure), in-process
sample callback into a rolling IQ buffer, periodic `decode_iq`
with stdout output.  No disk writes ever.

### Running it

```bash
cd /opt/git/sigmond/hf-timestd

# Interactive — Ctrl-C to stop
.venv/bin/python scripts/wwvb_live_tap.py

# Or background with output to a log
nohup .venv/bin/python scripts/wwvb_live_tap.py \
    > /tmp/wwvb_tap.log 2>&1 &
```

Options:
- `--radiod <host>`: mDNS status hostname (default
  `bee1-status.local`).
- `--frequency-hz <hz>`: WWVB carrier (default 60000).
- `--sample-rate <sr>`: IQ rate (default 24000).
- `--window-s <s>`: rolling buffer length (default 90).
- `--decode-interval-s <s>`: decode cadence (default 30).
- `--min-buffer-s <s>`: don't attempt decode until buffer ≥ this
  (default 65).
- `-v`: enable INFO logging from ka9q-python.

### Reading the output

Each decode pass prints one summary line, plus one indented line per
detected minute frame:

```
[2026-05-27T22:56:06] iq=75.0s mean|iq|=2.747e-06 carrier_offset=+0.000 Hz secs=45 bits=44 frames=0
[2026-05-28T06:32:15] iq=90.0s mean|iq|=2.144e-04 carrier_offset=-0.012 Hz secs=89 bits=88 frames=1
  → minute=2026-05-28T06:32:00 DST=IN_EFFECT     par_err=0 sync_err=0 pol=OK  vs_wallclock=+0s
```

**Fields:**
- `iq=<s>s` — buffered duration at decode time.
- `mean|iq|=` — magnitude of the IQ, an SNR proxy.  Daytime at AC0G
  is ~2e-6; expect ~1e-4 or higher on a healthy nighttime path.
- `carrier_offset=` — squared-FFT residual; should be sub-Hz.
- `secs=` — second-boundaries detected.  In a 90-s window, should
  be ≈ 89–90 when the signal is clean; lower means AM envelope
  detection is being defeated by noise.
- `bits=` — derived PM bits (= `secs - 1`).
- `frames=` — number of sync_T matches in `bits`.  **≥ 1 means
  we successfully decoded a minute frame.**

**Per-frame line:**
- `minute=` — decoded UTC of the minute.
- `DST=` — decoded DST state.
- `par_err=` — Hamming error count: 0 (clean), 1 (corrected),
  2 (uncorrectable).
- `sync_err=` — bits that disagreed with sync_T (max 1 due to
  `max_errors=1`).
- `pol=` — `OK` if upright, `INV` if the bit stream was inverted.
- `vs_wallclock=` — `(decoded_minute − host_clock_now)` in seconds.
  When GPS+PPS+T6 are healthy this is the difference between WWVB
  UTC (≈ propagation delay) and the host's own UTC.  A few-second
  lag for the propagation path + recovery latency is normal.

### Dual purpose: validation **and** reception monitoring

Running the tap continuously over a 24-hour window does two things
at once:

1. **Validates the decode chain.**  Any successful `frames=1`
   line proves Layers 1–3 work end-to-end on real signal.  If the
   `vs_wallclock` matches host time within seconds (or, once
   propagation-delay calibration is in, within sub-ms), we know
   the timing layer is solid.

2. **Characterizes WWVB reception at AC0G.**  The `mean|iq|` and
   `frames=N` fields plotted over time give a real reception
   profile for this antenna + site.  We learn:
   - When (UTC) does WWVB become decodable each evening?
   - When does daytime D-layer absorption knock it out each morning?
   - How does decodability correlate with solar conditions
     (magnetic indices, X-ray flares)?
   - Is the existing HF antenna chain actually adequate for LF, or
     would a loop antenna meaningfully improve coverage?

This second use is **independent reception-science value** above
and beyond the metrology mission, and it costs us nothing — the
tap is already running.

### What it doesn't do (yet)

- Doesn't write the L2 `broadcast_measurements` rows that Fusion
  consumes.  That's Layer 4 (§9).
- Doesn't apply propagation-delay calibration.  Decoded
  `minute_of_frame` is the *transmitter*-side UTC; for sub-second
  comparison to the host's GPS-disciplined clock you'd need to
  subtract the ~3–10 ms sky-wave delay (variable with terminator).
- Doesn't log SNR/decode-result to a file in a structured format.
  For long-term reception statistics, redirect stdout to a log file
  and post-process; or build a small follow-up tool that reads the
  same stream and writes one row per minute to SQLite.

---

## 8. Diurnal reception monitoring at AC0G

The tap is the simplest practical way to characterize when WWVB is
usable from EM38ww (AC0G's grid).  A continuous log over 24 hours,
post-processed, gives a per-hour decodability map.

**Hypothesis (to be tested):** WWVB at AC0G follows the typical
continental US LF reception curve:

| Local time (CDT) | Expected `mean|iq|` | Expected `frames=` |
|---|---|---|
| 12:00 (noon) | ~ 1–3 × 10⁻⁶ | 0 (D-layer absorbed) |
| 18:00 | 1 × 10⁻⁵ rising | 0–intermittent |
| 21:00 (post-sunset) | 5 × 10⁻⁵ rising | intermittent |
| 00:00 (midnight) | 1–5 × 10⁻⁴ | 1 per decode pass |
| 04:00 (peak nighttime) | 5 × 10⁻⁴ + | 1 per decode pass |
| 07:00 (post-sunrise) | dropping back to noise | dropping to 0 |

Sunrise / sunset terminator transitions are usually visible as
sharp SNR changes over ~30 min as the D-layer forms or dissolves.
That'll show up in the `mean|iq|` track.

**What this tells us operationally:**

- **Fusion-source availability window.**  WWVB joins the Fusion
  pool only during the hours it's decodable.  The window where
  WWVB *can't* contribute is exactly when HF (WWV/WWVH/BPM via
  high-band F-layer hops) is strongest — by design, the two are
  complementary.  We should see that the union has ≥ 3 sources
  24/7, which is the CHU-replacement criterion.
- **Antenna assessment.**  If WWVB never decodes even at deep
  night, the HF antenna chain isn't passing 60 kHz adequately,
  and a tuned loop would be the upgrade path.  If it decodes
  freely at night, the chain is fine and no antenna work needed.
- **CHU loss preview.**  Once we have a few days of WWVB
  decodability data, we can simulate the post-CHU world: count
  per-minute sources in the Fusion pool with CHU removed and WWVB
  added, and confirm the 3-source floor holds across the diurnal
  cycle.

---

## 9. What's not done — the roadmap

In rough dependency order:

1. **Nighttime live-tap run.**  Confirms Layer 3 works on real
   signal; gates everything below.  No code change.  Just queue
   the tap during a nighttime window (recommended 03–09 UTC at
   AC0G ≈ 22:00–04:00 local CDT) and check the log.

2. **Costas-loop refinement** if the FFT-only carrier estimate
   doesn't hold up on sky-wave wander.  This is the most likely
   place the live test fails first.

3. **Propagation-delay calibration.**  A learning pass that uses
   GPS-disciplined wallclock as ground truth to estimate the
   per-decoded-minute sky-wave delay.  Slowly-varying (diurnal),
   needs a temporal model not a fixed constant.

4. **Layer 4: Fusion ingestion writer.**  Wire decoded
   `WwvbTimeFrame` outputs into the L2 `broadcast_measurements`
   schema that the multi-broadcast Kalman + WLS combiner consumes.
   Schema-match the existing WWV/WWVH/CHU/BPM metrology workers
   (see `metrology_engine.py`).

5. **In-process consumer in core-recorder.**  Translate the tap's
   architecture into a `_start_wwvb_stream()` + `_wwvb_on_samples()`
   pair in `core_recorder_v2.py`, mirroring `_start_t6_stream()`.
   This replaces the standalone tap once we're confident in the
   decode chain.

6. **NIST §7 extended (6-minute) symbols** for low-SNR reception.
   124 PRBS sequences at half-hour boundaries, 10× link-budget
   gain.  Needs a clean source for the LFSR reference sequences
   (PDF text-extraction dropped bits).

7. **Optional: AM-bit value decoder** for per-minute cross-check
   of the PM time word against the legacy AM time word.  Catches
   Hamming mis-corrections.

8. **Long-term reception logger** (§8).  Companion tool that
   writes one structured row per decode pass (timestamp, mean|iq|,
   secs, frames, decoded minute, vs_wallclock) to a SQLite
   database for later analysis.

---

## 10. References

### Internal
- `METROLOGY.md` §4.5–§4.6 — timing-authority hierarchy.
- `BPSK-PPS-DETECTION-METHODS.md` — T6 design history; sibling
  living record for the in-shack BPSK PPS pipeline.
- `ARCHITECTURE-FIRST-PRINCIPLES.md` — §18 producer-side contract
  surface; relevant when wiring Layer 4 outputs into the Fusion
  combiner.

### External
- `docs/reference/NIST-Enhanced-WWVB-Broadcast-Format-1_01-2013-11-06.pdf`
  — John Lowe, "Enhanced WWVB Broadcast Format," NIST Time and
  Frequency Services, Rev 1.01, 2013-11-06.  Sections 2.2 (AM
  gating of PM extraction), 4 (1-minute time frame bit allocation),
  4.3 (Hamming(31,26) parity equations), 4.4 (Table 5 DST decode),
  4.6 (Table 8 DST-next), 6 (worked example for 2012-07-04 17:30
  UTC), 7 (extended 6-minute symbols + LFSR).
- NIST Special Publication 432 — "NIST Time and Frequency Services"
  (broader reference for the legacy AM/PWM time code).
- ITU-R TF.460-6 — "Standard-frequency and time-signal emissions."

### Authoritative implementation source files
- `src/hf_timestd/core/wwvb_protocol.py` (Layer 2)
- `src/hf_timestd/core/wwvb_demod.py` (Layer 3)
- `scripts/wwvb_live_tap.py` (validation / reception monitoring)
- `tests/test_wwvb_protocol.py` (72 protocol tests)
- `tests/test_wwvb_demod.py` (10 DSP tests, all synthesized)
- `/etc/hf-timestd/timestd-config.toml` (Layer 1 channel entry)

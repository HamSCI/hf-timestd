# Time-Station Tick Detector in radiod — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The ka9q-radio fork measures WWV/WWVH (and optionally BPM) tick correlations, marker energies and the AM envelope every second on each time-station channel, positioned on radiod's input-sample counter, and reports them in new status fields; hf-timestd tools prove the reports against the TS-1 pilot and hf-timestd's own detector on B1.

**Architecture:** A pure C core (`tick.c`, no radiod headers) computes per-second complex correlation profiles with real cosine templates, marker energies and a 1 kHz envelope; it is proven against a numpy reference by golden vectors. Glue (`tick_chan.c`) hooks it from the existing `pilot_on_block` call, reusing the pilot work's anchor, epoch and block-freshness rules, and fills new TLVs numbered below 228. Python tools decode, log and analyse.

**Tech Stack:** C (ka9q-radio Makefile, `complex.h`); Python 3.11, numpy, pytest (hf-timestd venv).

**Spec:** `hf-timestd/docs/superpowers/specs/2026-11-09-radiod-tick-detector-design.md`

## Global Constraints

- Fork: ka9q-radio branch `pilot-detector` (pushed to `mjh` = github.com/mijahauan/ka9q-radio); same branch, no new branch. Never push to `ka9q`.
- New logic in new files; Phil's files get only minimal hooks (preset keys, TLV enum entries, encode/decode/dump cases, one call from the existing HamSCI hook).
- Existing status fields and RTP behaviour unchanged. New TLV types take explicit values below 228 (from 227 down); nothing existing moves.
- radiod measures, hf-timestd judges: never drop a template's profile because a station looks absent; never report a single "best" arrival.
- Templates: WWV 1000 Hz, 5 cycles (5.0 ms); WWVH 1200 Hz, 6 cycles (5.0 ms); BPM 1000 Hz, 10 cycles (10.0 ms). Real template `cos(2π f t)` over the burst, correlated against carrier-centred complex IQ.
- Window: −10 ms to +60 ms around each nominal second; profile step = the integer number of channel samples nearest 0.1 ms (2 samples = 83.3 µs at 24 kHz); profile values complex, sent as int16 pairs with one float scale.
- Marker energy per tone over 0–800 ms of each second (tones 1000, 1200, 1500 Hz). AM envelope: |IQ| low-passed to 250 Hz, sampled at 1 kHz, 1000 int16 per second with one float scale.
- Option on linear channels: `ticks = wwv` | `wwv+wwvh`, optional `+bpm`.
- Nominal second: from the same-radiod pilot report when locked, else `gps_time_ns()` (host clock), used only to place the window; the window start is reported on the input counter.
- Detector CPU budget: under 5 % of one core for six channels. No block drops or USB losses caused by the detector.
- Do not restart radiod on B1 or touch B2's hf-timestd before the pilot 24 h run and its T6 A/B test finish (after ~23:35Z 2026-11-09).
- Python tools in `hf-timestd/scripts/radiod_ticks/`; hf-timestd service code untouched. Commit trailers as in the pilot plan.

## Review Focus

1. **Seconds 29 and 59** (no tick): a report still goes out for every template, profile included. Test in Task 1 (`test_skipped_second_still_reports`).
2. **Window crossing a block boundary or an epoch change**: the window is assembled from consecutive fresh blocks of one epoch; a window spanning an epoch change or a non-fresh block is reported as incomplete (flag), never stitched. Test in Task 2 (`test_window_spanning_epoch_is_flagged`).
3. **Both stations present with WWVH 25 ms after WWV**: each template's peak sits at its own station's arrival; the other station leaks less than −20 dB at the aligned lag. Test in Task 1 (`test_two_stations_separate`).
4. **Station absent**: its profile is noise-level, SNR reported low, never omitted. Test in Task 1 (`test_absent_station_reported`).
5. **Nominal second off by up to 5 ms** (host clock fallback): the tick still falls inside the window and its reported input-counter position stays exact. Test in Task 2 (`test_coarse_nominal_second`).

---

### Task 1: Pure C tick core, proven against a numpy reference

**Files:**
- Create: `ka9q-radio/src/tick.h`, `ka9q-radio/src/tick.c`, `ka9q-radio/src/tick_test.c`, `ka9q-radio/src/tick_replay.c`
- Modify: `ka9q-radio/src/Makefile` (targets `tick_test`, `tick-replay`; `tick.o` into `RADIOD_OBJECTS`), `.gitignore`
- Create: `hf-timestd/scripts/radiod_ticks/make_golden.py`, `hf-timestd/scripts/radiod_ticks/compare_golden.py`, `hf-timestd/tests/test_radiod_ticks_golden.py`

**Interfaces:**
- Produces (tick.h):

```c
enum tick_template { TICK_WWV, TICK_WWVH, TICK_BPM, TICK_NTEMPLATES };
struct tick_config { int samprate; unsigned templates;  /* bitmask of 1<<tick_template */ };
struct tick_second {                 // one report
  int64_t window_start_index;        // channel-output counter of the window's first sample
  int step;                          // samples per profile step
  int nprofile;                      // values per profile
  float complex profile[TICK_NTEMPLATES][TICK_MAX_PROFILE];
  double peak_index[TICK_NTEMPLATES];   // channel-output counter, fractional (parabolic on |profile|)
  double peak_snr_db[TICK_NTEMPLATES];
  double marker_energy[3];           // 1000, 1200, 1500 Hz over 0-800 ms
  int16_t envelope[1000]; float envelope_scale;
  bool complete;                     // false if the window was not fully covered
};
struct tick_state *tick_create(struct tick_config const *cfg);
void tick_destroy(struct tick_state *t);
void tick_reset(struct tick_state *t);
// Feed n consecutive samples starting at channel-output counter first_index; second_index is the
// channel-output counter of the nominal second this block belongs to (the caller computes it).
// Returns 1 and fills *out when a second's report is complete, 0 otherwise, -1 on a gap.
int tick_process(struct tick_state *t, float complex const *s, int n, int64_t first_index,
                 int64_t second_index, struct tick_second *out);
```

- `tick-replay FILE.cf32 RATE MASK SECOND0_INDEX` prints one JSON line per report (keys: `window_start_index, step, profile_<name>` as [re,im] pairs, `peak_index_<name>, peak_snr_db_<name>, marker_energy, envelope, envelope_scale, complete`).
- Golden set: `make_golden.py synth --out DIR` writes `<name>.cf32` (24 kHz, 120 s) + `<name>.golden.jsonl` computed by a straightforward numpy reference of the same math (not hf-timestd's detector): sets `wwv_only` (tick at +4.2 ms), `both` (WWV +4.2 ms, WWVH +29.0 ms, BCD subcarrier on both), `faded` (WWVH only, WWV absent), `multipath` (WWV at +4.2 and +5.1 ms, amplitudes 1.0 and 0.6), `noise` (no ticks).

- [ ] **Step 1: Failing tests** in `tick_test.c` (synthetic signals built in C): `test_two_stations_separate` (WWV at +4.2 ms, WWVH at +29.0 ms: each template's `peak_index` within 0.02 ms of truth; at WWV's lag, WWVH template magnitude ≤ −20 dB relative to its own peak), `test_absent_station_reported` (WWV only: WWVH profile present, `peak_snr_db[TICK_WWVH] < 6`), `test_skipped_second_still_reports` (a second with no tick yields a complete report for every template), `test_envelope_rate` (100 Hz test tone at AM depth 0.5 → envelope shows 100 Hz at the right amplitude within 2 %), `test_marker_energy` (800 ms of 1500 Hz at second 0 → `marker_energy[2]` dominates by ≥ 20 dB).
- [ ] **Step 2:** `make -C src tick_test && ./src/tick_test` → FAIL (no tick.c).
- [ ] **Step 3: Implement** `tick.c`: direct correlation `Σ s[n+k]·cos(2π f k/fs)` over the burst for each lag in the window; parabolic peak on |profile|; SNR = peak power over median profile power outside ±3 ms of the peak; marker energy by single-bin Goertzel over 0–800 ms; envelope by |s| through a windowed-sinc low-pass (250 Hz cutoff) decimated by fs/1000 (integer required, else `tick_create` fails). No allocation in `tick_process`.
- [ ] **Step 4:** Run `tick_test` → PASS.
- [ ] **Step 5:** `make_golden.py` + `compare_golden.py DIR --replay PATH/tick-replay`: per report, profile values within 1e-4 relative of the reference's largest magnitude, `peak_index` within 0.01 sample, `marker_energy` within 1e-4 relative, envelope within 1 int16 LSB. pytest wrapper `test_radiod_ticks_golden.py` runs the synth and checks the reference itself recovers truth on `wwv_only` and `both` (peaks within 0.02 ms). Run both → PASS.
- [ ] **Step 6:** Commit in both repos.

### Task 2: radiod glue — option, window placement, epoch and freshness

**Files:**
- Create: `ka9q-radio/src/tick_chan.c`, `ka9q-radio/src/tick_chan.h`
- Modify: `ka9q-radio/src/pilot_chan.c` (call `tick_on_block(chan)` at the end of `pilot_on_block`'s fresh-block path, and `tick_chan_stop` beside `pilot_chan_stop`), `radio.h` (channel fields), `modes.c` (preset key `ticks`), `Makefile`
- Test: `ka9q-radio/src/tick_test.c`

**Interfaces:**
- Consumes: Task 1's `tick_process`; pilot work's `chan->anchor`, `pilot_block_fresh`, `pilot_epoch_for_block`, `chan->pilot.last` (and, for channels without a pilot, the same radiod's pilot channel's report if one exists *(ruling for the builder: a front-end-wide pointer to the latest locked pilot report, set by the pilot channel; absent → host clock)*).
- Produces: `chan->ticks` { `unsigned templates; struct tick_state *det; struct tick_second last; bool last_valid; uint64_t last_epoch; int64_t last_second_utc_ns; bool used_pilot;` }; pure helper `int64_t tick_second_index(int64_t utc_second_ns, int64_t ref_index, int64_t ref_utc_ns, double samprate)` (channel-output counter of a UTC second given a reference pair).

- [ ] **Step 1: Failing tests:** `test_coarse_nominal_second` (reference pair off by 5 ms → tick still inside the window; reported `peak_index` unchanged to 0.01 sample), `test_window_spanning_epoch_is_flagged` (an epoch change mid-window → that report `complete = false`, next second complete), `test_second_index_math` (24 kHz, ref at index 1 000 000 = UTC S + 0.25 s → second S+1 at index 1 018 000).
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3: Implement** the glue; correlation runs on the demod thread (Task 1 cost ~1–2 ms once a second per channel); if a measured per-second cost exceeds 5 ms, move it to a worker like the pilot's and say so in the report. Name the UTC second from the pilot report when locked, else from `gps_time_ns()` rounded, and record which in `used_pilot`.
- [ ] **Step 4:** `make -C src radiod tick_test pilot_test` → links, all tests pass, no new warnings.
- [ ] **Step 5:** Commit.

### Task 3: Status TLVs and metadump

**Files:**
- Modify: `ka9q-radio/src/status.h`, `radio_status.c`, `decode_status.c`, `dump.c`; Create: `ka9q-radio/src/tick_status.c`, `tick_status.h`

**Interfaces:**
- Produces, explicit values (one-byte types, all sent once per second per tick channel when `last_valid`):

| value | name | encoding |
|---|---|---|
| 227 | `TICK_SECOND_UTC` | int64 ns since GPS epoch |
| 226 | `TICK_WINDOW_START` | int64, input-counter index of window start |
| 225 | `TICK_STEP` | int, input samples per profile step |
| 224 | `TICK_FLAGS` | int: bit0 complete, bit1 used_pilot, bits 4–6 template mask |
| 223 | `TICK_PROFILE_WWV` | vector: float scale then int16 re,im pairs |
| 222 | `TICK_PROFILE_WWVH` | same |
| 221 | `TICK_PROFILE_BPM` | same |
| 220 | `TICK_PEAKS` | vector of floats: per template peak input index offset from window start (samples), SNR dB |
| 219 | `TICK_MARKERS` | vector of 3 floats |
| 218 | `TICK_ENVELOPE` | vector: float scale then 1000 int16 |

  Input-counter values are channel-output positions converted with `pilot_out_to_input`. `INPUT_EPOCH` (240) already goes out once per packet; tick fields are sent only when `last_epoch` equals it.

- [ ] **Step 1: Failing test** `test_tick_tlv_roundtrip` in `tick_test.c` (encode a filled report, decode, every field equal; profile int16 quantisation error ≤ scale/32767).
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement encode/decode in `tick_status.c`, hook into `radio_status.c` beside the pilot group, names in `dump.c`. Confirm one status packet with three profiles plus envelope stays under 9 KB.
- [ ] **Step 4:** `pilot_test`, `tick_test` pass; `git diff ka9q/main -- src/status.h` shows only added lines.
- [ ] **Step 5:** Commit; push `pilot-detector` to `mjh`.

### Task 4: Python decoder and logger

**Files:**
- Create: `hf-timestd/scripts/radiod_ticks/tlv_ticks.py`, `hf-timestd/scripts/radiod_ticks/listen_ticks.py`; Test: `hf-timestd/tests/test_radiod_ticks_tlv.py`

**Interfaces:**
- Consumes: `scripts/radiod_pilot/tlv.py` (`decode`, `rtp_to_input`).
- Produces: `decode_ticks(packet: bytes) -> dict` adding keys `tick_second_utc, tick_window_start, tick_step, tick_flags, tick_profile_{wwv,wwvh,bpm}` (numpy complex64 arrays), `tick_peaks`, `tick_markers`, `tick_envelope` (numpy float32, scaled); `listen_ticks.py --group G --ssrc N [--ssrc ...] --out DIR` writes one `.npz` per channel per minute (profiles, envelope) plus a JSONL index row per second.
- [ ] **Step 1: Failing tests** with a packet built by the C encoder (as Task 7 of the pilot plan did: a scratch helper linked against `status.o` + `tick_status.o`, hex fixture committed with a regeneration note in the test docstring): every field decoded, profile values within quantisation, unknown types skipped.
- [ ] **Step 2–4:** RED, implement, GREEN.
- [ ] **Step 5:** Commit.

### Task 5: B1 deployment of tick channels (after the pilot 24 h run)

**Files:**
- Modify: `~/hf-b1-pilot-radiod.sh` (add `ticks-go|ticks-undo`) — staged for Michael if auto mode refuses.

- [ ] **Step 1:** Add six linear channels on B1-a only: 2.5, 5, 10, 15 MHz with `ticks = wwv+wwvh+bpm`; 20, 25 MHz with `ticks = wwv`; 24 kHz, F32 IQ, AGC off, filter ±5 kHz; fixed SSRCs stated in the script header; separate data group `ac0g-b1-a-ticks-data.local`. Same gating as `go`: build verified, conf block marked, 50 s health check (exe, NRestarts, status packet carrying `TICK_SECOND_UTC`), auto-rollback.
- [ ] **Step 2:** Run `ticks-go` only after the pilot 24 h comparison and the T6 A/B test have finished; restart B2 consumers afterwards.
- [ ] **Step 3:** Start `listen_ticks.py` on B2 for the six SSRCs as a transient unit (RuntimeMaxSec 26 h). Verify reports arrive for every template every second.

### Task 6: The three proofs and CPU

**Files:**
- Create: `hf-timestd/scripts/radiod_ticks/proof_pilot.py`, `proof_hftimestd.py`, `proof_bcd.py`; Test: `hf-timestd/tests/test_radiod_ticks_proofs.py`; Create: `~/hamsci/tasks/radiod-ticks/proofs.md`

**Interfaces:**
- Consumes: Task 4's logs; the pilot logger's ledger (`radiod_pilot/listen.py`) for the B1-a pilot; B2's hf-timestd raw buffer for its 24 kHz channels on B1-a; `hf_timestd.core.tick_edge_detector.TickEdgeDetector`; `hf_timestd.core.wwvh_discrimination.bcd_correlation_discrimination`.

- [ ] **Step 1: Unit tests** of each tool's pure parts with synthetic inputs of known answer (arrival delay from a profile + pilot pair; mapping of an hf-timestd tick sample through an ANCHOR pair; envelope upsampling to the BCD routine's expected rate).
- [ ] **Step 2: Proof 1** (`proof_pilot.py`): per second, peak and leading edge (first crossing of 50 % of peak magnitude) on the input counter minus the pilot's UTC → arrival delay per station and frequency. Pass: WWV median per frequency within 3.7–6.0 ms; robust scatter over an hour < 1 ms where SNR > 10 dB; 100 % of seconds carry every template.
- [ ] **Step 3: Proof 2** (`proof_hftimestd.py`): replay `TickEdgeDetector` over the same minutes of B2 raw-buffer IQ; map its peak and front edge through each channel's ANCHOR pair; compare like with like. Pass: medians within 0.2 ms per station and frequency; both scatters reported.
- [ ] **Step 4: Proof 3** (`proof_bcd.py`): `bcd_correlation_discrimination` on radiod's envelope (upsampled) and on hf-timestd's IQ for the same minutes. Pass: same verdict in ≥ 95 % of minutes where hf-timestd reaches one.
- [ ] **Step 5: CPU:** per-thread on B1 for the six tick channels vs ordinary channels; pass < 5 % of a core in all.
- [ ] **Step 6:** Write `proofs.md` (plain statements, what each number does and doesn't show, terms defined for an outside reader); commit tools.

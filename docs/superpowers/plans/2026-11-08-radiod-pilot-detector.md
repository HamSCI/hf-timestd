# TS-1 Pilot Detector in radiod — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A ka9q-radio fork measures the TS-1 pilot's once-a-second edge on radiod's input-sample counter and reports it, with an atomic RTP↔input tie and a counter epoch, in new status fields; hf-timestd tools prove it against hf-timestd's own T6 on B1.

**Architecture:** A pure C detector (`pilot.c`, no radiod dependencies) ports hf-timestd's `BpskEdgeFineStage` plus the Costas quiet gate, proven equal to the Python by golden vectors. A thin radiod glue file (`pilot_chan.c`) hooks it into the linear demod loop of a channel configured `pilot = yes`, so the channel keeps streaming its samples while the detector runs, converts output-sample positions to the input counter, names the second, and fills new status TLVs numbered from 240 down. Python tools in hf-timestd decode the TLVs, log them, and compare radiod's edges with hf-timestd's anchors on the same channel.

**Tech Stack:** C (gcc, FFTW-free: the detector needs no FFT beyond what radiod already does; `complex.h`, `math.h`), ka9q-radio Makefile; Python 3.11 with numpy, pytest, hf-timestd's venv.

**Spec:** `hf-timestd/docs/superpowers/specs/2026-11-08-radiod-pilot-detector-design.md`

**Ruling against the spec's writer's call:** the spec suggested a new `demod_type`. The plan instead adds a `pilot` option to ordinary linear channels: the channel keeps streaming its 96 kHz samples while the detector runs, so hf-timestd measures the very same samples for the side-by-side proof, and no demod enum entry can collide with Phil's. Cost if wrong: moving `pilot_on_block` into its own demod later is a small change.

## Global Constraints

- Fork: `HamSCI/ka9q-radio` on GitHub, one branch `pilot-detector`, based on `ka9q/main` (now `401992cd`), kept rebased onto Phil's main. Never push to `ka9q/ka9q-radio`.
- Changes to Phil's files stay minimal hooks: the linear-demod call, channel struct fields, preset keys, TLV enum entries with their encode/decode cases, the epoch counter. All logic lives in new files.
- Existing status fields (`GPS_TIME`, `RTP_TIMESNAP`, `INPUT_SAMPLES`, all others) keep exactly their current values and timing. No existing RTP behaviour changes.
- New TLV types take explicit values from 240 downward; nothing else in `enum status_type` moves.
- The detector must cause no block drop and no USB sample loss on B1. Detector CPU budget: under 3 % of one core.
- radiod measures, hf-timestd judges: `PILOT_LOCK` claims only the Costas quiet gate plus the SNR floor; the seven-criterion battery stays in Python.
- Edge positions are reported raw (nothing subtracted); the filter delay rides beside as a diagnostic.
- hf-timestd tools live in `hf-timestd/scripts/radiod_pilot/`; ka9q-python, the Offset Judge and hf-timestd's service code stay untouched.
- Defaults copied from hf-timestd: 96 000 Hz output, ±25 kHz filter, `fold_seconds = 30`, `search_window_ms = 6.0`, Costas constants `COSTAS_TAU_PHASE_EMA_S = 10.0`, `COSTAS_TAU_DPHASE_EMA_S = 0.5`, `COSTAS_DPHASE_MAX_RAD = 0.050`, `COSTAS_PHASE_BAND_RAD = 2.0`, `COSTAS_RELOCK_S = 0.5`.
- Station work on B1 goes through scripts staged as `~/hf-*.sh` for Michael when auto mode refuses root on a station.

## Review Focus

1. **A fold that straddles an epoch change** (USB drop mid-fold): expect the fold discarded, no estimate from mixed counters, and acquisition from scratch. Test in Task 3 (`test_epoch_change_discards_fold`) and Task 5.
2. **RTP wrap of the 96 kHz channel** (2^32 at 96 kHz ≈ 12.4 h): the comparison must not see a 242.667 ms jump. Test in Task 7 (`test_rtp_wrap_conversion`).
3. **No pilot present** (TS-1 off or unplugged): expect no estimate marked locked, `PILOT_LOCK = 0`, and reports still flowing with low SNR. Test in Task 3 (`test_noise_only_never_locks`).
4. **A Costas excursion mid-fold** (10–15 s phase swing): expect the fold's estimate withheld or reported unlocked, never a locked phantom. Test in Task 4 (`test_excursion_blocks_lock`).
5. **Host clock off by more than 0.5 s**: the named second is then wrong by one; expect `PILOT_HOST_TIME` to show the gap so a client can see it. Test in Task 5 (`test_name_second_reports_host_gap`).

---

### Task 1: Fork branch and a clean baseline build

**Files:**
- Modify: git remotes of `~/hamsci/repos/ka9q-radio`

- [ ] **Step 1:** Add the remote `hamsci` = `git@github.com:HamSCI/ka9q-radio.git`; `git fetch ka9q hamsci`; create branch `pilot-detector` from `ka9q/main`.
- [ ] **Step 2:** Build unchanged: `make -C src radiod metadump` (install missing `-dev` packages with apt if the build names them). Expected: both binaries link.
- [ ] **Step 3:** Push the branch: `git push hamsci pilot-detector`. Expected: branch visible on GitHub.
- [ ] **Step 4:** Draft a short note to Phil for Michael (`~/hamsci/tasks/radiod-pilot/note-to-phil.md`): what we try, why, that it lives on a fork branch, and that we'd offer pieces upstream. Michael sends it; do not send it.

### Task 2: Golden vectors from hf-timestd's Python

**Files:**
- Create: `hf-timestd/scripts/radiod_pilot/make_golden.py`
- Test: `hf-timestd/tests/test_radiod_pilot_golden.py`

**Interfaces:**
- Produces: a vector set = `<name>.cf32` (raw little-endian complex64 IQ, 96 kHz) plus `<name>.golden.jsonl`, one line per `FineEdgeEstimate` with keys `edge_rtp, edge_subsample, edge_offset_samples, n_seconds_folded, plateau_amplitude, fit_rms, fold_retention, split_half_delta_samples, peak_prominence, transition_width_samples, apex_distance_samples`, plus `truth_edge_offset_samples` for synthetic sets. CLI: `make_golden.py synth --out DIR` writes the standard set; `make_golden.py file IQ.cf32 --rate 96000 --out DIR` runs the Python over a capture.

- [ ] **Step 1: Write the failing test**

```python
def test_synth_set_recovers_truth(tmp_path):
    run_make_golden(["synth", "--out", str(tmp_path)])
    for name in ["clean", "cn0_48", "frac_0p37", "tilt"]:
        rows = load_jsonl(tmp_path / f"{name}.golden.jsonl")
        assert len(rows) >= 3
        for r in rows[1:]:                      # first fold may be bootstrap
            err = r["edge_offset_samples"] - r["truth_edge_offset_samples"]
            assert abs(err) < 0.01              # samples: ~104 ns at 96 kHz
```

- [ ] **Step 2:** Run `uv run pytest tests/test_radiod_pilot_golden.py -v`. Expected: FAIL, script missing.
- [ ] **Step 3: Implement `make_golden.py`.** Synthesis uses `tests/test_bpsk_edge_fine_stage.make_bpsk` (sr 96000, 150 s each): `clean` (edge 41234.0, no noise), `cn0_48` (noise for 48 dB-Hz), `frac_0p37` (edge 41234.37, bandlimit_hz 25000), `tilt` (tilt_per_s 0.02, phase 1.1). Replay with `BpskEdgeFineStage(96000, fold_seconds=30, search_window_ms=6.0)` fed in 1740-sample batches with RTP = file index, no coarse offset (bootstrap then tracking), as `tools/t6_estimator_sweep.replay_fine` does.
- [ ] **Step 4:** Run the test. Expected: PASS.
- [ ] **Step 5:** Commit (`scripts/radiod_pilot/make_golden.py`, the test). Vectors themselves stay out of git (`.gitignore` in `scripts/radiod_pilot/`).

### Task 3: The C detector, ported and proven equal

**Files:**
- Create: `ka9q-radio/src/pilot.h`, `ka9q-radio/src/pilot.c`, `ka9q-radio/src/pilot_test.c`, `ka9q-radio/src/pilot_replay.c`
- Modify: `ka9q-radio/src/Makefile` (targets `pilot_test`, `pilot-replay`; `pilot.o` into `RADIOD_OBJECTS`)
- Create: `hf-timestd/scripts/radiod_pilot/compare_golden.py`

**Interfaces:**
- Produces (pilot.h):

```c
struct pilot_config { int samprate; int fold_seconds; double search_window_ms; double snr_floor_db; };
struct pilot_estimate {
  int64_t edge_index;      // output-sample counter of the sample at/before the edge
  double edge_frac;        // [0,1) fraction of a sample after edge_index
  double edge_offset_samples; // position within the second, as Python's edge_offset_samples
  int n_seconds;
  double plateau_amplitude, fit_rms, fold_retention, split_half_delta,
         peak_prominence, transition_width, apex_distance, snr_db;
  bool costas_quiet;       // filled by Task 4; true until then
  bool locked;             // costas_quiet && snr_db >= snr_floor_db
  char mode[12];           // "bootstrap" | "tracking"
};
struct pilot_state;
struct pilot_state *pilot_create(struct pilot_config const *cfg);
void pilot_destroy(struct pilot_state *p);
void pilot_reset(struct pilot_state *p);   // drop the fold, keep config
// Feed n samples whose first one sits at output counter first_index.
// Returns 1 and fills *out when a fold completes, 0 otherwise, -1 on error
// (including a gap: first_index not equal to the previous call's end).
int pilot_process(struct pilot_state *p, float complex const *samples, int n,
                  int64_t first_index, struct pilot_estimate *out);
```

- `pilot-replay FILE.cf32 RATE` prints one JSON line per estimate with the Task 2 key names.

- [ ] **Step 1: Write the failing C tests** in `pilot_test.c` (plain `assert`, exit 0 on pass), each on an internally synthesized 96 kHz signal of 150 s:
  - `test_clean_edge`: edge 41234.0 → every estimate after the first within 0.001 samples, `locked`.
  - `test_fractional_edge`: edge 41234.37 band-limited → within 0.005 samples.
  - `test_noise_only_never_locks`: pure noise → no estimate with `locked`.
  - `test_epoch_change_discards_fold`: a `first_index` jump mid-fold → `pilot_process` returns -1, and the next fold's estimate counts only post-gap seconds (`n_seconds == fold_seconds`).
- [ ] **Step 2:** `make -C src pilot_test && ./src/pilot_test`. Expected: compile failure (no `pilot.c`).
- [ ] **Step 3: Implement `pilot.c`** as a line-for-line port of `BpskEdgeFineStage` (`process_samples`, `_finish_block`, `_search_centre`, `_band_limited_crossing`, `_fit_edge`, `_compute_estimate`, `_triangle_fidelity`, `_closed_form_T`, `_split_half_delta`, the bootstrap/tracking logic), with fixed arrays of `samprate` complex doubles replacing `np.add.at` (plain indexed `+=`). `snr_db` = 20·log10(plateau_amplitude / RMS of the fold off the transition) (*ruling: the Python reports no SNR; this one is new and only gates `locked`*). No allocation inside `pilot_process`.
- [ ] **Step 4:** Run `./src/pilot_test`. Expected: all four pass.
- [ ] **Step 5: Equivalence.** Write `compare_golden.py DIR --replay PATH/pilot-replay`: for each vector set run `pilot-replay`, pair folds by order, and assert `|Δedge_offset_samples| ≤ 0.001` (≈10 ns) and each battery field within 1 % relative or 1e-6 absolute. Run it on Task 2's synthetic set. Expected: all sets pass.
- [ ] **Step 6:** Commit in ka9q-radio (`pilot.h pilot.c pilot_test.c pilot_replay.c Makefile`) and in hf-timestd (`compare_golden.py`).

### Task 4: The Costas quiet gate

**Files:**
- Modify: `ka9q-radio/src/pilot.c`, `ka9q-radio/src/pilot_test.c`
- Modify: `hf-timestd/scripts/radiod_pilot/make_golden.py` (add set `excursion`)

**Interfaces:**
- Consumes: Task 3's `pilot_estimate.costas_quiet`, `locked`.
- Produces: `costas_quiet` true only when the gate stayed quiet through the whole fold.

- [ ] **Step 1: Failing test** `test_excursion_blocks_lock`: a signal whose carrier phase swings 5 rad over 12 s inside one fold → that fold's estimate has `costas_quiet == false` and `locked == false`; folds before and after stay locked.
- [ ] **Step 2:** Run. Expected: FAIL (gate absent).
- [ ] **Step 3: Implement** the port of `BpskPpsCalibratorMF._update_costas_lock` with its square-and-halve phase per batch and the five Global Constraints constants; any un-quiet batch in a fold clears `costas_quiet` for that fold.
- [ ] **Step 4:** Run `pilot_test` and `compare_golden.py` with the new `excursion` set (golden: the Python gate's verdict per fold, written by `make_golden.py` from `BpskPpsCalibratorMF.costas_locked` sampled per batch). Expected: both pass.
- [ ] **Step 5:** Commit both repos.

### Task 5: radiod glue — counter, epoch, naming the second

**Files:**
- Create: `ka9q-radio/src/pilot_chan.c`, `ka9q-radio/src/pilot_chan.h`
- Modify: `ka9q-radio/src/radio.h` (fields), `ka9q-radio/src/modes.c` (preset keys), `ka9q-radio/src/linear.c` (one call after `downconvert`), `ka9q-radio/src/rx888.c` and `radio.h` `struct frontend` (epoch), `Makefile`
- Test: `ka9q-radio/src/pilot_test.c` (glue functions are pure and take explicit inputs)

**Interfaces:**
- Produces:

```c
// struct frontend gains:  uint64_t input_epoch;   // rises on start and on every detected sample loss
// chan gains: struct { bool enable; int fold_seconds; struct pilot_state *det;
//                      struct pilot_report last; uint64_t epoch_seen; } pilot;
//             struct { uint32_t rtp; int64_t input_sample; uint64_t epoch; } anchor;  // every channel
struct pilot_report {
  bool valid; uint64_t epoch;
  int64_t edge_input_sample; double edge_input_frac;  // on radiod's input counter
  int64_t edge_utc_ns;       // named second, ns since GPS epoch (GPS_TIME's scale)
  int64_t host_time_ns;      // gps_time_ns() mapped back to the edge
  double sigma_s;            // |split_half_delta| / 2 / output samprate (ruling, spec §3)
  double snr_db; bool locked; int fold_count; double filter_delay_s;
  struct pilot_estimate est; // battery fields for the listener
};
int64_t pilot_out_to_input(int64_t block_input_index, int out_offset, int decim, double *frac_io);
int64_t pilot_name_second(int64_t edge_host_ns);   // round to nearest 1e9
void pilot_on_block(chan_t *chan);                 // called from demod_linear
```

- Preset keys in `modes.c`: `pilot` (bool, default false), `pilot-fold` (int, default 30), `pilot-snr-floor` (double dB, default 20.0 *(ruling)*).

- [ ] **Step 1: Failing tests** (pure functions): `test_out_to_input` (decim 675, block index 1 000 000, offset 10, frac 0.25 → 1 006 918 and frac 0.75 carried exactly: 10.25×675 = 6918.75); `test_name_second_reports_host_gap` (edge host time 1e9·S + 0.3e9 → named S, gap 0.3 s kept in `host_time_ns`); `test_epoch_change_resets` (report from epoch 3 then block from epoch 4 → detector reset, `last.valid` false until a new fold).
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3: Implement.**
  - `input_epoch`: increment at radiod start and wherever the existing loss monitor (`rx888.c` around line 586, the `clock-step-logging` check) detects missing samples; *ruling:* also on any `rx_callback` short transfer the driver already counts as lost.
  - `pilot_on_block`: on every block, record `chan->anchor` = (RTP timestamp of the block's first output sample, `chan->filter.out.sample_index`, `frontend->input_epoch`) for every linear channel; if `pilot.enable`, feed `chan->baseband`/`sampcount` to the detector with the output counter derived from the anchor; on an estimate, convert with `pilot_out_to_input` (decim = input samprate / output samprate, required integer, else disable pilot with a log line), name the second from `gps_time_ns()` minus the samples since the edge, fill `pilot.last`.
  - `filter_delay_s`: read the channel filter design in `filter.c`; if its response is real and zero-phase, report 0.0 and say so in a comment citing the lines; otherwise compute the delay from the response.
  - The hook in `linear.c` is one line after `downconvert` succeeds.
- [ ] **Step 4:** Run `pilot_test`; build `radiod`. Expected: tests pass, radiod links.
- [ ] **Step 5:** Commit.

### Task 6: Status TLVs and metadump

**Files:**
- Modify: `ka9q-radio/src/status.h`, `radio_status.c` (LINEAR section), `decode_status.c`, `dump.c`

**Interfaces:**
- Produces, explicit enum values after `LIFETIME` (types are one byte):

| value | name | encoding | content |
|---|---|---|---|
| 240 | `INPUT_EPOCH` | int64 | `frontend->input_epoch` |
| 239 | `ANCHOR_RTP` | int32 | `chan->anchor.rtp` |
| 238 | `ANCHOR_INPUT_SAMPLE` | int64 | `chan->anchor.input_sample` |
| 237 | `PILOT_EDGE_SAMPLE` | int64 | `edge_input_sample` |
| 236 | `PILOT_EDGE_FRAC` | double | `edge_input_frac` |
| 235 | `PILOT_EDGE_UTC` | int64 | `edge_utc_ns` |
| 234 | `PILOT_HOST_TIME` | int64 | `host_time_ns` |
| 233 | `PILOT_EDGE_SIGMA` | double | `sigma_s` |
| 232 | `PILOT_SNR` | double | `snr_db` |
| 231 | `PILOT_LOCK` | bool | `locked` |
| 230 | `PILOT_FOLD_COUNT` | int | `fold_count` |
| 229 | `PILOT_FILTER_DELAY` | double | `filter_delay_s` |
| 228 | `PILOT_BATTERY` | vector of 8 floats | plateau_amplitude, fit_rms, fold_retention, split_half_delta, peak_prominence, transition_width, apex_distance, edge_offset_samples |

  The three `INPUT_EPOCH`/`ANCHOR_*` go out for every linear channel; the `PILOT_*` group only when `pilot.last.valid`, and carry `INPUT_EPOCH` of the report (*ruling: the report's own epoch, so a client can match it*). `ANCHOR_*` are copied from the demod thread's record under `chan->status.lock`, never recomputed in the status thread.

- [ ] **Step 1: Failing test** `test_tlv_roundtrip` in `pilot_test.c`: encode a filled `pilot_report` and anchor with the same encoder calls `radio_status.c` uses, decode with `decode_status.c`'s functions → every field equal. Run `./src/pilot_test`. Expected: FAIL.
- [ ] **Step 2:** Implement encode, decode and `dump.c` names.
- [ ] **Step 3:** Run `pilot_test`; `make -C src radiod metadump`. Expected: pass and link.
- [ ] **Step 4:** Check that no existing enum value changed: `git diff ka9q/main -- src/status.h` shows only added lines after `LIFETIME`.
- [ ] **Step 5:** Commit; push branch.

### Task 7: hf-timestd decoder and listener

**Files:**
- Create: `hf-timestd/scripts/radiod_pilot/tlv.py`, `hf-timestd/scripts/radiod_pilot/listen.py`
- Test: `hf-timestd/tests/test_radiod_pilot_tlv.py`

**Interfaces:**
- Produces: `decode(packet: bytes) -> dict` returning the 13 new fields (Task 6 names, lower-case) plus `ssrc`, `rtp_timesnap`, `input_samples`, `gps_time`, `frequency`, `output_samprate`; unknown types skipped by length. `rtp_to_input(rtp: int, anchor_rtp: int, anchor_input: int, decim: int) -> int` with signed 32-bit wrap handling. `listen.py --status MCAST:PORT --ssrc N --out FILE.jsonl` writes one line per status packet that carries a pilot report, with `logged_at_unix`.

- [ ] **Step 1: Failing tests:** `test_decode_handcrafted_packet` (a packet built with the TLV layout of `ka9q/status.py:402-419` holding every new field plus one unknown type 200 → all values back, 200 ignored); `test_rtp_wrap_conversion` (anchor_rtp 2^32−100, rtp 50 → input = anchor_input + 150·decim).
- [ ] **Step 2:** Run `uv run pytest tests/test_radiod_pilot_tlv.py -v`. Expected: FAIL.
- [ ] **Step 3:** Implement `tlv.py` (status packet type byte 0 check as in ka9q-python) and `listen.py` (plain socket multicast join; no ka9q-python changes).
- [ ] **Step 4:** Run tests. Expected: PASS.
- [ ] **Step 5:** Commit.

### Task 8: B1 deployment, reversible

**Files:**
- Create: `~/hf-b1-pilot-radiod.sh` (staged for Michael; `status|go|undo`)

- [ ] **Step 1:** Write the script: `go` builds the branch on B1 into `/usr/local/sbin/radiod-pilot` (never over the stock radiod), adds a systemd drop-in for B1's radiod instance pointing `ExecStart` at it, adds one channel section per RX888 at the TS-1 alias frequency for B1's sample rate (19.425 MHz at 64.8 Msps, 45.375 MHz at 129.6 Msps; read the rate from B1's radiod config) with `preset = iq`-equivalent linear settings, 96000 Hz, filter ±25 kHz, `pilot = yes`; restarts radiod. `undo` removes the drop-in and the channel sections and restarts. `status` shows which binary runs and greps one status packet with `metadump` for `PILOT_LOCK`.
- [ ] **Step 2:** Michael runs `~/hf-b1-pilot-radiod.sh go`. Expected: `status` shows `radiod-pilot` running and `PILOT_LOCK 1` on both RX888 instances within 2 minutes.
- [ ] **Step 3:** Check B1's journal for block drops or sample-loss lines over 30 min. Expected: none beyond the stock baseline (record the baseline count first).

### Task 9: The first proof — 24 h side by side

**Files:**
- Create: `hf-timestd/scripts/radiod_pilot/compare_live.py`
- Create: `~/hamsci/tasks/radiod-pilot/proof1.md` (results)

**Interfaces:**
- Consumes: Task 7 ledger JSONL; hf-timestd's `t6-anchors-*.jsonl` from the station running hf-timestd against B1's pilot channel (B2 or sigma; its T6 config points at the same SSRC); `t6_battery.T6Battery.evaluate`.

- [ ] **Step 1:** Configure hf-timestd's T6 on B2 (or sigma) to the B1 pilot channel's SSRC and run the listener beside it for 24 h.
- [ ] **Step 2: Implement `compare_live.py LEDGER ANCHORS`:** convert each hf-timestd fine edge (`anchor_rtp` plus its sub-sample) to the input counter with `rtp_to_input` and the nearest `ANCHOR_*` pair of the same epoch; pair with radiod's edge of the same named second; report median and robust σ of the difference in ns, each side's own scatter, and the battery verdict of every radiod edge marked locked (feed `PILOT_BATTERY` into `T6Battery.evaluate` with `search_mode` from the report).
- [ ] **Step 3:** Run it on the 24 h. Pass: median |difference| within 10–50 ns, radiod's scatter ≤ hf-timestd's, zero locked radiod edges that the battery refuses, and Task 8's sample-loss check still clean. Write `proof1.md` with the numbers.
- [ ] **Step 4:** Commit `compare_live.py`.

### Task 10: The second proof — labelling recordings, and CPU

**Files:**
- Create: `hf-timestd/scripts/radiod_pilot/label_record.py`
- Create: `~/hamsci/tasks/radiod-pilot/proof2.md`

- [ ] **Step 1: Implement `label_record.py --channel SSRC --start UTC --seconds N --out DIR`:** receive RTP for the channel, keep samples whose UTC (via `ANCHOR_*` → input counter → latest locked pilot report of the same epoch) falls in [start, start+N), write `.cf32` plus a JSON sidecar with the first sample's UTC, the epoch and the pilot report used.
- [ ] **Step 2:** Record 60 s from a whole UTC second on the pilot channel and on B1's 10 MHz WWV channel together. Pass: in the pilot recording, every second's edge falls at the predicted sample within proof 1's scatter; for the WWV channel, compare the first sample's UTC with hf-timestd's CounterMap label for the same RTP (`hamsci_dsp.counter_map.CounterMap.input_index` plus hf-timestd's chain delay), and explain the difference in `proof2.md`.
- [ ] **Step 3: CPU.** On B1's host, read the pilot channel's demod thread CPU from `/proc/<radiod pid>/task/*/stat` over 10 min with `pilot = yes`, then over 10 min after `undo`-ing only the `pilot` key. Pass: difference under 3 % of one core. Record in `proof2.md`.
- [ ] **Step 4:** Commit; report both proofs to Michael.

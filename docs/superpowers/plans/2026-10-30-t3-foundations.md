# T3 Foundations Implementation Plan (stages 0, 1, 2 and 5)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Measure the HF receive-chain delay against the TS-1 pilot, stop every T3 path from mixing two channels' RTP counters, make the registration stand alone from T6, and keep a per-minute shadow record on B4.

**Architecture:** A pure `CounterMap` in hamsci-dsp maps one channel's RTP to radiod's absolute input-sample index, learned from that channel's own `(INPUT_SAMPLES, RTP_TIMESNAP)` status pair. hf-timestd polls it per channel, carries it through the ring buffer into every registration, and combines channels only through it. The acquirer starts from radiod's raw pair and checks its plane against its own fold every minute; T6 becomes a recorded comparison, never an input. Small JSONL rows, written by metrology and by the authority manager, hold the shadow record.

**Tech Stack:** Python 3.11, numpy, ka9q-python `RadiodControl.poll_status` (typed `ChannelStatus`), SysV ring buffer, pytest.

**Spec:** `docs/superpowers/specs/2026-10-30-t3-restoration-design.md` (binding). Evidence: `~/hamsci/tasks/2026-10-30-t3-recon.md`.

**Out of this plan:** spec §8 stages 3 (naming the second), 4 (honest sigma), 6 (model refinement), 7 (estimator routing), 8 (the shadow run under §9) and 9 (added sources). They follow the measured results of this plan. Nothing here changes the 1.0 ms sigma floor (stage 4).

**Operator steps, outside the implementer's work:** the stage-0 run on AC0G-B4 (Task 3, last item); raising the ka9q-radio per-channel origin with Rob and upstream (spec §8 stage 1); every deploy to B4 (bus first, hf-timestd git fast-forward plus restart of the two units; hamsci-dsp fast-forward too, because Task 1 adds a module there). Spec §8 says each stage reports a measured result on B4 before the next begins, so execution pauses after Task 3 until the operator reports the stage-0 numbers.

---

## Decisions this plan makes where the spec left a choice

1. **Counter relation: the absolute input-sample index, not the UTC plane.** The only quantity two channels share is radiod's ADC sample count. A "UTC-plane" combination would need a common reference, and the only candidates are radiod's `(GPS_TIME, RTP_TIMESNAP)` pairs (host clock, skewed by up to 816 ms on B4) or the host clock itself, which spec §5 forbids. Sibling adoption and `resolve_ambiguity` also need a plane in the adopter's own counter, which only a counter relation can give. So each channel's RTP maps to the input index through its own pair; members without a map never combine across channels (fail closed).
2. **Where INPUT_SAMPLES comes from: `RadiodControl.poll_status`, no ka9q-python change.** The dict decoder behind `StatusListener` and `ChannelInfo` (`ka9q-python/ka9q/control.py` ~760-830) does not decode INPUT_SAMPLES. The typed decoder behind `poll_status` (`ka9q/status.py:444`, `ChannelStatus.frontend.input_samples`) does. hf-timestd already polls this way (`core/frontend_probe.py`).
3. **`CounterMap` lives in hamsci-dsp.** Every radiod client meets the per-channel origin (phase-engine flagged it first). The map holds no I/O and no clock reads, like `hamsci_dsp/timing_map.py`. The polling and wiring stay in hf-timestd.
4. **The raw pair reaches metrology through new ring fields.** Today the ring stores the judged pair (radiod pair plus the judge's offset, T6's on B4; `stream_recorder_v2.py:875-930`), so metrology never sees radiod's raw pair. Stage 2 needs it.
5. **Stand-alone is the only behaviour; no config switch.** A switch would keep alive the path that verifies T3 against T6 (`metrology_service.py:835-899`), which spec §5 and the ruling "T3 stands alone" forbid. B4's shadow must run the code a T3-only station ships. The plane still never labels metrology while T6 is authoritative, and the summary keeps the state name WITNESS there, so `registration_refusal` keeps it away from every authority surface.
6. **Verification by the acquirer's own fold, on every station.** The engine's tick detector searches a window placed by whatever plane the engine holds, which on B4 is T6's. A one-to-three-minute fold on T3's own plane uses only T3's ticks and markers. `feed_back_ensembles` stops driving `verify`/`corroborate`.
7. **Shadow record: JSONL, one file per stream per UTC day, 14 days' retention.** Metrology writes per-broadcast rows (one stream per channel, so six processes never share a file). The authority manager writes the per-minute row, because only it holds T2 and T4 side by side; the judge publishes the sample-plane T3−T6 figure it needs in `offset_judge.json`.
8. **Stage-0 tool: one clone of each HF channel plus one clone of the T6 channel, all tuned to the pilot.** Identical clones started at different moments carry different RTP origins, so the same run also proves the counter map on live radiod.

## What the code says that the spec and survey do not

- **The radiod FIR delay does not depend on the channel's filter or rate.** `create_filter_output` sizes a channel as `points = olen·N/L` (`ka9q-radio/src/filter.c:315-317`), and `set_filter` builds a windowed sinc centred at `(M−1)/2` of the channel's `M = points−olen+1` (`filter.c:998-1027`). So the group delay is `(FILTER_FIR_LENGTH−1)/(2·INPUT_SAMPRATE)` seconds for every channel on a frontend: Blocktime/8, 2.5 ms, at Overlap 5. Stage 0 predicts a per-configuration difference of zero. The config template's warning that the T6 filter delay "follows the CHANNEL FILTER" (`config/timestd-config.toml.template:586-588`) disagrees with this reading.
- **On a T6 station no sibling fusion happens today.** The T6 path writes every channel file as WITNESS or CANDIDATE (`metrology_service.py:888-899`), and `read_siblings` returns only ACQUIRED files (`registration_store.py:421`). So the B4 witness resets at −30 to −113 ms (survey Q1) cannot come from sibling fusion. They compare each channel's own plane with the ring's label plane, and the judge's per-channel offset carries each pair's age at registration (memory `project_t3_rtp_wrap_defect_20260909`, 10-10 entry). The bench's whole-second errors do fit per-channel origins: B4's channel origins spread over 6 s.
- **A fourth shared-origin site:** `core_recorder_v2._t6_observe_channel_epochs` and `_t6_peer_rtp` (`:3631-3700`) label T6 ledger provenance in "any metrology channel" because `cross_channel_rtp.py:11-15` says the 24 kHz channels share one counter. That premise predates ka9q-radio e5f790dd. This plan does not touch the ledger; it corrects the docstring and leaves a pointer.
- **A fifth:** `StreamRecorderV2._label_anchor_state` (`stream_recorder_v2.py:815-837`) guards a label anchor by sample rate alone, so a T3 anchor stamped in one 24 kHz channel would label the others. The closure ships off (`anchor_closure = false`), but Task 8 closes the guard.

## Global Constraints

- Sign convention (spec §6): `label_utc = rtp_time + rtp_to_utc_offset_ns`; acquirer convention (`registration_acquirer.py:20-21`): `correction_s = expected_delay_s - fold_position_s`, `sample0_utc_acquired = sample0_utc_label + correction_s`.
- Counter domains (spec §6): "Every observation names the RTP counter it was measured in. Nothing combines two channels' RTP values directly."
- Light on the station (spec §6): "Shadow logging writes small rows; no analysis runs on a station VM."
- Layering (spec §6): shared signal processing in hamsci-dsp; "The reconstruction, the authority and the judge live in hf-timestd."
- Anti-goals (spec §5): no path by which T3's answer depends on the host clock, T2 or T6; no run-time calibration against a higher tier; "A step, a walk or a wrong second must show, loudly, once."
- MEASUREMENT_MODEL §7.1.1: nothing here feeds chrony.
- hf-timestd CLAUDE.md: no new `time.time()`, `datetime.now()` or `chronyc` in the timing path. Shadow rows take their UTC minute from the data, not from a clock read.
- One class per file in hf-timestd, file named for the class.
- Tests in hf-timestd: `cd /home/mjh/hamsci/repos/hf-timestd && TMPDIR=/home/mjh/t2tmp uv run pytest <paths> -q --override-ini addopts= -p no:cacheprovider`. Never write to /tmp.
- Tests in hamsci-dsp: `cd /home/mjh/hamsci/repos/hamsci-dsp && TMPDIR=/home/mjh/t2tmp .venv/bin/python -m pytest <paths> -q --override-ini addopts= -p no:cacheprovider` (`uv run` fails there: iri2020 does not resolve).
- Baselines, run 2026-10-30: hf-timestd `tests/unit/ tests/test_ring_buffer.py tests/test_ring_rtp_base_after_rebase.py tests/test_offset_judge.py tests/test_bpsk_edge_fine_stage.py` printed `1723 passed, 2 skipped, 2 warnings, 6 subtests passed`; hamsci-dsp `tests` printed `530 passed, 3 xfailed`.
- Every new test runs red before its code exists. A test that passes on today's code proves nothing; strengthen it until it fails.
- Synthetic origins: B4 and ND run 64.8 Msps and a 20 ms block, so `ilen = 1_296_000`, `olen = 480` at 24 kHz and `1920` at 96 kHz. ND's measured start blocks: `k0 = 51, 51, 51, 51, 50` (memory `project_rtp_origin_regression_20261006`). A channel's RTP for input block `b`: `rtp = (2**32 - olen + (b + 1 - k0) * olen) % 2**32`, the inverse of the memory's `k0 = INPUT_SAMPLES/L + 1 − ((RTP_TIMESNAP − (2^32 − olen)) mod 2^32)/olen`.
- Commits: one per task, no push, in the repo the task names. Every message ends with:
  ```
  Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01H4HYgCZ3dRtK4XWA1Xyoc3
  ```

## Review Focus

1. **A radiod restart between two channels' maps.** A sibling whose map belongs to an earlier radiod run must not fuse; a reader expects the plane to drop that member, not to shift by hours. Test: Task 6, `test_member_from_another_radiod_run_is_not_fused`.
2. **Long uptime past half the RTP range (24 h 51 m at 24 kHz).** The 10-10 re-anchor defect lived exactly there. A held map must keep answering. Tests: Task 1, `test_rolled_forward_survives_thirty_hours`; Task 5, `test_refresh_returns_a_map_rolled_to_the_reply`.
3. **`poll_status` answers a nonexistent SSRC with frontend status** (memory `reference_ssrc_invariant_adover`). A reply without `rtp_timesnap`, or with another output rate, must not become a map. Test: Task 5, `test_ghost_reply_is_not_a_poll`.
4. **A torn status packet** (INPUT_SAMPLES and RTP_TIMESNAP read one block apart). The majority wins; a reader expects no 20 ms step. Test: Task 1, `test_torn_poll_is_outvoted`.
5. **An old ring producer with a new reader, and a full disk under the shadow record.** Absent aux fields read as None and T3 waits; a failing write logs one ERROR per streak and never raises. Tests: Task 4, `test_old_producer_reads_as_absent`; Task 10, `test_no_raw_pair_means_no_registration`; Task 12, `test_unwritable_directory_logs_once_and_never_raises`.

---

## File structure

| File | Repo | Responsibility |
|---|---|---|
| `src/hamsci_dsp/counter_map.py` (new) | hamsci-dsp | `CounterPoll`, `CounterMap`: RTP ↔ input-sample index for one channel |
| `src/hf_timestd/core/hf_chain_delay_analysis.py` (new) | hf-timestd | stage 0, pure: edges per clone → delay per configuration, derived delay, pair link |
| `src/hf_timestd/core/hf_chain_delay_probe.py` (new) | hf-timestd | stage 0, I/O: clone channels on the pilot, stream, poll, clean up |
| `src/hf_timestd/cli.py` | hf-timestd | `hf-chain-delay` subcommand |
| `src/hf_timestd/core/ring_buffer.py`, `ring_buffer_reader.py` | hf-timestd | aux hot fields: raw pair and counter map |
| `src/hf_timestd/core/counter_map_probe.py` (new) | hf-timestd | `CounterMapProbe`: polls, map history, step alarm |
| `src/hf_timestd/core/stream_recorder_v2.py`, `core_recorder_v2.py` | hf-timestd | write aux fields; per-channel arrivals; T3 shadow provider |
| `src/hf_timestd/core/registration_acquirer.py` | hf-timestd | `Registration` map fields; `note_counter_map`; `measure_minute` |
| `src/hf_timestd/core/registration_store.py` | hf-timestd | payload, siblings, fusion through the input index |
| `src/hf_timestd/core/metrology_service.py` | hf-timestd | raw-pair start, self verification, shadow rows |
| `src/hf_timestd/core/offset_judge.py`, `native_anchor.py` | hf-timestd | bench per channel; label-anchor domain; `t3_shadow` block |
| `src/hf_timestd/core/t3_shadow_comparator.py` (new) | hf-timestd | T3−T6 in the sample plane |
| `src/hf_timestd/core/t3_shadow_record.py`, `state_transition_counter.py` (new) | hf-timestd | JSONL rows; transitions logged once and counted |
| `src/hf_timestd/core/metrology_engine.py` | hf-timestd | `expected_delay_details` |
| `src/hf_timestd/core/authority_manager.py`, `authority_runner.py`, `config/timestd-config.toml.template` | hf-timestd | per-minute row; `[timing.t3_shadow]` |
| `tests/unit/synth_origins.py` (new) | hf-timestd | shared synthetic per-channel origins |

---

## Stage 0 — the HF chain-delay measurement

### Task 1: `CounterMap` in hamsci-dsp

**Files:**
- Create: `/home/mjh/hamsci/repos/hamsci-dsp/src/hamsci_dsp/counter_map.py`
- Test: `/home/mjh/hamsci/repos/hamsci-dsp/tests/test_counter_map.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  ```python
  @dataclass(frozen=True)
  class CounterPoll:
      rtp_timesnap: int; input_samples: int; filter_blocksize: int
      input_samprate: int; output_samprate: int; gps_time_ns: int

  @dataclass(frozen=True)
  class CounterMap:
      rtp_ref: int            # block-aligned RTP in this channel's counter
      input_ref: int          # radiod input-sample index at rtp_ref
      ilen: int               # FILTER_BLOCKSIZE (input samples per block)
      olen: int               # output samples per block
      input_samprate: int
      input_epoch_gps_s: float  # least-late gps_s − input_samples/f_in; names the radiod run, never timing
      n_agree: int
      @classmethod
      def from_polls(cls, polls: Sequence[CounterPoll], min_agree: int = 3) -> Optional["CounterMap"]: ...
      def input_index(self, rtp: int) -> float: ...
      def rolled_forward(self, rtp_now: int) -> "CounterMap": ...
      def same_run(self, other: "CounterMap", tol_s: float = 0.5) -> bool: ...
      def intercept_blocks_from(self, other: "CounterMap") -> Optional[int]: ...
      def to_dict(self) -> dict: ...
      @classmethod
      def from_dict(cls, d: Optional[dict]) -> Optional["CounterMap"]: ...
  ```

- [ ] **Step 1: Write the failing tests.** A local helper builds polls from the Global Constraints formula. Names and assertions:

```python
L, F_IN, F_OUT, OLEN = 1_296_000, 64_800_000, 24_000, 480
ND_K0 = (51, 51, 51, 51, 50)

def test_nd_origins_map_one_instant_to_one_index():
    maps = [CounterMap.from_polls(polls_for(k0, blocks=range(10_000, 10_005))) for k0 in ND_K0]
    for m, k0 in zip(maps, ND_K0):
        assert m.input_index(rtp_at(12_000, k0)) == pytest.approx(12_000 * L, abs=1e-6)
    # the defect itself: same instant, raw RTP one block apart
    assert (rtp_at(12_000, 50) - rtp_at(12_000, 51)) % 2**32 == OLEN

def test_torn_poll_is_outvoted():          # one poll's input_samples is +L
    assert m.input_ref == majority_intercept and m.n_agree == 4

def test_too_few_agreeing_polls_gives_none():   # 2 agree of 4, min_agree=3
    assert CounterMap.from_polls(polls, min_agree=3) is None

def test_input_index_is_continuous_across_the_rtp_wrap():
    # rtp_ref near 2**32 - OLEN; rtp past the wrap maps to input_ref + n*L exactly

def test_rolled_forward_survives_thirty_hours():
    later = 10_000 + 30 * 3600 * 50          # 30 h of 20 ms blocks
    rolled = m.rolled_forward(rtp_at(later, 51))
    assert rolled.input_index(rtp_at(later, 51)) == pytest.approx(later * L, abs=1e-6)
    assert abs(m.input_index(rtp_at(later, 51)) - later * L) > 1e9   # unrolled aliases

def test_non_integer_block_is_refused():   # output_samprate 22_050
    assert CounterMap.from_polls(polls) is None

def test_another_run_is_not_the_same_run():   # input_epoch_gps_s differs by 3600 s
    assert not a.same_run(b) and a.intercept_blocks_from(b) is None

def test_same_run_step_counts_blocks():       # intercept moved by one block
    assert a.same_run(b) and b.intercept_blocks_from(a) == 1

def test_dict_round_trip():
    assert CounterMap.from_dict(m.to_dict()) == m and CounterMap.from_dict(None) is None
```

- [ ] **Step 2: Run the tests and see them fail.**
Run: `cd /home/mjh/hamsci/repos/hamsci-dsp && TMPDIR=/home/mjh/t2tmp .venv/bin/python -m pytest tests/test_counter_map.py -q --override-ini addopts= -p no:cacheprovider`
Expected: collection error, `ModuleNotFoundError: No module named 'hamsci_dsp.counter_map'`.

- [ ] **Step 3: Implement `counter_map.py`.** `olen = output_samprate * ilen / input_samprate`, refused unless exact. Each poll implies an intercept at the first poll's RTP: `input_samples − (signed32(rtp − rtp0) // olen) · ilen`, refused unless `signed32(rtp − rtp0) % olen == 0`. The mode of the intercepts wins when at least `min_agree` polls share it. `input_epoch_gps_s` is the minimum over agreeing polls of `gps_time_ns/1e9 − input_samples/input_samprate`. `input_index(rtp) = input_ref + signed32((rtp & 0xFFFFFFFF) − rtp_ref) · ilen / olen`. `rolled_forward` moves `rtp_ref` by whole blocks to the block nearest `rtp_now` (mod 2**32) and `input_ref` by the same number of `ilen`. `intercept_blocks_from(other)` returns None unless `same_run`, else the whole-block difference of the two maps' `input_index` at `self.rtp_ref`. Module docstring: cite `radio_status.c:670` (INPUT_SAMPLES = `chan->filter.out.sample_index`) and `:819` (RTP_TIMESNAP), state that the RTP↔INPUT_SAMPLES offset convention is assumed equal for every linear channel and that Task 3's live run tests that assumption.

- [ ] **Step 4: Run the tests and see them pass.** Same command. Expected: `9 passed`. Then the full hamsci-dsp suite: `539 passed, 3 xfailed`.

- [ ] **Step 5: Commit (hamsci-dsp).**
```bash
cd /home/mjh/hamsci/repos/hamsci-dsp && git add src/hamsci_dsp/counter_map.py tests/test_counter_map.py
git commit -m "counter_map: one channel's RTP to radiod's input-sample index

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01H4HYgCZ3dRtK4XWA1Xyoc3"
```

### Task 2: Stage-0 analysis (pure)

**Files:**
- Create: `src/hf_timestd/core/hf_chain_delay_analysis.py`
- Create: `tests/unit/synth_origins.py` (helpers `ND_K0`, `rtp_at(block, k0, olen)`, `polls_for(k0, blocks, f_out)`, `counter_map_for(k0, f_out)`; same formula as Task 1, shared by Tasks 2-14)
- Test: `tests/unit/test_hf_chain_delay_analysis.py`

**Interfaces:**
- Consumes: `CounterMap` (Task 1).
- Produces:
  ```python
  @dataclass(frozen=True)
  class EdgeObservation:   edge_rtp: int; edge_subsample: float; fit_rms: float
  @dataclass(frozen=True)
  class ProbeChannelCapture:
      label: str; preset: str; output_samprate: int; low_edge_hz: float; high_edge_hz: float
      counter_map: Optional[CounterMap]; edges: Tuple[EdgeObservation, ...]
      fir_length: int; input_samprate: int
      filter2_fir_length: Optional[int] = None
  @dataclass(frozen=True)
  class ConfigDelay:
      label: str; n_edges: int; delay_vs_ref_ms: Optional[float]; u_ms: Optional[float]
      derived_ms: float; derived_vs_ref_ms: float; verdict: str
  @dataclass(frozen=True)
  class HfChainDelayReport:
      reference_label: str; rows: Tuple[ConfigDelay, ...]
      clone_agreement_input_samples: Optional[float]
      link_ms: Optional[float]; link_minus_derived_ms: Optional[float]; n_pair_polls: int
      learned_chain_delay_ms: Optional[float]
  def derived_filter_delay_s(fir_length: int, input_samprate: int,
                             filter2_fir_length: Optional[int] = None,
                             output_samprate: Optional[int] = None) -> float
  def analyze(captures: Sequence[ProbeChannelCapture], reference_label: str,
              pair_epochs_gps_s: Sequence[float], learned_chain_delay_ms: Optional[float],
              tolerance_ms: float = 0.1) -> HfChainDelayReport
  def format_report(report: HfChainDelayReport) -> str
  ```

- [ ] **Step 1: Write the failing tests.**

```python
def test_identical_clones_with_different_origins_agree():
    # T6_REF (96 kHz, k0=40) + six 24 kHz clones, k0 = 51,51,51,51,50,120; true edge at input phase P
    r = analyze(caps, "T6_REF", pair_epochs_gps_s=[...], learned_chain_delay_ms=None)
    assert all(abs(row.delay_vs_ref_ms) < 1e-6 for row in r.rows if row.label != "T6_REF")
    assert r.clone_agreement_input_samples <= 1.0

def test_a_late_configuration_is_measured_and_fails_the_derivation():
    # one clone's edges sit 0.25 ms (16_200 input samples) later
    row = by_label(r, "WWV_25000")
    assert row.delay_vs_ref_ms == pytest.approx(0.25, abs=1e-3)
    assert row.derived_vs_ref_ms == 0.0 and row.verdict == "derivation_fails"

def test_matching_configuration_reports_derivation_holds():
    assert by_label(r, "SHARED_10000").verdict == "derivation_holds"

def test_derived_delay_is_half_the_fir_span_at_the_input_rate():
    assert derived_filter_delay_s(324_001, 64_800_000) == pytest.approx(0.0025)

def test_missing_counter_map_refuses_the_comparison():
    row = by_label(r, "SHARED_5000")
    assert row.delay_vs_ref_ms is None and row.verdict == "no_counter_map"

def test_link_is_the_pair_floor_minus_the_pps_epoch():
    # pps epoch x; pair epochs x+0.0161, x+0.0170, x+0.0300
    assert r.link_ms == pytest.approx(16.1, abs=1e-6) and r.n_pair_polls == 3

def test_report_prints_one_row_per_clone_and_the_link():
    text = format_report(r)
    assert text.count("\n") >= len(r.rows) + 2
    assert "T6_REF" in text and "link (radiod pair − PPS edge)" in text
    assert "learned T6 chain delay" in text
```

- [ ] **Step 2: Run and see them fail.** `TMPDIR=/home/mjh/t2tmp uv run pytest tests/unit/test_hf_chain_delay_analysis.py -q --override-ini addopts= -p no:cacheprovider` → `ModuleNotFoundError: No module named 'hf_timestd.core.hf_chain_delay_analysis'`.

- [ ] **Step 3: Implement.** Each edge's input phase is `(map.input_index(edge_rtp) + edge_subsample · ilen/olen) mod input_samprate`. Per clone: circular median of phases, `u` = 1.4826·MAD/√n in ms. `delay_vs_ref_ms` = (clone phase − reference phase), wrapped to ±0.5 s, in ms. `derived_vs_ref_ms` = derived(clone) − derived(reference). Verdict `derivation_holds` when `|delay − derived_vs_ref| ≤ tolerance_ms`, else `derivation_fails`; `no_counter_map` or `no_edges` when inputs are missing. `clone_agreement_input_samples` = spread of phases among clones whose `(preset, output_samprate, low, high)` match. The link: the PPS epoch is `S − ref_edge_index/f_in` with `S` the GPS second nearest the pair floor's implied time of the edge; `link_ms = (min(pair_epochs) − pps_epoch)·1000`; `link_minus_derived_ms` subtracts the reference's derived delay. Work in GPS seconds throughout: no leap table, no clock read. Docstring: name each sign in words ("radiod pair minus PPS edge: positive means the pair labels a sample later than the PPS says").

- [ ] **Step 4: Run and see them pass.** Expected `7 passed`.

- [ ] **Step 5: Commit.** `git add src/hf_timestd/core/hf_chain_delay_analysis.py tests/unit/synth_origins.py tests/unit/test_hf_chain_delay_analysis.py`, message `stage 0: HF chain-delay analysis against the TS-1 reference clone` plus the trailers.

### Task 3: Stage-0 probe and CLI

**Files:**
- Create: `src/hf_timestd/core/hf_chain_delay_probe.py`
- Modify: `src/hf_timestd/cli.py` (parser beside `chain-delay` at `:2001-2024`; dispatch beside `:2280`)
- Test: `tests/unit/test_hf_chain_delay_probe.py`, `tests/unit/test_cli_hf_chain_delay.py`

**Interfaces:**
- Consumes: `CounterPoll`, `CounterMap` (Task 1); `ProbeChannelCapture`, `EdgeObservation`, `analyze`, `format_report` (Task 2); `BpskEdgeFineStage(sample_rate, fold_seconds=30)` and `.process_samples(iq, rtp) -> Optional[FineEdgeEstimate]` (`bpsk_edge_fine_stage.py:156`, `:304`); `RadiodControl.create_channel(frequency_hz, preset, sample_rate, ssrc=, lifetime=, low_edge=, high_edge=)` (`ka9q-python/ka9q/control.py:1364`), `.remove_channel(ssrc)` (`:2099`), `.poll_status(ssrc, timeout)` (`:3457`); `RadiodStream(channel, on_samples)` (`ka9q/stream.py:286`); `ChainDelayStore` (judge record, `cli.py:1000`).
- Produces:
  ```python
  @dataclass(frozen=True)
  class ProbeChannelSpec:
      label: str; ssrc: int; frequency_hz: float; preset: str
      sample_rate: int; low_edge_hz: Optional[float]; high_edge_hz: Optional[float]
  class HfChainDelayProbe:
      SSRC_BASE = 0x7E5C_0000
      LIFETIME_S = 1800
      def __init__(self, control, stream_factory, fine_stage_factory=BpskEdgeFineStage,
                   fold_seconds: int = 30, poll_interval_s: float = 1.0,
                   mono_fn=time.monotonic, sleep_fn=time.sleep): ...
      @staticmethod
      def plan_channels(config: dict) -> List[ProbeChannelSpec]: ...
      def run(self, specs: Sequence[ProbeChannelSpec], duration_s: float
              ) -> Tuple[List[ProbeChannelCapture], List[float]]: ...
  ```
  CLI: `hf-timestd hf-chain-delay [--duration-s 600] [--config PATH] [--output PATH] [--json]`.

- [ ] **Step 1: Write the failing tests.** Fakes stand in for radiod: a control that records create/remove calls and answers `poll_status` with `SimpleNamespace` replies, a stream factory, and a fine-stage factory that returns canned `FineEdgeEstimate`s.

```python
def test_plan_clones_t6_and_every_hf_channel_on_the_pilot():
    specs = HfChainDelayProbe.plan_channels(cfg)   # t6 at 19_425_000 Hz, 96 kHz ±25 kHz; six 24 kHz iq
    assert [s.label for s in specs] == ["T6_REF", "SHARED_2500", ..., "WWV_25000"]
    assert {s.frequency_hz for s in specs} == {19_425_000.0}
    assert len({s.ssrc for s in specs}) == 7 and specs[1].sample_rate == 24_000

def test_plan_refuses_without_a_pilot():         # [timing.t6_pps] frequency_hz = 0 or enabled=false
    with pytest.raises(ValueError, match="no TS-1 pilot"): HfChainDelayProbe.plan_channels(cfg)

def test_every_clone_is_removed_even_when_streaming_fails():
    assert sorted(control.removed) == sorted(s.ssrc for s in specs)

def test_ghost_reply_is_not_a_poll():             # reply without rtp_timesnap
    caps, _ = probe.run(specs, 5)
    assert all(c.counter_map is None for c in caps)

def test_run_returns_edges_maps_and_pair_epochs():
    assert caps[0].edges and caps[0].counter_map is not None and len(epochs) >= 5

# test_cli_hf_chain_delay.py
def test_cli_prints_the_report_and_writes_json(monkeypatch, tmp_path, capsys):
    rc = main(["hf-chain-delay", "--duration-s", "5", "--config", str(cfg), "--output", str(out)])
    assert rc == 0 and "T6_REF" in capsys.readouterr().out and json.loads(out.read_text())["rows"]

def test_cli_exits_2_without_a_pilot(...):
    assert rc == 2
```

- [ ] **Step 2: Run and see them fail.** Expected: `ModuleNotFoundError: ... hf_chain_delay_probe`, and argparse `invalid choice: 'hf-chain-delay'` for the CLI tests.

- [ ] **Step 3: Implement.** `plan_channels` reads `[timing.t6_pps]` (`frequency_hz`, `sample_rate`, `low_edge_hz`, `high_edge_hz`) and `[recorder.channel_group.timestd]` with its `channels`, cloning each channel's `preset`, `sample_rate` and edges (group defaults where a channel names none). SSRCs are explicit, `SSRC_BASE + i`, because `create_channel` would give identical configurations one shared SSRC. `run` creates every clone with `lifetime=LIFETIME_S`, starts one stream per clone feeding its fine stage, polls each SSRC once per `poll_interval_s`, keeps replies that carry `rtp_timesnap`, `frontend.input_samples` and the clone's own `output_samprate`, builds each map with `CounterMap.from_polls`, collects the pair epochs of the T6 clone, and removes every clone in `finally`. The CLI handler reads the learned chain delay through `ChainDelayStore`, prints `format_report`, writes the report as JSON when `--output` is given, and returns 2 on `ValueError` from `plan_channels`. Module docstring: this tool creates radiod channels, so it runs only as an announced operator step.

- [ ] **Step 4: Run and see them pass.** Expected `7 passed`. Then the baseline paths: no failures.

- [ ] **Step 5: Commit.** Message `stage 0: hf-chain-delay probe clones the HF channels onto the TS-1 pilot` plus trailers.

- [ ] **Operator checklist (not implementer work).** After the bus announcement and the owner's approval, with hf-timestd and hamsci-dsp fast-forwarded on B4, run as the service user on the B4 VM:
  ```
  hf-timestd hf-chain-delay --duration-s 600 --output /var/lib/timestd/state/hf-chain-delay-$(date -u +%Y%m%dT%H%MZ).json
  ```
  It must print seven rows, `T6_REF` then `SHARED_2500` … `WWV_25000`, each with `n_edges` ≥ 15, `delay_vs_ref_ms`, `u_ms`, `derived_ms`, `derived_vs_ref_ms` and a verdict; a line `clone agreement: N input samples`; a line `link (radiod pair − PPS edge): X ms (derived FIR removed: Y ms) from N polls`; and a line `learned T6 chain delay: Z ms`. The run counts only if the clone agreement is ≤ 1 input sample (this also proves Task 1's convention assumption on live radiod) and every HF row has `u_ms` ≤ 0.05. Report the table to Michael before stage 1 deploys; criteria 1 and 2 of spec §9 take their expected offsets from it.

---

## Stage 1 — the per-channel counter

### Task 4: Ring buffer carries the raw pair and the counter map

**Files:**
- Modify: `src/hf_timestd/core/ring_buffer.py:101-117` (new hot indices), new methods beside `update_anchor` (`:540-575`)
- Modify: `src/hf_timestd/core/ring_buffer_reader.py` (`_read_gps_anchor` `:234-262`; metadata in `extract_interval` `:422-431` and `extract_samples` `:475-484`)
- Test: `tests/test_ring_buffer_aux.py`

**Interfaces:**
- Consumes: `CounterMap` (Task 1).
- Produces: hot indices `HOT_AUX_SEQ = 14`, `HOT_RAW_GPS_NS = 15`, `HOT_RAW_RTP_SNAP = 16`, `HOT_CMAP_RTP = 17`, `HOT_CMAP_INPUT = 18`, `HOT_CMAP_ILEN = 19`, `HOT_CMAP_OLEN = 20`, `HOT_CMAP_INPUT_RATE = 21`, `HOT_CMAP_EPOCH_GPS_MS = 22`; `RingBuffer.update_raw_pair(gps_time_ns: int, rtp_timesnap: int) -> None`; `RingBuffer.update_counter_map(cmap: Optional[CounterMap]) -> None`; `RingBufferReader.read_aux() -> dict`; metadata keys `raw_gps_time_ns: Optional[int]`, `raw_rtp_timesnap: Optional[int]`, `counter_map: Optional[CounterMap]`.

- [ ] **Step 1: Write the failing tests** (sysv_ipc, as `tests/test_ring_buffer.py:55` builds rings):
  - `test_raw_pair_reaches_the_reader_metadata`: write samples, `update_anchor(judged…)`, `update_raw_pair(g, r)`; `extract_interval(...)` metadata carries `raw_gps_time_ns == g` and `raw_rtp_timesnap == r`, while `gps_time_ns` stays the judged value.
  - `test_counter_map_round_trips_through_the_ring`: `update_counter_map(m)` then `metadata["counter_map"] == m` (epoch to the millisecond).
  - `test_old_producer_reads_as_absent`: a ring that never set aux fields gives `None` for all three keys.
  - `test_aux_update_does_not_bump_the_anchor_epoch`: `HOT_EPOCH_COUNTER` unchanged by both new writes, so an in-flight `extract_interval` never fails on them.
- [ ] **Step 2: Run and see them fail:** `AttributeError: 'RingBuffer' object has no attribute 'update_raw_pair'`.
- [ ] **Step 3: Implement.** A second seqlock on `HOT_AUX_SEQ` (odd while writing, even after); the reader retries while odd or changed. Zero means absent. Store `input_epoch_gps_s` as integer milliseconds. Layout comment: "do NOT renumber", as at `:98`.
- [ ] **Step 4: Run and see them pass.** `4 passed`; `tests/test_ring_buffer.py` still green.
- [ ] **Step 5: Commit.** `ring: carry radiod's raw pair and the channel's counter map` plus trailers.

### Task 5: `CounterMapProbe` and the recorder's writes

**Files:**
- Create: `src/hf_timestd/core/counter_map_probe.py`
- Modify: `src/hf_timestd/core/stream_recorder_v2.py` (`_update_ring_anchor` `:875-930`; new `publish_counter_map`)
- Modify: `src/hf_timestd/core/core_recorder_v2.py` (construct probe after `self.control` `:454`; refresh in the 60 s loop `:1368-1376`)
- Test: `tests/unit/test_counter_map_probe.py`, `tests/unit/test_stream_recorder_ring_aux.py`

**Interfaces:**
- Consumes: Task 1; Task 4's `update_raw_pair`, `update_counter_map`; `RadiodControl.poll_status`.
- Produces:
  ```python
  class CounterMapProbe:
      POLLS_AT_START = 5
      HISTORY = 5
      def __init__(self, control, poll_timeout: float = 1.0): ...
      def refresh(self, channel: str, ssrc: int, output_samprate: int) -> Optional[CounterMap]: ...
      @property
      def steps(self) -> Dict[str, int]: ...
  StreamRecorderV2.publish_counter_map(self, cmap: Optional[CounterMap]) -> None
  CoreRecorderV2._counter_maps: Dict[str, CounterMap]      # by channel description
  CoreRecorderV2._t6_counter_map: Optional[CounterMap]
  ```

- [ ] **Step 1: Write the failing tests.**
  - `test_first_refresh_polls_five_times_then_one`: fake control counts calls: 5, then 6 after a second refresh.
  - `test_ghost_reply_is_not_a_poll`: replies lacking `rtp_timesnap` or with `output_samprate` 12 000 for a 24 kHz channel give `None`.
  - `test_refresh_returns_a_map_rolled_to_the_reply`: the returned map's `rtp_ref` sits within one block of the newest reply's `rtp_timesnap`.
  - `test_map_step_on_the_same_run_logs_once_and_counts`: history shifted one block → one ERROR containing `COUNTER MAP STEP SHARED_10000` and `steps["SHARED_10000"] == 1`; a third identical refresh logs nothing new.
  - `test_new_radiod_run_replaces_the_map_without_a_step`: epoch moved by 3600 s → INFO, `steps` unchanged.
  - `test_update_ring_anchor_writes_the_raw_pair_beside_the_judged_one` (stream recorder with a fake ring): `update_raw_pair` receives the arguments `_update_ring_anchor` received, before any judge offset.
- [ ] **Step 2: Run and see them fail:** `ModuleNotFoundError ... counter_map_probe`; the stream test fails with `AttributeError ... update_raw_pair` never called.
- [ ] **Step 3: Implement.** Convert each `ChannelStatus` to `CounterPoll` from `rtp_timesnap`, `frontend.input_samples`, `filter_blocksize`, `frontend.input_samprate`, `output_samprate`, `gps_time`. Keep the last `HISTORY` polls per channel; rebuild with `CounterMap.from_polls`; compare with the held map through `same_run` and `intercept_blocks_from`. A step is a fault: log at ERROR once per step and count it (spec §5, "loudly, once"). Never raise; a failed poll returns the held map rolled forward. In the recorder loop, refresh each archive recorder by `recorder.channel_info.ssrc` and `recorder.config.sample_rate`, call `publish_counter_map`, and store `_counter_maps[desc]`; refresh the T6 channel into `_t6_counter_map` when `_t6_channel_info` exists.
- [ ] **Step 4: Run and see them pass.** `6 passed`.
- [ ] **Step 5: Commit.** `recorder: learn each channel's counter map from its own status pair` plus trailers.

### Task 6: Registrations carry their counter; fusion goes through the input index

**Files:**
- Modify: `src/hf_timestd/core/registration_acquirer.py` (`Registration` `:803-862`)
- Modify: `src/hf_timestd/core/registration_store.py` (docstring `:1-10`; `_same_counter_space` `:148-190`; `fuse_registrations_with_members` `:193-285`; `fuse_registrations` `:288-291`; `_payload` `:332-376`; `read_siblings` `:405-461`)
- Test: `tests/unit/test_registration_store_counter_domains.py`; rewrite in `tests/unit/test_registration_store.py` the eight tests that fuse several channels without maps (`:29-65`, `:186-260`, `:304-378`): give each member `counter_map_for(51, ...)` so they state a shared origin explicitly instead of assuming it.

**Interfaces:**
- Consumes: Task 1; `tests/unit/synth_origins.py`.
- Produces:
  ```python
  # Registration, new fields (appended, defaults keep positional construction working)
  counter_map: Optional[CounterMap] = None
  rtp_domain: str = ""            # channel whose counter rtp_ref is in
  def in_domain(self, rtp: int, cmap: Optional[CounterMap], domain: str) -> Optional["Registration"]
  # registration_store
  def fuse_registrations_with_members(regs, at_rtp: int, at_map: Optional[CounterMap] = None,
                                      domain: str = "") -> Tuple[Optional[Registration], List[str], List[str]]
  def fuse_registrations(regs, at_rtp: int, at_map: Optional[CounterMap] = None,
                         domain: str = "") -> Optional[Registration]
  # payload keys: "counter_map" (dict | None), "rtp_domain" (str)
  ```

- [ ] **Step 1: Write the failing tests.**

```python
def test_nd_siblings_fuse_to_truth_in_the_callers_counter():
    # five channels, k0 = ND_K0, each with a plane exactly at truth in its own counter
    # fuse in the counter of the odd one out (WWV_20000, k0 = 50)
    fused = fuse_registrations(regs, at_rtp=rtp_at(20_000, 50), at_map=m50, domain="WWV_20000")
    assert fused.sample0_utc_for(rtp_at(20_000, 50)) == pytest.approx(truth(20_000), abs=1e-6)
    assert fused.rtp_domain == "WWV_20000" and fused.counter_map == m50
    # today: the four k0=51 planes project 20 ms off in this counter, out-vote the
    # correct own plane at the median, and the fused plane sits 20 ms from truth

def test_member_without_a_map_never_combines_across_channels():
    _, kept, _ = fuse_registrations_with_members([own_with_map, sib_without_map], ...)
    assert kept == ["SHARED_10000"]

def test_own_channel_without_a_map_still_projects_in_its_own_counter():
    assert fuse_registrations([own_no_map], at_rtp=r, domain="SHARED_10000") is not None

def test_member_from_another_radiod_run_is_not_fused():
    # sibling map with input_epoch_gps_s 3600 s away
    assert kept == ["SHARED_10000"]

def test_store_round_trips_the_counter_map_and_domain(tmp_path):
    assert sib.counter_map == m50 and sib.rtp_domain == "WWV_20000"

def test_witness_files_are_siblings_too(tmp_path):          # used by Task 10
    assert [r.channel for r in store.read_siblings("X")] == ["SHARED_5000"]
```

- [ ] **Step 2: Run and see them fail:** `TypeError: __init__() got an unexpected keyword argument 'counter_map'`. Also record the defect's own number: a scratch copy of the first test that calls today's `fuse_registrations(regs, at_rtp)` without maps lands 20 ms off truth, because the four k0 = 51 members win the median and the correct member falls to the 3 ms outlier test. Do not commit the scratch copy.
- [ ] **Step 3: Implement.** `in_domain` returns a projection through `sample0_utc_for` when `domain == self.rtp_domain`; through both maps (`utc = utc_ref + (cmap.input_index(rtp) − self.counter_map.input_index(rtp_ref))/input_samprate`) when both exist and `same_run`; else None. Fusion converts every member first, drops the ones that return None, and runs the outlier test, corroboration floor and inverse-variance mean on converted planes. The output carries `rtp_ref=at_rtp`, `counter_map=at_map`, `rtp_domain=domain`. Retire `_same_counter_space`; conversion replaces it. `read_siblings` accepts states `ACQUIRED` and `WITNESS`. Replace the module docstring's premise (`:3-6`) with the per-channel-origin fact and its source (ka9q-radio e5f790dd; ND 2026-10-06).
- [ ] **Step 4: Run and see them pass.** New file `6 passed`; `tests/unit/test_registration_store.py` green after its rewrite.
- [ ] **Step 5: Commit.** `registration: fuse siblings through the input-sample index, never raw RTP` plus trailers.

### Task 7: Metrology hands each registration its own counter

**Files:**
- Modify: `src/hf_timestd/core/registration_acquirer.py` (new `note_counter_map`; `resolve_ambiguity` `:1247-1299` unchanged in shape)
- Modify: `src/hf_timestd/core/metrology_service.py` (`_apply_registration_unsafe` `:728-968`: fusion calls `:761`, `:805`; CONFLICT deltas `:927-935`; `_publish_registration` `:970-1038`)
- Test: `tests/unit/test_metrology_service_counter_domains.py`

**Interfaces:**
- Consumes: Task 4 metadata `counter_map`; Task 6 fusion signature and `Registration` fields.
- Produces: `RegistrationAcquirer.note_counter_map(cmap: Optional[CounterMap]) -> None`; summary and channel files carry `rtp_domain` and `counter_map`.

- [ ] **Step 1: Write the failing tests** (two `_service` instances from `tests/unit/test_metrology_service_registration.py:34-45`, one store; `_meta(k)` gains `counter_map`):
  - `test_adopted_sibling_plane_is_right_in_the_adopters_counter`: WWV_20000 (k0 = 50) acquires from tick audio at truth; SHARED_10000 (k0 = 51) hears noise only and adopts. Assert `svc_a.acquirer.registration.sample0_utc_for(start_rtp_a) == approx(truth_a, abs=1e-6)`. Today: off by 20 ms.
  - `test_sibling_resolves_ambiguity_in_its_own_counter`: as `test_shared_channel_ambiguity_resolved_by_same_site_sibling` (`:266`) but with k0 51 against 50; the resolved plane lands within 0.5 ms of truth. Today: no match (20 ms > `SAME_SITE_AGREE_MS`), so it stays ambiguous.
  - `test_counter_map_step_resets_the_plane`: a map one block away on the same run → `reset` logged with `counter map moved 1 block`.
  - `test_conflict_deltas_are_named_in_the_callers_counter`: the warning's per-sibling deltas read ≈ +0.0 ms for a sibling at truth with another origin.
- [ ] **Step 2: Run and see them fail:** values above, and `AttributeError: ... has no attribute 'note_counter_map'`.
- [ ] **Step 3: Implement.** Each minute the service calls `self.acquirer.note_counter_map(metadata.get("counter_map"))`. The acquirer stamps the map and `rtp_domain=self.channel` on its held and future registrations; a map on another run or with a moved intercept resets the plane, loudly. Every fusion call passes `at_map` and `domain=self.channel_name`. CONFLICT deltas use `r.in_domain(start_rtp, cmap, channel)`.
- [ ] **Step 4: Run and see them pass.** `4 passed`; `tests/unit/test_metrology_service_registration.py` green.
- [ ] **Step 5: Commit.** `metrology: every registration names its counter; adoption converts` plus trailers.

### Task 8: The judge's T3 bench and the label anchor stay in one counter

**Files:**
- Modify: `src/hf_timestd/core/core_recorder_v2.py` (`_hf_arrival` `:762-773`; tap `:2995-3014`; `_hf_acquired_bench_state` `:2823-2838`)
- Modify: `src/hf_timestd/core/offset_judge.py` (`HfAcquiredBench` `:861-990`)
- Modify: `src/hf_timestd/core/native_anchor.py` (`LabelAnchor` `:98-134`), `t3_registration_anchor.py` (`state` `:123-138`), `stream_recorder_v2.py` (`_label_anchor_state` `:815-837`)
- Modify: `src/hf_timestd/core/cross_channel_rtp.py` docstring `:11-20` (premise void since e5f790dd; points at `_t6_peer_rtp` as a known remaining user)
- Test: `tests/unit/test_hf_acquired_bench_domains.py`; rewrite the tuple-provider tests in `tests/unit/test_offset_judge_hf_acquired.py` (`:22-182`, `:241-352`) to the dict shape.

**Interfaces:**
- Consumes: summary key `rtp_domain` (Task 7).
- Produces: `CoreRecorderV2._hf_arrivals: Dict[str, Tuple[int, float, int]]`; `HfAcquiredBench(provider: Callable[[], Optional[Dict[str, Tuple[int, float, int]]]], ...)`; `LabelAnchor.rtp_domain: Optional[str] = None`, `LabelAnchor.matches_domain(channel: str) -> bool`.

- [ ] **Step 1: Write the failing tests.**
  - `test_bench_projects_through_the_summarys_own_channel`: summary `rtp_domain="SHARED_10000"` (k0 = 51) at truth; arrivals dict holds SHARED_10000 and a newer WWV_20000 (k0 = 50). Assert `reading.utc == approx(truth_at_arrival, abs=1e-6)`. Today: the tuple provider takes the newest arrival, so the bench reads 20 ms off (or a whole second off for B4-like origins 6 s apart).
  - `test_bench_is_silent_without_a_domain_or_its_arrival`: summary lacking `rtp_domain`, or no arrival for that channel → `None`.
  - `test_tap_records_arrivals_per_channel`: two `_wire_t5_fallback_arrival` taps fill two keys.
  - `test_t3_label_anchor_refused_on_another_channel_of_the_same_rate`: a `StreamRecorderV2` for WWV_20000 with a provider returning a T3 `LabelAnchor(rtp_domain="SHARED_10000")` at 24 kHz → `_label_anchor_state()` is None. Today it returns the anchor.
- [ ] **Step 2: Run and see them fail** with those values.
- [ ] **Step 3: Implement.** The tap writes `self._hf_arrivals[description]`; the provider returns a copy. The bench picks `summary["rtp_domain"]` and its arrival and keeps every other gate. `T3RegistrationAnchor.state()` sets `rtp_domain` from the summary; the stream recorder adds `matches_domain(self.config.description)` beside `matches_rate`. T6 anchors keep `rtp_domain=None` and the rate guard.
- [ ] **Step 4: Run and see them pass.** `4 passed`; rewritten bench tests green.
- [ ] **Step 5: Commit.** `judge: the T3 bench and the label anchor project in one channel's counter` plus trailers.

---

## Stage 2 — stand-alone shadow mode

### Task 9: The acquirer measures its own plane

**Files:**
- Modify: `src/hf_timestd/core/registration_acquirer.py` (beside `verify` `:1302`; reuses `band_envelopes` `:103`, `fold_envelope` `:108`, `find_fold_peaks` `:170`, `marker_in_envelope` `:544`, `whole_second_from_marker` `:744`, `_sigma_ms_from_snr` `:367`)
- Test: `tests/unit/test_registration_acquirer_self_measure.py`

**Interfaces:**
- Produces:
  ```python
  @dataclass(frozen=True)
  class MinuteArrival:            # one station, this minute, on the held plane
      station: str; residual_ms: float; sigma_ms: float
      arrival_rtp: float; fold_snr_db: float
  def measure_minute(self, audio: np.ndarray, start_rtp: int, minute_utc: int,
                     expected_delays_s: Dict[str, float]) -> Dict[str, MinuteArrival]
  def residuals_for_verify(arrivals: Dict[str, MinuteArrival]) -> Dict[str, Tuple[float, float]]
  ```

- [ ] **Step 1: Write the failing tests** (`tests/unit/synth_ticks.py`):
  - `test_a_true_plane_measures_near_zero`: acquire on minutes 0-2 (label +250 ms), measure minute 3 → `abs(residual_ms) < 0.5`, `sigma_ms <= 6.0`.
  - `test_a_plane_three_ms_off_measures_three_ms`: shift `utc_ref` by +3 ms, so the plane labels every sample 3 ms late → `residual_ms == approx(+3.0, abs=0.3)`. That is the sign of `TickEdgeDetector.timing_error_ms`, which `corroborate` subtracts (`:1430-1436`).
  - `test_the_acquisition_minute_is_not_measured`: same minute as acquisition → `{}`.
  - `test_a_plane_one_second_off_is_rejected_by_the_marker`: `utc_ref` +1 s, marker present: after two consecutive minutes `residual_ms` magnitude ≥ 1000 and `verify(...)` returns `"rejected"`.
  - `test_weak_minutes_fold_together`: at an SNR where one minute folds below 10 dB, the third call still returns the station (rolling fold of up to three minutes).
- [ ] **Step 2: Run and see them fail:** `AttributeError: 'RegistrationAcquirer' object has no attribute 'measure_minute'`.
- [ ] **Step 3: Implement.** Fold this minute's band envelopes on the held plane (`label = registration.sample0_utc_for(start_rtp)`), add the profile to a per-band history of the last three minutes (cleared on `reset` and `adopt`), find peaks, and take each eligible station's peak position minus its expected delay, wrapped to ±0.5 s. `arrival_rtp` = `start_rtp` + the sample offset of that peak in the first full second. The marker check runs on the same plane; a whole second confirmed on two consecutive minutes becomes a residual of ±1000 ms for that station, so `verify` rejects and `corroborate` resets. Expose, never correct: log the marker disagreement once.
- [ ] **Step 4: Run and see them pass.** `5 passed`.
- [ ] **Step 5: Commit.** `acquirer: measure the held plane against its own fold and marker` plus trailers.

### Task 10: Metrology runs T3 stand-alone on every station

**Files:**
- Modify: `src/hf_timestd/core/metrology_service.py` (`_apply_registration_unsafe` `:728-968`; delete the T6 verify and reset `:850-899`; `feed_back_ensembles` `:1041-1097`; `__init__` `:246-260`)
- Test: `tests/unit/test_t3_standalone_shadow.py`; in `tests/unit/test_metrology_service_registration.py` replace the tests that verify through `feed_back_ensembles` or through T6 (`:76-210`, `:302-325`, `:346-390`, `:651-668`, `:863-960`) with the same claims through `measure_minute`.

**Interfaces:**
- Consumes: Task 4 metadata `raw_gps_time_ns`, `raw_rtp_timesnap`; Task 9 `measure_minute`, `residuals_for_verify`; Task 6 `read_siblings` accepting WITNESS.
- Produces: summary key `standalone_reason: Optional[str]` (`"raw_pair_unavailable"` or None); `MetrologyService._last_arrivals: Dict[str, MinuteArrival]` for Task 13.

- [ ] **Step 1: Write the failing tests.** The test file defines its own helpers: `run_minutes(tmp_path, raw_offset, t6_label_offset, n, marker=True)` builds a `_service` as `tests/unit/test_metrology_service_registration.py:34-45` does, feeds `n` minutes and keeps the last BufferTiming as `svc.last_bt`; `truth(k)` and `rtp_of_minute(k)` give minute `k`'s true sample-0 UTC and start RTP. The harness drives `apply_registration` for six minutes. Ticks sit at truth. `metadata` carries a raw pair built with `unix_ns_to_gps_time_ns` (`buffer_timing.py:281`) at truth + `raw_offset`. The ring label (T6's plane, `judge_tier="T6"`) sits at truth + 40 ms, a plane T3 must never consult.

```python
@pytest.mark.parametrize("raw_offset", [0.007, -0.013, 1.000])
def test_t3_recovers_truth_from_an_offset_raw_pair(tmp_path, raw_offset):
    svc = run_minutes(tmp_path, raw_offset=raw_offset, t6_label_offset=0.040, n=6)
    s = svc.reg_store.read_summary()
    assert s["state"] == "WITNESS"
    assert svc.acquirer.registration.sample0_utc_for(rtp_of_minute(5)) == pytest.approx(truth(5), abs=0.001)
    # today: CANDIDATE and reset every 3 minutes ("disagrees with T6")

def test_registration_ignores_the_t6_plane(tmp_path):
    a = run_minutes(tmp_path / "a", raw_offset=0.007, t6_label_offset=0.0, n=6)
    b = run_minutes(tmp_path / "b", raw_offset=0.007, t6_label_offset=0.040, n=6)
    assert a.acquirer.registration.utc_ref == b.acquirer.registration.utc_ref
    assert a.reg_store.read_summary()["state"] == b.reg_store.read_summary()["state"]

def test_the_second_comes_from_the_raw_pair_not_the_ring_label(tmp_path):
    svc = run_minutes(tmp_path, raw_offset=0.007, t6_label_offset=1.000, n=4, marker=False)
    assert svc.acquirer.registration.sample0_utc_for(rtp_of_minute(3)) == pytest.approx(truth(3), abs=0.001)

def test_no_raw_pair_means_no_registration(tmp_path):
    s = run_minutes(tmp_path, raw_offset=None, t6_label_offset=0.0, n=2).reg_store.read_summary()
    assert s["state"] == "BOOTSTRAP" and s["standalone_reason"] == "raw_pair_unavailable"

def test_t3_only_station_runs_the_same_code(tmp_path):
    svc = run_minutes(tmp_path, raw_offset=0.007, t6_label_offset=None, n=6)   # judge_tier None
    assert svc.reg_store.read_summary()["state"] == "ACQUIRED" and svc.last_bt.origin_source == "acquired"

def test_t6_residual_is_published_and_never_resets(tmp_path, caplog):
    svc = run_minutes(tmp_path, raw_offset=0.007, t6_label_offset=0.040, n=6)
    assert svc.reg_store.read_summary()["residual_vs_t6_ms"] == pytest.approx(-40.0, abs=1.0)
    assert "disagrees with T6" not in caplog.text
```

- [ ] **Step 2: Run and see them fail.** Expected today: the parametrised test ends in CANDIDATE with "witness plane disagrees with T6" in the log; the ignore-T6 test finds different states; the whole-second test lands 1 s off; the raw-pair test lacks `standalone_reason`.
- [ ] **Step 3: Implement.** Build the acquirer's label with `resolve_buffer_timing({**metadata, "gps_time_ns": raw_gps, "rtp_timesnap": raw_rtp, "timing": None}, sample_rate=...)`. Without a raw pair, publish BOOTSTRAP with `standalone_reason="raw_pair_unavailable"` and log it once per episode. The epoch tracker observes the raw pair. Expected delays come from the raw label's time. After acquisition and fusion, call `measure_minute` on the held plane and route its residuals to `verify` (unverified) or `corroborate` (verified), as `_feed_back_ensembles_unsafe` does today (`:1068-1097`). On a T6 station publish WITNESS for a verified plane and CANDIDATE otherwise; keep `residual_vs_t6_ms` as a diagnostic; never set `verified` from it and never reset on it. Hand the engine T6's plane while T6 is authoritative, as today. Keep the minute's `measure_minute` result as `self._last_arrivals` for Task 13. `feed_back_ensembles` keeps its call site (`:1129`) and returns without touching the plane; its docstring says why.
- [ ] **Step 4: Run and see them pass.** New file `8 passed` (the parametrised test counts three); the rewritten service tests green; baseline paths green.
- [ ] **Step 5: Commit.** `metrology: T3 starts from radiod's raw pair and verifies on its own ticks` plus trailers.

---

## Stage 5 — the durable shadow record

### Task 11: Expected delay with its mode and data source

**Files:**
- Modify: `src/hf_timestd/core/metrology_engine.py` (`expected_delays_s` `:521-551`; `_predict_geometric_delay` `:570-671`)
- Test: `tests/unit/test_expected_delay_details.py`

**Interfaces:**
- Produces: `MetrologyEngine.expected_delay_details(system_time: float, minute_utc: int) -> Dict[str, Dict[str, Any]]`, each value `{"delay_s": float, "sigma_ms": float, "mode": str, "iono_source": str}`; `expected_delays_s` returns `{s: d["delay_s"]}` of it.

- [ ] **Step 1: Write the failing tests.** A stubbed `_predict_geometric_delay` sets `_last_prediction_meta` per station. `test_details_carry_mode_and_source` asserts `{"delay_s": 0.0125, "sigma_ms": 0.5, "mode": "1F", "iono_source": "giro"}` for WWV. `test_delays_are_the_details_delays` asserts equality of the two methods.
- [ ] **Step 2: Run and see them fail:** `AttributeError: ... 'expected_delay_details'`.
- [ ] **Step 3: Implement** by capturing `_last_prediction_meta` right after each `_predict_geometric_delay` call in the loop; the eligibility gate stays as it is.
- [ ] **Step 4: Run and see them pass.** `2 passed`; `tests/test_metrology_engine.py` green.
- [ ] **Step 5: Commit.** `engine: expected delay names its mode and ionospheric source` plus trailers.

### Task 12: `T3ShadowRecord` and `StateTransitionCounter`

**Files:**
- Create: `src/hf_timestd/core/t3_shadow_record.py`, `src/hf_timestd/core/state_transition_counter.py`
- Test: `tests/unit/test_t3_shadow_record.py`, `tests/unit/test_state_transition_counter.py`

**Interfaces:**
- Produces:
  ```python
  class T3ShadowRecord:
      DEFAULT_DIR = Path("/var/lib/timestd/t3_shadow")
      def __init__(self, stream: str, directory: Path = DEFAULT_DIR, retention_days: int = 14): ...
      def append(self, row: dict) -> None      # row["utc_minute"] (int, Unix s) names the day file
  class StateTransitionCounter:
      def __init__(self, scope: str): ...
      def observe(self, state: str, reason: str, utc_minute: int) -> Optional[dict]
      @property
      def count(self) -> int: ...
  ```

- [ ] **Step 1: Write the failing tests.**
  - `test_rows_land_in_the_day_file_of_their_minute`: two rows on 2026-10-30 and one on 10-31 → `minutes-20261030.jsonl` holds 2 lines, `minutes-20261031.jsonl` holds 1.
  - `test_files_past_retention_are_pruned_on_a_new_day`: a 20-day-old file of the same stream vanishes; another stream's file stays.
  - `test_unwritable_directory_logs_once_and_never_raises`: a read-only directory; three appends; exactly one ERROR; no exception; a later good write logs nothing.
  - `test_a_row_stays_small`: a full minute row (Task 15 shape) serialises under 2048 bytes.
  - `test_first_state_is_startup_not_a_transition`: `observe("BOOTSTRAP", ...)` returns a row with `"reason": "startup"`, `count == 0`, no WARNING.
  - `test_each_change_logs_once_and_counts`: BOOTSTRAP → CANDIDATE → CANDIDATE → WITNESS: two WARNINGs matching `T3 STATE SHARED_10000: BOOTSTRAP -> CANDIDATE`, `count == 2`, the repeat returns None.
- [ ] **Step 2: Run and see them fail:** `ModuleNotFoundError` for both modules.
- [ ] **Step 3: Implement.** Append one compact JSON line per call (`separators=(",", ":")`), file `f"{stream}-{YYYYMMDD}.jsonl"` from `utc_minute`, prune this stream's files older than `retention_days` when the day changes, and count one failure streak per ERROR (the pattern of `offset_judge._append_tier_transition`, `:1814-1830`). The counter's row: `{"kind": "transition", "scope", "from", "to", "reason", "utc_minute", "count"}`.
- [ ] **Step 4: Run and see them pass.** `6 passed`.
- [ ] **Step 5: Commit.** `shadow record: small JSONL rows; transitions logged once and counted` plus trailers.

### Task 13: Metrology writes per-broadcast rows and its channel's transitions

**Files:**
- Modify: `src/hf_timestd/core/metrology_service.py` (`__init__` `:246-260`; `_apply_registration_unsafe`; `_publish_registration` `:970-1038`)
- Test: `tests/unit/test_metrology_shadow_rows.py`

**Interfaces:**
- Consumes: Task 9 `MinuteArrival`; Task 10 `_last_arrivals`; Task 11 `expected_delay_details`; Task 12 classes; config `[timing.t3_shadow]` (Task 15 adds it to the template).
- Produces: stream `broadcasts-<channel>` rows `{"kind": "broadcast", "utc_minute", "channel", "frequency_hz", "station", "arrival_rtp", "arrival_input_index", "arrival_ms_in_plane", "fold_snr_db", "residual_ms", "sigma_ms", "expected_delay_ms", "model_sigma_ms", "mode", "iono_source", "t3_state", "rtp_domain"}`; transition rows in the same stream.

- [ ] **Step 1: Write the failing tests** (Task 10 harness; `T3ShadowRecord` pointed at `tmp_path`):
  - `test_each_measured_station_writes_one_row_per_minute`: after minute 4, the file holds one WWV row per measured minute with `expected_delay_ms == 12.5`, `mode == "1F"`, `iono_source == "giro"`, and `arrival_input_index == approx(counter_map.input_index(arrival_rtp))`.
  - `test_channel_state_changes_are_rows_and_warnings`: BOOTSTRAP → CANDIDATE → WITNESS gives two transition rows, two WARNINGs and `count == 2`.
  - `test_shadow_disabled_writes_nothing`: `[timing.t3_shadow] enabled = false` → no file.
- [ ] **Step 2: Run and see them fail:** no file exists.
- [ ] **Step 3: Implement.** Build the record and counter in `__init__` from `config["timing"]["t3_shadow"]` (`enabled` default true, `directory`, `retention_days`). Each minute, write one row per entry of `_last_arrivals`, joined with that minute's `expected_delay_details`. `_publish_registration` passes the channel file's state to the counter and appends any row it returns. `arrival_ms_in_plane` = `residual_ms` + expected delay in ms.
- [ ] **Step 4: Run and see them pass.** `3 passed`.
- [ ] **Step 5: Commit.** `metrology: per-broadcast shadow rows and channel transitions` plus trailers.

### Task 14: The judge publishes T3 against T6 in the sample plane

**Files:**
- Create: `src/hf_timestd/core/t3_shadow_comparator.py`
- Modify: `src/hf_timestd/core/offset_judge.py` (provider setter beside `set_t6_rate_provider` `:1526`; snapshot `:3799-3990`)
- Modify: `src/hf_timestd/core/core_recorder_v2.py` (wire the provider beside the bench wiring `:817-860`)
- Test: `tests/unit/test_t3_shadow_comparator.py`, `tests/unit/test_offset_judge_t3_shadow_block.py`

**Interfaces:**
- Consumes: `NativeAnchor` (`native_anchor.py:43`, `anchor_rtp`, `anchor_utc_ns`); `CoreRecorderV2._t6_counter_map`, `_counter_maps` (Task 5); summary `rtp_domain`, `counter_map` (Task 7); `OffsetJudge.chain_delay_store.record.offset_ns`.
- Produces:
  ```python
  class T3ShadowComparator:
      @staticmethod
      def t3_minus_t6_ms(summary: Optional[dict], t6_anchor: Optional[NativeAnchor],
                         t6_map: Optional[CounterMap]) -> Optional[float]
      @staticmethod
      def block(summary, t6_anchor, t6_map, chain_delay_ms: Optional[float]) -> dict
  OffsetJudge.set_t3_shadow_provider(self, provider: Optional[Callable[[], Optional[dict]]]) -> None
  # offset_judge.json gains "t3_shadow": {"t3_minus_t6_ms", "t6_chain_delay_ms",
  #   "t6_anchor_input_index", "t6_anchor_utc_ns", "t3_rtp_domain", "t3_state", "t3_sigma_ms"} | null
  ```

- [ ] **Step 1: Write the failing tests.**
  - `test_t3_minus_t6_in_the_sample_plane`: T6 anchor at 96 kHz (k0 = 40) at truth; T3 summary at 24 kHz (k0 = 51) at truth + 0.3 ms, both maps on one run → `+0.3` within 1e-6.
  - `test_no_answer_across_runs_or_without_maps`: either map missing, or another run → None.
  - `test_judge_publishes_the_provider_block`: a judge with the provider set → the published snapshot carries `t3_shadow` equal to the provider's dict.
  - `test_a_failing_provider_publishes_null_and_ticks_on`: the provider raises → `t3_shadow` is None and `tick()` completes.
- [ ] **Step 2: Run and see them fail:** `ModuleNotFoundError ... t3_shadow_comparator`; `AttributeError ... set_t3_shadow_provider`.
- [ ] **Step 3: Implement.** The comparator evaluates T6's UTC at the input index of the summary's `rtp_ref`, through both maps, and subtracts it from `utc_ref`. It reads no clock and no host offset. The core recorder's provider reads the summary through its `RegistrationStore`, takes `_t6_native_anchor` and `_t6_counter_map`, and passes the judge's chain-delay record in ms. The judge calls the provider outside its lock, like `t6_rate`.
- [ ] **Step 4: Run and see them pass.** `4 passed`; `tests/test_offset_judge*.py` green.
- [ ] **Step 5: Commit.** `judge: publish T3 against T6 in the sample plane` plus trailers.

### Task 15: The authority manager writes the per-minute row

**Files:**
- Modify: `src/hf_timestd/core/authority_manager.py` (constructor `:280-340`; `tick` `:400-450`, after `_write_snapshot`)
- Modify: `src/hf_timestd/core/authority_runner.py` (build the record beside the snapshot store `:388-407`)
- Modify: `config/timestd-config.toml.template` (new `[timing.t3_shadow]`: `enabled = true`, `directory = "/var/lib/timestd/t3_shadow"`, `retention_days = 14`, with a comment on what the rows hold and that no analysis runs on the station)
- Test: `tests/unit/test_authority_t3_shadow_minute.py`

**Interfaces:**
- Consumes: Task 12 classes; Task 14 `t3_shadow` block; `ProbeResult.offset_ms` for T6, T4, T2 (`authority_manager.py:201-222`); `self._published_registration` (`:1116-1119`).
- Produces: `AuthorityManager(..., t3_shadow_record: Optional[T3ShadowRecord] = None, judge_status_path: Path = Path("/run/hf-timestd/offset_judge.json"))`; stream `minutes` rows `{"kind": "minute", "utc_minute", "t3": {state, sigma_ms, contributing, waiting, n_minutes, verified, whole_second_unresolved, rtp_domain, counter_epoch_id}, "t3_shadow": {...}|null, "t3_vs_adopted": shadow_residuals["T3"]|null, "t6_offset_ms", "t4_offset_ms", "t2_offset_ms", "host_clock_verdict", "t3_transitions_total"}`; station-level transition rows in the same stream.

- [ ] **Step 1: Write the failing tests** (fake probes, `now_fn` stepping through minutes, judge file in `tmp_path`):
  - `test_one_row_per_utc_minute`: three ticks in minute M and one in M+1 → two minute rows, `utc_minute` M and M+1.
  - `test_row_holds_t2_and_t4_side_by_side`: T4 probe `offset_ms=0.02`, T2 probe `offset_ms=6.4` → the row's `t4_offset_ms == 0.02` and `t2_offset_ms == 6.4`.
  - `test_station_state_change_logs_once_and_counts`: registration state CANDIDATE then WITNESS → one WARNING `T3 STATE station: CANDIDATE -> WITNESS`, `t3_transitions_total == 1`.
  - `test_missing_judge_file_still_writes_the_row`: `t3_shadow` and `t3_vs_adopted` are None.
  - `test_runner_builds_the_record_from_config`: `build_authority_runner_from_config` with `[timing.t3_shadow] enabled = false` leaves `manager.t3_shadow_record` None.
- [ ] **Step 2: Run and see them fail:** `TypeError: __init__() got an unexpected keyword argument 't3_shadow_record'`.
- [ ] **Step 3: Implement.** `_write_t3_shadow_minute(state, results)` runs after `_write_snapshot`, writes once per UTC minute of `now_fn()` (the manager's existing time source, a record stamp and not a timing input), reads the judge file once, and never raises. A `StateTransitionCounter(scope="station")` watches the published registration state every tick, so a change between minute rows still counts.
- [ ] **Step 4: Run and see them pass.** `5 passed`; then all baseline paths plus every new test file: `1723` plus the new count, no failures.
- [ ] **Step 5: Commit.** `authority: per-minute T3 shadow row with T6, T4 and T2 beside it` plus trailers.

- [ ] **Operator checklist (not implementer work).** After deploy to B4, confirm `[timing.authority_manager.t2] enabled = true` there, so `t2_offset_ms` is not null; then check after one hour that `/var/lib/timestd/t3_shadow/minutes-<day>.jsonl` holds about 60 rows and each `broadcasts-<channel>-<day>.jsonl` grows by one row per measured station per minute.

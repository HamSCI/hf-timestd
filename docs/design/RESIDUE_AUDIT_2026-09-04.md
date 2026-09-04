# Residue audit: what still serves a superseded model

**Status:** Audit 2026-09-04; §7 steps 1–2 executed the same day (`9346bc5`,
and the commit that follows it). Nothing deployed, no service touched. The
config-dead and labelling sessions still work from this document.
**Reference:** `MEASUREMENT_MODEL.md` (054dc73). Where this document says
measurand, registration, or ruler, it means what that document means.

---

## 0 · Why an audit came before any deletion

The bus message of 2026-09-04 12:11Z sampled four leads and called them
"not an audit, not a scope". This document supplies the scope. It measured the
whole tree once, classified every finding by the strength of its proof, and
counted before anything moved.

The scope line stays where the previous session left it:

    IN    delete what the model proves dead; make what survives state which
          measurand it serves.  No behaviour change.
    OUT   anything that changes what the station believes -- the archive-writer
          flip, the judge's sigma source, the bench rewiring.  All gated on
          Phase 2 measurements nobody has taken.

And the discipline that bounds every entry below: **prove each thing dead;
never infer death from appearance.** Three times this repository kept a thing
that looked dead and was load-bearing. So each finding here names its proof,
and the proof names what the deletion session must re-run before it deletes.

---

## 1 · Method

Two tests ran over the whole of `src/hf_timestd`, 188 modules, 93,443 lines.

**The reachability test.** An AST walk over every module collected every
`import` and `from ... import` node, including those inside functions, and
resolved the `_LAZY` maps in the two package `__init__` files. From the real
entry points — the six services' `python -m` targets, the `hf-timestd` CLI,
`scripts/live_vtec.py`, `scripts/monitor_radiod_health.py`, and the web API's
`main.py` — it computed what a running station can ever import. A module
outside that set was then checked three more ways before it counted as dead:
tests, scripts and `web-api/` that import it; `python -m` invocations in every
shell script, unit file and TOML across the repo; and a bare-name sweep over
every non-Python file in this repo, `sigmond`, and `ops`, for a dynamic load by
string. `sigmond_tui.py` survived that last sweep — `deploy.toml` names it as a
parser file — which shows why the sweep earns its place.

**The measurand test.** For each module the reachability test left alive on a
timing path, the question was the one the model supplies: which measurand does
this code serve? `t(n)`, the sample epoch at the reference plane; `D_clock`,
the host clock against UTC; the ruler, `f_s`; or a diagnostic that serves
neither. `D_clock` inside a labelling or registration path counts as residue
by construction. `D_clock` inside a chrony feed or a bootstrap step counts as
a derived product and stays, because §7.1 of the model places it there.

**Configuration.** Where a live import runs only under a configuration key, the
audit read the shipped template and the live configuration of both fleet
stations, AC0G-B4 and AC0G-ND, read-only.

The script behind the reachability test sits at
`scripts/import_reachability.py` so the deletion session re-runs it rather
than trusting this page.

---

## 2 · The counts

| class | modules | lines | proof strength |
|---|---|---|---|
| unreachable from every root, no consumer of any kind | 25 | 10,904 | import graph + string sweep |
| unreachable from every root, pinned only by tests or tooling | 33 | 10,561 | import graph; deletion costs tests (includes `timing_validation_service`, web-pinned) |
| reachable, but the output goes nowhere | 2 | 2,612 | data flow read in full |
| reachable, dead under every shipped and fleet configuration | 6 paths + the `authority` key | ~1,900 | config read on template, B4, ND |
| live, serves the superseded measurand, gated on measurement | 8 sites | — | not for this programme |
| live, correct, mislabelled — a comment or a name away | 5 groups | — | label only |

The first two rows together hold 58 modules and 21,465 lines, 23 % of the
tree. Not all of that concerns timing; §3 separates the timing residue from the
merely abandoned.

---

## 3 · Provably dead

### 3.1 Dead by reachability, and about timing

Eleven modules carry the `D_clock`-era architecture — the "Set, Monitor,
Intervention" family, the consensus estimator of system-clock offset, the
validators that compared fusion against radiod's `GPS_TIME` as ground truth.
No root reaches them. No test, script or web page imports them. No file of any
kind names them.

| module | lines | what it served |
|---|---|---|
| `core/clock_convergence.py` | 1,022 | convergence-to-lock model for `D_clock` |
| `core/ground_truth_validator.py` | 890 | `D_clock` against radiod as truth |
| `core/timing_metrics_writer.py` | 782 | time-snap and NTP comparison for a web UI |
| `core/operational_phase_manager.py` | 688 | system-wide phase, keyed on `D_clock` lock |
| `core/sliding_window_monitor.py` | 648 | 10 s window beside the 60 s `D_clock` buffer |
| `core/primary_time_standard.py` | 611 | "verify UTC(NIST) directly" |
| `core/quality_metrics.py` | 551 | 2024-era quality tracking |
| `core/gpsdo_monitor.py` | 522 | anchor watchdog; not the GPSDO probe |
| `core/consensus_combiner.py` | 437 | weighted consensus of `D_clock` |
| `core/global_timing_coordinator.py` | 331 | one verified UTC(NIST) back-calculation |
| `core/station_identifier.py` | 475 | phase-keyed station identification; sole importer of the phase manager |

6,957 lines. Four of them (`sliding_window_monitor`, `consensus_combiner`,
`global_timing_coordinator`, `station_identifier`) have a unit test each,
126 tests in all; those die with them.

Two corrections from the deletion session. `timing_validation_service` (541)
and its helper `timing_validation` (225) first stood in this table and do
not belong here: B4's `timestd-web-api` runs enabled and active, and
`web-api/routers/timing_validation.py:91` imports the service lazily. They
move to the pinned class of §2 and to the web API's own future. And
`station_identifier` joins the set, because it hard-imports
`operational_phase_manager` and nothing reaches it either.

**Executed 2026-09-04.** Suite 2,680 passed, 0 failed.

**Proof re-run before deletion:** `scripts/import_reachability.py` with the
same roots, then `grep -rlw <name>` across `hf-timestd`, `sigmond` and `ops`
excluding `archive/`. Both returned nothing on 2026-09-04.

### 3.2 Dead by reachability, not about timing

The remaining 46 unreachable modules fall outside the measurand question. They
divide into the abandoned (`audio_stream`, `audio_streamer`, `stream/*` — 1,110
lines of an audio streaming API; `recording_session` and `packet_resequencer`,
1,020 lines that ka9q-python's `RadiodStream` replaced; `core/legacy/
wwvh_discrimination_archive.py`, 3,940 lines kept as a named archive inside
`src/`), the seven-line relocation shims that point at `hamsci-physics` and
`hamsci-dsp` (`tec_estimator`, `vtec_mapper`, `carrier_tec`, `ionex_parser`,
`iono_tomography`, `propagation_*`, `io/*`), and tooling that scripts or the
web API still import (`stability_analysis`, `replay/*`, `standard_signal_generator`).

The shims stay; they are deliberate. The abandoned code belongs to a general
dead-code pass, not to this programme, and this document lists it only so the
next reader does not re-measure it. The deletion session should take the
timing twelve and leave the rest for a separate decision.

### 3.3 Dead by data flow

Two modules sit on live import paths and produce nothing anyone reads.

**`core/timing_calibrator.py`, 2,039 lines.** The recorder constructs a
`TimingCalibrator` at `core_recorder_v2.py:1042` and calls exactly one method
on it, `register_channel_ssrc`, at lines 1086 and 1624. That method records an
SSRC and, on a branch that only fires when a calibration already exists in the
state file, saves the state file. The state file,
`state/timing_calibration.json`, has no reader anywhere in `src/`, `scripts/`
or `web-api/`; the analytics service that read it was archived on 2026-01-22.
The other 2,000 lines implement the bootstrap-then-calibrate `D_clock` model
the module's docstring describes. Measurand: `D_clock`, wearing the name
"RTP-to-UTC calibration".

**`core/bootstrap_validator.py`, 573 lines, plus its call site.**
`multi_broadcast_fusion.py:655` constructs a `BootstrapValidator` and
`_process_bootstrap` at 2379 feeds it every measurement's `d_clock_ms`. Its
product, `offset_correction`, lands in `self._bootstrap_offset_correction` at
line 2394. That attribute has two references in the file: the assignment and
its initialiser at 660. Nothing reads it. The module's other effect is a log
line every tenth call. Measurand: `D_clock`, described as "validates the
RTP-to-UTC offset using cross-station agreement" — the two measurands in one
sentence.

**Executed 2026-09-04, `9346bc5`.** Proof re-run first: two call sites of
`self.calibrator`, two references to `_bootstrap_offset_correction`. Suite
2,806 passed, 0 failed; the two tests removed asserted the deleted code's own
consistency.

### 3.4 Dead by configuration

These run under keys that no shipped template, and neither fleet station, sets.
Deleting them changes no station's behaviour today; it does retire a key, and
`hf-timestd validate` should say so when it meets one.

**The FUSION authority mode.** `[timing] authority` reads `"rtp"` in the
template, on B4 and on ND. Under `"fusion"` the metrology engine builds a
`FusionTimingState` and a `BootstrapStateWriter` (`metrology_engine.py:342`),
and the fusion service starts a `BootstrapStateWatcher`
(`multi_broadcast_fusion.py:4737`). That path made `D_clock` the thing the
station establishes before it may search narrowly for ticks — the model's
§0.1 contradiction as a mode switch. Modules: `fusion_timing_state.py` (339),
`bootstrap_state.py` (288), and the `not self.is_rtp_authority` branches in
`metrology_engine.py` (lines 342–346, 1504, 1914) and `multi_broadcast_fusion.py`
(4715–4740). No test pins any of it.

**The legacy per-sample-Δφ calibrator.** `use_matched_filter` reads `true` in
the template and on B4. Under `false`, `core_recorder_v2.py:912` builds the
original `BpskPpsCalibrator` from `bpsk_pps_calibrator.py` (497 lines; the
matched-filter module still imports its `PpsCalibrationResult` dataclass, which
would move). That calibrator has no fold buffer, so the fine stage and the
anchor authority never run on it: a station on this path fits a chain delay
against T4 and registers the ruler by the host clock.

**The diff-detector sidecar and its persisted store.** `enable_diff_sidecar`
appears in no template and no fleet config. Under it,
`bpsk_pps_calibrator_diff.py` (453) runs beside the main calibrator, and
`bpsk_chain_delay_store.py` (292) persists its disambiguated chain delay across
restarts. `T6_ANCHOR_INVERSION_DESIGN.md` §6 records the store's deletion as
approved "on the T6 path"; the recorder honours that for the matched-filter
side (`_t6_mf_chain_delay_store = None` at line 630) and keeps the diff side
(`ChainDelayStore("diff")` at 631, load and save at 5583–5671). The import at
629 and the leftover-file unlink at 4898 are the residue the bus message
sampled. Two tests pin the store; one pins the diff calibrator.

**Proof to re-run:** the three keys against the template and both live
configs, which this audit read on 2026-09-04. The deletion must also retire
`use_matched_filter`, `enable_diff_sidecar`, `diff_sidecar_path`,
`diff_sidecar_threshold_factor` and `diff_to_shm_unit` from `validate`'s
accepted set, with a warning rather than silence when it finds one.

### 3.5 The `[timing] authority` key itself

Michael asked, mid-audit, whether `authority = "rtp"` counts as legacy in its
own right. It does, and the point deserves its own entry rather than a line
under §3.4.

`TimingAuthority` (`interfaces/data_models.py:26`) offers three values and
states what each means: `rtp` trusts radiod's `GPS_TIME` / `RTP_TIMESNAP` pair
as the RTP-to-UTC mapping, `fusion` lets HF signals establish timing, `auto`
was never built — one comment in `authority_runner.py` mentions it and no code
reads it. The enum's docstring keys the choice to the old L-levels: GPS+PPS
means `rtp`, NTP-or-worse means `fusion`. That whole framing predates the
tiers, the judge and T6.

Under the model the choice dissolves. The pair belongs in the record as
engineering provenance (§7.2), and the registration comes from whichever
estimator ranks best on that station, TS-1 or no TS-1. Neither value of the
key describes that. `rtp` names a framing the model retired — and it stays the
only value any station runs, the template's default, and the mode inside which
the T6 path operates today, by adding the judge's offset to the pair. The
value is load-bearing while its meaning is legacy.

Two consequences fall out of reading the key's neighbourhood.

`rtp_expected_accuracy_ms = 0.001` asserts one microsecond for the pair. The
same pair measured 2.31 ms median and 47.70 ms maximum on B4 (model §8). That
is the silent assumption §9.2 forbids, in the shipped template. The only method
that reads it, `TimingConfig`'s search-window helper at line 92, has no caller
anywhere in `src/`. So the key is dead by data flow, and its deletion removes
an untrue statement rather than a behaviour.

All 24 `is_rtp_authority` branch points across `metrology_service`,
`metrology_engine` and `multi_broadcast_fusion` evaluate to one constant on
every configuration that exists. Collapsing them is the same step as removing
FUSION mode in §3.4, and costs no behaviour.

**Disposition.** The `fusion` and `auto` values, `rtp_expected_accuracy_ms`,
the enum and the 24 branches can go in the §3.4 step. What cannot go yet is
what `rtp` *does* at the label — the pair inside the arithmetic — because that
is the archive-writer flip of §4. So the key retires, the behaviour stays, and
the label docstring says `origin: sysclock` in the model's word until the
TimeMap replaces it. `validate` should warn on a config that still carries the
key, and say which value the station now runs without it.

---

## 4 · Live, serving the superseded measurand, gated on measurement

These are the places the model calls wrong and this programme may not touch,
because each one changes what the station believes. They are listed so the
deletion session does not talk itself into them, and so the labelling pass of
§5 knows where to write.

1. **`authority_manager._build_state`, lines 723–806.** The six-quantity field,
   exactly as the model's §0.2 table has it. `wspr-recorder` reads it through
   `hamsci_dsp.timing` (`sync_strategy.py:246`, `timing_service.py:727`).
   Replacing it is the TimeMap of §8.
2. **The label arithmetic.** `buffer_timing.resolve_buffer_timing` (line 194),
   `binary_archive_writer` (271), and through them `metrology_engine` compute
   `gps_utc + Δrtp/f_s + judge_offset`. radiod's pair sits inside the label;
   §7.2 says it belongs in the record and nowhere in the arithmetic. This is
   the archive-writer flip, named OUT.
3. **The T6 sigma the authority publishes.** `bpsk_pps_probe.py:281` takes
   `max(std of the coarse MF chain delay over 60 edges, |local_minus_source_ns|,
   1 µs)`. The first term measures the coarse stage, not the fine edge; the
   second measures a residual against radiod's host-clock pair, a cross-
   measurand quantity; neither is the edge scatter §6.3 asks for. The judge's
   sigma source, named OUT.
4. **`t6_arrival_floor.py`.** Feeds the `NativeAnchorBench` floor through
   `_t6_bench_state` (`core_recorder_v2.py:2648`) and the SHM pair through
   `t6_shm_pair.py:57`. It measures transport latency. §6.3 returns it to
   diagnostics; today it sets what the judge treats as T6's precision.
5. **`label_plane.py`.** Live at `offset_judge.py:1522`, where its estimate
   becomes the expected difference between a sample-epoch bench and a
   host-clock bench. It exists because the judge compares across the two
   measurands. Under the model the comparison itself goes; until then the
   tracker stays. The bench rewiring, named OUT.
6. **The 250 ms `T6_PHYSICAL_CHAIN_DELAY_MAX_NS` guard.** §6 of the inversion
   design approved its deletion alongside the store. It still runs at four
   sites — `_t6_on_samples` 4977, `_t6_disambiguate_via_t5_lb1421` 4062,
   `_t6_disambiguate_via_external_reference` 4299, all on the matched-filter
   path, and the diff path at 4201. Removing it widens what the coarse cascade
   will accept. That changes acceptance, so it waits; the design document and
   the code disagree, and one of them should say so.
7. **The T3 branch of `_build_state`.** `d_clock_fused_ms` published as a
   correction to the sample epoch. The fusion service's chrony feed
   (`multi_broadcast_fusion.py:5096`) uses the same number correctly as §7.1's
   derived `D_clock`. One number, two roles; only the second survives the model.
8. **The trust-tier zero.** `offset_ns = 0` at `authority_manager.py:779` for
   an ordinary T5/T4/T2 station — the assertion the model's §0.2 names.

---

## 5 · Live and correct, mislabelled

No behaviour changes here; each is a docstring, a comment, or a name.

**The convention split.** `labeling_convention = "legacy" | "content"` threads
through `core_recorder_v2.py` (32 mentions of *convention*, 53 of *legacy*),
`t6_anchor_ledger.py`, `t6_naming_continuity.py`, `offset_judge.py`,
`wwvb_fusion.py`. The model's §8 makes it two plane fields,
`measurand_plane` and `calibration_plane`, rather than a mode. A rename of this
width belongs to the TimeMap work; for now each module that branches on it
should say, in one sentence, that the branch selects the reference plane of
§1 and nothing else.

**`D_clock` where §7.1 puts it.** `chrony_shm`, `chrony_stats`,
`chrony_refclock_gate`, `chrony_stepper`, `chrony_tracking_probe`,
`coarse_time_source`, `coarse_time_writer`, `bootstrap_coordinator`, and the
SHM push in `multi_broadcast_fusion` all handle the host clock, and all of
them may. Their docstrings should name the quantity as derived, so a later
reader does not mistake the chrony feed for the measurand again.

**`buffer_timing` and the archive writer.** Until the flip, the label they
compute descends from radiod's pair. The docstring should say `origin:
sysclock` in the model's vocabulary, so the provenance sidecar, when it
arrives, has the honest word ready.

**The deprecated keys.** `_t6_pps_edge_phase_keys` still emits `chain_delay_ns`
beside `edge_phase_in_named_second_ns` (`core_recorder_v2.py:252`), and
`_t6_authority_status` emits `asserted_chain_delay_ns` beside
`applied_delay_budget_ns` (3929). `TIMING_PROVENANCE_MODEL.md` §4.5 retires
both "one release after 2026-09-01". No tag has been cut since v7.0.0 on
2026-04-11; stations move by fast-forward on `main`. So the clause needs a
decision about what a release means here before the keys go. The deletion
itself changes nothing outside this repo — `tests/test_chain_delay_ns_rename.py`
pins both keys and goes with them. The same name inside `NativeAnchor`,
the ledger and the store denotes a different quantity, the anchor's delay
term, and stays.

**The `TimingCalibrator` remnant.** If the deletion session keeps
`register_channel_ssrc` for its SSRC-change detection, the surviving forty
lines should move to a module named for what they do, and the state file
should either gain a reader or stop being written.

---

## 6 · Documents that still state the superseded form

Five non-archive documents still carry `D_clock` as the measurand or the
uniform-application rule in their own voice: `docs/ARCHITECTURE.md`,
`docs/METROLOGY.md` (§2.1 and §4.5, which `MEASUREMENT_MODEL.md` §10 already
marks superseded but the text does not yet say so in place),
`docs/TIMING-PIPELINE-WIRING.md`, and `docs/design/METROLOGY_PHYSICS_SPLIT.md`.
Twenty-one non-archive documents mention `D_clock` at all. The docs pass that
follows the deletion should mark the superseded sections in place, the way
`TIMING_AUTHORITY_ARCHITECTURE.md` was marked on 2026-08-25, rather than
rewrite them.

`archive/` holds 147 Python files in fifteen subdirectories. Nothing live
imports from it, and the graph now excludes it. It needs no code change; it
needs to stay out of searches, which `.graphifyignore` already does.

---

## 7 · The order for the deletion session

Strongest proof first, so an early mistake costs least.

1. ✅ §3.3, dead by data flow — done 2026-09-04, `9346bc5`.
2. ✅ §3.1, the timing eleven — done 2026-09-04, the commit after it.
3. §3.4 and §3.5, dead by configuration — retire the keys including
   `[timing] authority` and `rtp_expected_accuracy_ms`, teach `validate` to
   warn, delete the paths. This one touches `core_recorder_v2.py` in several places
   and deserves its own commit per key.
4. §5, the labels — one commit, docstrings and comments only.
5. §6, the documents — mark in place.

Leave §4 alone. Every entry there waits on a measurement, and the model's own
§9.4 forbids swapping one asserted number for another to make a symptom go
away.

The suite stood at 2,397 passing on 2026-09-03 (the last full run on record; this audit ran no tests). Each step above should leave
that count lower only by the tests it deliberately removed, and should say by
how many.

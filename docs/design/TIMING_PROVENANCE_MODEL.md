# Timing Provenance Metadata Model — Design

**Date:** 2026-08-29
**Status:** Approved design, pre-implementation
**Approvers:** Michael (mjh)
**Scope:** GRAPE (hf-timestd) and magnetometer (mag-recorder) products; schema
lands in `hamsci_dsp.timing` as v2.
**Builds on:** `docs/METROLOGY.md` §13.2 (the fusion error budget),
`hamsci_dsp.timing` schema v1 (`AuthoritySnapshot`), and
`mag-recorder/src/mag_recorder/core/timing_sidecar.py` (the write policy, to be
reused rather than reinvented).
**Prerequisites:** hf-timestd `038faf9` and hamsci-dsp `4e88b71` — the receiver
operating point (`rf_gain`, `if_power`, `t6_baseband_power`, `t6_n0`) and the
acquisition provenance (`t6_fine_search_mode`, `t6_fine_coarse_unverified`) are
recorded per authority tick. Without those, the Type A term in §4 cannot be
computed and this model would be a schema around asserted constants.

## 0. One fixed invariant, one continuing programme

These are not two goals with a tie-break. They are different KINDS of thing, and
the difference is structural.

### FIXED — honesty, clarity, and recognisable description

Every product describes its timing in terms metrology and physics both
recognise: a stated measurand, corrections separated from uncertainties,
coverage stated, each term typed A or B with its evaluation method, and no claim
the evidence does not support. **This is settled and not renegotiable.** It does
not improve, it does not get traded against accuracy, and it does not relax when
a deadline or a result would be more convenient without it.

Three consequences that shape the design:

1. **The schema must be able to describe every instrument state honestly,
   including bad ones and ones we have not imagined.** A schema that only
   expresses the instrument working well is not honest; it just fails silently
   when it matters most. Hence `origin: null`, unqualified chains, and absence
   recorded as absence (§7) are first-class, not error paths.
2. **No future improvement may weaken the description.** A term may not be
   dropped from the budget because it became small, a correction may not be
   applied because it makes a symptom disappear, and a chain may not be
   relabelled to look better. If a change to the instrument would make its
   description less honest, the change is rejected however much accuracy it
   promises. This is the rule that forbids pasting 16.618 ms into the config to
   clear the falseticker.
3. **The invariant must be mechanically enforced, not merely intended** — hence
   the overclaim gate in §8. An invariant nobody can check is an aspiration.

### CONTINUING — the best instrument the hardware and methods allow

Arranging the hardware and using the best available methods to extract the most
information from it. This has no completion state; it is the work, ongoing.

The measurable form of progress is the **Type B → Type A burn-down**: every term
that moves from asserted to measured is a real improvement in what the instrument
can be said to know, and the budget in §4.2 is the ledger of how far that has
got. Today two terms are Type B and the model says so.

### Why the pairing works

The fixed half is what makes the continuing half safe. Because the description
cannot be weakened, the instrument can be changed freely — aggressively, even —
without any risk of quietly overstating what came out of it. Data taken today
under a Type B group delay stays comparable with data taken after that term
becomes Type A, because both carry an honest account of what was known at the
time. Improvement never invalidates the archive; it only narrows the error bars
on the parts recorded after it.

## 1. Problem

Three things are wrong today, and they compound.

**The science does not use the timing work.** `stream_recorder_v2.py:1149` — the
archive writer "uses GPS_TIME/RTP_TIMESNAP as its authoritative source once
locked", and radiod's `GPS_TIME` **is the host clock**. The T6 anchor inversion
shipped into the timing subsystem (authority, SHM pair, offset judge); nothing
outside it consumes `NativeAnchor`. So GRAPE timestamps derive from a
chrony-disciplined host clock, and the only route by which T6 could improve them
is via chrony — which is the circular one: T6 → chrony → host clock → radiod
`GPS_TIME` → T6's own reference frame.

**The tier label does not describe the instrument.** On 2026-08-29, for 7h01m,
`t6_available` read `0` with reason `"not locked"` while the anchor authority was
continuously AUTHORITATIVE and anchoring. Same station, same instant, two
subsystems, two incompatible answers. At DASI2 the tiers are structurally
unavailable — they are defined by hardware that station does not have.

**A known systematic is neither corrected nor reported.** The radiod filter
group delay is configured as `filter_group_delay_ns = 16618000` and armed as
`filter_group_delay=0 ns`. chrony sees HPPS at **+17 ms** and marks it `#x`
falseticker. Metrologically this is the worst of both worlds: a correction that
is not applied, and an uncertainty that does not report it.

## 2. The model: a chain, not a tier

A **chain** is an ordered list of links from a realisation of UTC to the
timestamp written on a sample. Each link names what it is, what mechanism
realises it, and what it contributes to the uncertainty. A station describes its
own chain. No field presumes a GPSDO, a Stratum-1 LAN server, or a BPSK pilot.

```
payload-anchored (GRAPE / hf-timestd)
  UTC(USNO via GPS) → GPSDO → TS-1 modulator → RF path → RX888 ADC
                    → radiod channel filter → edge detection → RTP↔UTC anchor

host-clock (mag-recorder)
  UTC(NIST) → NTP/chrony reference → system clock → sample restamp
```

⚡ **The chain is the portable structure; the uncertainty is the portable
quantity; the tier is local shorthand.** A station with no GPSDO publishes a
shorter chain with a wider uncertainty, and its data stays comparable with ours
because both are stated in the same terms rather than ranked on a ladder neither
built. The tier label MAY be carried as an optional local annotation and is
explicitly **non-normative**.

⚠ Consequence to accept deliberately: the published chain will sometimes look
less impressive than the tier implied. A magnetometer's host-clock chain is short
and honest where "T6 station" sounded uniformly excellent.

## 3. The record

### 3.0 ⚡ This EXTENDS an existing mechanism — it does not replace one

An earlier draft of this section specified a fresh JSONL sidecar as the only
carrier. That was written without knowing GRAPE already has a per-chunk timing
sidecar, and building a parallel one would have duplicated it.

`binary_archive_writer` already writes, per chunk, a `timing` block described in
its own source as "fully self-describing (raw mapping + applied correction)":

```python
{'radiod_gps_time_ns': ..., 'radiod_rtp_timesnap': ...,   # the raw host-clock mapping
 'offset_ns': ..., 'offset_sigma_ns': ..., 'judge_tier': ...,
 'judge_age_s': ..., 'segment_id': ..., 'rate_ppm': ...}
```

`hamsci_physics.grape.decimation_pipeline.timing_from_sidecar()` consumes it and
already honours two of this model's principles: a verdict without an uncertainty
is treated as no verdict, and the `"X"` sentinel keeps saying "no verdict" rather
than defaulting to a good-looking one. That is absence-as-absence, already
shipped.

So the mapping is:

- **`state` block → the existing per-chunk `timing` block, extended.** No new
  per-chunk file.
- **`chain` block → a new JSONL sidecar**, because the chain description and its
  budget are cross-chunk and change rarely. This is where mag-recorder's
  change-plus-heartbeat policy applies.

Two existing constructs are retained but demoted, not deleted:
`judge_tier` becomes the optional non-normative local shorthand of §2, and
`decimation_pipeline`'s A/B/C/D grade ladder (bounds 2 / 4 / 8 ms) is left
untouched for the fusion chain while being explicitly noted as having **no
resolution for a payload-anchored chain** — every µs-class chunk grades "A", so
the ladder stops discriminating exactly where the instrument gets good. Choosing
its replacement is out of scope here and belongs with Phase 3.

### 3.0.1 Delivery

The JSONL chain sidecar is bundled into the OBS zip by
`hamsci_physics.grape.packager`, using mag-recorder's proven policy: append when
the STABLE identity changes, plus an unconditional heartbeat to bound staleness.
Two block types.

### 3.1 `state` — every interval

```json
{"t":"2026-08-29T10:42:00Z", "type":"state", "chain":"payload-anchored@1",
 "origin":"native_anchor", "u_epoch_ns":1500000, "k":2, "p":0.95,
 "stability_ns":120, "tau_s":60,
 "how":"seeded", "cross_checked":true,
 "cn0_db_hz":55.1, "rf_gain_db":7.5}
```

- `origin` — `native_anchor` | `sysclock` | `null`. Which chain was actually in
  force. A switch is a visible event, never a silent change of meaning.
- `u_epoch_ns` — combined standard uncertainty of the epoch, **computed** where
  possible (§4), with `k` and `p` always stated. Never a bare sigma.
- `stability_ns` / `tau_s` — see §3.3.
- `how` / `cross_checked` — acquisition provenance: which search mode produced
  the estimate, and whether an independent witness verified it.
- `cn0_db_hz` / `rf_gain_db` — the receiver operating point, which is what makes
  `u_epoch_ns` computable rather than asserted.

### 3.2 `chain` — on change, plus heartbeat

```json
{"type":"chain", "id":"payload-anchored@1",
 "measurand":"UTC instant of digitisation of sample n, at the antenna terminals",
 "reference_plane":"antenna_terminals",
 "traceability":{"claim":"UTC(USNO) via GPS", "qualified":true,
   "qualification":"instrumental delay links are not independently calibrated; 2 terms remain Type B"},
 "budget":[
  {"term":"filter_group_delay","correction_ns":16618000,"u_ns":1500000,"type":"B",
   "method":"asserted from config; disagrees with T4-referenced median 15.153 ms by 1.5 ms"},
  {"term":"ts1_modulator_delay","correction_ns":0,"u_ns":10000,"type":"B",
   "method":"vendor guide assertion, WB6CXC TimeSync-1"},
  {"term":"edge_estimation","correction_ns":0,"u_ns":5000,"type":"B",
   "method":"conservative bound; becomes Type A computed from cn0_db_hz once the fine-stage sweep of §4.3 has run"}],
 "u_combined_ns":1500042, "k":2}
```

`u_combined_ns` is the RSS of the `u_ns` terms — the uncertainty **remaining
after** the `correction_ns` values have been applied. It is NOT the size of any
correction. In the example the 16.618 ms correction dominates the corrections
while contributing 1.5 ms of residual uncertainty, and those are different
numbers in different fields on purpose: an uncorrected 16.6 ms bias reported as
a 16.6 ms "uncertainty" would be precisely the error this section prevents.

⚡ **`correction_ns` and `u_ns` are separate fields on every term.** A known
systematic that is applied shows a non-zero correction; one that is known but
unapplied shows the correction it *should* carry and is flagged. That split is
what makes the §1 falseticker legible instead of invisible, and it is the
difference between a GUM budget and a list of numbers.

### 3.3 Two uncertainties, because two audiences need different ones

`u_epoch_ns` is absolute epoch uncertainty. `stability_ns` over `tau_s` is
short-term stability. **On this instrument they differ by roughly five orders of
magnitude**, and a single combined figure would hide the one that matters to
Doppler science: a 16 ms constant epoch offset barely affects a TID measurement,
while 100 ns of jitter inside a file does. The metrologist reads the first; the
physicist usually reads the second; neither is served by an average of them.

## 4. The budgets

### 4.1 ⚡ §13.2 does not apply to the payload-anchored chain

`METROLOGY.md` §13.2 is the **fusion** budget — WWV/WWVH over the ionosphere,
where propagation dominates at 3–15 ms. The TS-1 chain is a local,
GPSDO-locked signal over a short path. **The ionospheric and multipath terms are
simply not in it.** That absence is the entire reason a payload-anchored chain
can be microsecond-class where fusion is millisecond-class. The model publishes
two chains with different dominant terms; it does not publish one budget with a
footnote.

### 4.2 Payload-anchored chain

Independent standard uncertainties combine by RSS. Corrections sum algebraically
and are reported separately.

| Term | Correction | u | Type | Basis |
|---|---|---|---|---|
| GPS → GPSDO epoch | 0 | ~20 ns | B | standard GPSDO discipline |
| GPSDO holdover (coasting only) | 0 | 1.44 µs/h | **A** | measured on B4 |
| TS-1 modulator | 0 | 10 µs | B | vendor guide assertion |
| RF path electrical length | 0 | < 1 µs | B | named for completeness |
| ADC clock | 0 | ppm-level over interval | B | GPSDO-locked |
| **radiod filter group delay** | **16.618 ms** | **1.5 ms** | **B** | asserted; vs T4-referenced 15.153 ms |
| Edge estimation | 0 | from C/N0 | **A** (§4.3) | per record |

The group delay dominates by three orders of magnitude — and dominates as an
*uncorrected* term, which is the indefensible part, not its size.

### 4.3 ⛔ The Type A claim is not yet earned, and the spec must not pretend it is

The σ-vs-C/N0 relation measured on 2026-08-29 — 12 % of σ per dB, obeying
1/√SNR — was measured on the **coarse** matched-filter stage. The stage that
produces the published edge is the **fine** stage, for which we have three points
at a single C/N0 (0.50 / 0.50 / 2.73 µs at 48.5 dB-Hz) and a 22 ns figure from
`BPSK-PPS-DETECTION-METHODS.md` at unstated conditions.

⇒ **`edge_estimation` MUST be published as Type B with a conservative bound until
the fine-stage σ-vs-C/N0 sweep has run.** Reusing the coarse relation for the
fine stage would be exactly the laundering this model exists to prevent. The
sweep uses the same harness as `tests/test_t6_acquisition_cliff.py`, costs hours
of compute and no station risk.

### 4.4 Host-clock chain (mag-recorder)

`UTC(NIST) → chrony reference → system clock → restamp`. chrony reports its own
root dispersion and maximum error, so the top of this chain is genuinely Type A.
The **restamp latency** (sample acquisition → `time.time()`) is the term that
needs measuring rather than assuming.

## 5. What a metrologist gets, and what we do not claim

Provided: a defined measurand and reference plane; corrections separated from
uncertainties; coverage (`k`, `p`) on every record; per-term Type A/B with the
evaluation method stated.

⛔ **Not claimed: full metrological traceability.** The instrumental delay links
have no calibrations of their own. What is claimed is traceability to UTC(USNO)
via GPS for the epoch and frequency references — a standard, accepted route —
plus a documented chain in which two instrumental terms remain Type B
assertions. `traceability.qualified` is `true` and the qualification text says
so. An unqualified claim of "traceable" would be rejected by any metrologist who
read the budget, and rightly.

## 6. Staging, and what licenses the flip

**Phase 1 — publish the correction.** Archive writer untouched. The sidecar
records the chain in force and, per interval, the offset between the host-clock
timestamps actually written and the payload anchor's statement, with its
uncertainty. Zero risk to the data path; applies retroactively to existing data.

**Phase 2 — measure what is currently asserted.**
- Filter group delay by **two independent routes**: computed from radiod's
  published `filter L` / `filter M` as `(N−1)/2` for a linear-phase FIR, and
  measured against T5's GPS PPS over long baselines — newly possible because T6
  now holds lock for hours. Agreement converts assertion into measurement;
  disagreement is itself the finding.
- Fine-stage σ-vs-C/N0 sweep (§4.3).
- Restamp latency for the host-clock chain (§4.4).

**Phase 3 — flip the archive writer to the native anchor.** Licensed by four
falsifiable conditions:
1. computed and measured group delay agree within their combined uncertainty;
2. the anchor-vs-host-clock offset shows no unexplained steps over several days;
3. fine-stage σ characterised, so `u_epoch_ns` is computed, not asserted;
4. T6 lock fraction over 00–06Z high enough that flipping does not cause
   constant chain-switching.

The flip puts a discontinuity in the archive. Handled by **not** retro-applying
anything: the chain id changes, the sidecar marks the switch, and Phase 1's
published correction lets anyone fix older data themselves.

### 6.1 Scope of the first implementation plan

⚠ **Only Phase 1 is a single implementation plan.** Phase 2 is three independent
measurement campaigns and Phase 3 is a change to the archive writer's time source
gated on their results; each gets its own spec-to-plan cycle. A plan that tried to
carry all three would be committing to a flip whose licensing conditions have not
yet been evaluated.

## 7. Error handling

Best-effort throughout, copying mag-recorder verbatim: a sidecar failure must
never take down a recorder, so nothing raises.

⛔ **Absence stays visible as absence.** If no chain can be qualified, emit a
`state` block with `origin: null`, `u_epoch_ns: null` and a reason. Never a
silently omitted record; never a narrower uncertainty than the evidence
supports. If a term is unknown, the chain is unqualified.

## 8. Testing and verification

- Schema round-trip; budget combination arithmetic; the unqualified path; a
  golden sidecar file.
- `u_epoch_ns` computed from a recorded `cn0_db_hz` reproduces the measured σ
  sweep (once §4.3 has run).
- ⚡ **The overclaim gate**: an automated check that the published uncertainty is
  never smaller than the observed scatter against an independent reference
  (T5/T4 residuals over the same interval). If the record ever claims better than
  reality, that fails a gate rather than being noticed in a paper two years on.
  This is what mechanically enforces the fixed invariant of §0 instead of leaving it an aspiration.

## 9. Out of scope

The probe availability gate (`bpsk_pps_probe.py:194`) and the edge-driven HPPS
SHM push are real open defects but belong to the T6 tier's own repair, not to
this model. Whether T6 should feed chrony at all is deliberately left open: once
Phase 3 lands, the science no longer needs it to, and feeding it re-closes the
independence loop described in §1.

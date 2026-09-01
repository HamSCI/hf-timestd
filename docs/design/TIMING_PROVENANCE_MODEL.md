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
   promises. This is the rule that forbade pasting 16.618 ms into the config to
   drive HPPS toward zero — and, as it turned out, the rule that made the
   misdiagnosis in §1 recoverable rather than baked into the instrument.
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

**The instrumental terms now divide into three.** Paul Elliott (WB6CXC), who
designed the TS-1, gives the injector modulator delay as under 200 ns in
standard injector mode. That settles the largest term the earlier draft
carried. Two terms remain unevaluated. The first covers the run from the
antenna terminals to the injection point, together with any preamplifier and
filter the signal passes on the way. The second covers the receiver front end.
Both remain fixed properties of a given installation, so a station can measure
them. None has.

⛔ **The software still applies 10 µs.** `delay_budget_ns` defaults to 10,000
in `core_recorder_v2.py`, and B4 does not override it, so every T6 anchor
carries that correction. Against WB6CXC's figure the station over-corrects by
roughly 9.8 µs. That exceeds the Type A uncertainty of a 30-second carry by a
factor of sixty-five, and it exceeds every other term in this budget combined.
The number entered as a comment in our own configuration template, and this
document then cited it as vendor documentation. Nothing outside the project
ever supported it. Correcting a live instrument's published timestamps calls
for an operator decision, so this document records the defect and proposes the
change rather than making it.

⛔ **A correction to an earlier draft of this document.** It claimed the radiod
filter group delay was "a known systematic neither corrected nor reported",
citing HPPS reading +17 ms in chrony as evidence of a defect. That was wrong.
B4 runs `labeling_convention = "content"`: the label denotes the **antenna
epoch**, so the processing interval Λ is excluded from the measurand *by
definition*, not by omission. HPPS reporting ≈+17 ms is the label-vs-host plane
offset — which IS Λ — and chrony refusing it is the correct outcome, not a
falseticker bug. The open item is the known one-line change that brings HPPS
near zero under content by subtracting the floor-measured transport; it is not a
metrological defect. The earlier draft made the instrument look two orders of
magnitude worse than it is by importing a term its own measurand excludes.

## 2. The model: a chain, not a tier

A **chain** is an ordered list of links from a realisation of UTC to the
timestamp written on a sample. Each link names what it is, what mechanism
realises it, and what it contributes to the uncertainty. A station describes its
own chain. No field presumes a GPSDO, a Stratum-1 LAN server, or a BPSK pilot.

```
payload-anchored (GRAPE / hf-timestd)

  reference  UTC(USNO via GPS) → TS-1 onboard GPS → modulator ──┐
                                                                ├─ injection
  signal     antenna terminals → feed, preamp, filter ──────────┘   point
                                                                       │
             shared coax → RX888 ADC → radiod channel filter →─────────┘
             edge detection → RTP↔UTC anchor

host-clock (mag-recorder)
  UTC(NIST) → NTP/chrony reference → system clock → sample restamp
```

⚡ **The injection point serves as the reference plane, and that choice does
real work.** Downstream of it the measured signal and the injected reference
travel the same coax, so that delay cancels and never enters the measurand.
WB6CXC states the same cancellation independently: the line from the TS-1 to
the receiver does not affect a time-of-arrival measurement, because both
signals ride it together. Upstream the two paths differ. The reference
originates inside the TS-1, while the signal crosses the antenna feed and
whatever preamplifier and filter sit in front of the injector. That segment
survives in the measurand, it varies from station to station, and §3.2 asks
each station to declare it.

A third chain waits on hardware. WB6CXC reports that a TS-1 generating an
over-the-air timestamped signal would carry an effective modulator delay near
5 µs, and that the work under way there leaves injector operation untouched.
The chain model absorbs such a variant as a chain of its own, with its own
budget, rather than as a revision to this one. Recording it now costs nothing
and demonstrates what the structure claims to do.

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
 "measurand_plane":"antenna_terminals",
 "calibration_plane":"ts1_injection_point",
 "traceability":{"claim":"UTC(USNO) via GPS", "qualified":true,
   "qualification":"antenna-to-injector path not declared; receiver front end not characterised"},
 "budget":[
  {"term":"filter_group_delay","correction_ns":16618000,"u_ns":1500000,"type":"B",
   "method":"asserted from config; disagrees with T4-referenced median 15.153 ms by 1.5 ms"},
  {"term":"ts1_modulator_delay","correction_ns":0,"u_ns":200,"type":"B",
   "method":"designer statement, P. Elliott WB6CXC, 2026-08-30; standard injector mode"},
  {"term":"antenna_to_injector","disposition":"not_declared","type":"B",
   "spans":["antenna_terminals","ts1_injection_point"],
   "method":"feed, preamp and filter ahead of the injection point; station-specific"},
  {"term":"injector_to_receiver","disposition":"cancels",
   "spans":["ts1_injection_point","rx888_adc"],
   "method":"identical path for signal and injected reference; cancels by construction"},
  {"term":"gnss_antenna_feed","disposition":"not_declared","type":"B",
   "method":"cable length x velocity factor; a sign-known bias, not an uncertainty"},
  {"term":"anchor_origin_dispersion","correction_ns":0,"u_ns":1900,"type":"A",
   "measured_on":{"build":"pre-folding","date":"2026-08-24"},"disposition":"historical",
   "method":"63 anchors over 4.5 h ACROSS RE-LOCKS; the folded build of 2026-08-29 removed the re-locks"},
  {"term":"edge_estimation","correction_ns":0,"u_ns":5000,"type":"B",
   "method":"conservative bound; becomes Type A computed from cn0_db_hz once the fine-stage sweep of §4.3 has run"}],
 "u_combined_ns":1500008, "k":2}
```

`u_combined_ns` is the RSS of the `u_ns` terms — the uncertainty **remaining
after** the `correction_ns` values have been applied. It is NOT the size of any
correction. In the example the 16.618 ms correction dominates the corrections
while contributing 1.5 ms of residual uncertainty, and those are different
numbers in different fields on purpose: an uncorrected 16.6 ms bias reported as
a 16.6 ms "uncertainty" would be precisely the error this section prevents.

⚡ **A chain carries two planes, not one.** `measurand_plane` names where the
timestamp claims to apply — the antenna terminals. `calibration_plane` names
where the reference enters — the TS-1 injection point. Earlier revisions
carried a single `reference_plane` and thereby conflated them. That conflation
did real damage: the antenna-to-injector run lives precisely in the gap
between the two planes, and with only one field the schema offered nowhere to
put it. The term went undeclared because it stayed invisible, not because
anyone neglected it. **Every term spanning the gap between the two planes MUST
appear in the budget**, declared or explicitly marked undeclared.

⚡ **A term states a value or states a `disposition`. Silence never means
zero.** Three dispositions carry meaning a number cannot. `cancels` records a
term someone considered and found common to both paths, which differs
absolutely from a term nobody listed. `not_declared` records a term the
station owes and has not supplied. `historical` records a term measured on a
configuration the station no longer runs. A reader can distinguish all three
from a genuine zero, and from each other.

⚡ **Every Type A term carries `measured_on`.** Build identifier and date, both
required. A budget assembled from terms measured across a month of changing
software describes no instrument that ever existed, and a term whose
configuration no longer matches the running station publishes as `historical`
rather than as current.

⚡ **A term's name MUST denote its measurand.** This rule looks like
housekeeping and is not — §4.4 shows a field named for a quantity it does not
measure, already written into a cross-repo contract as a correction every
client subtracts.

⚡ **`correction_ns` and `u_ns` are separate fields on every term.** A known
systematic that is applied shows a non-zero correction; one that is known but
unapplied shows the correction it *should* carry and is flagged. That split is
what distinguishes a GUM budget from a list of numbers — and what would have
caught the §1 misdiagnosis on its own, since a term whose `correction_ns` is
excluded by the measurand cannot be silently reported as an uncertainty.

### 3.2.1 Inherited chains — when the payload carries the authority

A station does not have to own the instrument whose timing it publishes.
`[ka9q].status` is an mDNS name, so a host runs its clients against a
radiod anywhere on the network, and the RTP stream it consumes already
carries the timing: the TS-1 pilot was injected ahead of that radiod's
ADC, and everything downstream of the injection point is common-mode.
**Where the payload contains the timing authority, local hardware
detection is ancillary.** It answers what this box could originate, not
what the data it handles is worth.

⚡ **Expect this configuration to be common, for a reason that has
nothing to do with metrology.** Running radiod and the full client suite
on one small machine is a resource fight: contention for cores, thread
placement, IRQ and cache pressure, and a scheduling jitter that shows up
directly in the timing products. Moving radiod to its own computer
retires that whole class of problem at a stroke. A collaborator adding a
second host is therefore doing the obvious thing, and the model has to
describe the result rather than treat it as an exception.

Today it cannot. A client-only station has two bad options and no good
one: publish a chain describing an injector, a feed and a front end it
does not have, which is false; or publish `origin: null` with an
unqualified chain, which discards ns-class provenance it legitimately
inherited. The real chain is

    TS-1 (remote site) → remote radiod → RTP multicast → this host → archive

where every instrumental term belongs to the far end and this host
contributes transport and labelling only.

**A chain expresses this; a tier cannot.** "T6" does not say whose T6, or
which links are the publisher's own. A chain is an ordered list, so the
first links can belong to another host and the record can name where
custody transferred:

```json
{"type":"chain", "id":"payload-anchored@1",
 "inherited_from":{"radiod_id":"AC0G-B4-status.local",
                   "chain_id":"payload-anchored@1",
                   "chain_seen_utc":"2026-09-01T02:14:07Z"},
 "custody_boundary":"rtp_multicast",
 "measurand_plane":"antenna_terminals",
 "calibration_plane":"ts1_injection_point",
 "traceability":{"claim":"UTC(USNO) via GPS", "qualified":true,
   "qualification":"links before rtp_multicast are inherited and NOT independently verifiable here"},
 "budget":[ ...terms this host can itself evaluate... ]}
```

Three rules follow, and the third is the one that keeps the record
honest:

1. **`inherited_from` names the source**, by the same `radiod_id` §18
   already carries for radiod subscribers, so the two records can be
   joined rather than guessed at.
2. **`custody_boundary` names where the chain stops being ours.** Terms
   before it are the remote station's; terms after it are this host's.
   The publisher's budget carries only what it can evaluate.
3. **An inherited chain is qualified, always.** The publisher is
   asserting links it cannot measure, and saying so is not a weakness of
   the record but the whole point of it. A station that inherits a chain
   and reports `qualified: false` is claiming to have verified an
   injector it has never seen.

⚠ Open, and deliberately not resolved here: whether the inheriting host
should COPY the remote budget's terms into its own record or merely
reference them. Copying makes the record self-contained and immediately
stale; referencing keeps it true and makes a reader fetch two documents.
The staleness matters — a remote station that recalibrates its feed
invalidates every copy downstream, silently. Resolving this needs a
second station to exist and be publishing, which is the point at which
the question stops being hypothetical.

### 3.3 Two uncertainties, because two audiences need different ones

`u_epoch_ns` is absolute epoch uncertainty. `stability_ns` over `tau_s` is
short-term stability. **On this instrument they differ by roughly five orders of
magnitude**, and a single combined figure would hide the one that matters to
Doppler science: a 16 ms constant epoch offset barely affects a TID measurement,
while 100 ns of jitter inside a file does. The metrologist reads the first; the
physicist usually reads the second; neither is served by an average of them.

### 3.4 Two namespaces, one declared normative subset

Everything we can profitably act on is captured. But what a metrologist or a
physicist needs is a **declared subset** of that, and the rest is noise to them —
so the record separates the two by namespace rather than by file.

⛔ **One record, two namespaces — not two files.** The moment they are separate
files they drift, and the correlation that makes the engineering data worth
keeping is lost: "this uncertainty was large *because* the front-end gain was at
-7 dB" is only answerable while both live in the same record.

**The membership rule**, which is testable rather than a matter of taste:

> A field belongs in the normative subset if it changes a decision a
> **metrologist or physicist** would make about *this data*. If it only changes
> a decision **we** would make about *the instrument*, it is engineering.

| Normative (the formal model) | Engineering (under `engineering:`) |
|---|---|
| `measurand`, `measurand_plane`, `calibration_plane` | `fine_search_mode`, `cross_checked` |
| `inherited_from`, `custody_boundary` | `radiod_gps_time_ns`, `radiod_rtp_timesnap` |
| `chain`, `origin` | `rf_gain_db`, `cn0_db_hz`, `if_power_dbfs` |
| per-term `correction_ns` / `u_ns` / `type` / `method` / `disposition` / `measured_on` | `fold_blocks_discarded`, `fold_seconds` |
| `u_epoch_ns`, `k`, `p` | `judge_age_s`, `segment_id`, `rate_ppm` |
| `stability_ns`, `tau_s` | `judge_tier` (the demoted shorthand of §2) |
| `traceability.claim` / `.qualified` / `.qualification` | `judge_age_s` |

⚡ Note `cn0_db_hz` is **engineering**, even though the normative `u_epoch_ns` is
computed from it. That is deliberate and worth being explicit about: the
physicist needs the uncertainty, not the mechanism that produced it. The
derivation is ours to defend, not theirs to audit — but it stays in the record so
that it *can* be audited, by us or by a reviewer who asks.

A science consumer reads the top level and ignores `engineering:` wholesale. A
developer reads both. Neither has to negotiate with the other about what belongs
in the file.

### 3.5 The three decisions the record must support

The model earns its place only if each audience can act on it. Stated as the
decision each one is trying to make:

| Audience | Decision | What answers it |
|---|---|---|
| Metrologist | Can we rely on this clock? | corrections applied, uncertainties remaining, coverage, the traceability qualification |
| Physicist | Is this a real ionospheric phenomenon, or the instrument? | `stability_ns` over `tau_s`, and the **chain** — a chain with no ionospheric term cannot manufacture one |
| Us | Did we build the instrument correctly? | the `engineering:` namespace |

That middle row is the one that justifies publishing the chain at all rather than
just a number: the strongest evidence that a Doppler feature is ionospheric is
that the timing chain which recorded it contains no ionospheric term.

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
| GNSS 1 PPS at the injector | 0 | 100 ns | B | WB6CXC, 2026-08-30; Type A/B open |
| GNSS antenna feed | **not declared** | — | B | length × velocity factor; a sign-known bias |
| Injector modulator delay | 0 | ≤ 200 ns | B | P. Elliott WB6CXC (designer), 2026-08-30 |
| **Antenna to injection point** | **not declared** | **not evaluated** | B | feed, preamp, filter; station-specific |
| Injection point to receiver | cancels | cancels | — | common to signal and reference |
| **Receiver front end** | 0 | **not evaluated** (sub-µs) | B | not characterised |
| Fiducial localisation | 0 | 150 ns | A | repeatability, 120 s |
| Sub-aperture interpolation | 0 | 5 ns | A | quantisation |
| Anchor-to-anchor agreement | 0 | 32 ns | A | consecutive anchors, 30 s |
| Anchor origin dispersion | 0 | 1.9 µs ⚠ | A | 63 anchors over 4.5 h, **2026-08-24, pre-folding** |
| Rate reference, carried 1 h | 0 | 1.44 µs | A | −0.0004 ± 0.0004 ppm |

Type A combines to **0.15 µs** over a 30 s carry and **2.4 µs** over 4.5 h.
Once the modulator settles at 200 ns or below, the Type A terms dominate
everything this budget has actually evaluated. The uncertainty that remains
lives in our estimator and in how long we carry the rate, not in the injector.
Two analogue terms stay unevaluated, and both describe plumbing rather than
physics: a station closes them by measuring its own cable runs and front end.
That leaves the largest single error in the payload-anchored chain sitting in
software — the 10 µs `delay_budget_ns` default described in §2, which no
measurement supports.

⚠ **The budget mixes instrument epochs, and it must stop doing that.** The
1.9 µs origin dispersion dates to 2026-08-24. It measured scatter *across
re-locks*, on a build whose coarse stage chased a detection threshold and
re-locked repeatedly. The folded self-acquisition of 2026-08-29 removed the
re-locks: T6 afterwards held 7 h 01 m with zero transitions, then a further
6 h 52 m overnight. A term that counts variation between re-locks cannot mean
the same thing on an instrument that no longer re-locks, so the number now
serves as a historical bound rather than a current uncertainty. Carrying it
forward would overstate the instrument, which errs toward honesty but still
errs.

The deeper fault lies in the table's shape, not in one row. **Every Type A
term needs the configuration it was measured on**, and none of them carries
it. A budget assembled from terms measured across a month of changing
software describes no instrument that ever existed. §3.2 therefore gains a
`measured_on` field beside every Type A term — build identifier and date —
and a term whose configuration no longer matches the running station gets
published as historical, never as current. Re-measuring the dispersion on the
folded build remains open work.

⚡ **The 100 ns GNSS term needs one clarification before it can settle.**
WB6CXC describes it as PPS variability. If that variability means
pulse-to-pulse jitter, it averages down as 1/√N, and our fine stage already
exploits exactly that. If instead it bounds a bias that wanders slowly, it
averages down not at all and it sets the floor. The distinction changes the
term's classification and its weight, so the budget carries it as open rather
than guessing. One observation makes the question worth asking: our measured
T6 floor lands near 0.11 µs at τ ≈ 280 s, which sits almost exactly on the
stated 100 ns. Either the two agree by coincidence, or the GNSS pulse itself
sets our floor.

⛔ **`chain_delay_ns` names three different quantities, and one of them is a
cross-repo contract.** See §4.5.

⚡ The processing interval Λ (≈14.1 ms, load-dependent) does **not** appear here.
Under the content convention the label denotes the antenna epoch, so Λ is outside
the measurand by definition. Under the legacy convention it is inside it, and
then it dominates and is not a constant of the design. Which convention is in
force is therefore a statement about *what was measured*, not a correction to it
— which is why `chain` carries it and why the two are reported separately
throughout.

### 4.3 ⛔ The Type A claim is not yet earned, and the spec must not pretend it is

The σ-vs-C/N0 relation measured on 2026-08-29 — 12 % of σ per dB, obeying
1/√SNR — was measured on the **coarse** matched-filter stage. The stage that
produces the published edge is the **fine** stage, for which we have three points
at a single C/N0 (0.50 / 0.50 / 2.73 µs at 48.5 dB-Hz) a 22 ns figure from `BPSK-PPS-DETECTION-METHODS.md` at unstated conditions, and
the 150 ns fiducial-localisation Type A above, also at unstated C/N0.

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


### 4.5 The `chain_delay_ns` collision

Three distinct quantities currently wear this one name.

| Where | Reads | What it actually measures |
|---|---|---|
| `CLIENT-CONTRACT.md` §RADIOD facts | e.g. 4250 ns | a real analogue path delay, per radiod |
| `core-recorder-status.json` `t6_pps.chain_delay_ns` | **0.5955 s** ± 2.37 µs | where the recovered edge falls inside the named second |
| `t6_authority.asserted_chain_delay_ns` | 10 000 ns | the configured `delay_budget_ns` being applied |

Only the first matches the name. The second measures the coarse cascade's
naming of the second and has no analogue interpretation — no path in this
station spans half a second. The third reports an assertion rather than a
measurement.

**The collision reaches beyond this repository, and it carries a hazard.** The
client contract publishes a per-radiod fact,
`RADIOD_<id>_CHAIN_DELAY_NS`, and requires every timing-critical client to
apply it:

    utc_corrected = utc_raw − chain_delay_ns / 1e9

The contract sources that value from "the calibrating hf-timestd instance",
and the hf-timestd field bearing the matching name reads 0.5955 s. Nothing
publishes it today: B4 carries no such fact in `coordination.env`, no client
reports `chain_delay_ns_applied`, and the contract still marks the publishing
mechanism "TBD in sigmond Phase 4". So the hazard stays latent. But an
implementer wiring that mechanism as written, reaching for the field whose
name matches the contract, would shift every client's UTC by 596 ms — and each
client would faithfully report the correction it was applying.

**Proposed renaming**, which needs coordination with sigmond because the
contract owns the surviving name:

| Now | Proposed | Rationale |
|---|---|---|
| contract `chain_delay_ns` | unchanged | the name fits the quantity |
| `t6_pps.chain_delay_ns` | `edge_phase_in_named_second_ns` | states what it measures |
| `asserted_chain_delay_ns` | `applied_delay_budget_ns` | an assertion, not a measurement |

`core-recorder-status.json` serves as a published surface, so the rename runs
through a deprecation window: emit both keys, mark the old one deprecated in
`docs/`, and retire it one release later. No consumer outside hf-timestd reads
the two renamed keys today, which keeps the window cheap.

**What none of this supplies is a measurement of the analogue chain.** The
contract asks hf-timestd to calibrate one, and hf-timestd does not. Section 4
should propose an estimator that does — most plausibly a two-point method that
compares the injected edge against the same edge taken at a second, known
plane. Until such an estimator exists, the honest publication for
`RADIOD_<id>_CHAIN_DELAY_NS` remains absent rather than a plausible number,
and this document's budget marks the antenna-to-injector term `not_declared`
for exactly that reason.

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

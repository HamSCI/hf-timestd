# Timing — Physical Configuration & Graceful Degradation

**Status**: DESIGN DRAFT — 2026-05-23
**Audience**: operators configuring hf-timestd; reviewers evaluating the
timing authority hierarchy.
**Related**:
- `docs/METROLOGY.md` §4.5–§4.6 — the T-level hierarchy, the
  RTP-reference invariant, and chrony / NTP integration
- `docs/design/TIMING_AUTHORITY_ARCHITECTURE.md` — the RTP-vs-FUSION
  mode framework and the data-label / system-clock separation
- `CLAUDE.md` "Timing-authority invariant" — the project-wide rule

## 1. Why this doc

The two existing docs explain *how* the T-level hierarchy and the RTP-
reference invariant work algorithmically. This doc describes how the
**operator's physical site** maps onto that hierarchy: what hardware
the operator declares is present, what gracefully degrades when each
piece fails, and what minimum configuration still produces useful
timing. It is the bridge between the site survey and the runtime.

## 2. The RTP-reference invariant (in one sentence)

> RTP timestamps from radiod are the only authoritative timing
> substrate; the host wall clock is a derived product and never a
> source. Whatever authority sets `rtp_to_utc_offset_ns` — peer
> (T5), fusion (T3), or BPSK-PPS (T6) — every data label is built as
> `rtp_time + rtp_to_utc_offset_ns`.

Everything below is a consequence of that one rule.

## 3. The four physical authorities

A hf-timestd site can have *at most* four independent timing
authorities. Each one is a separate piece of hardware (or a separate
external dependency), each can be present or absent independently, and
each gives the system something different:

| Authority | What hardware | What it provides | If absent |
|---|---|---|---|
| **GPSDO** | Disciplines the RX-888 ADC sample clock (and typically feeds TS-1) | `sample_rate` is *exactly* nominal. Required for the existing assumption that one RTP tick = `1 / sample_rate` seconds. | TCXO drift ±5 ppm; fusion must estimate rate, not just origin |
| **TS-1** | HF-PPS injection into the RF chain | T6 BPSK chain-delay-calibrated reference at ~150 ns precision | T6 disappears; no chain_delay to disambiguate or persist |
| **Local GNSS** | ZED-F9P (or equivalent) on the radiod host or on a LAN timeserver | T5 (direct refclock) or T4 (NTP peer) — true GPS+PPS authority that chrony can use as master | No GPS peer authority; chrony has no NTP source from a known-good clock |
| **WAN NTP** | Internet access to NIST / pool servers | External cross-check for chrony only | Most graceful loss — RTP-reference invariant means **data labels are unaffected**; only chrony's own master-selection changes |

**Fusion (T3)** is **always-on**, regardless of what authorities are
present. It is not in the table above because it is a software service
fed by the HF time-station decodes, not a piece of hardware. Its job
shifts depending on what else is available:

- When peers (T5/T4) exist: fusion runs as a *cross-check witness*
  against the higher reference. Its output is logged for quality
  analysis; chrony prefers the lower-jitter peer.
- When peers are absent: fusion's output **is** the authority. Its
  uncertainty becomes the system's clock uncertainty.
- When the GPSDO is also absent: fusion must additionally estimate
  the TCXO rate, not just the origin (see §6).

## 4. Operator declaration — `[timing.physical]`

The operator declares what hardware is *physically wired in* at the
site. The runtime derives everything else from these facts. There is
**no** "mode" selector — mode is a consequence of the hardware.

```toml
[timing.physical]
# Required. Determines whether sample_rate is fixed or estimated.
#   "gpsdo"  — GPSDO disciplines the ADC; sample_rate is exact.
#   "tcxo"   — RX-888 internal TCXO only; fusion must estimate rate.
sample_clock = "gpsdo"

# Required. Determines T6 availability.
#   "ts1"    — TS-1 HF-PPS injection present (assumes same GPSDO).
#   "none"   — no PPS injection.
pps_injection = "ts1"

# Required. Determines T4/T5 availability.
#   "direct" — GPS+PPS refclock on the radiod host itself (T5).
#   "lan"    — GPS+PPS on a LAN NTP timeserver (T4 via chrony peer).
#   "none"   — no local GPS authority.
local_gnss = "lan"

# Optional, informational. Affects only chrony's source-selection
# logic, never the RTP→UTC labeling path.
#   "available" — internet reachable; chrony may use WAN NTP.
#   "none"      — isolated site.
wan_ntp = "available"
```

The runtime's authority hierarchy is then determined:

| Hardware | Available T-levels | Disambiguation reference | chrony master candidates |
|---|---|---|---|
| GPSDO + TS-1 + direct GNSS + WAN | T6, T5, T3 (T4 not needed) | T5 | T5, WAN NTP |
| GPSDO + TS-1 + LAN GNSS + WAN | T6, T4, T3 | T3 (preferred) → T4 (bootstrap only) | T4, WAN NTP |
| GPSDO + TS-1 + no GNSS + WAN | T6, T3 | T3 (only option) | none locally; WAN NTP only |
| GPSDO + TS-1 + no GNSS + no WAN | T6, T3 | T3 | T6 SHM, T3 SHM (isolated island) |
| no GPSDO + no TS-1 + no GNSS + no WAN | **T3 only** | n/a (no T6 to disambiguate) | T3 SHM (minimal floor — see §6) |

## 5. Graceful degradation — the four loss cases

The system must function (with reduced precision) through any single
authority failing.

### 5.1 No TS-1
- T6 disappears. Chain-delay disambiguation and persistence
  (`bpsk_chain_delay_store.py`) are inert.
- T3, T4, T5 unaffected. Chrony's master shifts from T6 SHM (if it
  was) to the highest remaining peer.
- Data labeling unchanged.

### 5.2 No local GNSS (T4/T5 lost)
- Chrony loses its GPS peer. Falls back to WAN NTP (if available) or
  to T3 SHM as its master.
- T6 disambiguation must use T3 (no T4 fallback available). This is
  the case our 2026-05-23 leak fix already anticipated — see commit
  `4b00f8c` and `_get_disambiguation_reference()`.
- Data labeling unchanged.

### 5.3 No WAN NTP
- The most graceful loss. Chrony loses external cross-check.
- T3, T4/T5, T6 all unaffected.
- Data labeling unchanged (the invariant guarantees this).
- Only operator-visible wall clock and journald timestamps lose
  external anchoring, but they were already chrony-disciplined from
  the remaining peer authorities.

### 5.4 No GPSDO — the deepest change
- TCXO drift ±5 ppm. `sample_rate` is no longer exact; one RTP tick
  is `(1 + ε) / sample_rate` seconds where ε drifts slowly.
- TS-1 (if present) drifts with the sample clock — its PPS edges no
  longer arrive at exact integer seconds. T6 reports edge positions
  that wander.
- **Fusion's responsibility expands** from origin estimation (where
  is RTP=0 in UTC?) to origin + rate estimation (how many TCXO
  samples per true second?).
- The Kalman state in `multi_broadcast_fusion.py` must track
  `(d_clock, d_rate)` not just `d_clock`. Audit pending — see §7.

## 6. The minimal-best system

When the operator declares:

```toml
[timing.physical]
sample_clock  = "tcxo"
pps_injection = "none"
local_gnss    = "none"
wan_ntp       = "none"
```

…the system must produce useful timing from **RX-888 TCXO + HF time
stations alone**. This is the architectural floor.

### What works
- RTP timestamps remain *sample-accurate* — gaps and sequencing are
  unambiguous, IQ data has known sample-relative timing.
- Fusion still runs and decodes WWV/WWVH/CHU.
- Chrony still receives a T3 SHM feed; locally it is the only source,
  so it becomes the master by default.

### What changes
- **Rate authority.** Fusion must continuously estimate the TCXO
  rate offset (`d_rate`) in addition to the origin offset (`d_clock`).
  Per-broadcast Kalmans observe both as the offset between predicted
  and observed station arrival times drifts.
- **Cold-start seed (bootstrap).** The system has no GPS-disciplined
  wall clock at boot. The seed must come from:
  - **CHU FSK decoder** — Frame B carries `year + DUT1 + TAI-UTC`;
    correct decode places UTC to within sub-second from a single
    one-minute cycle (see `chu_fsk_decoder.py`, fix recorded in
    `project_chu_fsk_frame_b_fix.md`).
  - **WWV/WWVH BCD time-code decoder** — minute-by-minute BCD digits
    encode hh:mm of the current minute; analogous sub-second seed
    once the first minute is decoded.
  - The host's battery-backed RTC may seed coarse (minutes-accurate)
    UTC as a Day-0 startup hint, but it never becomes a runtime
    source. Once fusion has decoded a single time-code frame from
    any station, the RTC's role is over.
- **Achievable precision.** Bounded by HF fusion noise — currently
  sub-100 µs steady-state, observed today on bee1 with the
  GPSDO+TS-1 cross-checks for ground truth.

### What this does not produce
- Sub-µs precision (that needs T5 or T6).
- A bias-free absolute time reference. Fusion has a propagation-model
  residual that varies seasonally and diurnally; characterizing it
  remains a science task.

## 7. Implementation gaps

Items where the current code does not yet match this design:

1. **`[timing.physical]` config block does not exist.** The runtime
   derivation rules in §4 need wiring once the schema lands.
2. **Fusion rate estimation in TCXO mode** — confirmed not yet
   audited. The Kalman *probably* already tracks `(d_clock, d_rate)`
   for per-broadcast filters; whether `d_rate` propagates back as a
   sample-rate correction for the recorder/labeler is unverified.
3. **Cold-start seed handoff from CHU FSK / WWV BCD into fusion.**
   The decoders exist and decode correctly; whether fusion can
   consume their first-decode as a seed (vs. only using them as
   ongoing measurements) is unverified.
4. **T5 (on-host GPS+PPS refclock probe)** is documented as a
   placeholder in `_get_disambiguation_reference()` but not wired.

## 8. What this doc explicitly is not

- It is **not** a new authority hierarchy. The T-levels remain as
  defined in METROLOGY.md §4.5.
- It is **not** a new mode selector. RTP Mode and Fusion Mode remain
  the operational shorthands; they are *consequences* of the
  hardware declared in `[timing.physical]`, not orthogonal switches.
- It is **not** a runtime preference (`authority = "auto"` etc.).
  That key continues to govern *which* available T-level to prefer
  at runtime when multiple are simultaneously available, exactly as
  in METROLOGY.md §4.5.

This doc only adds the *operator-facing physical-configuration
declaration* and the *graceful-degradation contract* that follows
from the invariant — pieces the existing docs assume but do not
spell out.

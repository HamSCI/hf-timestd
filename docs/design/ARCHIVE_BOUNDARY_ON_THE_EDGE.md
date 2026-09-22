# Cutting the archive on the pulse, not on the anchor

**Date:** 2026-09-22
**Status:** DESIGN. Nothing built. Written before code at Michael's direction.
**Companions:** `T6_ANCHOR_INVERSION_DESIGN.md` (the principle this applies),
`T6_NEWELL_VS_FOLD.md` §5f (which method suits which job)

---

## 1. Where every stored chunk begins today

`binary_archive_writer.py:834`:

```python
chunk_boundary = (int(rtp_derived_time) // self.file_duration_sec) * self.file_duration_sec
...
time_delta = chunk_boundary - offset_s - self._gps_time_unix
chunk_boundary_rtp = (self._rtp_timesnap + rtp_delta) & 0xFFFFFFFF
```

Read that in order. We take a UTC time derived from radiod's anchor, round it
down to a chunk, and convert the result back into an RTP sample through the
same anchor. Every raw file on every station starts where that arithmetic
lands.

The arithmetic rests on radiod's `GPS_TIME` / `RTP_TIMESNAP` pair, and that
pair does not update atomically. B4 reported it plainly on 2026-09-22:

```
ANCHOR PAIR AUDIT: updates=1485052 max_disagreement=-83951787 ns (-83.952 ms)
```

Eighty-four milliseconds of self-disagreement over 1.5 million updates, and
the known bound reaches 816 ms. The same afternoon B4's advertised epoch went
wrong by +320.793 ms and stayed wrong for hours while six source identities
drifted apart by a quarter of a second.

So the boundary of every archived chunk inherits an error we already
characterise as a fault elsewhere in this system.

## 2. What we have instead, and why this is not a new idea

The TS-1 injects its pulse ahead of the RX888. We detect that pulse to the
sample. It carries no propagation, it cannot fade, and its position does not
move.

⚡ **T6 already inverted this relationship everywhere else.** The shipped
anchor inversion says: the edge registers the ruler, and the coarse cascade
only names the integer second. The archive boundary is the last place still
running the pre-inversion arithmetic — deriving a position from the anchor
rather than reading it off the edge.

This design does not introduce a new timing source. It applies the one we
already trust to the one consumer that still asks radiod where the second
started.

## 3. The proposal

Choose the cut at a **detected pulse**, and use the anchor only to **name**
which second that pulse belongs to.

```
  today   anchor ──► UTC ──► round ──► back through anchor ──► RTP sample
  here    detected edge ──► RTP sample (exact)
          anchor ──► which second it is (integer, tolerant of ~0.5 s error)
```

The naming step survives a wrong anchor. Placing the cut no longer depends on
it at all.

**Which detector.** Scott Newell's per-sample phase jump, fed through his own
two-second smoother. §5f argues the general case: a file starts at a sample
and cannot start between two, so the act quantises the answer and a
sub-sample estimate buys nothing. His detector returns exactly one integer,
decides from a single pulse rather than waiting 30 s of folding, and carries
a ±5-sample consistency gate anchored to the previous accepted edge with a
ten-pulse agreement requirement. Measured on B4 2026-09-22: one report per
second, every one on the pulse, through 14 dB of gain movement.

**What we keep doing.** Write the residual into the sidecar. The cut lands on
a sample; the record of where it landed need not. `start_rtp_timestamp`,
`starting_offset` and `pipeline_offset_samples` already carry it.

## 4. Four things that could go wrong

**⛔ The channels run at different rates.** We detect the edge on the TS-1
channel at 96 kHz. The archived WWV channels run at **24 kHz** — the sidecars
say so. radiod's RTP timestamps are `input_sample_index / decimation`, so the
two domains relate by the decimation ratio and share one ADC clock. That
makes the mapping exact in principle and a place to get it wrong in practice.
Any implementation must derive the ratio, never assume 4.

**Not every station has a TS-1, and that settles itself.** AC0G-ND cannot
reach T6 at all, so it places boundaries by today's arithmetic. That needs no
decision and no new branch of behaviour: we always archive the receptions, and
the sidecar's `timing` block already declares the authority that governed the
sampling — `_chunk_timing_block` writes a schema v2 state record carrying
`judge_tier`, `counter_space`, `counter_epoch_id`, `f_s_hz`, `gps_time_ns`
and `rtp_timesnap` per chunk.

⚡ So "which method placed this boundary" becomes **one more field in a block
that already exists**, beside the tier that governed it. A reader of any chunk
can tell. Nothing is refused, nothing is silent, and a station without T6
differs from one with T6 exactly as much as its metadata says it does — which
is the point of recording it.

Channels do not diverge either. Every channel on a station shares one ADC
clock and one timing authority; only `counter_space` and `f_s_hz` differ, and
those already vary by channel and are already recorded.

**⚠ Adopting it moves the boundaries once.** Chunks will start up to a few
hundred milliseconds from where they used to. Downstream — GRAPE spectrograms,
the physics products — expects minute alignment and continuity. One
discontinuity at adoption, recorded in the sidecar, seems acceptable. Nobody
should discover it from a spectrogram.

**⚠ A stuck edge is worse than a noisy anchor.** If the gate ever locked onto
a wrong position it would cut every chunk in the wrong place, consistently and
without complaint. B4 sat on a phantom 20 ms lattice on 2026-09-04. Whatever
gate we use must re-acquire on sustained disagreement, and the ledger must
record which source placed each boundary.

## 5. How it ships

**Opt-in, default off.** The switch goes in the build so B4 can run it while
every other station keeps today's behaviour unchanged. Two boxes leave for
McMurdo; none of them should meet this first.

Sequence:

1. Land the code with the flag off. Prove by test that off means
   byte-identical boundaries to today.
2. Turn it on for B4 alone. Run both computations in parallel and record the
   difference per chunk without acting on it — a shadow measurement.
3. Read the difference. If the edge-placed boundary and the anchor-placed one
   agree to well under a sample, the anchor was fine and this buys little. If
   they diverge by the tens of milliseconds the anchor audit predicts, that
   difference is the error we have been archiving all along.
4. Only then consider switching the default.

Step 3 is the whole point. We can learn the size of the problem without
changing a single stored file.

## 6. What would falsify the premise

If the shadow measurement shows the anchor-derived boundary already sitting
within a sample of the detected edge, across a day that includes an anchor
excursion, then the anchor arithmetic is good enough and this work should
stop. That outcome would be worth knowing and cheap to obtain.

## 7. The division of labour, stated once

**Scott's method places the cut. Our estimator produces the time.**

A file or segment starts at a sample and cannot start between two, so the act
of cutting quantises the answer and a sub-sample estimate buys nothing there.
Reporting a time forces no such grid, and there the fraction is the whole
point — 0.444 us measured against the 3.007 us a rounded answer inherits.

The two never compete. They answer different questions about the same pulse.

## 8. Open question

Do we want the boundary on the pulse, or on the pulse *plus* the calibrated
chain delay? The pulse marks the TS-1 injection point, not the antenna
terminals. For choosing where to cut a file that distinction may not matter;
for anything that later reads the boundary as a time, it does.

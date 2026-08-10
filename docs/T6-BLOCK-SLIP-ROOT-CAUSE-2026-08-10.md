# T6 origin block-slips: root cause (2026-08-10)

**Question chased:** why does the T6 origin land an integer number of
radiod blocks (±1 packet) away at each re-lock, spreading 1.78 s over a
6 h / n=13 window against a 10.4 µs criterion — while the fine-stage
fraction repeats to the nanosecond?

## Layer 1 — environmental trigger (host, episodic)

Quarter-hourly decode bursts (jt9 at 270–340 % CPU; the 15-minute digimode
cycle) episodically starve the RX888 USB service path. radiod's own
measured-rate line shows it: `129597654.8 Hz … −18.10 ppm` at 21:14:29 —
impossible as a real GPSDO clock change; it is radiod counting fewer
samples arriving. Result: **genuine sample gaps enter the RTP stream**,
packet/block-quantised (measured via the diff-detector CSV: 132 slips
over ~20 h, dominant sizes −4 blocks ×23, −1 block ×9, up to −17 blocks,
ALL negative, clustered at :00/:15/:30/:45 ±40 s — but only on
quarter-hours with heavy decodes).

radiod is correctly pinned to its cache pair (CPUs 0–1 of 14; using
~110 % of the 200 % budget), but decoders are not fenced off that pair or
off the xhci IRQ path. Layer-1 hardening (CPUAffinity fences on the
decoder units / IRQ affinity) is a sigmond/host task — worthwhile, but
gaps can never be fully prevented; Layer 2 must tolerate them.

## Layer 2 — the bookkeeping defect (hf-timestd, deterministic)

Two distinct RTP-label pathologies reach the MF calibrator, measured from
a 180 s `debug_dump` NPZ (17.28 M samples, 9 902 batches):

1. **Benign label wobble:** 1 800 batch-boundary mismatches, every one
   exactly ±60 samples, perfectly cancelling (+60 → −60; cumulative
   divergence only ever 0 or 60). This is the recorder's 11-batches-per-
   10-blocks repackaging (LABEL AUDIT). Data contiguous, labels
   transiently wrong. → Explains the TWO-STATE 625 µs (one packet) origin
   phase: an edge falling in a divergent region reads 60 samples off.
2. **Genuine gaps (Layer 1):** declared RTP jumps by the lost amount;
   the buffer stays contiguous-by-concatenation.

The calibrator tracks edge periodicity in **buffer-index space**
(`_I_buf` concatenated as contiguous; expected next edge = +96 000
buffer samples). A genuine gap therefore slips the tracker by the gap
size; folds discard ("stream gap inside fold block"), estimates go stale
(the rigid DEGRADED→UNLOCKED rhythm ~1 min after each burst), and every
re-acquisition inherits the accumulated offset — integer blocks ± one
packet. Journal confirms end-to-end: consecutive native anchors differ by
exactly N×1249 s×96 000 − (blocks lost), e.g. −7 680 = −4 blocks across
the 14:50→15:11 re-lock.

**Exonerated (again, now terminally):** RF level, MF peak shape, the
fine-stage estimator (ns-exact), NMEA second-naming (raw_wall_time
fractions consistent at +30–33 ms across all 13 re-locks), host clock,
recorder socket buffers (all data sockets rb=128 MB; the 5 MB ones are
status sockets).

## Fix direction (Layer 2, hf-timestd)

Make edge tracking **gap-aware in declared-RTP space**:
- Expected next edge = `last_edge_rtp + round(Δrtp/96000)·96000` using
  DECLARED RTP deltas (wrap-safe), not buffer-index deltas — a sustained
  label jump then moves the search window WITH the gap instead of
  slipping the lock.
- Treat cancelling ±60 wobble as label noise: reconstruct per-sample RTP
  from net divergence (transient excursions that return to 0 within a few
  batches → contiguous interpretation; sustained shifts → honor the jump
  and resync fold indexing across it).
- Fold blocks spanning a sustained gap still discard (correct), but
  registration must re-home from post-gap declared RTP so estimates
  resume within one fold period instead of going estimate_stale.

Acceptance: re-run the stage-1 origin-spread measurement
(`scripts/t6_origin_spread.py`); criterion unchanged — < 10.4 µs across a
night's re-locks within one channel lifetime, THROUGH quarter-hour decode
bursts.

## Instruments left armed

- `debug_dump_*` keys live in B4 `[timing.t6_pps]` (one-shot per restart;
  108 MB NPZ at /var/lib/timestd/debug/). Remove after fix validation.
- Diff CSV (`bpsk_diff_edges.csv`) is the long-window slip recorder.
- Slip timeline analyzers: session scratchpad; spread tool on B4
  ~hamsci/w2-ab/t6_origin_spread.py.

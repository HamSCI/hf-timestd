# Dual Chrony Feed Architecture — DEPRECATED

**Status:** Obsolete.  Retained as a redirect for any external links
that still land here.

---

The dual-feed architecture described in earlier versions of this
document (parallel `timestd.L1` + `timestd.L2` SHM segments at units 0
and 1) was **dropped on 2026-05-23** in the TSL→FUSE/HPPS rename.
The current chrony feed architecture is:

| SHM unit | Refid  | Source                                              |
|----------|--------|-----------------------------------------------------|
| 1        | `FUSE` | `multi_broadcast_fusion` calibrated L2 timing       |
| 2        | `HPPS` | T6 BPSK-PPS via matched-filter calibrator           |
| 3        | `HFPS` | T6 BPSK-PPS via diff calibrator (Method 5)          |

The legacy SHM unit 0 (L1 raw-metrology feed) was retired because in
single-station mode it produced byte-identical `d_clock_fused_ms`
output to the L2 fusion feed, making the second feed redundant.
`result_l1` is still computed for the L1-vs-L2 diagnostic comparison
in fusion logs but is no longer written to chrony.

## Where to read the current story

* **Foundational architecture**: [ARCHITECTURE-FIRST-PRINCIPLES.md](ARCHITECTURE-FIRST-PRINCIPLES.md) — the
  RTP-substrate framing that supersedes any "what we feed chrony"
  framing.
* **System architecture**: [ARCHITECTURE.md](ARCHITECTURE.md).
* **Metrological story**: [METROLOGY.md](METROLOGY.md) — see §4.5 for
  the current T-level hierarchy and §4.6 for `authority.json`.
* **Chrony-facing tuning** (the surviving HPPS/HFPS feeds):
  [HF-PPS-CHRONY-TUNING.md](HF-PPS-CHRONY-TUNING.md).
* **Wiring** of producers to consumers:
  [TIMING-PIPELINE-WIRING.md](TIMING-PIPELINE-WIRING.md).

## Why "dual feed" was the wrong frame anyway

In the substrate framing
([ARCHITECTURE-FIRST-PRINCIPLES.md](ARCHITECTURE-FIRST-PRINCIPLES.md)),
chrony is one **downstream consumer** of hf-timestd's per-sample
annotation stream.  The number of SHM segments we write is an
implementation detail of how we present that annotation to *one*
consumer — chrony — and the right number is whatever serves chrony's
selection algorithm best, not a load-bearing architectural choice.

The original "dual feed" framing inverted that: it treated the two
SHM feeds as the architecturally interesting thing.  The historical
discussion (L1 raw vs L2 calibrated) is still preserved in git
history if needed for context.

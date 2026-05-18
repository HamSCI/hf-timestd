# M-H14 — GNSS-VTEC TEC correction in RTP mode

## Finding
The code review (2026-05-17) flagged the GNSS-VTEC block in `fuse()` as
mutating `m.d_clock_ms` with no RTP gate, injecting iono model error into a
GPS+PPS-pinned D_clock.

## Outcome — already fixed
Investigation (git blame) shows M-H14 was **already remediated** by commit
`c9117b3` ("Metrology/physics review + Tier-1 remediation + dual-Kalman
increment 1"). The GNSS-VTEC block now gates on `if self.is_rtp_authority:` —
in RTP mode it is a cross-check only (tags `propagation_mode`/`confidence`);
the `m.d_clock_ms` mutation lives in the `elif`/`else` arms, reached only when
`not is_rtp_authority`. The HF-TEC block below is likewise gated.

No code change required. The fix had **no regression test** — that gap is
what this task closes.

## Task — done
- [x] git branch `mh14-rtp-gate-test` off `metrology-physics-review-remediation`
- [x] Add `tests/test_fusion_gnss_vtec_rtp_gate.py` (2 tests)
- [x] Full suite run

## Review

**Files changed**
- `tests/test_fusion_gnss_vtec_rtp_gate.py` — new, 2 tests. No source change.

**What the test pins**
- RTP mode (`is_rtp_authority=True`): a fresh GNSS-VTEC reading far from the
  modelled TEC leaves every measurement's `d_clock_ms` untouched; the block
  still runs (modes tagged `+GNSS_VALIDATED`), so the assertion is not vacuous.
- Fusion mode (`is_rtp_authority=False`): the same discrepancy DOES correct
  `d_clock_ms` (modes tagged `+GNSS_TEC`) — the contrast that makes the RTP
  assertion meaningful.

**Verification**
- New suite: 2/2 pass (verified the RTP/Fusion split directly:
  RTP `[2,2,2]→[2,2,2]`; Fusion `[2,2,2]→[5.27,3.45,2.36]`).
- Full repo suite: 1616 passed, 9 subtests passed (1614 + 2 new). One
  pre-existing unrelated `test_l2_clickhouse_wire` failure, deselected.

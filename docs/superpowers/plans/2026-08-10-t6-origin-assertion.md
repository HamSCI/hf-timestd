# T6 Origin Assertion — Stage 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop deriving T6's sub-second RTP→UTC term from radiod's advertised wall clock; assert it from configuration and demote the derived residual to a reported diagnostic.

**Architecture:** A pure static resolver returns `(asserted_ns, reported_residual_ns)`. Both T5/NMEA disambiguation paths call it instead of computing `int(round(residual_sec * 1e9))`. The existing Layer-B plausibility guard is re-pointed at the *reported* residual so its safety behaviour is preserved. A throttled reporter mirrors the existing `_t6_report_naming_vs_radiod_pair` pattern (spec §6 invariant 5: reported, never corrective).

**Tech Stack:** Python 3.13, pytest (`addopts = "-ra -q"`, `testpaths = ["tests"]`), hf-timestd on B3 at `/root/appliance/repos/hf-timestd`, deployed to B4 VM at `/opt/git/sigmond/hf-timestd`.

## Global Constraints

- Spec: `docs/design/T6_ORIGIN_ASSERTION_DESIGN.md` (commits df5e065, 8df3881, e9b5345).
- `chain_delay_calib_s` stays **0.0** for stage 1. It is a knowingly wrong constant chosen to make the experiment clean (spec §9.1).
- **Do not commit the code change to the repo until the acceptance criterion passes on B4.** Patch B4's checkout with a backup; commit from B3 afterwards.
- Do not touch `_t6_name_integer_second` or `_t6_name_second_via_nmea`. They are already NMEA-derived and host-clock-free.
- Branch: `main`. No feature branches (fleet convention).
- Commit messages end with `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`.

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `src/hf_timestd/core/core_recorder_v2.py` | T6 disambiguation + anchor capture | Add `_t6_resolve_chain_delay_ns` (static, pure) and `_t6_report_derived_residual` (throttled reporter); re-point two call sites and their Layer-B guards |
| `tests/test_core_recorder_t6_origin_assertion.py` | Unit tests for the resolver | Create |

Two call sites contain the **identical line** `effective_chain_delay_ns = int(round(residual_sec * 1e9))`. They must be edited individually by surrounding context, never with a global replace.

---

### Task 1: Pure resolver + reporter, wired into the MF/HPPS path

**Files:**
- Modify: `src/hf_timestd/core/core_recorder_v2.py`
- Test: `tests/test_core_recorder_t6_origin_assertion.py`

**Interfaces:**
- Produces: `CoreRecorderV2._t6_resolve_chain_delay_ns(residual_sec: float, chain_delay_calib_s: float) -> tuple[int, int]` returning `(asserted_ns, reported_residual_ns)`; `CoreRecorderV2._t6_report_derived_residual(feed: str, reported_ns: int, asserted_ns: int) -> None`; class constant `T6_RESIDUAL_REPORT_PERIOD_SEC = 300.0`.
- Consumes: existing `self._t6_chain_delay_calib_s` (set in `__init__` from `chain_delay_calib_s`, default 0.0).

- [ ] **Step 1: Write the failing test**

Create `tests/test_core_recorder_t6_origin_assertion.py`:

```python
"""T6 origin assertion — chain delay is asserted, never derived.

Spec: docs/design/T6_ORIGIN_ASSERTION_DESIGN.md §5
"""
from hf_timestd.core.core_recorder_v2 import CoreRecorderV2


class TestResolveChainDelay:
    def test_asserts_calib_and_reports_residual(self):
        asserted, reported = CoreRecorderV2._t6_resolve_chain_delay_ns(
            residual_sec=0.03184, chain_delay_calib_s=0.0)
        assert asserted == 0
        assert reported == 31_840_000

    def test_asserted_tracks_calib_not_residual(self):
        asserted, reported = CoreRecorderV2._t6_resolve_chain_delay_ns(
            residual_sec=0.10889, chain_delay_calib_s=0.000250)
        assert asserted == 250_000
        assert reported == 108_890_000

    def test_differing_residuals_yield_one_origin(self):
        # The defect this fixes: 31.84 ms and 47.30 ms were measured at
        # identical 96 kHz/+-25 kHz config 15 minutes apart and produced
        # two different origins.  They must now produce one.
        a1, r1 = CoreRecorderV2._t6_resolve_chain_delay_ns(0.03184, 0.0)
        a2, r2 = CoreRecorderV2._t6_resolve_chain_delay_ns(0.04730, 0.0)
        assert a1 == a2 == 0
        assert r1 != r2  # the diagnostic still distinguishes them

    def test_negative_residual_reported_signed(self):
        asserted, reported = CoreRecorderV2._t6_resolve_chain_delay_ns(
            residual_sec=-0.002, chain_delay_calib_s=0.0)
        assert asserted == 0
        assert reported == -2_000_000


class TestReporter:
    def test_reporter_records_and_throttles(self):
        class Fake:
            T6_RESIDUAL_REPORT_PERIOD_SEC = 300.0
            _t6_report_derived_residual = (
                CoreRecorderV2._t6_report_derived_residual)
        f = Fake()
        f._t6_report_derived_residual("HPPS", 31_840_000, 0)
        first = f._t6_residual_report_wall
        f._t6_report_derived_residual("HFPS", 47_300_000, 0)
        # Second call inside the window must not move the throttle stamp,
        # but the latest value is always recorded for the status path.
        assert f._t6_residual_report_wall == first
        assert f._t6_derived_residual_ns == 47_300_000
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_core_recorder_t6_origin_assertion.py -v`
Expected: FAIL — `AttributeError: type object 'CoreRecorderV2' has no attribute '_t6_resolve_chain_delay_ns'`

- [ ] **Step 3: Add the resolver and reporter**

Insert immediately **after** the `_t6_report_naming_vs_radiod_pair` method (it ends with the `logger.warning(... delta,)` call and its closing paren, around line 2592):

```python
    # Throttle for the derived-residual diagnostic.
    T6_RESIDUAL_REPORT_PERIOD_SEC = 300.0

    @staticmethod
    def _t6_resolve_chain_delay_ns(
        residual_sec: float, chain_delay_calib_s: float
    ) -> tuple[int, int]:
        """Return ``(asserted_ns, reported_residual_ns)``.

        Stage 1 of ``docs/design/T6_ORIGIN_ASSERTION_DESIGN.md`` §5: the
        chain delay is ASSERTED from configuration and never derived
        from radiod's advertised wall clock.  Deriving it made the
        correction to radiod's RTP→UTC mapping a function of that same
        mapping, and re-deriving it at every authority UNLOCK (58 in one
        night on AC0G-B4) produced a different origin each time.

        The derived residual is returned alongside for REPORTING only.
        """
        return (int(round(chain_delay_calib_s * 1e9)),
                int(round(residual_sec * 1e9)))

    def _t6_report_derived_residual(
        self, feed: str, reported_ns: int, asserted_ns: int
    ) -> None:
        """Spec §6 invariant 5 pattern: report, never correct.

        The derived residual is the diagnostic that produced
        T6_ORIGIN_ASSERTION_DESIGN.  It keeps being measured and
        surfaced; it simply stops steering the anchor.
        """
        self._t6_derived_residual_ns = reported_ns
        now = time.monotonic()
        last = getattr(self, '_t6_residual_report_wall', None)
        if (last is not None
                and now - last < self.T6_RESIDUAL_REPORT_PERIOD_SEC):
            return
        self._t6_residual_report_wall = now
        logger.warning(
            "T6 %s: derived residual %+.3f ms (radiod wall-clock minus "
            "NMEA integer second) — REPORTED ONLY, not corrective; "
            "chain_delay asserted as %+.3f ms from chain_delay_calib_s "
            "(T6_ORIGIN_ASSERTION_DESIGN §5)",
            feed, reported_ns / 1e6, asserted_ns / 1e6,
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /root/appliance/repos/hf-timestd && python -m pytest tests/test_core_recorder_t6_origin_assertion.py -v`
Expected: PASS — every test in the new file green

- [ ] **Step 5: Wire the MF/HPPS call site**

In `_t6_disambiguate_via_t5_lb1421`, find this block (it is preceded by the comment `# Physical chain_delay = sub-second residual after the` / `# integer-second alignment above.`):

```python
            effective_chain_delay_ns = int(round(residual_sec * 1e9))
```

Replace with:

```python
            effective_chain_delay_ns, reported_residual_ns = (
                self._t6_resolve_chain_delay_ns(
                    residual_sec, self._t6_chain_delay_calib_s))
            self._t6_report_derived_residual(
                "HPPS", reported_residual_ns, effective_chain_delay_ns)
```

Then, in the Layer-B guard immediately below, change the quantity under test from the asserted value to the reported residual (the asserted value is a constant and would never trip the guard, silently disabling it):

```python
            if abs(reported_residual_ns) > T6_PHYSICAL_CHAIN_DELAY_MAX_NS:
```

and in that guard's `logger.warning` f-string, replace both occurrences of `effective_chain_delay_ns` with `reported_residual_ns`.

- [ ] **Step 6: Run the full T6 suite for regressions**

Run: `cd /root/appliance/repos/hf-timestd && python -m pytest tests/ -k "t6 or T6 or chain_delay" -q`
Expected: PASS, no failures. If `test_core_recorder_t6_step_recovery.py` fails, read it before changing anything — it exercises the step-recovery path that consumes `_t6_disambiguation_ns`.

- [ ] **Step 7: Commit**

```bash
cd /root/appliance/repos/hf-timestd
git add tests/test_core_recorder_t6_origin_assertion.py src/hf_timestd/core/core_recorder_v2.py
git commit -m "feat(t6): assert chain_delay in the MF/HPPS path instead of deriving it

Spec: docs/design/T6_ORIGIN_ASSERTION_DESIGN.md §5.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 2: Wire the diff/HFPS path

**Files:**
- Modify: `src/hf_timestd/core/core_recorder_v2.py`
- Test: `tests/test_core_recorder_t6_origin_assertion.py`

**Interfaces:**
- Consumes: `_t6_resolve_chain_delay_ns`, `_t6_report_derived_residual` from Task 1.

- [ ] **Step 1: Note — this task has no new unit test**

The change is pure wiring inside a long method that would need heavy
mocking of `_lb1421_probe`, `_t6_diff_calibrator` and `_t6_channel_info`
to exercise directly. The resolver it calls is already covered by Task 1.
Verification for this task is the full suite (Step 4) plus the live
`REPORTED ONLY` check in Task 3 Step 4, which proves the HFPS path
actually reaches the new code on hardware.

Do not invent a test that passes before the change — it proves nothing.

- [ ] **Step 2: Wire the diff/HFPS call site**

In `_t6_diff_disambiguate_via_t5_lb1421`, find:

```python
        effective_chain_delay_ns = int(round(residual_sec * 1e9))
```

(note: eight-space indent, and preceded by the comment `# Layer B physical-plausibility guard — same rationale as` on the following lines). Replace with:

```python
        effective_chain_delay_ns, reported_residual_ns = (
            self._t6_resolve_chain_delay_ns(
                residual_sec, self._t6_chain_delay_calib_s))
        self._t6_report_derived_residual(
            "HFPS", reported_residual_ns, effective_chain_delay_ns)
```

Then in that function's Layer-B guard, change:

```python
        if abs(reported_residual_ns) > T6_PHYSICAL_CHAIN_DELAY_MAX_NS:
```

and replace `effective_chain_delay_ns` with `reported_residual_ns` inside that guard's `logger.warning` f-string.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /root/appliance/repos/hf-timestd && python -m pytest tests/test_core_recorder_t6_origin_assertion.py -v`
Expected: PASS — same count as Task 1 left behind; this task adds no tests, so any change here is a regression

- [ ] **Step 4: Run the full suite**

Run: `cd /root/appliance/repos/hf-timestd && python -m pytest tests/ -q`
Expected: no new failures relative to a pre-change baseline. Capture that baseline first with `git stash && python -m pytest tests/ -q > /tmp/baseline.txt; git stash pop` if unsure.

- [ ] **Step 5: Commit**

```bash
cd /root/appliance/repos/hf-timestd
git add src/hf_timestd/core/core_recorder_v2.py
git commit -m "feat(t6): assert chain_delay in the diff/HFPS path too

Spec: docs/design/T6_ORIGIN_ASSERTION_DESIGN.md §5.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 3: Deploy to B4 and run the acceptance experiment

**Files:**
- Modify (B4 only, with backup): `/opt/git/sigmond/hf-timestd/src/hf_timestd/core/core_recorder_v2.py`
- Create: `/root/appliance/t6-origin-acceptance/` on B3 for captured evidence

**Interfaces:**
- Consumes: the two commits from Tasks 1 and 2.

**Access:** B4 PM host `root@192.168.1.244` password `hamsci-sigmond`; run inside VM 100 via `qm guest exec 100 --timeout N -- /bin/bash -c "..."`. The `sigmond` user cannot read the system journal and has no passwordless sudo — always use guest exec. Long shell snippets must be base64-encoded through the ssh→guest-exec→bash layers; naked nested quoting silently returns empty.

- [ ] **Step 1: Capture the pre-change baseline**

The acceptance measurement is the **implied origin** of each captured native anchor:

```
origin_ns = utc_ns - (rtp / sample_rate) * 1e9
```

taken from the `native_anchor: rtp=..., utc_ns=..., sr=...` field of each `T6 chain_delay disambiguated against T5` log line. Extract the last 24 h:

```bash
journalctl -u timestd-core-recorder --since "-24h" --no-pager \
  | grep -oE 'native_anchor: rtp=[0-9]+, utc_ns=[0-9]+, sr=[0-9]+' \
  > /tmp/origin_before.txt
wc -l /tmp/origin_before.txt
```

Expect several entries with visibly different implied origins — that is the defect.

- [ ] **Step 2: Deploy the patched file to B4 with a backup**

From B3, base64 the patched file through the guest agent (naked `scp` to
the VM is not available; the ssh→guest-exec→bash path needs encoding):

```bash
# on B3
F=/root/appliance/repos/hf-timestd/src/hf_timestd/core/core_recorder_v2.py
B64=$(base64 -w0 "$F")
P=/opt/git/sigmond/hf-timestd/src/hf_timestd/core/core_recorder_v2.py
sshpass -p 'hamsci-sigmond' ssh -o StrictHostKeyChecking=no root@192.168.1.244 \
  "qm guest exec 100 --timeout 120 -- /bin/bash -c \"cp $P $P.bak-pre-origin-assert && echo $B64 | base64 -d > $P && python3 -c 'import ast,sys; ast.parse(open(\\\"$P\\\").read())' && echo DEPLOYED_OK\""
```

The `ast.parse` check catches a truncated transfer before the restart.
Expected output: `DEPLOYED_OK`.

Rollback at any point: restore `$P.bak-pre-origin-assert` and restart the
service.

- [ ] **Step 3: Restart cleanly and confirm the detector is not deadlocked**

The diff detector's `_running_max` can latch on the channel-startup transient and go permanently silent (see spec context). Always verify the CSV is growing:

```bash
systemctl stop timestd-core-recorder
sleep 3
rm -f /var/lib/timestd/bpsk_diff_chain_delay.json
systemctl start timestd-core-recorder
sleep 200
wc -l /var/lib/timestd/debug/bpsk_diff_edges.csv   # run twice, 200 s apart
```

Expected: ~1 new row per second. If it is not growing, restart again — it is a race, not a permanent failure.

- [ ] **Step 4: Confirm the new diagnostic is firing**

```bash
journalctl -u timestd-core-recorder --since "-10 min" --no-pager \
  | grep "REPORTED ONLY"
```

Expected: at least one line, throttled to one per 300 s, showing a derived residual in the tens of milliseconds alongside `chain_delay asserted as +0.000 ms`.

- [ ] **Step 5: Run overnight, then evaluate the criterion**

After ≥8 h, extract origins as in Step 1 into `/tmp/origin_after.txt` and compute the spread:

```python
import re, sys
rows = []
for line in open(sys.argv[1]):
    m = re.search(r'rtp=(\d+), utc_ns=(\d+), sr=(\d+)', line)
    if m:
        rtp, utc_ns, sr = int(m[1]), int(m[2]), int(m[3])
        rows.append(utc_ns - (rtp / sr) * 1e9)
if not rows:
    sys.exit("no anchors captured")
spread_us = (max(rows) - min(rows)) / 1e3
print(f"n={len(rows)}  spread={spread_us:.3f} us")
print("PASS" if spread_us < 10.4 else "FAIL")
```

**PASS:** spread < 10.4 µs (one sample at 96 kHz) across all re-locks in the window → the derivation was the variance source; the spec's wider architectural change is warranted.
**FAIL:** spread still tens of ms → the diagnosis is wrong. Do not commit. Return to what else changes at re-lock; the reported residuals in the journal are the evidence trail.

- [ ] **Step 6: On PASS, push the two commits**

Confirm with the operator before pushing — this is outward-facing.

```bash
cd /root/appliance/repos/hf-timestd
git push origin main
```

- [ ] **Step 7: On PASS, record the result in the spec**

Add an "Outcome" section to `docs/design/T6_ORIGIN_ASSERTION_DESIGN.md` with `n`, the measured spread, and the window, then commit.

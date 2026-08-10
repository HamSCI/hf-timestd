"""Acceptance metric: circular spread of the T6 anchor origin.

origin = utc_ns - rtp/sr  is defined only modulo the RTP wrap period
(2**32 / sr seconds) because rtp is a 32-bit counter.  All arithmetic is
integer: utc_ns is ~1.8e18, where float64 granularity is ~256 ns.
"""
import re, sys

rows = []
for line in open(sys.argv[1]):
    m = re.search(r'rtp=(\d+), utc_ns=(\d+), sr=(\d+)', line)
    if m:
        rows.append((int(m[1]), int(m[2]), int(m[3])))
if not rows:
    sys.exit("no anchors captured")

for sr in sorted({r[2] for r in rows}):
    sub = [(rtp, utc) for rtp, utc, s in rows if s == sr]
    period = (2**32 * 10**9) // sr          # ns, exact
    vals = sorted(((utc - (rtp * 10**9) // sr) % period) for rtp, utc in sub)
    if len(vals) < 2:
        print(f"sr={sr:>6}  n={len(vals)}  (need >=2 to measure spread)")
        continue
    gaps = [vals[i+1] - vals[i] for i in range(len(vals) - 1)]
    gaps.append(period - (vals[-1] - vals[0]))     # the wrap-around gap
    spread_ns = period - max(gaps)
    verdict = "PASS" if spread_ns < 10_400 else "FAIL"
    print(f"sr={sr:>6}  n={len(vals):>3}  period={period/1e9:.1f}s  "
          f"spread={spread_ns/1000:.3f} us  {verdict}")
    if verdict == "FAIL" and len(vals) <= 12:
        for v in vals:
            print(f"        origin_ns={v}")

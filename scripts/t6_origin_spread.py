"""Origin stability WITHIN each channel lifetime.

RTP epochs reset on radiod restart AND on T6 channel re-creation (each
recorder restart destroys/recreates the channel and radiod restarts its
counter near 2**31).  Origins are therefore only comparable inside one
channel lifetime.  Segment on channel creation, measure inside each.
"""
import re, sys

CREATE = re.compile(r'^(\S+ \d+ \d+:\d+:\d+).*T6 BPSK PPS first samples')
ANCHOR = re.compile(r'^(\S+ \d+ \d+:\d+:\d+).*native_anchor: rtp=(\d+), utc_ns=(\d+), sr=(\d+)')

segments, cur = [], None
for line in open(sys.argv[1], errors='replace'):
    if CREATE.search(line):
        cur = {'start': CREATE.search(line).group(1), 'anchors': []}
        segments.append(cur)
        continue
    m = ANCHOR.search(line)
    if m and cur is not None:
        cur['anchors'].append((int(m.group(2)), int(m.group(3)), int(m.group(4)), m.group(1)))

print(f"{len(segments)} channel lifetime(s) found\n")
for i, s in enumerate(segments, 1):
    a = s['anchors']
    if len(a) < 2:
        print(f"  segment {i} (from {s['start']}): {len(a)} anchor(s) — cannot measure")
        continue
    sr = a[0][2]
    period = (2**32 * 10**9) // sr
    vals = sorted((utc - (rtp * 10**9) // sr) % period for rtp, utc, _, _ in a)
    gaps = [vals[j+1] - vals[j] for j in range(len(vals)-1)]
    gaps.append(period - (vals[-1] - vals[0]))
    spread = period - max(gaps)
    verdict = 'PASS' if spread < 10_400 else 'FAIL'
    print(f"  segment {i} (from {s['start']}): n={len(a)}  "
          f"spread={spread/1000:.3f} us  {verdict}   [{a[0][3]} .. {a[-1][3]}]")

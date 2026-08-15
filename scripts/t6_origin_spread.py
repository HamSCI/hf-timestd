"""Origin stability WITHIN each channel lifetime.

RTP epochs reset on radiod restart AND on T6 channel re-creation (each
recorder restart destroys/recreates the channel and radiod restarts its
counter near 2**31).  Origins are therefore only comparable inside one
channel lifetime.  Segment on channel creation, measure inside each.

Usage:
    journalctl -u timestd-core-recorder --since -24h > /tmp/j.log
    python3 scripts/t6_origin_spread.py /tmp/j.log

Exits non-zero when nothing could be measured.  That matters: this tool
used to print "0 channel lifetime(s) found" and exit 0 when its regexes
matched nothing, so a clean exit meant either "origins are stable" or
"I read the wrong log format" and the caller could not tell which.
Any journalctl -o format is accepted now; the payload is matched
wherever it appears rather than anchored to a timestamp shape.
"""
import re
import sys

# Payloads are searched anywhere in the line, so journalctl's output
# format (default, short-iso, short-precise, ...) is irrelevant.  The
# old version anchored on the DEFAULT timestamp shape and therefore
# matched NOTHING under `-o short-iso` -- silently.
CREATE = re.compile(r'T6 BPSK PPS first samples')
# utc_ns and sr are optional in the pattern so a partial anchor line is
# COUNTED as unusable rather than skipped as if it were not an anchor.
# The external-reference log lines omit sr=, and the "already aligned
# within one sample" variant omits utc_ns= as well.
ANCHOR = re.compile(
    r'native_anchor: rtp=(\d+)'
    r'(?:, utc_ns=(\d+))?'
    r'(?:, sr=(\d+))?'
)
# Leading timestamp in either journalctl style, for the report only.
TIMESTAMP = re.compile(
    r'^(\w{3}\s+\d+\s+\d+:\d+:\d+'
    r'|\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\S*)'
)

# Spec gate: the authority's delay budget resolution (10.4 us).
SPREAD_GATE_NS = 10_400


def _timestamp(line):
    m = TIMESTAMP.search(line)
    return m.group(1) if m else '?'


def _new_segment(start, implicit=False):
    return {'start': start, 'anchors': [], 'timestamps': [],
            'unusable': 0, 'implicit': implicit}


def parse_log(lines):
    """Segment a journal by channel lifetime.

    Returns a list of segments, each with usable ``anchors`` as
    ``(rtp, utc_ns, sr)`` and a count of ``unusable`` anchor lines --
    ones that named an anchor but lacked utc_ns or sr, which cannot be
    turned into an origin and must not be quietly ignored.
    """
    segments, cur = [], None
    for line in lines:
        if CREATE.search(line):
            cur = _new_segment(_timestamp(line))
            segments.append(cur)
            continue
        m = ANCHOR.search(line)
        if not m:
            continue
        if cur is None:
            # Anchors before the first creation line: the log window
            # opened mid-lifetime (`--since` almost guarantees this).
            # Previously dropped without a word.
            cur = _new_segment(_timestamp(line), implicit=True)
            segments.append(cur)
        rtp, utc_ns, sr = m.group(1), m.group(2), m.group(3)
        if utc_ns is None or sr is None:
            cur['unusable'] += 1
            continue
        cur['anchors'].append((int(rtp), int(utc_ns), int(sr)))
        cur['timestamps'].append(_timestamp(line))
    return segments


def segment_spread_ns(anchors):
    """Angular spread of the origins, in ns, on the RTP-wrap circle.

    Origins live modulo one 32-bit wrap period, so the spread is the
    complement of the largest empty arc -- not max minus min, which
    would read a cluster straddling the wrap as maximally spread.
    """
    if len(anchors) < 2:
        raise ValueError("need at least two anchors")
    sr = anchors[0][2]
    if any(a[2] != sr for a in anchors):
        raise ValueError("mixed sample rates within one segment")
    period = (2**32 * 10**9) // sr
    vals = sorted((utc - (rtp * 10**9) // sr) % period
                  for rtp, utc, _ in anchors)
    gaps = [vals[j + 1] - vals[j] for j in range(len(vals) - 1)]
    gaps.append(period - (vals[-1] - vals[0]))
    return period - max(gaps)


def main(argv):
    if not argv:
        print(__doc__)
        return 2
    with open(argv[0], errors='replace') as fh:
        segments = parse_log(fh)

    measured = 0
    unusable_total = sum(s['unusable'] for s in segments)
    print(f"{len(segments)} channel lifetime(s) found\n")
    for i, s in enumerate(segments, 1):
        tag = ' (log opened mid-lifetime)' if s['implicit'] else ''
        note = (f"  [{s['unusable']} unusable anchor line(s): no utc_ns/sr]"
                if s['unusable'] else '')
        a = s['anchors']
        if len(a) < 2:
            print(f"  segment {i} (from {s['start']}){tag}: "
                  f"{len(a)} usable anchor(s) — cannot measure{note}")
            continue
        try:
            spread = segment_spread_ns(a)
        except ValueError as e:
            print(f"  segment {i} (from {s['start']}){tag}: {e}{note}")
            continue
        measured += 1
        verdict = 'PASS' if spread < SPREAD_GATE_NS else 'FAIL'
        print(f"  segment {i} (from {s['start']}){tag}: n={len(a)}  "
              f"spread={spread/1000:.3f} us  {verdict}   "
              f"[{s['timestamps'][0]} .. {s['timestamps'][-1]}]{note}")

    if unusable_total:
        print(f"\n{unusable_total} anchor line(s) were unusable — they named "
              f"an anchor without utc_ns and/or sr, so no origin could be "
              f"derived.  The external-reference disambiguation log lines "
              f"are the usual source.")
    if measured == 0:
        print("\nNOTHING MEASURED — this is a tool failure, not a result. "
              "Check that the log covers a period with T6 anchor captures "
              "and that it came from timestd-core-recorder.")
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))

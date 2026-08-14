"""Add N 96 kHz load channels to bee1 radiod, or remove them.

Overloading radiod by giving it more channels than its isolated cache pair can
serve reproduces B4's actual condition (over capacity) without throttling the
cgroup -- so the USB reader keeps its priority and the FX3 is not put at risk.
"""
import sys, time
from ka9q.control import RadiodControl

MON = 1253864984                      # the monitored channel; never touched
BASE = 14_000_000                     # load channels spread above this
c = RadiodControl("bee1-status.local")

if sys.argv[1] == "add":
    n = int(sys.argv[2]); start = int(sys.argv[3])
    made = []
    for i in range(start, start + n):
        f = BASE + i * 200_000
        try:
            s = c.create_channel(frequency_hz=f, preset="iq", sample_rate=96000,
                                 agc_enable=0, gain=0.0)
            made.append((s, f))
        except Exception as e:
            print("  create failed", f, e)
    print(f"created {len(made)}: {[m[0] for m in made]}")
elif sys.argv[1] == "remove":
    from ka9q.control import discover_channels
    chs = discover_channels("bee1-status.local", listen_duration=8.0)
    n = 0
    for s, ch in chs.items():
        if s == MON or not ch.frequency:
            continue
        c.remove_channel(s); n += 1
    print(f"removed {n} load channels (monitored {MON} preserved)")

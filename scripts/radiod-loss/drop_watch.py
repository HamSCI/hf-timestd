"""Per-channel FILTER_DROPS + published-epoch watcher, all metrology channels.

Question: is the WWV/WWVH tick PAYLOAD contaminated by radiod block drops,
or is only the T6 timing REFERENCE compromised?  Different severity.

FILTER_DROPS is cumulative, so cadence sets attribution resolution, not the
count.  Passive listener => no polling load added to radiod.

Identifies channels by TUNED FREQUENCY, never by computing an SSRC (SSRCs are
hash-assigned and channels get recreated).  Re-learns the ssrc->name mapping
whenever a channel is recreated (drops counter resets).
"""
import sys, time
from ka9q.control import RadiodControl

GPS_UTC_OFFSET, GPS_LEAP, B = 315964800, 18, 1_000_000_000
BLOCK_MS = 20.0

# tuned frequency (Hz) -> label.  Six metrology channels + T6 + two controls.
BY_FREQ = {
    2_500_000: "SHARED_2500", 5_000_000: "SHARED_5000", 10_000_000: "SHARED_10000",
    15_000_000: "SHARED_15000", 20_000_000: "WWV_20000", 25_000_000: "WWV_25000",
    45_375_000: "T6_PPS_96k",
    14_074_000: "ctl_ft8_12k", 7_074_000: "ctl_ft8b_12k",
}
TOL = 500.0

state = {}   # name -> dict(ssrc, first_drops, last_drops, first_epoch, last_epoch, resets)
t0 = time.time()
dur = float(sys.argv[1]) if len(sys.argv) > 1 else 7200.0
report_every = 300.0
next_report = t0 + report_every

def label_for(freq):
    if freq is None:
        return None
    for f, nm in BY_FREQ.items():
        if abs(freq - f) <= TOL:
            return nm
    return None

def on_status(st):
    global next_report
    nm = label_for(st.frequency)
    if nm is None:
        return
    g, r, sr, dr = st.gps_time, st.rtp_timesnap, st.output_samprate, st.filter_drops
    if g is None or r is None or not sr:
        return
    per = B * (1 << 32) // sr
    ep = ((g + B * (GPS_UTC_OFFSET - GPS_LEAP)) - (B * r) // sr) % per
    s = state.get(nm)
    # New channel, or channel recreated (ssrc changed / counter went backwards)
    if s is None or s["ssrc"] != st.ssrc or (dr is not None and dr < s["last_drops"]):
        resets = 0 if s is None else s["resets"] + 1
        state[nm] = {"ssrc": st.ssrc, "sr": sr, "per": per,
                     "first_drops": dr or 0, "last_drops": dr or 0,
                     "first_epoch": ep, "last_epoch": ep, "resets": resets,
                     "max_step_ms": 0.0}
        print(f"{time.strftime('%H:%M:%S', time.gmtime())} {nm:<14} "
              f"BASELINE ssrc={st.ssrc} sr={sr} drops={dr}", flush=True)
        return
    if dr is not None and dr > s["last_drops"]:
        d_drops = dr - s["last_drops"]
        d_ep = ep - s["last_epoch"]
        if d_ep > s["per"] // 2: d_ep -= s["per"]
        if d_ep < -s["per"] // 2: d_ep += s["per"]
        print(f"{time.strftime('%H:%M:%S', time.gmtime())} {nm:<14} "
              f"DROPS +{d_drops} (total {dr - s['first_drops']})  "
              f"d_epoch={d_ep/1e6:+9.3f} ms  predicted={d_drops*BLOCK_MS:+.1f} ms  "
              f"ratio={(d_ep/1e6)/(d_drops*BLOCK_MS):+.2f}", flush=True)
        s["last_drops"] = dr
    s["last_epoch"] = ep
    if time.time() >= next_report:
        next_report = time.time() + report_every
        print(f"--- {time.strftime('%H:%M:%S', time.gmtime())} SUMMARY "
              f"(elapsed {(time.time()-t0)/60:.0f} min) ---", flush=True)
        for k, v in sorted(state.items()):
            tot = v["last_drops"] - v["first_drops"]
            de = v["last_epoch"] - v["first_epoch"]
            if de > v["per"] // 2: de -= v["per"]
            print(f"      {k:<14} sr={v['sr']:>6} drops={tot:>6} "
                  f"d_epoch={de/1e6:>10.1f} ms  recreates={v['resets']}", flush=True)

c = RadiodControl("AC0G-B4-status.local")
print(f"watching for {dur/3600:.1f} h", flush=True)
c.listen_status(on_status, duration=dur)
print("--- FINAL ---", flush=True)
for k, v in sorted(state.items()):
    tot = v["last_drops"] - v["first_drops"]
    de = v["last_epoch"] - v["first_epoch"]
    if de > v["per"] // 2: de -= v["per"]
    print(f"{k:<14} sr={v['sr']:>6} drops={tot:>6} d_epoch={de/1e6:>10.1f} ms "
          f"recreates={v['resets']}", flush=True)

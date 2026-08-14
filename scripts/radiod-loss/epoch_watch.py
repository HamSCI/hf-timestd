"""radiod block-drop reproduction — isolated instance, all three planes.

HYPOTHESIS
  When a demod thread misses its deadline, filter.c "skip ahead to the oldest
  block still available" discards whole frequency-domain blocks.  audio.c
  advances chan->output.rtp.timestamp only by frames EMITTED, so the discarded
  time is never reflected in the RTP numbering.  Result: the published
  (GPS_TIME, RTP_TIMESNAP) epoch steps by N*Blocktime while the RTP stream
  stays perfectly contiguous and no marker bit is set.

MUTUALLY EXCLUSIVE PREDICTIONS
  radiod block drop : FILTER_DROPS +N ; E_pub steps +N*20 ms ;
                      RTP seq CONTIGUOUS ; RTP timestamp CONTIGUOUS ;
                      wall-clock inter-packet gap of N*20 ms.
  socket/net loss   : RTP seq GAP ; timestamp JUMP ; resequencer fires ;
                      FILTER_DROPS unchanged ; E_pub unchanged.

Emits one CSV row per status sample so the two planes can be joined offline
against the tcpdump capture.
"""
import sys, time, threading
from ka9q.control import RadiodControl

HOST = "239.190.0.11"
GPS_UTC_OFFSET, GPS_LEAP, B = 315964800, 18, 1_000_000_000

def epoch_ns(st):
    g, r, sr = st.gps_time, st.rtp_timesnap, st.output_samprate
    if g is None or r is None or not sr:
        return None, None
    per = B * (1 << 32) // sr
    return ((g + B * (GPS_UTC_OFFSET - GPS_LEAP)) - (B * r) // sr) % per, per

def main():
    ssrc = int(sys.argv[1])
    dur = float(sys.argv[2])
    print("wall_unix,ssrc,epoch_ns,filter_drops,rtp_timesnap,gps_time,output_samprate", flush=True)
    def cb(st):
        if st.ssrc != ssrc:
            return
        ep, _ = epoch_ns(st)
        if ep is None:
            return
        print(f"{time.time():.3f},{st.ssrc},{ep},{st.filter_drops},"
              f"{st.rtp_timesnap},{st.gps_time},{st.output_samprate}", flush=True)
    c = RadiodControl(HOST)
    c.listen_status(cb, duration=dur, ssrcs={ssrc})

main()

"""Measure T6 stream transport latency: arrival wall-clock minus the UTC the
RTP timestamp implies. That difference is what LATENCY_SIGMA_FLOOR_NS bounds."""
import socket, struct, time, statistics as st
from ka9q.control import discover_channels
GRP, PORT, SSRC = "239.28.203.44", 5004, 2072147062
GPS_UTC_OFFSET, GPS_LEAP, B = 315964800, 18, 1_000_000_000
ch = discover_channels("AC0G-B4-status.local", listen_duration=6.0).get(SSRC)
sr = ch.sample_rate
epoch = ((ch.gps_time + B*(GPS_UTC_OFFSET-GPS_LEAP)) - (B*ch.rtp_timesnap)//sr)
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("", PORT))
s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
             struct.pack("4s4s", socket.inet_aton(GRP), socket.inet_aton("0.0.0.0")))
s.settimeout(3)
lat = []
end = time.time() + 30
while time.time() < end:
    try: d, _ = s.recvfrom(65536)
    except socket.timeout: break
    if len(d) < 12: continue
    _, _, ts, ss = struct.unpack("!HHIL", d[:12])
    if ss != SSRC: continue
    arrival = time.time()
    created = (epoch + (B*ts)//sr) / 1e9          # UTC of the FIRST sample in this packet
    lat.append((arrival - created) * 1000.0)
lat = [x for x in lat if -1000 < x < 1000]
lat.sort(); n = len(lat)
print(f"packets: {n}")
print(f"  transport latency ms: min={lat[0]:.2f} p05={lat[int(.05*n)]:.2f} median={st.median(lat):.2f} "
      f"p95={lat[int(.95*n)]:.2f} max={lat[-1]:.2f}")
print(f"  mean={st.mean(lat):.2f}  stdev={st.pstdev(lat):.3f}  IQR={lat[3*n//4]-lat[n//4]:.3f}")
print(f"\n  current LATENCY_SIGMA_FLOOR_NS = 25.000 ms")
print(f"  measured spread (stdev)        = {st.pstdev(lat):.3f} ms")
print(f"  measured p95-p05 span          = {lat[int(.95*n)]-lat[int(.05*n)]:.3f} ms")

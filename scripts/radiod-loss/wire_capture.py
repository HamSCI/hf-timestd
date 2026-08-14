"""Raw RTP wire receiver — the plane never sampled before.

Joins the channel's data multicast group and logs, per packet:
    arrival_unix, seq, rtp_timestamp, marker, payload_bytes

This is the ground truth that separates the two failure modes:

  radiod block drop -> seq CONTIGUOUS, rtp_timestamp CONTIGUOUS,
                       but a wall-clock arrival gap of N*Blocktime.
  socket/net loss   -> seq GAP and rtp_timestamp JUMP.

No root needed (multicast join is unprivileged), so this runs anywhere --
including on the maintainer's machine.
"""
import socket, struct, sys, time

group, port, dur = sys.argv[1], int(sys.argv[2]), float(sys.argv[3])
want_ssrc = int(sys.argv[4]) if len(sys.argv) > 4 else None
iface = sys.argv[5] if len(sys.argv) > 5 else "0.0.0.0"

s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
try:
    s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 64 * 1024 * 1024)
except OSError:
    pass
s.bind(("", port))
mreq = struct.pack("4s4s", socket.inet_aton(group), socket.inet_aton(iface))
s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
s.settimeout(2.0)

# Report the socket buffer actually granted -- if the kernel gave us less than
# asked, receiver-side overflow becomes a live risk and must not be mistaken
# for radiod loss.
print(f"# SO_RCVBUF granted = {s.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)} bytes", flush=True)
print("arrival_unix,seq,rtp_timestamp,marker,payload_bytes", flush=True)

end = time.time() + dur
while time.time() < end:
    try:
        data, _ = s.recvfrom(65536)
    except socket.timeout:
        continue
    if len(data) < 12:
        continue
    b0, b1 = struct.unpack("!BB", data[0:2])
    seq, ts, ssrc = struct.unpack("!HIL", data[2:12])
    if want_ssrc is not None and ssrc != want_ssrc:
        continue
    cc = b0 & 0x0F
    hdr = 12 + 4 * cc
    print(f"{time.time():.6f},{seq},{ts},{1 if (b1 & 0x80) else 0},{len(data)-hdr}", flush=True)

#!/bin/bash
# FILTER_DROPS vs load, with the wire truth alongside it.
# For each channel count: start radiod, settle, measure CPU + FILTER_DROPS +
# actual timeline loss (wall elapsed - RTP advanced) on one monitored channel.
#   $1 = build dir   $2 = tag   $3 = measure seconds
set -u
S=/tmp/claude-0/-root/e1693eaa-ab8b-45c6-b0f6-cead7b801216/scratchpad
BUILD=$1; TAG=$2; MEAS=${3:-120}
V=/root/appliance/repos/hf-timestd/.venv/bin/python3
OUT=$S/ramp_$TAG.csv
echo "channels,cpu_pct,drops,drops_ms,wall_s,rtp_s,wire_loss_s,wire_loss_pct" > $OUT

for N in 1 20 40 60 80 100 120 150; do
  pkill -9 -x radiod 2>/dev/null; sleep 2
  python3 - "$BUILD" "$N" <<'PY'
import sys
W,N=sys.argv[1],int(sys.argv[2])
hdr=open(f"{W}/../radiod@bdtest.conf").read().split("\n[load0]")[0] if False else None
PY
  # regenerate config with N load channels, pointing at this build
  python3 - "$BUILD" "$N" <<'PY'
import sys
B,N=sys.argv[1],int(sys.argv[2])
S="/tmp/claude-0/-root/e1693eaa-ab8b-45c6-b0f6-cead7b801216/scratchpad"
hdr=open(f"{S}/radiod@bdtest.conf").read().split("\n[load0]")[0]
import re
hdr=re.sub(r'library = \S+', f'library = {B}/src/sig_gen.so', hdr)
hdr=re.sub(r'presets-file = \S+', f'presets-file = {B}/share/presets.conf', hdr)
load="".join(f"\n[load{i}]\nmode = iq\nfreq = {9_000_000+i*20_000}\nsamprate = 96000\nssrc = {20000+i}\n" for i in range(N))
open(f"{S}/ramp.conf","w").write(hdr+load)
PY
  setsid nohup taskset -c 4 $BUILD/src/radiod $S/ramp.conf > /tmp/ramp_rd.log 2>&1 </dev/null & disown
  sleep 45
  P=$(ps -eo pid,stat,comm | awk '$3=="radiod" && $2!~/Z/ {print $1; exit}')
  [ -z "${P:-}" ] && { echo "$N,FAILED" >> $OUT; continue; }
  read -r _ _ _ _ _ _ _ _ _ _ _ _ _ u1 s1 _ < /proc/$P/stat
  $V -u $S/wire_rx.py 239.190.0.12 5004 $MEAS 10000 > /tmp/ramp_wire.csv 2>&1 &
  $V -u $S/blockdrop_test.py 10000 $MEAS > /tmp/ramp_status.csv 2>&1 &
  sleep $((MEAS+6))
  read -r _ _ _ _ _ _ _ _ _ _ _ _ _ u2 s2 _ < /proc/$P/stat
  CPU=$(awk -v a=$u1 -v b=$s1 -v c=$u2 -v d=$s2 -v m=$MEAS 'BEGIN{printf "%.1f",((c-a)+(d-b))/100/m*100}')
  $V - "$CPU" "$N" >> $OUT <<'PY'
import sys,csv
cpu,N=sys.argv[1],sys.argv[2]; SR=96000
P=[]
for l in open('/tmp/ramp_wire.csv'):
    if l[0]=='#' or l.startswith('arrival'): continue
    try:
        a,s,t,m,n=l.strip().split(','); P.append((float(a),int(t),int(n)))
    except ValueError: pass
S=[]
for r in csv.DictReader(open('/tmp/ramp_status.csv')):
    try: S.append((float(r['wall_unix']),int(r['filter_drops'])))
    except: pass
if len(P)<10 or len(S)<2:
    print(f"{N},{cpu},,,,,,"); sys.exit()
wall=P[-1][0]-P[0][0]
rtp=((((P[-1][1]-P[0][1])&0xFFFFFFFF))+P[-1][2]//4)/SR
dr=S[-1][1]-S[0][1]
loss=wall-rtp
print(f"{N},{cpu},{dr},{dr*20},{wall:.3f},{rtp:.3f},{loss:.3f},{100*loss/wall:.3f}")
PY
  echo "  N=$N done" >&2
done
pkill -9 -x radiod 2>/dev/null
echo "=== $TAG complete" >&2; cat $OUT >&2

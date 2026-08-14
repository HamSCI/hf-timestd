"""Three-plane analysis of a block-drop capture directory."""
import sys, csv
D=sys.argv[1]; LABEL=sys.argv[2]; SR=96000; BLK_MS=20.0
P=[]
for line in open(f"{D}/wire.csv"):
    if line.startswith('#') or line.startswith('arrival'): continue
    try:
        a,s,t,m,n=line.strip().split(','); P.append((float(a),int(s),int(t),int(m),int(n)))
    except ValueError: pass
S=[]
for r in csv.DictReader(open(f"{D}/status.csv")):
    try: S.append((float(r['wall_unix']),int(r['epoch_ns']),int(r['filter_drops'])))
    except: pass
seq=sum(1 for i in range(1,len(P)) if ((P[i][1]-P[i-1][1])&0xFFFF)!=1)
ts =sum(1 for i in range(1,len(P)) if ((P[i][2]-P[i-1][2])&0xFFFFFFFF)!=P[i-1][4]//4)
mk =sum(p[3] for p in P)
wall=P[-1][0]-P[0][0]
rtp=((((P[-1][2]-P[0][2])&0xFFFFFFFF))+P[-1][4]//4)/SR
drops=S[-1][2]-S[0][2] if S else 0
dep=(S[-1][1]-S[0][1])/1e6 if S else 0
print(f"\n===== {LABEL} =====")
print(f"  packets {len(P)}   status samples {len(S)}")
print(f"  RTP sequence gaps        : {seq}")
print(f"  RTP timestamp anomalies  : {ts}")
print(f"  RTP marker bits set      : {mk}")
print(f"  FILTER_DROPS delta       : {drops}  (= {drops*BLK_MS/1000:.3f} s)")
print(f"  published epoch move     : {dep/1000:+.3f} s")
print(f"  wall elapsed             : {wall:.3f} s")
print(f"  RTP time advanced        : {rtp:.3f} s")
print(f"  >>> RTP FELL BEHIND BY   : {wall-rtp:+.3f} s   ({(wall-rtp)/wall*100:+.2f}% of wall)")
if drops: print(f"  loss accounted by counter: {100*drops*BLK_MS/1000/(wall-rtp):.1f}%" if wall-rtp>0.001 else "")

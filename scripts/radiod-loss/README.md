# radiod loss / timeline diagnostics

The tooling that found and measured the radiod silent block-loss defect
(upstream PR ka9q/ka9q-radio#243), 2026-08-14.

## Why these exist

radiod discards whole `Blocktime` blocks under load. Because RTP timestamps
count samples **emitted** rather than time **elapsed**, the discard leaves no
sequence gap, no timestamp jump and no marker bit — the stream is byte-for-byte
indistinguishable from clean data while everything after the hole is silently
re-dated.

Every instrument we had was blind to it by construction: the T6 LABEL AUDIT
(RTP labels stay contiguous), the RX888 loss monitor (the ADC samples *did*
arrive), the measured-rate line (front-end count unaffected), and radiod's own
`FILTER_DROPS` (only one of two skip paths increments it — it reported 6–15% of
real loss).

The only honest measure is the **wire**: wall-clock elapsed vs RTP time
advanced. That is what `wire_capture.py` exists for.

## The tools

| script | plane | what it measures |
|---|---|---|
| `wire_capture.py` | data | joins the channel's multicast group, logs arrival / seq / RTP timestamp / marker / length per packet. Unprivileged — no root, no tcpdump. |
| `epoch_watch.py` | status | radiod's published `(GPS_TIME, RTP_TIMESNAP)` epoch + `FILTER_DROPS` for one SSRC, to CSV |
| `drop_watch.py` | status | per-channel `FILTER_DROPS` + epoch across many channels, with channel-recreation detection |
| `analyse.py` | both | joins a capture dir: seq gaps, timestamp anomalies, marker bits, wall vs RTP, and the share of loss the counter explains |
| `transport_latency.py` | data | arrival wall-clock minus the UTC the RTP timestamp implies — the quantity `NativeAnchorBench.LATENCY_SIGMA_FLOOR_NS` bounds |
| `load_ramp.sh` + `add_load_channels.py` | both | walks channel count, recording CPU / reported loss / actual loss at each point |
| `radiod-vm-fence.{sh,service}` | host | keeps qemu's non-vCPU threads off radiod's isolated CPU pair and holds KSM off (Proxmox hosts) |

## Reading the results

- **seq gaps > 0** → packets lost to the network or socket buffer. Not this defect.
- **seq contiguous, timestamp contiguous, but wall > RTP advanced** → radiod
  discarded blocks and re-dated the stream. This defect.
- **`FILTER_DROPS × Blocktime` ≪ (wall − RTP advanced)** → the uncounted skip path.

Worked example (real RX888, 129.6 MS/s, deliberately over capacity, 300 s):
0 seq gaps, 0 timestamp anomalies, 0 marker bits, wall 299.871 s, RTP advanced
104.840 s — **195.031 s of timeline gone, silently**. Patched: −0.012 s.

## Caveats

- `load_ramp.sh` assumes a `sig_gen` radiod instance and rewrites its config per
  step; it is not for production receivers.
- Onset channel counts do not transfer between hosts — front-end rate, cache and
  isolation all move it. The *shape* and the counter divergence do.
- A capture left running across a radiod restart silently merges two instances'
  streams; truncate at the RTP-timestamp reset before analysing.
- Several scripts carry hardcoded SSRCs / multicast groups from the B4 and bee1
  investigations; check them before reuse.

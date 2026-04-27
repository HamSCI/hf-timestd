# core-recorder: 9 sockets → 1 MultiStream

## Context

`timestd-core-recorder.service` currently opens **one ka9q-python
`RadiodStream` per archive channel + one for the L6 BPSK PPS calibrator**
— ~10 UDP sockets all bound to `0.0.0.0:5004` and joined to the same
radiod multicast group. Per Linux kernel semantics, each socket joining
the same multicast group receives its own clone of every packet, so
core-recorder is processing roughly **N× the radiod firehose** through
a single Python GIL-locked thread (~270 MB/s observed on bee1 with
N=9 sockets at 24 kHz IQ each).

Diagnosed live on bee1 2026-04-27:

- core-recorder's 9 sockets: 49,609,334 cumulative drops, several at
  80%+ of their 16 MiB per-socket buffer.
- radiod itself pinned/scheduled to CPU 1 at 130% CPU; CPU 1 at 0% idle.
- hfdl-recorder's resequencer reports ~2 "Lost packet recovery" events/s
  with mean gap = 70 875 samples (~320 ms) — **ALL clients see these
  gaps because they originate at the radiod source, starved by softirq
  pressure on CPU 1.**

The 9-socket pattern is **historical, not load-bearing** (operator-
confirmed). Replacing it with a single `MultiStream` subscription that
demuxes by SSRC drops kernel multicast-fanout work for core-recorder
from N× to 1×, freeing CPU 1 softirq cycles for radiod. **Timing
precision is paramount and must be fully preserved.**

## Architecture decision

**Adopt:** `CoreRecorderV2` owns one shared `MultiStream`. Each
`StreamRecorderV2` keeps its public shape (config, archive writer,
ring buffer, health monitor, `start()`/`stop()` API), but its `start()`
no longer creates a `RadiodStream` — it just performs `ensure_channel`,
seeds timing, and registers its `_handle_samples` callback with the
parent `MultiStream`. The L6 BPSK PPS channel registers an additional
callback on the same `MultiStream`. `CoreRecorderV2` starts the
`MultiStream` exactly once after every channel is added and stops it
exactly once at shutdown.

**Reject:** custom RTPRecorder + manual SSRC demux. Reinvents work
ka9q-python already does in `MultiStream`, and we'd have to maintain
the per-channel `PacketResequencer` / `StreamQuality` / batched
delivery machinery ourselves.

**Reject:** keeping per-channel `RadiodStream`s and only consolidating
the L6 channel. Doesn't address the 9× fanout that's actually causing
CPU 1 starvation.

### Timing-precision preservation

`MultiStream` and `RadiodStream` deliver `on_samples(samples, quality)`
identically per slot — same `~10-packet` batch interval, same
`quality.last_rtp_timestamp` semantics. Per `ka9q-python/multi_stream.py`
[L305–310](../../ka9q-python/ka9q/multi_stream.py#L305) each
`_ChannelSlot` updates its own `quality.last_rtp_timestamp = header.timestamp`
on every packet; the callback is invoked at the same cadence as
`RadiodStream`. **No precision is lost.**

The two precision-critical paths in this service are unaffected:

1. **`_l6_on_samples` PPS calibration** ([core_recorder_v2.py:645](../src/hf_timestd/core/core_recorder_v2.py#L645))
   reads `quality.last_rtp_timestamp` per batch — identical under
   `MultiStream`.
2. **Per-channel timing seed** ([stream_recorder_v2.py:384–410](../src/hf_timestd/core/stream_recorder_v2.py#L384))
   reads `gps_time_ns` and `rtp_timesnap` from `channel_info` returned
   by `ensure_channel()` — independent of which receive abstraction
   we use. Stays exactly as-is.

## Files modified

- `src/hf_timestd/core/core_recorder_v2.py` — owns the shared
  `MultiStream`, registers each `StreamRecorderV2` and the L6
  callback on it, starts it once.
- `src/hf_timestd/core/stream_recorder_v2.py` — `start()` no longer
  creates a `RadiodStream`; instead exposes a `register_with(multi)`
  method that calls `multi.add_channel(...)`. `stop()` no longer
  stops the stream (parent owns it); just finalizes archive + ring.
  Health monitor's "stream dead" branch becomes "ask parent to
  re-add this channel" instead of recreating the per-channel socket.

## Sequenced implementation

Each step gates on the verification before moving on. Service can be
stopped between steps.

### Step 1 — Add a `register_with(multi)` method to `StreamRecorderV2`

- New method that does what `_create_channel()` does today minus the
  `RadiodStream` creation: calls `ensure_channel()`, captures
  `channel_info`, seeds archive writer + ring buffer with `gps_time_ns`
  / `rtp_timesnap`, then calls
  `multi.add_channel(frequency_hz=…, preset=…, sample_rate=…,
  encoding=…, on_samples=self._handle_samples,
  on_stream_dropped=…, on_stream_restored=…)`.
- Leave the old `start()` / `_create_channel()` paths in place for now.
- Unit test: a `StreamRecorderV2` instance can be `register_with`'d
  against a fake `MultiStream`-shaped mock and the per-channel timing
  seed runs exactly once.

### Step 2 — Have `CoreRecorderV2._initialize_channels()` build a shared `MultiStream`

- After the existing `for ch_spec in self.channel_specs` loop, instead
  of constructing each `StreamRecorderV2` and letting it self-start,
  build one `multi = MultiStream(control=self.control)` and call
  `recorder.register_with(multi)` for each.
- Save `multi` on `self._multi`.
- Don't call `multi.start()` yet — keep that for the run/serve path.
- Unit test: with a stubbed `MultiStream`, `_initialize_channels()`
  produces the right number of `add_channel` calls.

### Step 3 — Migrate the L6 BPSK PPS channel onto the shared `MultiStream`

- `_start_l6_stream()` ([core_recorder_v2.py:607](../src/hf_timestd/core/core_recorder_v2.py#L607))
  drops the `RadiodStream(channel=channel_info, …)` construction. It
  instead calls `self._multi.add_channel(frequency_hz=…, preset='iq',
  sample_rate=…, encoding=Encoding.F32, on_samples=self._l6_on_samples)`.
- Verification: `quality.last_rtp_timestamp` still arrives in the
  callback (smoke check at INFO level).

### Step 4 — Start the `MultiStream` once, after all channels added

- In whichever method begins receiving (likely `run()` or the start
  flow), call `self._multi.start()` after `_initialize_channels()` and
  `_start_l6_stream()`. Verify `READY=1` sd_notify still fires at the
  right point.

### Step 5 — Stop tearing down per-channel streams in `StreamRecorderV2.stop()`

- Remove the `self.stream.stop()` call ([stream_recorder_v2.py:598](../src/hf_timestd/core/stream_recorder_v2.py#L598)).
- Keep the archive-writer flush + final-quality return. The final
  quality comes from the per-slot `quality` object on the parent
  `MultiStream`'s `_ChannelSlot`.
- `CoreRecorderV2.shutdown()` calls `self._multi.stop()` exactly once
  after all `StreamRecorderV2.stop()`s have flushed.

### Step 6 — Re-route the health monitor's "stream dead" recovery

- Today: `_health_monitor_loop` detects a stale stream and calls
  `_create_channel()` to rebuild its own `RadiodStream`.
- New: detect a stale slot (no `last_packet_utc` advance) and call
  `self._multi.remove_channel(ssrc)` followed by `self.register_with(self._parent_multi)`.
- ka9q-python's MultiStream-level health monitor handles "socket died /
  radiod restarted" globally; the per-channel one becomes "this slot
  isn't producing — re-provision it" — narrower job.

### Step 7 — End-to-end verification on bee1 (service stopped first)

```bash
sudo systemctl stop timestd-core-recorder.service
sudo systemctl restart timestd-core-recorder.service   # post-deploy
sudo ss -uan -p | grep timestd-core-recorder | wc -l   # expect: 1, was: 9
nstat -az UdpRcvbufErrors                              # rate-of-change should fall sharply
```

Then verify the timing chain end-to-end:

- `journalctl -u timestd-core-recorder -f` shows per-channel
  "Seeded timing from channel_info" lines for all archive channels.
- `journalctl -u timestd-core-recorder -f` shows the
  "L6 BPSK PPS LOCKED: chain_delay=…" line within ~30 s of restart.
- `ls /var/lib/timestd/raw_buffer/<channel>/*.bin.zst` shows the same
  10-min cadence as before.
- `journalctl -u timestd-metrology@CHU_7850 -n 50` shows L1A tone
  detection still producing measurements with the same precision band
  (sub-50 µs per METROLOGY.md §4.3).
- `journalctl -u timestd-fusion -n 50` shows Chrony SHM still being
  fed at the configured cadence.
- hfdl-recorder's resequencer rate (`/var/log/hfdl-recorder/*.log`)
  drops measurably as a side-effect — direct evidence that radiod's
  CPU 1 pressure relaxed.

## Risks & rollback

| Risk | Likelihood | Mitigation |
|---|---|---|
| Single-socket failure stops all channels | low (psk-recorder runs 65 channels on one MultiStream in production) | MultiStream's internal health monitor + ka9q-python reconnect logic; systemd `Restart=on-failure` for the unit |
| Re-seeding timing on radiod restart breaks across the SSRC-demux boundary | medium (the hairy part) | `register_with()` path explicitly re-runs `ensure_channel()` and re-seeds — same logic as `_create_channel()` today, just gated on `multi.remove_channel/add_channel` round-trip |
| Per-channel back-pressure interferes with others on shared callback thread | low (callbacks complete quickly: numpy→bytes→queue) | Profile if drops appear; archive writer already runs writes async via ring buffer |
| L6 calibrator timing slips on the shared callback thread | low (PPS is per-second, callback batch is ~10 ms) | Smoke test L6 LOCKED message after restart; if jitter increases, fall back to L6-on-its-own-MultiStream |

**Rollback:** keep the old `_create_channel()` and `_start_l6_stream()`
paths (don't delete) and add a `recorder.legacy_per_channel_streams =
true` config flag. If verification step 7 fails, set the flag and
restart — previous behavior. Remove the flag in a follow-up commit
once production has run cleanly for a week.

## Success criteria

1. `sudo ss -uan -p | grep timestd-core-recorder` shows **1 UDP socket**
   (previously 9 + L6).
2. `nstat UdpRcvbufErrors` rate-of-change drops at least an order of
   magnitude system-wide (the timestd contribution was ~30 k/s).
3. `journalctl -u timestd-metrology@*` shows no precision regression
   over a 24 h sample (compare L1A tone-edge std-dev pre vs. post).
4. `timestd-fusion` continues to drive Chrony SHM at the same cadence;
   `chronyc sources` shows no degradation in the timestd refclock
   stratum.
5. `hfdl-recorder`'s resequencer "Lost packet recovery" rate falls —
   independent confirmation that radiod source-side pressure relaxed.

## Critical files to modify

- [src/hf_timestd/core/core_recorder_v2.py](../src/hf_timestd/core/core_recorder_v2.py) — `_initialize_channels`, `_start_l6_stream`, `run`, `shutdown`
- [src/hf_timestd/core/stream_recorder_v2.py](../src/hf_timestd/core/stream_recorder_v2.py) — add `register_with`, deprecate inline `RadiodStream` creation in `start`/`_create_channel`, narrow `stop`
- [tests/](../tests/) — at least one new test asserting one MultiStream is created, all add_channel calls happen before start, and per-channel timing seed still runs

"""Stage 0 probe: clone the T6 channel and every HF channel onto the TS-1 pilot.

⛔ This tool CREATES radiod channels.  Run it only as an announced operator
step (claude-bus message first, owner's approval), never from a timer or a
test against a live radiod.  Every clone carries a LIFETIME, so radiod removes
a clone that a crash leaves behind, and `run` removes each clone in `finally`.

Recommended operator run: `--duration-s 900`.  600 s is too thin to collect 15
edges on every clone.

Each clone tunes to the pilot, so every clone sees the same BPSK edges through
its own filter.  The probe streams each clone's samples into a BpskEdgeFineStage,
polls each clone's status to learn its counter map, and hands the captures to
`hf_chain_delay_analysis.analyze`.  No clock is read for timing: the monotonic
clock only paces the polling loop, and nothing here reaches chrony.
"""
from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Deque, Dict, List, Optional, Sequence, Tuple

from hamsci_dsp.counter_map import CounterMap, CounterPoll

from .bpsk_edge_fine_stage import BpskEdgeFineStage
from .hf_chain_delay_analysis import EdgeObservation, ProbeChannelCapture

logger = logging.getLogger(__name__)

REFERENCE_LABEL = "T6_REF"
MAP_POLLS = 5            # each map comes from the most recent 5 accepted polls
MAP_MIN_AGREE = 3        # 3 of 5 is a strict majority; a tie never picks a side
POLL_TIMEOUT_S = 2.0
GRANT_TRIES = 10         # polls a fresh clone gets to answer before the run aborts
# Encodings ka9q-python decodes to samples.  Anything else cannot feed the fine stage.
DECODABLE_ENCODINGS = (1, 2, 4, 8)   # S16LE, S16BE, F32LE, F32BE


@dataclass(frozen=True)
class ChannelGrant:
    """What radiod granted a clone, read from its own status reply."""
    encoding: int
    address: str
    port: int


@dataclass(frozen=True)
class ProbeChannelSpec:
    label: str
    ssrc: int
    frequency_hz: float
    preset: str
    sample_rate: int
    low_edge_hz: Optional[float]
    high_edge_hz: Optional[float]


def _batch_rtp(quality) -> int:
    """RTP of the first delivered sample: the resequencer's own label, else the
    last received header."""
    rtp = getattr(quality, "delivered_rtp_start", None)
    return int(rtp if rtp is not None else quality.last_rtp_timestamp)


def _opt(x) -> Optional[float]:
    return None if x is None else float(x)


def ka9q_stream_factory() -> Callable:
    """Real stream factory: one RadiodStream per clone, built from the encoding
    and destination radiod granted (it can downgrade F32 to S16 on a wide,
    high-rate channel).  Nothing opens until the stream's start()."""
    from ka9q import RadiodStream
    from ka9q.discovery import ChannelInfo

    def factory(spec: ProbeChannelSpec, on_samples, grant: ChannelGrant):
        info = ChannelInfo(
            ssrc=spec.ssrc, preset=spec.preset, sample_rate=spec.sample_rate,
            frequency=spec.frequency_hz, snr=float("-inf"),
            multicast_address=grant.address, port=grant.port,
            encoding=grant.encoding)
        return RadiodStream(channel=info, on_samples=on_samples,
                            samples_per_packet=200, resequence_buffer_size=256)

    return factory


class HfChainDelayProbe:
    SSRC_BASE = 0x7E5C_0000
    # Unit: radiod main-loop FRAMES (about 20 ms each), so 1800 is about 36 s,
    # not 30 minutes.  Every poll_status refreshes it, so a clone lives while the
    # probe polls and vanishes about 36 s after a crash.
    LIFETIME_S = 1800
    ENCODING_F32 = 4

    def __init__(self, control, stream_factory, fine_stage_factory=BpskEdgeFineStage,
                 fold_seconds: int = 30, poll_interval_s: float = 1.0,
                 mono_fn=time.monotonic, sleep_fn=time.sleep):
        self._control = control
        self._stream_factory = stream_factory
        self._fine_factory = fine_stage_factory
        self._fold_seconds = fold_seconds
        self._poll_interval_s = poll_interval_s
        self._mono = mono_fn
        self._sleep = sleep_fn

    @staticmethod
    def plan_channels(config: dict) -> List[ProbeChannelSpec]:
        timing = config.get("timing", {}) or {}
        t6 = timing.get("t6_pps")
        if t6 is None:
            t6 = timing.get("l6_pps")
        t6 = t6 or {}
        try:
            pilot = float(t6.get("frequency_hz") or 0)
        except (TypeError, ValueError):
            pilot = 0.0
        if not t6.get("enabled") or pilot <= 0:
            raise ValueError("no TS-1 pilot: [timing.t6_pps] needs enabled = true "
                             "and a frequency_hz above 0")

        def spec(i: int, label: str, preset, rate, low, high) -> ProbeChannelSpec:
            return ProbeChannelSpec(
                label=label, ssrc=HfChainDelayProbe.SSRC_BASE + i, frequency_hz=pilot,
                preset=str(preset), sample_rate=int(rate),
                low_edge_hz=_opt(low), high_edge_hz=_opt(high))

        specs = [spec(0, REFERENCE_LABEL, t6.get("preset", "iq"),
                      t6.get("sample_rate", 96_000), t6.get("low_edge_hz"),
                      t6.get("high_edge_hz"))]
        group = ((config.get("recorder", {}) or {}).get("channel_group", {}) or {}
                 ).get("timestd", {}) or {}
        for ch in group.get("channels", []):
            merged = {k: v for k, v in group.items() if k != "channels"}
            merged.update(ch)
            label = merged.get("description") or f"{merged['frequency_hz']}"
            specs.append(spec(len(specs), str(label), merged.get("preset", "iq"),
                              merged.get("sample_rate", 24_000),
                              merged.get("low_edge_hz"), merged.get("high_edge_hz")))
        return specs

    # ---- one status reply -> one poll, or nothing -------------------------

    @staticmethod
    def _accept(reply, spec: ProbeChannelSpec) -> Optional[CounterPoll]:
        """radiod answers a ghost SSRC with frontend status alone.  A reply
        without a pair, or from another output rate, is not a poll."""
        fe = getattr(reply, "frontend", None)
        gps = getattr(reply, "gps_time", None)
        rtp = getattr(reply, "rtp_timesnap", None)
        n_in = getattr(fe, "input_samples", None)
        f_in = getattr(fe, "input_samprate", None)
        ilen = getattr(reply, "filter_blocksize", None)
        if not gps or rtp is None or n_in is None or not f_in or not ilen:
            return None
        if getattr(reply, "output_samprate", None) != spec.sample_rate:
            return None
        return CounterPoll(rtp_timesnap=int(rtp), input_samples=int(n_in),
                           filter_blocksize=int(ilen), input_samprate=int(f_in),
                           output_samprate=int(spec.sample_rate), gps_time_ns=int(gps))

    def run(self, specs: Sequence[ProbeChannelSpec], duration_s: float
            ) -> Tuple[List[ProbeChannelCapture], List[float]]:
        edges: Dict[int, List[EdgeObservation]] = {s.ssrc: [] for s in specs}
        recent: Dict[int, Deque[CounterPoll]] = {
            s.ssrc: deque(maxlen=MAP_POLLS) for s in specs}
        last_reply: Dict[int, object] = {}
        ref = specs[0] if specs else None
        pair_epochs: List[float] = []
        streams: list = []
        created: List[int] = []

        def take(spec, reply) -> Optional[CounterPoll]:
            poll = self._accept(reply, spec)
            if poll is None:
                return None
            recent[spec.ssrc].append(poll)
            last_reply[spec.ssrc] = reply
            if spec is ref:
                pair_epochs.append(poll.gps_time_ns / 1e9
                                   - poll.input_samples / poll.input_samprate)
            return poll

        try:
            for s in specs:                  # never retune someone's live channel
                try:
                    reply = self._control.poll_status(s.ssrc, POLL_TIMEOUT_S)
                except Exception:  # noqa: BLE001 - no answer: the SSRC is free
                    continue
                if getattr(reply, "output_samprate", None) and getattr(reply, "frequency", None):
                    raise ValueError(f"SSRC {s.ssrc:#x} ({s.label}) is already a live "
                                     f"channel on radiod; refusing to take it over")
            for s in specs:
                created.append(s.ssrc)       # remove even if this create half-landed
                self._control.create_channel(
                    frequency_hz=s.frequency_hz, preset=s.preset,
                    sample_rate=s.sample_rate, ssrc=s.ssrc,
                    lifetime=self.LIFETIME_S, low_edge=s.low_edge_hz,
                    high_edge=s.high_edge_hz, encoding=self.ENCODING_F32)
            for s in specs:
                grant = self._await_grant(s, take)
                stage = self._fine_factory(s.sample_rate, fold_seconds=self._fold_seconds)
                stream = self._stream_factory(
                    s, self._make_callback(s, stage, edges[s.ssrc]), grant)
                streams.append(stream)
                stream.start()

            t0 = self._mono()
            try:
                while True:
                    for s in specs:
                        try:
                            reply = self._control.poll_status(s.ssrc, POLL_TIMEOUT_S)
                        except Exception as exc:  # noqa: BLE001 - a lost poll is not fatal
                            logger.warning("poll of %s failed: %s", s.label, exc)
                            continue
                        take(s, reply)
                    self._sleep(self._poll_interval_s)
                    if self._mono() - t0 >= duration_s:
                        break
            except KeyboardInterrupt:
                logger.warning("interrupted: reporting what was gathered")
        finally:
            for stream in streams:
                try:
                    stream.stop()
                except Exception:  # noqa: BLE001
                    logger.exception("stopping a probe stream failed")
            for ssrc in created:
                try:
                    self._control.remove_channel(ssrc)
                except Exception:  # noqa: BLE001
                    logger.exception("removing probe clone %#x failed; radiod will "
                                     "remove it when its LIFETIME runs out, about "
                                     "36 s after the last poll", ssrc)

        captures = [self._capture(s, edges[s.ssrc], recent[s.ssrc],
                                  last_reply.get(s.ssrc)) for s in specs]
        return captures, pair_epochs

    def _await_grant(self, spec: ProbeChannelSpec, take) -> ChannelGrant:
        """Poll a fresh clone until radiod answers, and read what it granted."""
        for _ in range(GRANT_TRIES):
            try:
                reply = self._control.poll_status(spec.ssrc, POLL_TIMEOUT_S)
            except Exception as exc:  # noqa: BLE001
                logger.warning("poll of %s failed: %s", spec.label, exc)
                reply = None
            if reply is not None and take(spec, reply) is not None:
                enc = getattr(reply, "output_encoding", None)
                dest = getattr(reply, "output_data_dest_socket", None) or {}
                if enc not in DECODABLE_ENCODINGS:
                    raise RuntimeError(
                        f"clone {spec.label} (SSRC {spec.ssrc:#x}): radiod granted "
                        f"encoding {enc}, which the fine stage cannot decode")
                if not dest.get("address") or not dest.get("port"):
                    raise RuntimeError(f"clone {spec.label} (SSRC {spec.ssrc:#x}): "
                                       f"radiod reported no data destination")
                return ChannelGrant(int(enc), str(dest["address"]), int(dest["port"]))
            self._sleep(self._poll_interval_s)
        raise RuntimeError(f"clone {spec.label} (SSRC {spec.ssrc:#x}) never answered "
                           f"a status poll")

    @staticmethod
    def _make_callback(spec, stage, sink: List[EdgeObservation]):
        def on_samples(samples, quality):
            est = stage.process_samples(samples, _batch_rtp(quality))
            if est is not None:
                sink.append(EdgeObservation(int(est.edge_rtp),
                                            float(est.edge_subsample),
                                            float(est.fit_rms)))
        return on_samples

    @staticmethod
    def _capture(spec: ProbeChannelSpec, edges: List[EdgeObservation],
                 polls: Sequence[CounterPoll], reply) -> ProbeChannelCapture:
        cmap = CounterMap.from_polls(list(polls), min_agree=MAP_MIN_AGREE)
        fe = getattr(reply, "frontend", None)
        f2 = getattr(getattr(reply, "filter2", None), "fir_length", None)
        low = spec.low_edge_hz if spec.low_edge_hz is not None else getattr(reply, "low_edge", None)
        high = spec.high_edge_hz if spec.high_edge_hz is not None else getattr(reply, "high_edge", None)
        return ProbeChannelCapture(
            label=spec.label, preset=spec.preset, output_samprate=spec.sample_rate,
            low_edge_hz=float(low or 0.0), high_edge_hz=float(high or 0.0),
            counter_map=cmap, edges=tuple(edges),
            # With no accepted reply the FIR is unknown; 1 gives a derived delay
            # of 0, and the row reads "no_counter_map" regardless.
            fir_length=int(getattr(reply, "filter_fir_length", None) or 1),
            input_samprate=int(getattr(fe, "input_samprate", None) or 1),
            filter2_fir_length=int(f2) if f2 else None)

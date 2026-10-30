"""Stage 0: where does the HF chain delay lie?

Every HF channel, plus the T6 channel, is cloned onto the TS-1 pilot.  Each
clone sees the same BPSK edges.  Through that clone's own counter map
(`hamsci_dsp.counter_map.CounterMap`) each edge lands on radiod's absolute
input-sample index, so clones with different RTP origins compare directly.
The edge's phase inside the input second, in input samples, is the quantity
compared.  The reference clone (T6) fixes the zero.

Pure analysis.  Every time here counts GPS seconds: no leap table, no clock
read, nothing reaches chrony.

Signs, in words:
  delay_vs_ref_ms         clone phase minus reference phase.  Positive means
                          the clone's edges sit later on the input axis.
  derived_vs_ref_ms       derived delay of the clone minus derived delay of the
                          reference, from the FIR lengths alone.
  link_ms                 radiod pair minus PPS edge.  Positive means the pair
                          labels a sample later than the PPS says.
  link_minus_derived_ms   link_ms minus the reference's derived delay.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import median
from typing import Dict, List, Optional, Sequence, Tuple

from hamsci_dsp.counter_map import CounterMap


@dataclass(frozen=True)
class EdgeObservation:
    edge_rtp: int            # RTP of the block holding the edge, in the clone's counter
    edge_subsample: float    # output samples past edge_rtp
    fit_rms: float


@dataclass(frozen=True)
class ProbeChannelCapture:
    label: str
    preset: str
    output_samprate: int
    low_edge_hz: float
    high_edge_hz: float
    counter_map: Optional[CounterMap]
    edges: Tuple[EdgeObservation, ...]
    fir_length: int
    input_samprate: int
    filter2_fir_length: Optional[int] = None


@dataclass(frozen=True)
class ConfigDelay:
    label: str
    n_edges: int
    delay_vs_ref_ms: Optional[float]
    u_ms: Optional[float]
    derived_ms: float
    derived_vs_ref_ms: float
    verdict: str


@dataclass(frozen=True)
class HfChainDelayReport:
    reference_label: str
    rows: Tuple[ConfigDelay, ...]
    clone_agreement_input_samples: Optional[float]
    link_ms: Optional[float]
    link_minus_derived_ms: Optional[float]
    n_pair_polls: int
    learned_chain_delay_ms: Optional[float]


def derived_filter_delay_s(fir_length: int, input_samprate: int,
                           filter2_fir_length: Optional[int] = None,
                           output_samprate: Optional[int] = None) -> float:
    """Group delay of a linear-phase FIR chain: half the span of each filter,
    the first at the input rate, the second at the output rate."""
    delay = (fir_length - 1) / 2 / input_samprate
    if filter2_fir_length is not None:
        if not output_samprate:
            raise ValueError("filter2_fir_length needs output_samprate")
        delay += (filter2_fir_length - 1) / 2 / output_samprate
    return delay


def _phases(cap: ProbeChannelCapture) -> List[float]:
    """Each edge's phase in input samples within the input second."""
    cm = cap.counter_map
    scale = cm.ilen / cm.olen
    return [(cm.input_index(e.edge_rtp) + e.edge_subsample * scale) % cm.input_samprate
            for e in cap.edges]


def _wrap(x: float, period: float) -> float:
    return (x + period / 2) % period - period / 2


def _centre(phases: Sequence[float], period: float) -> float:
    """Circular median: the circular mean, then the median of wrapped offsets."""
    ang = [2 * math.pi * p / period for p in phases]
    mean = math.atan2(sum(math.sin(a) for a in ang),
                      sum(math.cos(a) for a in ang)) * period / (2 * math.pi)
    return (mean + median(_wrap(p - mean, period) for p in phases)) % period


def _u_samples(phases: Sequence[float], centre: float, period: float) -> float:
    dev = [_wrap(p - centre, period) for p in phases]
    mad = median(abs(d - median(dev)) for d in dev)
    return 1.4826 * mad / math.sqrt(len(phases))


def _derived_s(cap: ProbeChannelCapture) -> float:
    return derived_filter_delay_s(cap.fir_length, cap.input_samprate,
                                  cap.filter2_fir_length, cap.output_samprate)


def analyze(captures: Sequence[ProbeChannelCapture], reference_label: str,
            pair_epochs_gps_s: Sequence[float], learned_chain_delay_ms: Optional[float],
            tolerance_ms: float = 0.1) -> HfChainDelayReport:
    ref = next((c for c in captures if c.label == reference_label), None)
    if ref is None:
        raise ValueError(f"no capture labelled {reference_label!r}")
    period_of = {}
    centres: Dict[str, float] = {}
    us: Dict[str, float] = {}
    usable: Dict[str, ProbeChannelCapture] = {}
    for c in captures:
        if c.counter_map is None or not c.edges:
            continue
        ph = _phases(c)
        period = c.counter_map.input_samprate
        period_of[c.label] = period
        centres[c.label] = _centre(ph, period)
        us[c.label] = _u_samples(ph, centres[c.label], period)
        usable[c.label] = c

    ref_derived_ms = _derived_s(ref) * 1e3
    ref_ok = reference_label in usable
    rows: List[ConfigDelay] = []
    for c in captures:
        derived_ms = _derived_s(c) * 1e3
        derived_vs = derived_ms - ref_derived_ms
        n = len(c.edges)
        if c.counter_map is None:
            rows.append(ConfigDelay(c.label, n, None, None, derived_ms, derived_vs,
                                    "no_counter_map"))
        elif not c.edges:
            rows.append(ConfigDelay(c.label, 0, None, None, derived_ms, derived_vs,
                                    "no_edges"))
        elif c.label == reference_label:
            rows.append(ConfigDelay(c.label, n, 0.0, us[c.label] / c.input_samprate * 1e3,
                                    derived_ms, 0.0, "reference"))
        elif not ref_ok:
            rows.append(ConfigDelay(c.label, n, None, None, derived_ms, derived_vs,
                                    "no_reference"))
        else:
            period = period_of[c.label]
            delay_ms = _wrap(centres[c.label] - centres[reference_label],
                             period) / c.input_samprate * 1e3
            u_ms = us[c.label] / c.input_samprate * 1e3
            ok = abs(delay_ms - derived_vs) <= tolerance_ms
            rows.append(ConfigDelay(c.label, n, delay_ms, u_ms, derived_ms, derived_vs,
                                    "derivation_holds" if ok else "derivation_fails"))

    # Clones with one configuration must agree whatever their origins.
    groups: Dict[tuple, List[str]] = {}
    for label, c in usable.items():
        groups.setdefault((c.preset, c.output_samprate, c.low_edge_hz, c.high_edge_hz),
                          []).append(label)
    spreads = []
    for labels in groups.values():
        if len(labels) < 2:
            continue
        period = period_of[labels[0]]
        mid = _centre([centres[l] for l in labels], period)
        dev = [_wrap(centres[l] - mid, period) for l in labels]
        spreads.append(max(dev) - min(dev))
    agreement = max(spreads) if spreads else None

    link_ms = link_minus_derived = None
    if ref_ok and pair_epochs_gps_s:
        f_in = ref.counter_map.input_samprate
        floor = min(pair_epochs_gps_s)
        t_phase = centres[reference_label] / f_in
        second = round(floor + t_phase)          # GPS second the edge belongs to
        pps_epoch = second - t_phase
        link_ms = (floor - pps_epoch) * 1e3
        link_minus_derived = link_ms - ref_derived_ms

    return HfChainDelayReport(
        reference_label=reference_label, rows=tuple(rows),
        clone_agreement_input_samples=agreement, link_ms=link_ms,
        link_minus_derived_ms=link_minus_derived,
        n_pair_polls=len(pair_epochs_gps_s),
        learned_chain_delay_ms=learned_chain_delay_ms)


def _f(x: Optional[float], spec: str = "+.4f") -> str:
    return "n/a" if x is None else format(x, spec)


def format_report(report: HfChainDelayReport) -> str:
    lines = [f"HF chain delay against {report.reference_label} (ms, input axis)",
             f"{'config':<16}{'edges':>6}{'delay':>11}{'u':>9}{'derived':>10}"
             f"{'d-vs-ref':>10}  verdict"]
    for r in report.rows:
        lines.append(f"{r.label:<16}{r.n_edges:>6}{_f(r.delay_vs_ref_ms):>11}"
                     f"{_f(r.u_ms, '.4f'):>9}{r.derived_ms:>10.4f}"
                     f"{r.derived_vs_ref_ms:>+10.4f}  {r.verdict}")
    lines.append(f"clone agreement (input samples): "
                 f"{_f(report.clone_agreement_input_samples, '.2f')}")
    lines.append(f"link (radiod pair − PPS edge): {_f(report.link_ms)} ms "
                 f"({report.n_pair_polls} pair polls); "
                 f"minus derived {_f(report.link_minus_derived_ms)} ms")
    lines.append(f"learned T6 chain delay: {_f(report.learned_chain_delay_ms)} ms")
    return "\n".join(lines) + "\n"

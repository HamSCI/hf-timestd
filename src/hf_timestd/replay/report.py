"""Aggregate replay verdicts, and diff them against what was deployed."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Iterable

from hf_timestd.core.admission_cascade import AdmissionState, ChannelState


@dataclass
class ReplayReport:
    state_counts: Counter = field(default_factory=Counter)
    by_hour: Dict[int, Counter] = field(default_factory=lambda: defaultdict(Counter))
    # Per-channel-minute layer, computed by the cascade alongside
    # state_counts: CHANNEL_UNIDENTIFIED is "the state that protects us"
    # (spec) and must not be thrown away between the cascade and the
    # report.
    channel_states: Counter = field(default_factory=Counter)
    unclaimed_arrivals: int = 0
    deployed_over_reports: int = 0
    deployed_under_reports: int = 0
    geometry_refused: int = 0
    skipped_null_snr_total: int = 0
    minutes: int = 0

    def render(self) -> str:
        lines = [f"minutes replayed: {self.minutes}", "", "state counts:"]
        total = sum(self.state_counts.values()) or 1
        for state, n in self.state_counts.most_common():
            lines.append(f"  {state.value:<14} {n:8d}  {100*n/total:5.1f}%")
        # Every state prints, including zero: most_common() over a Counter
        # silently omits a state that never fired, and a reader cannot
        # tell "zero" from "not tracked".
        seen = set(self.state_counts)
        for state in AdmissionState:
            if state not in seen:
                lines.append(f"  {state.value:<14} {0:8d}  {0.0:5.1f}%")
        lines += ["", "channel states:"]
        chan_total = sum(self.channel_states.values()) or 1
        chan_seen = set(self.channel_states)
        for state in ChannelState:
            n = self.channel_states.get(state, 0)
            lines.append(f"  {state.value:<22} {n:8d}  {100*n/chan_total:5.1f}%")
        lines += ["", "deployed model vs cascade:",
                  f"  deployed named a station the cascade refused: "
                  f"{self.deployed_over_reports}",
                  f"  cascade named a station deployed missed:      "
                  f"{self.deployed_under_reports}",
                  "",
                  f"geometry refused: {self.geometry_refused}",
                  f"arrivals with null SNR (skipped): {self.skipped_null_snr_total}",
                  f"unclaimed arrivals: {self.unclaimed_arrivals}"]
        return "\n".join(lines)


def summarise(verdicts: Iterable) -> ReplayReport:
    report = ReplayReport()
    for mv in verdicts:
        report.minutes += 1
        report.skipped_null_snr_total += mv.skipped_null_snr
        if mv.geometry_refused:
            report.geometry_refused += 1
            continue
        hour = datetime.fromtimestamp(mv.minute_utc, tz=timezone.utc).hour
        admitted = set()
        for station, sv in mv.verdict.stations.items():
            report.state_counts[sv.state] += 1
            report.by_hour[hour][sv.state] += 1
            if sv.state is AdmissionState.ADMITTED:
                admitted.add(station)
        report.channel_states[mv.verdict.channel_state] += 1
        report.unclaimed_arrivals += len(mv.verdict.unclaimed_ms)
        if mv.deployed_labels - admitted:
            report.deployed_over_reports += 1
        if admitted - mv.deployed_labels:
            report.deployed_under_reports += 1
    return report

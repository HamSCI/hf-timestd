"""Aggregate replay verdicts, and diff them against what was deployed."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Iterable

from hf_timestd.core.admission_cascade import AdmissionState


@dataclass
class ReplayReport:
    state_counts: Counter = field(default_factory=Counter)
    by_hour: Dict[int, Counter] = field(default_factory=lambda: defaultdict(Counter))
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
        lines += ["", "deployed model vs cascade:",
                  f"  deployed named a station the cascade refused: "
                  f"{self.deployed_over_reports}",
                  f"  cascade named a station deployed missed:      "
                  f"{self.deployed_under_reports}",
                  "",
                  f"geometry refused: {self.geometry_refused}",
                  f"arrivals with null SNR (skipped): {self.skipped_null_snr_total}"]
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
        if mv.deployed_labels - admitted:
            report.deployed_over_reports += 1
        if admitted - mv.deployed_labels:
            report.deployed_under_reports += 1
    return report

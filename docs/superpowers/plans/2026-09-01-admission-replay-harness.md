# Admission Replay Harness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an off-station harness that replays the three-key admission cascade against B4's archived arrivals, so we learn how often each verdict fires and what admission would have changed — without touching the station.

**Architecture:** The cascade and the history tracker are written as pure, importable modules under `core/` because later work lifts them straight into production. The harness around them (`replay/`) reads a read-only COPY of `timestd.db`, recomputes geometric windows from the FIXED propagation predictor at each past timestamp, runs the cascade, and aggregates verdicts by channel and UTC hour.

**Tech Stack:** Python ≥3.10, sqlite3 (stdlib), NumPy, pytest. Reuses `core/station_arrival_gate.py` (`arrival_windows`, `gate_arrivals`, `eligible_candidates`, `StationWindow`) and `hamsci_dsp.propagation.arrival_matrix.ArrivalPatternMatrix`.

**Spec:** `docs/superpowers/specs/2026-09-01-timing-admission-three-keys-design.md`

## Global Constraints

- **Read-only against the archive.** The harness opens sqlite with `mode=ro` and never writes to any `timestd.db`. It must never connect to B4.
- **Changes no station behaviour.** This plan adds no service, no unit, no config, and modifies no existing production code path. `core/station_arrival_gate.py` and `core/metrology_engine.py` are **read but not edited** in this plan.
- **Abstention is the correct output.** Exactly one state (`ADMITTED`) yields a value. No confidence score, no weight, no fallback branch. A cascade that cannot decide returns a non-admitting state — never a guess.
- **Numeric thresholds are outputs, not inputs.** `P_fa`, floor SNR, history tolerance and re-acquisition N are all **parameters** with defaults marked provisional. No task may hard-code one as if settled.
- **Key 1 is approximated in this harness.** The archive stores `corr_snr_db` but not the correlation waveform or the noise floor in force, so the harness tests the floor *relatively* in existing SNR units. Absolute floor calibration needs 24 kHz IQ and is out of scope here.
- Style: one class per file, filename matches class, type hints throughout, `black` formatting, tests under `tests/`.
- Run tests with `.venv/bin/python -m pytest ... -p no:cacheprovider` from the repo root.

---

## File Structure

| File | Responsibility |
|---|---|
| `src/hf_timestd/core/admission_cascade.py` | **Production-bound.** The seven-state per-station cascade and the three channel states, as pure functions over already-computed windows and arrivals. |
| `src/hf_timestd/core/arrival_history.py` | **Production-bound.** Key 3: per-(channel, station) recent-track tracker with the re-acquisition escape. |
| `src/hf_timestd/replay/archive_reader.py` | Reads a read-only sqlite copy, yields arrivals grouped by (channel, minute). |
| `src/hf_timestd/replay/window_source.py` | Recomputes corrected expected delays + windows for a past timestamp. |
| `src/hf_timestd/replay/runner.py` | Drives reader → windows → cascade → verdict stream. |
| `src/hf_timestd/replay/report.py` | Aggregates verdicts by channel/hour/state and diffs against what was deployed. |
| `scripts/replay_admission.py` | Thin CLI wrapper. |

---

### Task 1: The admission cascade

**Files:**
- Create: `src/hf_timestd/core/admission_cascade.py`
- Test: `tests/test_admission_cascade.py`

**Interfaces:**
- Consumes: `StationWindow` from `core/station_arrival_gate.py` — FOUR fields (`station`, `min_ms`, `max_ms`, `scatter_max_ms`) and two predicates: `.contains(x)` = inside the modelled direct modes (timing-usable), `.admits(x)` = physically possible for this station including the scatter tail. The gap between them IS `DEGRADED`.
- Produces: `AdmissionState`, `ChannelState`, `ObservedArrival`, `StationVerdict`, `ChannelVerdict`, `adjudicate_channel(...)`. Tasks 4 and 6 rely on these exact names.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_admission_cascade.py
import pytest

from hf_timestd.core.admission_cascade import (
    AdmissionState, ChannelState, ObservedArrival, adjudicate_channel,
)
from hf_timestd.core.station_arrival_gate import StationWindow


def _win(station, lo, hi, scatter=None):
    """StationWindow has FOUR fields.  `max_ms` ends the modelled direct
    modes; `scatter_max_ms` runs out to where another station could own the
    arrival.  Between them lies DEGRADED: physically this station, but not
    usable for timing."""
    return StationWindow(station=station, min_ms=lo, max_ms=hi,
                         scatter_max_ms=scatter if scatter is not None else hi)


def test_clean_arrival_in_one_window_is_admitted():
    windows = {"WWV": _win("WWV", 3.0, 5.0), "WWVH": _win("WWVH", 22.0, 24.0)}
    arrivals = [ObservedArrival(arrival_ms=4.0, corr_snr_db=20.0)]

    verdict = adjudicate_channel(
        windows=windows, arrivals=arrivals, eligible={"WWV", "WWVH"},
        floor_snr_db=10.0, history_ok=lambda station, ms: True,
    )

    assert verdict.stations["WWV"].state is AdmissionState.ADMITTED
    assert verdict.stations["WWV"].arrival_ms == 4.0
    # WWVH got nothing above the floor in its window
    assert verdict.stations["WWVH"].state is AdmissionState.BELOW_FLOOR
    assert verdict.channel_state is ChannelState.CHANNEL_PARTIAL
    assert verdict.admitted_count == 1


def test_all_three_absent_is_channel_silent():
    windows = {"WWV": _win("WWV", 3.0, 5.0), "WWVH": _win("WWVH", 22.0, 24.0),
               "BPM": _win("BPM", 39.0, 41.0)}
    # one arrival, but under the floor — nothing counts
    arrivals = [ObservedArrival(arrival_ms=4.0, corr_snr_db=2.0)]

    verdict = adjudicate_channel(
        windows=windows, arrivals=arrivals, eligible={"WWV", "WWVH", "BPM"},
        floor_snr_db=10.0, history_ok=lambda station, ms: True,
    )

    assert all(v.state is AdmissionState.BELOW_FLOOR
               for v in verdict.stations.values())
    assert verdict.channel_state is ChannelState.CHANNEL_SILENT
    assert verdict.admitted_count == 0


def test_above_floor_outside_every_window_is_unidentified():
    """Energy arrived; no station's window claims it.  It belongs to none."""
    windows = {"WWV": _win("WWV", 3.0, 5.0), "WWVH": _win("WWVH", 22.0, 24.0)}
    arrivals = [ObservedArrival(arrival_ms=13.0, corr_snr_db=30.0)]

    verdict = adjudicate_channel(
        windows=windows, arrivals=arrivals, eligible={"WWV", "WWVH"},
        floor_snr_db=10.0, history_ok=lambda station, ms: True,
    )

    assert verdict.channel_state is ChannelState.CHANNEL_UNIDENTIFIED
    assert verdict.admitted_count == 0
    assert verdict.unclaimed_ms == [13.0]


def test_history_rejection_yields_inconsistent_not_a_value():
    windows = {"WWV": _win("WWV", 3.0, 5.0)}
    arrivals = [ObservedArrival(arrival_ms=4.0, corr_snr_db=20.0)]

    verdict = adjudicate_channel(
        windows=windows, arrivals=arrivals, eligible={"WWV"},
        floor_snr_db=10.0, history_ok=lambda station, ms: False,
    )

    assert verdict.stations["WWV"].state is AdmissionState.INCONSISTENT
    assert verdict.stations["WWV"].arrival_ms is None
    assert verdict.admitted_count == 0


def test_ineligible_station_is_not_below_floor():
    """BPM off-schedule says nothing about the ionosphere."""
    windows = {"WWV": _win("WWV", 3.0, 5.0), "BPM": _win("BPM", 39.0, 41.0)}
    arrivals = [ObservedArrival(arrival_ms=4.0, corr_snr_db=20.0)]

    verdict = adjudicate_channel(
        windows=windows, arrivals=arrivals, eligible={"WWV"},
        floor_snr_db=10.0, history_ok=lambda station, ms: True,
    )

    assert verdict.stations["BPM"].state is AdmissionState.NOT_ELIGIBLE


def test_scattered_arrival_is_degraded_not_admitted():
    """Inside admits() but outside contains(): this station, unusable."""
    windows = {"WWV": _win("WWV", 3.0, 5.0, scatter=12.0)}
    arrivals = [ObservedArrival(arrival_ms=9.0, corr_snr_db=25.0)]

    verdict = adjudicate_channel(
        windows=windows, arrivals=arrivals, eligible={"WWV"},
        floor_snr_db=10.0, history_ok=lambda station, ms: True,
    )

    assert verdict.stations["WWV"].state is AdmissionState.DEGRADED
    assert verdict.stations["WWV"].arrival_ms is None


def test_only_admitted_carries_a_value():
    """The invariant: six of seven states emit nothing."""
    windows = {"WWV": _win("WWV", 3.0, 5.0)}
    arrivals = [ObservedArrival(arrival_ms=99.0, corr_snr_db=30.0)]

    verdict = adjudicate_channel(
        windows=windows, arrivals=arrivals, eligible={"WWV"},
        floor_snr_db=10.0, history_ok=lambda station, ms: True,
    )

    for v in verdict.stations.values():
        if v.state is not AdmissionState.ADMITTED:
            assert v.arrival_ms is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_admission_cascade.py -v -p no:cacheprovider`
Expected: FAIL — `ModuleNotFoundError: No module named 'hf_timestd.core.admission_cascade'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/hf_timestd/core/admission_cascade.py
"""Which measurements may reach the timing path, and which may not.

A measurement counts when it clears the noise floor, lands inside exactly
one geometric window, and stays consistent with history.  Nothing else
counts.  Abstention is the correct output rather than a degraded
measurement, so exactly one of the seven states carries a value.

The rule this replaces had to emit a station label whether or not evidence
supported one, which is a machine for producing measurements nobody can
stand behind.  See docs/superpowers/specs/2026-09-01-timing-admission-three-keys-design.md
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable, Dict, Iterable, List, Mapping, Optional, Set

from hf_timestd.core.station_arrival_gate import StationWindow


class AdmissionState(str, Enum):
    NOT_ELIGIBLE = "NOT_ELIGIBLE"   # we did not look (BPM schedule only)
    BELOW_FLOOR = "BELOW_FLOOR"     # path delivered nothing detectable
    OFF_MODEL = "OFF_MODEL"         # path delivered; our model missed it
    AMBIGUOUS = "AMBIGUOUS"         # cannot say whose signal it is
    INCONSISTENT = "INCONSISTENT"   # lone outlier against the track
    DEGRADED = "DEGRADED"           # present, unusable
    ADMITTED = "ADMITTED"           # the only state carrying a value


class ChannelState(str, Enum):
    CHANNEL_SILENT = "CHANNEL_SILENT"
    CHANNEL_UNIDENTIFIED = "CHANNEL_UNIDENTIFIED"
    CHANNEL_PARTIAL = "CHANNEL_PARTIAL"


@dataclass(frozen=True)
class ObservedArrival:
    arrival_ms: float
    corr_snr_db: float


@dataclass(frozen=True)
class StationVerdict:
    station: str
    state: AdmissionState
    arrival_ms: Optional[float]
    reason: str


@dataclass(frozen=True)
class ChannelVerdict:
    stations: Dict[str, StationVerdict]
    channel_state: ChannelState
    unclaimed_ms: List[float]

    @property
    def admitted_count(self) -> int:
        return sum(1 for v in self.stations.values()
                   if v.state is AdmissionState.ADMITTED)


HistoryCheck = Callable[[str, float], bool]


def adjudicate_channel(
    *,
    windows: Mapping[str, StationWindow],
    arrivals: Iterable[ObservedArrival],
    eligible: Set[str],
    floor_snr_db: float,
    history_ok: HistoryCheck,
) -> ChannelVerdict:
    """Run the cascade for every station this channel could carry."""
    arrivals = list(arrivals)
    above = [a for a in arrivals if a.corr_snr_db >= floor_snr_db]

    stations: Dict[str, StationVerdict] = {}
    claimed: List[float] = []

    for station, window in windows.items():
        if station not in eligible:
            stations[station] = StationVerdict(
                station, AdmissionState.NOT_ELIGIBLE, None,
                "station not a candidate this minute")
            continue

        inside = [a for a in above if window.contains(a.arrival_ms)]
        if not inside:
            # Nothing in the direct-mode window.  Did anything land in the
            # scatter tail?  That is this station, delayed by sidescatter —
            # real, and not usable as a timing measurement.
            scattered = [a for a in above
                         if window.admits(a.arrival_ms)
                         and not window.contains(a.arrival_ms)]
            if scattered:
                claimed.extend(a.arrival_ms for a in scattered)
                stations[station] = StationVerdict(
                    station, AdmissionState.DEGRADED, None,
                    "arrival lies in the scatter tail, not the direct modes")
                continue
            stations[station] = StationVerdict(
                station, AdmissionState.BELOW_FLOOR, None,
                f"nothing above {floor_snr_db:.1f} dB in window")
            continue

        # More than one window claiming the same arrival means we cannot
        # say whose it is.  Refuse rather than pick.
        contested = [
            a for a in inside
            if sum(1 for w in windows.values() if w.contains(a.arrival_ms)) > 1
        ]
        if contested:
            stations[station] = StationVerdict(
                station, AdmissionState.AMBIGUOUS, None,
                "arrival satisfies more than one station window")
            claimed.extend(a.arrival_ms for a in inside)
            continue

        best = max(inside, key=lambda a: a.corr_snr_db)
        claimed.append(best.arrival_ms)

        if not history_ok(station, best.arrival_ms):
            stations[station] = StationVerdict(
                station, AdmissionState.INCONSISTENT, None,
                "arrival disagrees with the recent track")
            continue

        stations[station] = StationVerdict(
            station, AdmissionState.ADMITTED, best.arrival_ms, "")

    unclaimed = [a.arrival_ms for a in above if a.arrival_ms not in claimed]

    if any(v.state is AdmissionState.ADMITTED for v in stations.values()):
        channel_state = ChannelState.CHANNEL_PARTIAL
    elif above:
        channel_state = ChannelState.CHANNEL_UNIDENTIFIED
    else:
        channel_state = ChannelState.CHANNEL_SILENT

    return ChannelVerdict(stations=stations, channel_state=channel_state,
                          unclaimed_ms=unclaimed)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_admission_cascade.py -v -p no:cacheprovider`
Expected: PASS, 7 passed

- [ ] **Step 5: Run the whole suite for regressions**

Run: `.venv/bin/python -m pytest tests/ -q -p no:cacheprovider`
Expected: 2722 passed + 7 new = 2729 passed

- [ ] **Step 6: Commit**

```bash
git add src/hf_timestd/core/admission_cascade.py tests/test_admission_cascade.py
git commit -m "admission: the seven-state cascade, where only one state carries a value"
```

---

### Task 2: Arrival history with a re-acquisition escape

**Files:**
- Create: `src/hf_timestd/core/arrival_history.py`
- Test: `tests/test_arrival_history.py`

**Interfaces:**
- Produces: `ArrivalHistory(tolerance_ms, lookback, reacquire_after)` with methods `accepts(station, arrival_ms) -> bool` and `observe(station, arrival_ms) -> None`. Task 4 constructs one per channel and passes `history.accepts` as the `history_ok` callable from Task 1.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_arrival_history.py
from hf_timestd.core.arrival_history import ArrivalHistory


def _settle(h, station="WWV", value=4.0, n=5):
    for _ in range(n):
        h.observe(station, value)


def test_empty_history_accepts_anything():
    """With no track yet there is nothing to disagree with."""
    h = ArrivalHistory(tolerance_ms=1.0, lookback=5, reacquire_after=3)
    assert h.accepts("WWV", 4.0) is True


def test_arrival_near_the_track_is_accepted():
    h = ArrivalHistory(tolerance_ms=1.0, lookback=5, reacquire_after=3)
    _settle(h)
    assert h.accepts("WWV", 4.5) is True


def test_lone_outlier_is_rejected():
    h = ArrivalHistory(tolerance_ms=1.0, lookback=5, reacquire_after=3)
    _settle(h)
    assert h.accepts("WWV", 40.0) is False


def test_sustained_disagreement_forces_reacquisition():
    """A gate that can never be overruled by evidence is the stale-lock bug."""
    h = ArrivalHistory(tolerance_ms=1.0, lookback=5, reacquire_after=3)
    _settle(h)

    assert h.accepts("WWV", 40.0) is False   # 1st
    assert h.accepts("WWV", 40.1) is False   # 2nd
    # third consecutive arrival agreeing with the others but not the track
    assert h.accepts("WWV", 40.2) is True    # re-acquire


def test_a_return_to_track_clears_the_reacquire_run():
    h = ArrivalHistory(tolerance_ms=1.0, lookback=5, reacquire_after=3)
    _settle(h)
    assert h.accepts("WWV", 40.0) is False
    assert h.accepts("WWV", 4.1) is True     # back on track, run resets
    assert h.accepts("WWV", 40.0) is False   # counts as the first again


def test_stations_are_tracked_independently():
    h = ArrivalHistory(tolerance_ms=1.0, lookback=5, reacquire_after=3)
    _settle(h, "WWV", 4.0)
    _settle(h, "WWVH", 23.0)
    assert h.accepts("WWVH", 23.2) is True
    assert h.accepts("WWVH", 4.0) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_arrival_history.py -v -p no:cacheprovider`
Expected: FAIL — `ModuleNotFoundError: No module named 'hf_timestd.core.arrival_history'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/hf_timestd/core/arrival_history.py
"""Key 3: does this arrival agree with the recent track?

Propagation delay moves smoothly and mode changes step by known amounts, so
a lone arrival far from the recent track is more likely a sidelobe or a
mis-assignment than a real path change.  T6 already reasons this way — it
estimates from the PPS history rather than each pulse, because the GPSDO
pins the slope.

⛔ The gate must stay falsifiable.  Admit only what matches history and the
tracker can lock onto a wrong track and then refuse the very evidence that
would correct it — the stale-lock failure behind the August T6 -26 ms
excursion.  So a LONE outlier gets rejected, while `reacquire_after`
consecutive arrivals that agree with each other force re-acquisition.
"""

from __future__ import annotations

from collections import deque
from statistics import median
from typing import Deque, Dict, List


class ArrivalHistory:
    """Per-(station) recent track with a re-acquisition escape."""

    def __init__(self, tolerance_ms: float, lookback: int,
                 reacquire_after: int) -> None:
        self.tolerance_ms = float(tolerance_ms)
        self.lookback = int(lookback)
        self.reacquire_after = int(reacquire_after)
        self._track: Dict[str, Deque[float]] = {}
        self._dissent: Dict[str, List[float]] = {}

    def _centre(self, station: str) -> float | None:
        seen = self._track.get(station)
        if not seen:
            return None
        return median(seen)

    def accepts(self, station: str, arrival_ms: float) -> bool:
        centre = self._centre(station)
        if centre is None:
            self.observe(station, arrival_ms)
            return True

        if abs(arrival_ms - centre) <= self.tolerance_ms:
            self._dissent.pop(station, None)
            self.observe(station, arrival_ms)
            return True

        # Disagrees with the track.  Does it agree with the other dissenters?
        run = self._dissent.setdefault(station, [])
        if run and abs(arrival_ms - median(run)) > self.tolerance_ms:
            run.clear()
        run.append(arrival_ms)

        if len(run) >= self.reacquire_after:
            self._track[station] = deque(run, maxlen=self.lookback)
            self._dissent.pop(station, None)
            return True
        return False

    def observe(self, station: str, arrival_ms: float) -> None:
        self._track.setdefault(
            station, deque(maxlen=self.lookback)).append(float(arrival_ms))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_arrival_history.py -v -p no:cacheprovider`
Expected: PASS, 6 passed

- [ ] **Step 5: Commit**

```bash
git add src/hf_timestd/core/arrival_history.py tests/test_arrival_history.py
git commit -m "admission: key 3, and an escape so it cannot become a stale lock"
```

---

### Task 3: Archive reader

**Files:**
- Create: `src/hf_timestd/replay/__init__.py` (empty), `src/hf_timestd/replay/archive_reader.py`
- Test: `tests/test_replay_archive_reader.py`

**Interfaces:**
- Produces: `MinuteGroup` (fields `channel: str`, `minute_utc: int`, `frequency_mhz: float`, `arrivals: List[ObservedArrival]`, `deployed_labels: Set[str]`) and `read_minutes(db_path, *, channel=None, start_utc=None, end_utc=None) -> Iterator[MinuteGroup]`. Task 4 consumes both.
- Consumes: `ObservedArrival` from Task 1.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_replay_archive_reader.py
import sqlite3

import pytest

from hf_timestd.replay.archive_reader import read_minutes


@pytest.fixture
def tiny_db(tmp_path):
    """A two-minute archive shaped exactly like B4's L1_all_arrivals."""
    path = tmp_path / "timestd.db"
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE L1_all_arrivals ("
        " channel TEXT, timestamp_utc TEXT, minute_boundary_utc INTEGER,"
        " station TEXT, frequency_mhz REAL, utc_second INTEGER,"
        " peak_rank INTEGER, arrival_ms REAL, timing_error_ms REAL,"
        " corr_snr_db REAL, peak_value REAL, model_expected_ms REAL)")
    rows = [
        ("SHARED_10000", "2026-08-31T17:19:00+0000", 1788196740, "WWV",
         10.0, 1788196741, 0, 59004.24, 0.0, 22.5, 1.0, 4.24),
        ("SHARED_10000", "2026-08-31T17:19:00+0000", 1788196740, "WWVH",
         10.0, 1788196741, 1, 59022.85, 0.0, 4.0, 0.2, 22.85),
        ("SHARED_10000", "2026-08-31T17:20:00+0000", 1788196800, "WWV",
         10.0, 1788196801, 0, 59004.30, 0.0, 21.0, 1.0, 4.24),
    ]
    con.executemany(
        "INSERT INTO L1_all_arrivals VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    con.commit()
    con.close()
    return path


def test_groups_rows_by_minute(tiny_db):
    groups = list(read_minutes(tiny_db))
    assert [g.minute_utc for g in groups] == [1788196740, 1788196800]
    assert len(groups[0].arrivals) == 2
    assert groups[0].channel == "SHARED_10000"
    assert groups[0].frequency_mhz == 10.0


def test_arrival_ms_is_reduced_to_position_in_the_second(tiny_db):
    """arrival_ms is ms-into-the-minute; the cascade works within a second."""
    groups = list(read_minutes(tiny_db))
    assert groups[0].arrivals[0].arrival_ms == pytest.approx(4.24, abs=0.01)
    assert groups[0].arrivals[0].corr_snr_db == 22.5


def test_deployed_labels_are_recorded_for_the_counterfactual(tiny_db):
    groups = list(read_minutes(tiny_db))
    assert groups[0].deployed_labels == {"WWV", "WWVH"}


def test_channel_and_time_filters(tiny_db):
    assert list(read_minutes(tiny_db, channel="NOPE")) == []
    got = list(read_minutes(tiny_db, start_utc=1788196800))
    assert [g.minute_utc for g in got] == [1788196800]


def test_opens_read_only(tiny_db):
    """The harness must never be able to write to an archive."""
    groups = list(read_minutes(tiny_db))
    assert groups  # sanity: it did read
    con = sqlite3.connect(f"file:{tiny_db}?mode=ro", uri=True)
    with pytest.raises(sqlite3.OperationalError):
        con.execute("INSERT INTO L1_all_arrivals (channel) VALUES ('x')")
    con.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_replay_archive_reader.py -v -p no:cacheprovider`
Expected: FAIL — `ModuleNotFoundError: No module named 'hf_timestd.replay'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/hf_timestd/replay/archive_reader.py
"""Read an archived arrival stream, grouped into minutes.

Opens the database READ-ONLY.  This harness exists to ask what the cascade
WOULD have decided; it must never be able to alter the record it is asking
about.

`arrival_ms` in L1_all_arrivals counts milliseconds into the MINUTE, so a
tick at second 59 plus 4.24 ms reads 59004.24.  The cascade reasons within
a second, so the reader reduces it modulo 1000.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Iterator, List, Optional, Set

from hf_timestd.core.admission_cascade import ObservedArrival


@dataclass
class MinuteGroup:
    channel: str
    minute_utc: int
    frequency_mhz: float
    arrivals: List[ObservedArrival] = field(default_factory=list)
    deployed_labels: Set[str] = field(default_factory=set)


def read_minutes(
    db_path,
    *,
    channel: Optional[str] = None,
    start_utc: Optional[int] = None,
    end_utc: Optional[int] = None,
) -> Iterator[MinuteGroup]:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        sql = ("SELECT channel, minute_boundary_utc, frequency_mhz, station,"
               " arrival_ms, corr_snr_db FROM L1_all_arrivals")
        clauses, params = [], []
        if channel is not None:
            clauses.append("channel = ?")
            params.append(channel)
        if start_utc is not None:
            clauses.append("minute_boundary_utc >= ?")
            params.append(int(start_utc))
        if end_utc is not None:
            clauses.append("minute_boundary_utc < ?")
            params.append(int(end_utc))
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY channel, minute_boundary_utc"

        current: Optional[MinuteGroup] = None
        for ch, minute, freq, station, arrival_ms, snr in con.execute(sql, params):
            if current is None or (ch, minute) != (current.channel,
                                                   current.minute_utc):
                if current is not None:
                    yield current
                current = MinuteGroup(channel=ch, minute_utc=int(minute),
                                      frequency_mhz=float(freq))
            current.arrivals.append(ObservedArrival(
                arrival_ms=float(arrival_ms) % 1000.0,
                corr_snr_db=float(snr if snr is not None else 0.0)))
            if station:
                current.deployed_labels.add(str(station))
        if current is not None:
            yield current
    finally:
        con.close()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_replay_archive_reader.py -v -p no:cacheprovider`
Expected: PASS, 5 passed

- [ ] **Step 5: Commit**

```bash
git add src/hf_timestd/replay/ tests/test_replay_archive_reader.py
git commit -m "replay: read the archive read-only, grouped by minute"
```

---

### Task 4: Window source — corrected geometry for a past minute

**Files:**
- Create: `src/hf_timestd/replay/window_source.py`
- Test: `tests/test_replay_window_source.py`

**Interfaces:**
- Produces: `WindowSource(receiver_lat, receiver_lon, reference_sigma_ms=0.7)` with `windows_for(minute_utc, frequency_mhz) -> Dict[str, StationWindow]` and `eligible_for(minute_utc, frequency_mhz) -> Set[str]`. Task 5 consumes both.
- Consumes: `ArrivalPatternMatrix` (hamsci-dsp), `arrival_windows` / `eligible_candidates` from `core/station_arrival_gate.py`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_replay_window_source.py
import pytest

from hf_timestd.replay.window_source import WindowSource

# AC0G / B4, Columbia MO — the station the archive came from.
LAT, LON = 38.9187497, -92.1277207
MINUTE_1719Z = 1788196740  # 2026-08-31T17:19Z


@pytest.fixture
def source():
    return WindowSource(receiver_lat=LAT, receiver_lon=LON,
                        reference_sigma_ms=0.7)


def test_windows_use_the_corrected_predictor(source):
    """The archive's model_expected_ms is the CORRUPTED prediction.

    Replay must recompute, not reuse: 10 MHz once read WWV 8.49 / WWVH 44.49
    against a true 4.24 / 22.85.
    """
    windows = source.windows_for(MINUTE_1719Z, 10.0)
    wwv = windows["WWV"]
    centre = (wwv.min_ms + wwv.max_ms) / 2.0
    assert 3.5 < centre < 5.0, f"WWV centre {centre:.2f} ms is not physical"


def test_wwvh_and_bpm_separate_after_the_fix(source):
    """The 2x bug collapsed WWVH and BPM to 1.05 ms apart, forcing abstention."""
    windows = source.windows_for(MINUTE_1719Z, 10.0)
    wwvh = (windows["WWVH"].min_ms + windows["WWVH"].max_ms) / 2.0
    bpm = (windows["BPM"].min_ms + windows["BPM"].max_ms) / 2.0
    assert bpm - wwvh > 10.0


def test_eligibility_excludes_stations_off_this_frequency(source):
    eligible = source.eligible_for(MINUTE_1719Z, 20.0)
    assert "WWV" in eligible
    assert "WWVH" not in eligible  # WWVH does not transmit on 20 MHz


def test_unbuildable_windows_return_empty_rather_than_raise(source):
    """arrival_windows refuses an overlapping set; replay must not crash."""
    wide = WindowSource(receiver_lat=LAT, receiver_lon=LON,
                        reference_sigma_ms=50.0)
    assert wide.windows_for(MINUTE_1719Z, 10.0) == {}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_replay_window_source.py -v -p no:cacheprovider`
Expected: FAIL — `ModuleNotFoundError: No module named 'hf_timestd.replay.window_source'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/hf_timestd/replay/window_source.py
"""Recompute the geometry a past minute SHOULD have had.

The archive's `model_expected_ms` records the prediction that was actually
used, and before 2026-09-01 that prediction was roughly doubled on any band
sitting under foF2 — 10 MHz read WWV 8.49 / WWVH 44.49 against a true
4.24 / 22.85.  Replay therefore recomputes windows from the fixed predictor
rather than reusing what was stored.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, Set

from hf_timestd.core.station_arrival_gate import (
    StationWindow, arrival_windows, eligible_candidates,
)


class WindowSource:
    def __init__(self, receiver_lat: float, receiver_lon: float,
                 reference_sigma_ms: float = 0.7) -> None:
        from hamsci_dsp.propagation.arrival_matrix import ArrivalPatternMatrix

        self.reference_sigma_ms = float(reference_sigma_ms)
        self._matrix = ArrivalPatternMatrix(receiver_lat=receiver_lat,
                                            receiver_lon=receiver_lon)

    def _expected(self, minute_utc: int, frequency_mhz: float) -> Dict[str, float]:
        dt = datetime.fromtimestamp(minute_utc, tz=timezone.utc)
        matrix = self._matrix.get_expected_arrivals(dt)
        out: Dict[str, float] = {}
        for station in ("WWV", "WWVH", "BPM"):
            arrival = matrix.get_arrival(station, frequency_mhz)
            if arrival is not None:
                out[station] = float(arrival.expected_delay_ms)
        return out

    def eligible_for(self, minute_utc: int, frequency_mhz: float) -> Set[str]:
        dt = datetime.fromtimestamp(minute_utc, tz=timezone.utc)
        candidates = eligible_candidates(
            self._expected(minute_utc, frequency_mhz),
            utc_minute=dt.minute, utc_hour=dt.hour,
            frequency_mhz=frequency_mhz)
        return set(candidates)

    def windows_for(self, minute_utc: int,
                    frequency_mhz: float) -> Dict[str, StationWindow]:
        expected = self._expected(minute_utc, frequency_mhz)
        if not expected:
            return {}
        try:
            return arrival_windows(
                expected, reference_sigma_ms=self.reference_sigma_ms)
        except ValueError:
            # Overlapping windows: the geometry cannot separate these
            # stations at this sigma.  Refusing is the honest answer.
            return {}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_replay_window_source.py -v -p no:cacheprovider`
Expected: PASS, 4 passed

- [ ] **Step 5: Commit**

```bash
git add src/hf_timestd/replay/window_source.py tests/test_replay_window_source.py
git commit -m "replay: recompute past geometry from the fixed predictor"
```

---

### Task 5: Runner and report

**Files:**
- Create: `src/hf_timestd/replay/runner.py`, `src/hf_timestd/replay/report.py`
- Test: `tests/test_replay_runner.py`

**Interfaces:**
- Produces: `replay(db_path, source, *, floor_snr_db, tolerance_ms, lookback, reacquire_after, **filters) -> Iterator[MinuteVerdict]` where `MinuteVerdict` has `channel`, `minute_utc`, `verdict: ChannelVerdict`, `deployed_labels: Set[str]`; and `summarise(verdicts) -> ReplayReport` with `.state_counts`, `.by_hour`, `.deployed_over_reports`, `.deployed_under_reports`.
- Consumes: Tasks 1–4.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_replay_runner.py
import sqlite3

import pytest

from hf_timestd.core.admission_cascade import AdmissionState, ChannelState
from hf_timestd.core.station_arrival_gate import StationWindow
from hf_timestd.replay.report import summarise
from hf_timestd.replay.runner import replay


class FakeSource:
    """Fixed geometry, so the test exercises the runner not the ionosphere."""
    def windows_for(self, minute_utc, frequency_mhz):
        return {"WWV": StationWindow(station="WWV", min_ms=3.0, max_ms=5.0),
                "WWVH": StationWindow(station="WWVH", min_ms=22.0, max_ms=24.0)}

    def eligible_for(self, minute_utc, frequency_mhz):
        return {"WWV", "WWVH"}


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "t.db"
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE L1_all_arrivals ("
        " channel TEXT, timestamp_utc TEXT, minute_boundary_utc INTEGER,"
        " station TEXT, frequency_mhz REAL, utc_second INTEGER,"
        " peak_rank INTEGER, arrival_ms REAL, timing_error_ms REAL,"
        " corr_snr_db REAL, peak_value REAL, model_expected_ms REAL)")
    rows = []
    for i in range(4):
        minute = 1788196740 + 60 * i
        # WWV real and strong; WWVH labelled by the deployed model but weak
        rows.append(("SHARED_10000", "", minute, "WWV", 10.0, minute + 1,
                     0, 59004.0, 0.0, 25.0, 1.0, 4.24))
        rows.append(("SHARED_10000", "", minute, "WWVH", 10.0, minute + 1,
                     1, 59023.0, 0.0, 3.0, 0.1, 22.85))
    con.executemany(
        "INSERT INTO L1_all_arrivals VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    con.commit()
    con.close()
    return path


def test_replay_admits_wwv_and_refuses_weak_wwvh(db):
    verdicts = list(replay(db, FakeSource(), floor_snr_db=10.0,
                           tolerance_ms=1.0, lookback=5, reacquire_after=3))
    assert len(verdicts) == 4
    for mv in verdicts:
        assert mv.verdict.stations["WWV"].state is AdmissionState.ADMITTED
        assert mv.verdict.stations["WWVH"].state is AdmissionState.BELOW_FLOOR
        assert mv.verdict.channel_state is ChannelState.CHANNEL_PARTIAL


def test_summary_counts_the_deployed_over_report(db):
    """The deployed model labelled WWVH every minute; the cascade did not."""
    report = summarise(replay(db, FakeSource(), floor_snr_db=10.0,
                              tolerance_ms=1.0, lookback=5, reacquire_after=3))
    assert report.state_counts[AdmissionState.ADMITTED] == 4
    assert report.state_counts[AdmissionState.BELOW_FLOOR] == 4
    assert report.deployed_over_reports == 4
    assert report.deployed_under_reports == 0


def test_summary_buckets_by_utc_hour(db):
    report = summarise(replay(db, FakeSource(), floor_snr_db=10.0,
                              tolerance_ms=1.0, lookback=5, reacquire_after=3))
    assert 17 in report.by_hour
    assert sum(report.by_hour[17].values()) == 8  # 2 stations x 4 minutes
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_replay_runner.py -v -p no:cacheprovider`
Expected: FAIL — `ModuleNotFoundError: No module named 'hf_timestd.replay.runner'`

- [ ] **Step 3: Write the runner**

```python
# src/hf_timestd/replay/runner.py
"""Drive the cascade across an archived arrival stream."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator, Set

from hf_timestd.core.admission_cascade import ChannelVerdict, adjudicate_channel
from hf_timestd.core.arrival_history import ArrivalHistory
from hf_timestd.replay.archive_reader import read_minutes


@dataclass
class MinuteVerdict:
    channel: str
    minute_utc: int
    verdict: ChannelVerdict
    deployed_labels: Set[str]


def replay(db_path, source, *, floor_snr_db: float, tolerance_ms: float,
           lookback: int, reacquire_after: int, **filters
           ) -> Iterator[MinuteVerdict]:
    histories = {}
    for group in read_minutes(db_path, **filters):
        history = histories.setdefault(
            group.channel,
            ArrivalHistory(tolerance_ms=tolerance_ms, lookback=lookback,
                           reacquire_after=reacquire_after))
        windows = source.windows_for(group.minute_utc, group.frequency_mhz)
        if not windows:
            continue
        verdict = adjudicate_channel(
            windows=windows, arrivals=group.arrivals,
            eligible=source.eligible_for(group.minute_utc, group.frequency_mhz),
            floor_snr_db=floor_snr_db, history_ok=history.accepts)
        yield MinuteVerdict(channel=group.channel, minute_utc=group.minute_utc,
                            verdict=verdict,
                            deployed_labels=group.deployed_labels)
```

- [ ] **Step 4: Write the report**

```python
# src/hf_timestd/replay/report.py
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
                  f"{self.deployed_under_reports}"]
        return "\n".join(lines)


def summarise(verdicts: Iterable) -> ReplayReport:
    report = ReplayReport()
    for mv in verdicts:
        report.minutes += 1
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
```

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_replay_runner.py -v -p no:cacheprovider`
Expected: PASS, 3 passed

- [ ] **Step 6: Commit**

```bash
git add src/hf_timestd/replay/runner.py src/hf_timestd/replay/report.py tests/test_replay_runner.py
git commit -m "replay: drive the cascade over the archive and diff it against the deployed model"
```

---

### Task 6: CLI and a real run against B4's archive

**Files:**
- Create: `scripts/replay_admission.py`
- Test: `tests/test_replay_cli.py`

**Interfaces:**
- Consumes: Tasks 1–5.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_replay_cli.py
import subprocess
import sqlite3
import sys


def _db(tmp_path):
    path = tmp_path / "t.db"
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE L1_all_arrivals ("
        " channel TEXT, timestamp_utc TEXT, minute_boundary_utc INTEGER,"
        " station TEXT, frequency_mhz REAL, utc_second INTEGER,"
        " peak_rank INTEGER, arrival_ms REAL, timing_error_ms REAL,"
        " corr_snr_db REAL, peak_value REAL, model_expected_ms REAL)")
    con.execute(
        "INSERT INTO L1_all_arrivals VALUES"
        " ('SHARED_10000','',1788196740,'WWV',10.0,1788196741,0,"
        "  59004.0,0.0,25.0,1.0,4.24)")
    con.commit()
    con.close()
    return path


def test_cli_runs_and_reports(tmp_path):
    db = _db(tmp_path)
    out = subprocess.run(
        [sys.executable, "scripts/replay_admission.py", str(db),
         "--channel", "SHARED_10000"],
        capture_output=True, text=True, timeout=300)
    assert out.returncode == 0, out.stderr
    assert "minutes replayed" in out.stdout
    assert "state counts" in out.stdout
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_replay_cli.py -v -p no:cacheprovider`
Expected: FAIL — script does not exist, returncode 2

- [ ] **Step 3: Write the CLI**

```python
#!/usr/bin/env python3
"""Replay the three-key admission cascade against an archived timestd.db.

Read-only.  Never connects to a station.

    scripts/replay_admission.py /path/to/copy-of-timestd.db \
        --channel SHARED_10000 --floor-snr-db 10.0

⚠ The thresholds below are PROVISIONAL.  Their values are what this harness
exists to determine; do not treat the defaults as settled policy.
"""

import argparse
import sys

from hf_timestd.replay.report import summarise
from hf_timestd.replay.runner import replay
from hf_timestd.replay.window_source import WindowSource

# AC0G / B4, Columbia MO.
DEFAULT_LAT, DEFAULT_LON = 38.9187497, -92.1277207


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("db_path")
    p.add_argument("--channel", default=None)
    p.add_argument("--start-utc", type=int, default=None)
    p.add_argument("--end-utc", type=int, default=None)
    p.add_argument("--floor-snr-db", type=float, default=10.0)
    p.add_argument("--tolerance-ms", type=float, default=1.0)
    p.add_argument("--lookback", type=int, default=10)
    p.add_argument("--reacquire-after", type=int, default=3)
    p.add_argument("--reference-sigma-ms", type=float, default=0.7)
    p.add_argument("--lat", type=float, default=DEFAULT_LAT)
    p.add_argument("--lon", type=float, default=DEFAULT_LON)
    args = p.parse_args(argv)

    source = WindowSource(receiver_lat=args.lat, receiver_lon=args.lon,
                          reference_sigma_ms=args.reference_sigma_ms)
    filters = {}
    if args.channel:
        filters["channel"] = args.channel
    if args.start_utc:
        filters["start_utc"] = args.start_utc
    if args.end_utc:
        filters["end_utc"] = args.end_utc

    report = summarise(replay(
        args.db_path, source, floor_snr_db=args.floor_snr_db,
        tolerance_ms=args.tolerance_ms, lookback=args.lookback,
        reacquire_after=args.reacquire_after, **filters))
    print(report.render())
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_replay_cli.py -v -p no:cacheprovider`
Expected: PASS, 1 passed

- [ ] **Step 5: Pull a read-only copy of B4's archive**

⚠ This is the only step that touches the station, and it is a READ. Do not
run it while a deploy is in progress.

```bash
mkdir -p ~/hamsci/archive-copy-20260901
ssh root@192.168.1.244 "ssh hamsci@192.168.1.176 'sudo -n cat /var/lib/timestd/phase2/timestd.db'" \
  > ~/hamsci/archive-copy-20260901/timestd.db
ls -la ~/hamsci/archive-copy-20260901/timestd.db   # expect ~16 GB
```

- [ ] **Step 6: Run the real replay and record the numbers**

```bash
.venv/bin/python scripts/replay_admission.py \
    ~/hamsci/archive-copy-20260901/timestd.db \
    --channel SHARED_10000 | tee /tmp/replay-shared10000.txt
```

Expected: a state distribution over the archived minutes. Record the result in
the spec — these are the numbers steps 2–5 of the spec depend on. Pay
particular attention to `deployed_under_reports`: over 6461 gated ensembles
the live gate recorded **zero** cases of the cascade naming a station the
deployed model missed, so a non-zero count here needs explaining before
anything proceeds.

- [ ] **Step 7: Run the whole suite**

Run: `.venv/bin/python -m pytest tests/ -q -p no:cacheprovider`
Expected: 2722 + 25 new = 2747 passed

- [ ] **Step 8: Commit**

```bash
git add scripts/replay_admission.py tests/test_replay_cli.py
git commit -m "replay: CLI, and the first run against B4's archive"
```

---

## Done when

- All six tasks committed, full suite green.
- A replay report exists for at least one shared channel over a full diurnal
  cycle, showing how often each of the seven states fires.
- The `deployed_over_reports` / `deployed_under_reports` counts are recorded in
  the spec alongside the live gate's 2776 / 0.
- No station behaviour has changed, no service touched, no config edited.

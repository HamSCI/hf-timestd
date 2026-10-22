"""Fusion feeds each L1 row once (fusion Kalman review, stage 0).

Fusion reads every L1 row in a 30-minute lookback on every ~8 s cycle,
for both feeds.  Before this change it handed every row in the window to
every stateful estimator on every cycle:

  - each per-broadcast Kalman filter ran about 200 updates per row, and
    stepped its model by a fixed minute per update, so it believed 30
    minutes passed every cycle;
  - the long-term drift regression took every row on every cycle,
    stamped with the cycle's time, so its n ran about 400 times the row
    count and it raised "SIGNIFICANT GPSDO DRIFT" on data with no drift;
  - the calibration's history took every row twice a cycle.  That one
    stays for now: feeding the history each row once moved fused D_clock
    on AC0G-ND's recorded rows by 2 ms rms, which no feeding fix may do
    without a ruling.

Michael ruled on 2026-10-22: feed each row once
(docs/design/review-fusion-kalman-20261022.md, section 15).  These tests
pin that down.  Each one fails on the replaying code; the commit message
names the mutation that makes each fail again.
"""

import logging
import tempfile
import time
from pathlib import Path

import numpy as np
import pytest

from hf_timestd.core.broadcast_kalman_filter import BroadcastKalmanFilter
from hf_timestd.core.fusion_loop_metrics import FusionLoopMetrics
from hf_timestd.core.multi_broadcast_fusion import (
    BroadcastMeasurement,
    MultiBroadcastFusion,
)

KALMAN_LOGGER = "hf_timestd.core.broadcast_kalman_filter"
FUSION_LOGGER = "hf_timestd.core.multi_broadcast_fusion"

# Four broadcasts with fixed model biases, as in the review's simulation.
BCASTS = [("WWV", 5.0, +3.0), ("WWV", 10.0, -2.0),
          ("WWV", 15.0, +5.0), ("WWVH", 10.0, +8.0)]
CYCLE_S = 8.2
LOOKBACK_MIN = 30


def _row(t, station, freq, d_clock, snr=15.0):
    return BroadcastMeasurement(
        timestamp=t, station=station, frequency_mhz=freq, d_clock_ms=d_clock,
        propagation_delay_ms=10.0, propagation_mode="1F", confidence=0.9,
        snr_db=snr, quality_grade="A", channel_name=f"{station}_{freq}",
    )


class SlidingWindowRun:
    """Drive fuse() as the service does: L1 pass then L2 pass every cycle,
    each over the rows of the last 30 minutes.  One row per broadcast per
    minute; no drift; 1.5 ms white noise about a fixed bias."""

    def __init__(self, td, minutes, sigma=1.5, seed=3, gaps=()):
        rng = np.random.default_rng(seed)
        # Whole minute, so row k sits exactly k minutes after t_base.
        self.t_base = float(int(time.time() // 60) * 60)
        self.minutes = minutes
        # gaps: (station, first minute, last minute) with no rows.
        self.rows = {
            k: [(s, f, b + rng.normal(0.0, sigma)) for s, f, b in BCASTS
                if not any(s == gs and g0 <= k <= g1 for gs, g0, g1 in gaps)]
            for k in range(minutes + 1)
        }
        self.now_min = 0.0
        fu = MultiBroadcastFusion(data_root=Path(td))
        fu.physics_model = None
        fu.arrival_matrix = None
        fu._run_global_differential_solve = lambda lookback_minutes=30: (None, 0)
        fu._read_tick_timing_observations = lambda lb: {}
        fu._read_gnss_vtec = lambda: None
        fu._read_latest_measurements = self.read
        self.fusion = fu
        self.cycles = 0

    def row_time(self, k):
        return self.t_base + 60.0 * k

    def read(self, lookback_minutes=30, force_l1_only=False):
        newest = int(np.floor(self.now_min))
        out = []
        for k in range(max(0, newest - lookback_minutes + 1), newest + 1):
            for s, f, d in self.rows[k]:
                out.append(_row(self.row_time(k), s, f, d))
        return out

    def distinct_rows_seen(self):
        return int(np.floor(self.now_min)) + 1

    def cycle(self):
        self.now_min = self.cycles * CYCLE_S / 60.0
        r1 = self.fusion.fuse(lookback_minutes=LOOKBACK_MIN,
                              force_l1_only=True, skip_write=True)
        r2 = self.fusion.fuse(lookback_minutes=LOOKBACK_MIN,
                              force_l1_only=False, skip_write=True)
        self.cycles += 1
        return r1, r2

    def fuse_at(self, now_min):
        self.now_min = now_min
        self.fusion.fuse(lookback_minutes=LOOKBACK_MIN,
                         force_l1_only=True, skip_write=True)
        self.fusion.fuse(lookback_minutes=LOOKBACK_MIN,
                         force_l1_only=False, skip_write=True)

    def run_minutes(self, minutes):
        n = int(minutes * 60 / CYCLE_S)
        for _ in range(n):
            self.cycle()


def _count_mode_changes(monkeypatch):
    """Count every MODE_CHANGE verdict any filter reaches."""
    counter = {"n": 0}
    orig = BroadcastKalmanFilter.detect_mode_transition

    def spy(self, innovation_ms):
        verdict = orig(self, innovation_ms)
        if verdict == "MODE_CHANGE":
            counter["n"] += 1
        return verdict

    monkeypatch.setattr(BroadcastKalmanFilter, "detect_mode_transition", spy)
    return counter


# ---------------------------------------------------------------------------
# The filter keeps time by the row's own timestamp
# ---------------------------------------------------------------------------

class TestFilterKeepsRowTime:

    def test_predict_spans_the_real_gap(self):
        """A row ten minutes after the last one adds ten minutes of
        process noise, and the delay state moves by ten minutes of rate."""
        kf = BroadcastKalmanFilter("WWV_10000", "WWV", 10.0)
        t0 = 1.8e9
        kf.update(17.0, snr_db=20.0, t_obs=t0)
        # A known posterior: tof 17 ms, rate 0.02 ms/min, P = diag(0.5, 0).
        kf.state = np.array([17.0, 0.02])
        kf.P = np.array([[0.5, 0.0], [0.0, 0.0]])
        # Measure exactly the predicted delay, so the innovation is zero
        # and no adaptive multiplier fires (SNR 20 dB: snr scale 1).
        kf.update(17.0 + 0.02 * 10.0, snr_db=20.0, t_obs=t0 + 600.0)
        q_tof = kf.characteristics.q_tof          # 0.3 ms² per minute
        assert kf.last_dt_min == pytest.approx(10.0)
        # F P Fᵀ [0,0] = 0.5 + 2·10·0 + 10²·0 = 0.5; plus Q·dt = 0.3·10.
        assert kf.last_prior_P[0, 0] == pytest.approx(0.5 + 10.0 * q_tof)
        assert kf.last_innovation == pytest.approx(0.0, abs=1e-12)

    def test_duplicate_and_older_rows_are_refused_and_counted(self, caplog):
        kf = BroadcastKalmanFilter("WWV_10000", "WWV", 10.0)
        t0 = 1.8e9
        kf.update(17.0, snr_db=20.0, t_obs=t0)
        kf.update(17.4, snr_db=20.0, t_obs=t0 + 60.0)
        state, P, n = kf.state.copy(), kf.P.copy(), kf.n_updates
        caplog.set_level(logging.DEBUG, logger=KALMAN_LOGGER)

        out_dup = kf.update(90.0, snr_db=20.0, t_obs=t0 + 60.0)   # duplicate
        out_old = kf.update(90.0, snr_db=20.0, t_obs=t0)          # older

        assert kf.n_refused == 2
        assert kf.n_updates == n
        np.testing.assert_array_equal(kf.state, state)
        np.testing.assert_array_equal(kf.P, P)
        assert out_dup[0] == pytest.approx(state[0])
        assert out_old[0] == pytest.approx(state[0])
        refusals = [r for r in caplog.records
                    if r.name == KALMAN_LOGGER and "refused" in r.getMessage()]
        assert len(refusals) == 2
        assert all(r.levelno == logging.DEBUG for r in refusals)

    def test_last_row_time_survives_a_restart(self, tmp_path):
        kf = BroadcastKalmanFilter("WWV_10000", "WWV", 10.0)
        t0 = 1.8e9
        kf.update(17.0, snr_db=20.0, t_obs=t0)
        kf.update(17.4, snr_db=20.0, t_obs=t0 + 60.0)
        kf.save_state(tmp_path)

        kf2 = BroadcastKalmanFilter("WWV_10000", "WWV", 10.0)
        assert kf2.load_state(tmp_path)
        assert kf2.last_obs_time == t0 + 60.0
        kf2.update(90.0, snr_db=20.0, t_obs=t0 + 60.0)
        assert kf2.n_refused == 1
        kf2.update(17.5, snr_db=20.0, t_obs=t0 + 300.0)
        assert kf2.last_dt_min == pytest.approx(4.0)

    def test_a_gap_of_hours_restarts_the_filter(self, caplog):
        """After a long outage the motion model means nothing; the filter
        starts again from the row, with a wide P, and says so once."""
        kf = BroadcastKalmanFilter("WWV_10000", "WWV", 10.0)
        t0 = 1.8e9
        kf.update(17.0, snr_db=20.0, t_obs=t0)
        kf.state = np.array([17.0, 0.5])            # a wild rate
        caplog.set_level(logging.INFO, logger=KALMAN_LOGGER)
        tof, sigma = kf.update(4.0, snr_db=20.0,
                               t_obs=t0 + 3600.0 * 24)
        assert tof == pytest.approx(4.0)
        assert kf.state[1] == 0.0
        assert sigma == pytest.approx(10.0)
        lines = [r for r in caplog.records if "re-initialised" in r.getMessage()]
        assert len(lines) == 1


# ---------------------------------------------------------------------------
# Fusion hands each row to each filter once
# ---------------------------------------------------------------------------

class TestFusionFeedsEachRowOnce:

    def test_n_updates_equals_distinct_rows(self):
        with tempfile.TemporaryDirectory() as td:
            run = SlidingWindowRun(td, minutes=90)
            run.run_minutes(90)
            rows = run.distinct_rows_seen()
            assert rows > 80
            assert len(run.fusion.broadcast_kalmans) == 2 * len(BCASTS)
            for key, kf in run.fusion.broadcast_kalmans.items():
                assert kf.n_updates == rows, key

    def test_filters_step_by_the_real_row_spacing(self):
        """WWVH goes quiet for minutes 25-34: its first row back, at 35,
        predicts across the 11 minutes since row 24; WWV steps by 1."""
        with tempfile.TemporaryDirectory() as td:
            run = SlidingWindowRun(td, minutes=40, gaps=[("WWVH", 25, 34)])
            run.run_minutes(20)
            run.fuse_at(35.05)
            for key, kf in run.fusion.broadcast_kalmans.items():
                expected = 11.0 if "WWVH" in key else 1.0
                assert kf.last_dt_min == pytest.approx(expected), key
                assert kf.last_obs_time == run.row_time(35), key

    def test_a_consumed_row_keeps_its_output(self):
        """The WLS sees every row in the lookback, as before; a row the
        filter consumed on an earlier cycle carries the output the filter
        gave it then."""
        with tempfile.TemporaryDirectory() as td:
            run = SlidingWindowRun(td, minutes=20)
            run.run_minutes(5)
            fu = run.fusion
            first = {m.timestamp: (m.d_clock_ms, m.kalman_uncertainty_ms)
                     for m in fu._apply_broadcast_kalmans(
                         [r for r in run.read() if r.station == "WWVH"],
                         feed="l2")}
            run.run_minutes(5)
            again = {m.timestamp: (m.d_clock_ms, m.kalman_uncertainty_ms)
                     for m in fu._apply_broadcast_kalmans(
                         [r for r in run.read() if r.station == "WWVH"],
                         feed="l2")}
            shared = set(first) & set(again)
            assert len(shared) >= 4
            for t in shared:
                assert again[t] == first[t]

    def test_an_unseen_older_row_is_refused_and_counted(self, caplog):
        """A row older than the filter's last update that the filter never
        consumed (it arrived late) is refused, counted, and left out."""
        with tempfile.TemporaryDirectory() as td:
            run = SlidingWindowRun(td, minutes=20)
            run.run_minutes(10)
            fu = run.fusion
            kf = fu.broadcast_kalmans["l2:WWV_10000"]
            n_before = kf.n_updates
            late = _row(kf.last_obs_time - 30.0, "WWV", 10.0, 40.0)
            caplog.set_level(logging.DEBUG, logger=FUSION_LOGGER)
            out = fu._apply_broadcast_kalmans([late], feed="l2")
            assert out == []
            assert kf.n_updates == n_before
            assert fu.kalman_row_counts["l2"]["refused"] == 1
            assert any("refused" in r.getMessage() for r in caplog.records
                       if r.name == FUSION_LOGGER
                       and r.levelno == logging.DEBUG)

    def test_refusals_reach_fusion_metrics(self, tmp_path):
        with tempfile.TemporaryDirectory() as td:
            run = SlidingWindowRun(td, minutes=20)
            metrics = FusionLoopMetrics(watchdog_sec=120.0,
                                        path=tmp_path / "fusion_metrics.json")
            run.fusion.loop_metrics = metrics
            metrics.start_cycle()
            run.run_minutes(3)
            kf = run.fusion.broadcast_kalmans["l2:WWV_10000"]
            run.fusion._apply_broadcast_kalmans(
                [_row(kf.last_obs_time - 30.0, "WWV", 10.0, 40.0)], feed="l2")
            payload = metrics.finalize_and_emit()
            counts = payload["counts"]
            assert counts["kalman_rows_refused"] == 1
            assert counts["kalman_rows_fed"] == 2 * len(BCASTS) * run.distinct_rows_seen()
            assert counts["kalman_rows_reused"] > counts["kalman_rows_fed"]

    def test_mode_transitions_fall_with_the_replay(self, monkeypatch):
        """A filter can flag a mode change only on an update, and it now
        updates once per row: at most one flag per row per bank, against
        about 200 per row when every cycle replayed the window."""
        changes = _count_mode_changes(monkeypatch)
        with tempfile.TemporaryDirectory() as td:
            run = SlidingWindowRun(td, minutes=60, sigma=3.0)
            run.run_minutes(60)
            rows_fed = 2 * len(BCASTS) * run.distinct_rows_seen()
            per_cycle = changes["n"] / run.cycles
            assert changes["n"] <= rows_fed
            # A new row arrives every ~7 cycles per broadcast: under 2 per
            # cycle across eight filters, against ~100+ under replay.
            assert per_cycle < 2.0

    def test_restart_does_not_replay_the_window(self):
        """The filter's outputs persist with its state, so a restarted
        fusion serves the rows the old process consumed and feeds only
        the new ones."""
        with tempfile.TemporaryDirectory() as td:
            run = SlidingWindowRun(td, minutes=40)
            run.run_minutes(20)
            run.fusion._save_broadcast_kalman_states()
            before = {k: kf.n_updates for k, kf in run.fusion.broadcast_kalmans.items()}
            window = run.read()

            fu2 = MultiBroadcastFusion(data_root=Path(td))
            fu2.physics_model = None
            out = fu2._apply_broadcast_kalmans(window, feed="l2")
            assert len(out) == len(window)
            for k, kf in fu2.broadcast_kalmans.items():
                if k.startswith("l2:"):
                    assert kf.n_updates == before[k], k

    def test_leap_second_hold_coasts_each_row_once(self):
        """During a hold each new row advances the filter once, by its real
        gap; a cycle with no new row leaves the filter where it was."""
        with tempfile.TemporaryDirectory() as td:
            run = SlidingWindowRun(td, minutes=40)
            run.run_minutes(10)
            fu = run.fusion
            fu._leap_second_hold_active = lambda now=None: True
            run.fuse_at(10.05)                 # consumes row 10, if new
            kf = fu.broadcast_kalmans["l2:WWV_10000"]
            assert kf.last_obs_time == run.row_time(10)
            kf.state = np.array([kf.state[0], 0.01])
            s1, p1, n1 = kf.state.copy(), kf.P.copy(), kf.n_updates
            run.fuse_at(10.2)                  # no new row
            run.fuse_at(10.4)                  # no new row
            np.testing.assert_array_equal(kf.state, s1)
            np.testing.assert_array_equal(kf.P, p1)
            run.fuse_at(12.0)                  # rows 11 and 12: two coasts
            assert kf.n_updates == n1          # a coast is not an update
            assert kf.state[0] == pytest.approx(s1[0] + 0.01 * 2.0)
            assert kf.last_obs_time == run.row_time(12)


# ---------------------------------------------------------------------------
# Long-term statistics and calibration history take each row once
# ---------------------------------------------------------------------------

def _windows(t_base, minutes, passes_per_minute=7):
    """The lookbacks fuse() hands its consumers: ~7 cycles a minute, two
    passes (L1, L2) a cycle, each over the last 30 minutes of rows."""
    rng = np.random.default_rng(11)
    rows = [[_row(t_base + 60.0 * k, s, f, b + rng.normal(0.0, 1.5))
             for s, f, b in BCASTS] for k in range(minutes)]
    for newest in range(minutes):
        window = [m for k in range(max(0, newest - LOOKBACK_MIN + 1), newest + 1)
                  for m in rows[k]]
        for _ in range(passes_per_minute):
            yield window       # L1 pass
            yield window       # L2 pass


class TestLongTermStatsTakeEachRowOnce:

    def test_each_row_once_at_its_own_time(self):
        minutes = 50
        t_base = 1.8e9
        times = [t_base + 60.0 * k for k in range(minutes)]
        with tempfile.TemporaryDirectory() as td:
            fu = MultiBroadcastFusion(data_root=Path(td))
            for window in _windows(t_base, minutes):
                fu._update_long_term_stats(window)
            assert len(fu.long_term_stats) == len(BCASTS)
            for key, stats in fu.long_term_stats.items():
                assert stats["n"] == minutes, key
                assert stats["sum_t"] == pytest.approx(
                    sum(t / 3600.0 for t in times), rel=1e-12), key
                assert stats["sum_tt"] == pytest.approx(
                    sum((t / 3600.0) ** 2 for t in times), rel=1e-12), key
                assert stats["first_time"] == times[0], key
                assert stats["last_time"] == times[-1], key

    def test_fuse_feeds_the_statistics_each_row_once(self):
        """Through fuse(): never more than one entry per row (a row the
        outlier gate rejects on its first cycle may never enter)."""
        with tempfile.TemporaryDirectory() as td:
            run = SlidingWindowRun(td, minutes=60)
            run.run_minutes(60)
            rows = run.distinct_rows_seen()
            for key, stats in run.fusion.long_term_stats.items():
                assert rows - 3 <= stats["n"] <= rows, key
                assert stats["last_time"] <= run.row_time(rows - 1), key

    def test_no_drift_alarm_on_a_four_hour_null(self, caplog):
        caplog.set_level(logging.WARNING, logger=FUSION_LOGGER)
        with tempfile.TemporaryDirectory() as td:
            run = SlidingWindowRun(td, minutes=240)
            run.run_minutes(240)
            alarms = [r for r in caplog.records
                      if "SIGNIFICANT GPSDO DRIFT" in r.getMessage()]
            assert alarms == []
            est = run.fusion.get_long_term_drift_estimate()
            assert abs(est["slope_ppb"]) < 3 * est["slope_uncertainty_ppb"]
            assert est["n_samples"] <= len(BCASTS) * run.distinct_rows_seen()

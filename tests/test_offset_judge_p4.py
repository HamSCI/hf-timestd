"""Offset Judge P4 tests (docs/OFFSET-JUDGE-SPEC-2026-08-05.md §9 + §13).

The escalation ladder and the §18 export surface:

  * step 2 — sustained (>= alert_after_s) in_violation OR rate_alarm
    emits ONE alert through the freshness-alert channel
    (`logger -t hf-timestd-alert -p user.crit` + state file + optional
    mail), with cooldown; clear resets the channel;
  * classification honesty: constant lag => radiod-epoch-fault,
    measured slope => rate-disagreement;
  * step 3 — opt-in (DEFAULT FALSE) radiod restart REQUEST artifact:
    never written when disabled (even at 10 h), atomic
    radiod-restart-request-v1 at the threshold when enabled, withdrawn
    on clear, 6 h re-request cooldown, adoption across judge restarts;
  * §18 contract_v07 subscriber surface in offset_judge.json: field
    names verbatim, judged values, nulls before a verdict;
  * atomicity: tmp+rename writes leave no *.tmp behind.

Runnable with SYSTEM python3 + numpy only (P1 namespace bootstrap).
"""
from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import types
from datetime import datetime, timezone
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"


def _install_namespace() -> None:
    """Register hf_timestd / hf_timestd.core without running __init__.py."""
    if "hf_timestd" not in sys.modules:
        pkg = types.ModuleType("hf_timestd")
        pkg.__path__ = [str(SRC / "hf_timestd")]
        sys.modules["hf_timestd"] = pkg
    if "hf_timestd.core" not in sys.modules:
        core = types.ModuleType("hf_timestd.core")
        core.__path__ = [str(SRC / "hf_timestd" / "core")]
        sys.modules["hf_timestd.core"] = core
    try:
        import toml  # noqa: F401
    except ImportError:
        stub = types.ModuleType("toml")
        stub.load = lambda *a, **k: {}
        stub.loads = lambda *a, **k: {}
        sys.modules["toml"] = stub


_install_namespace()

oj = importlib.import_module("hf_timestd.core.offset_judge")

OffsetJudge = oj.OffsetJudge
BenchReading = oj.BenchReading
RateEstimate = oj.RateEstimate

WALL0 = 1_800_000_000.0
KEY = ("hf-status.local", 0x1234ABCD)
KEY_STR = "hf-status.local/1234abcd"


def unix_to_gps_ns(unix_s: float) -> int:
    """Invert gps_time_ns_to_unix without hardcoding the leap count."""
    guess = int((unix_s - 315964800) * 1e9)
    back = oj.gps_time_ns_to_unix(guess)
    return guess + int(round((unix_s - back) * 1e9))


class FakeClock:
    def __init__(self, wall: float = WALL0, mono: float = 1000.0):
        self.wall = wall
        self.mono_v = mono

    def time(self) -> float:
        return self.wall

    def mono(self) -> float:
        return self.mono_v

    def advance(self, dt: float) -> None:
        self.wall += dt
        self.mono_v += dt


class FakeBench:
    """Bench whose truth is wallclock + bench_error (0 = perfect)."""

    def __init__(self, clock: FakeClock, tier: str = "T4",
                 sigma_ns: float = 1e5, bench_error_s: float = 0.0,
                 available: bool = True, drift_ppm: float = 0.0):
        self.clock = clock
        self.tier = tier
        self.sigma_ns = sigma_ns
        self.bench_error_s = bench_error_s
        self.available = available
        self.drift_ppm = drift_ppm       # bench truth walks vs radiod
        self._t0 = clock.wall

    def poll(self):
        if not self.available:
            return None
        drift_s = self.drift_ppm * 1e-6 * (self.clock.wall - self._t0)
        return BenchReading(
            tier=self.tier,
            utc=self.clock.wall + self.bench_error_s + drift_s,
            sigma_ns=self.sigma_ns,
            mono=self.clock.mono_v,
        )


class RecordingRunner:
    """Injectable subprocess.run stand-in — records every invocation."""

    def __init__(self):
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        return subprocess.CompletedProcess(argv, 0, "", "")

    def logger_calls(self):
        return [c for c in self.calls if c[0][0] == "logger"]

    def mail_calls(self):
        return [c for c in self.calls if c[0][0] == "mail"]


def make_judge(clock: FakeClock, tmp_path: Path, bench,
               runner: RecordingRunner, **cfg) -> OffsetJudge:
    config = {
        "enabled": True,
        "tick_seconds": 10.0,
        "alert_state_path": str(tmp_path / "state" / "alert_sent"),
        "restart_request_path": str(
            tmp_path / "run" / "radiod-restart-request.json"),
        **cfg,
    }
    benches = bench if isinstance(bench, list) else [bench]
    return OffsetJudge(
        config=config,
        benches=benches,
        publish_path=tmp_path / "offset_judge.json",
        time_fn=clock.time,
        mono_fn=clock.mono,
        alert_runner=runner,
    )


def drive(judge: OffsetJudge, clock: FakeClock, seconds: float,
          step: float = 10.0) -> None:
    """Tick then advance, `seconds` of fake time total."""
    t = 0.0
    while t < seconds:
        judge.tick()
        clock.advance(step)
        t += step


def wedge(judge: OffsetJudge, clock: FakeClock, error_s: float = -2.0,
          snap: int = 5000, rate: int = 24000, key=KEY) -> None:
    """Register a radiod pair whose epoch is wrong by -error_s
    (bench-truth minus radiod => offset = -error_s ... i.e. error_s=-2
    yields a +2 s judged offset)."""
    judge.register_radiod_pair(
        key, unix_to_gps_ns(clock.wall + error_s), snap, rate)


def read_json(path: Path):
    return json.loads(path.read_text())


# ────────────────────────────────────────────────────────────────────
# Ladder step 2 — the alert
# ────────────────────────────────────────────────────────────────────

class TestAlertLadderTiming:
    def test_no_alert_below_alert_after_s(self, tmp_path):
        clock = FakeClock()
        runner = RecordingRunner()
        judge = make_judge(clock, tmp_path, FakeBench(clock), runner)
        wedge(judge, clock)          # +2 s offset >> 5*sigma
        drive(judge, clock, 800.0)   # sustained < 900 s
        assert runner.logger_calls() == []
        assert not (tmp_path / "state" / "alert_sent").exists()
        # ... but the step-1 flag is up (spec §9.1 precedes step 2)
        snap = read_json(tmp_path / "offset_judge.json")
        assert snap["sources"][KEY_STR]["in_violation"] is True
        assert snap["sources"][KEY_STR]["escalation"]["alert_active"] is False

    def test_alert_once_at_threshold_then_cooldown(self, tmp_path):
        clock = FakeClock()
        runner = RecordingRunner()
        judge = make_judge(clock, tmp_path, FakeBench(clock), runner)
        wedge(judge, clock)
        drive(judge, clock, 1000.0)  # crosses 900 s sustained
        assert len(runner.logger_calls()) == 1
        assert (tmp_path / "state" / "alert_sent").exists()
        snap = read_json(tmp_path / "offset_judge.json")
        assert snap["sources"][KEY_STR]["escalation"]["alert_active"] is True
        # Violation persists another 30 min: still just the one alert.
        drive(judge, clock, 1800.0, step=60.0)
        assert len(runner.logger_calls()) == 1

    def test_realert_after_cooldown_expires(self, tmp_path):
        clock = FakeClock()
        runner = RecordingRunner()
        judge = make_judge(clock, tmp_path, FakeBench(clock), runner)
        wedge(judge, clock)
        drive(judge, clock, 1000.0)
        assert len(runner.logger_calls()) == 1
        drive(judge, clock, 3700.0, step=60.0)  # cooldown (3600 s) passes
        assert len(runner.logger_calls()) == 2

    def test_clear_resets_alert_channel(self, tmp_path):
        clock = FakeClock()
        runner = RecordingRunner()
        judge = make_judge(clock, tmp_path, FakeBench(clock), runner)
        wedge(judge, clock)
        drive(judge, clock, 1000.0)
        assert len(runner.logger_calls()) == 1
        # radiod fixed (correct pair adopted) -> violation clears.
        wedge(judge, clock, error_s=0.0, snap=900000)
        drive(judge, clock, 120.0)
        assert not (tmp_path / "state" / "alert_sent").exists()
        snap = read_json(tmp_path / "offset_judge.json")
        assert snap["sources"][KEY_STR]["escalation"]["alert_active"] is False
        assert snap["sources"][KEY_STR]["escalation"]["sustained_s"] is None
        # Fresh violation alerts again on ITS OWN 900 s clock — the old
        # cooldown was reset with the clear.
        wedge(judge, clock, error_s=-3.0, snap=1800000)
        drive(judge, clock, 1000.0)
        assert len(runner.logger_calls()) == 2

    def test_preexisting_state_file_suppresses_within_cooldown(self, tmp_path):
        """The state file is the cross-restart half of the cooldown
        (mirror of check-freshness-alert.sh ALERT_FILE)."""
        clock = FakeClock()
        runner = RecordingRunner()
        judge = make_judge(clock, tmp_path, FakeBench(clock), runner)
        state = tmp_path / "state" / "alert_sent"
        state.parent.mkdir(parents=True)
        state.touch()
        os.utime(state, (WALL0 - 100.0, WALL0 - 100.0))  # 100 s ago
        wedge(judge, clock)
        drive(judge, clock, 1000.0)
        assert runner.logger_calls() == []  # suppressed by state file


class TestAlertContent:
    def _alert_text(self, runner: RecordingRunner) -> str:
        calls = runner.logger_calls()
        assert len(calls) >= 1
        argv = calls[0][0]
        assert argv[:5] == ["logger", "-t", "hf-timestd-alert",
                            "-p", "user.crit"]
        return argv[5]

    def test_epoch_fault_alert_names_the_evidence(self, tmp_path):
        clock = FakeClock()
        runner = RecordingRunner()
        judge = make_judge(clock, tmp_path,
                           FakeBench(clock, tier="T4", sigma_ns=1e5), runner)
        wedge(judge, clock)  # +2 s constant lag
        drive(judge, clock, 1000.0)
        text = self._alert_text(runner)
        assert KEY_STR in text
        assert "radiod hf-status" in text          # radiod_id derived
        assert "radiod-epoch-fault" in text        # classification
        assert "+2000.000 ms" in text              # offset_ms
        assert "5 x 0.1000 ms" in text             # k * sigma bound
        assert "tier T4" in text
        assert "ppm" in text                       # rate line present
        assert "Sustained:" in text

    def test_slope_classified_as_rate_disagreement(self, tmp_path):
        """The offset SERIES actually slopes (5 ppm bench-vs-radiod
        rate disagreement on top of a wedged epoch) — the measured
        slope reclassifies the alert."""
        clock = FakeClock()
        runner = RecordingRunner()
        judge = make_judge(clock, tmp_path,
                           FakeBench(clock, drift_ppm=5.0), runner)
        wedge(judge, clock)
        drive(judge, clock, 1000.0)
        text = self._alert_text(runner)
        assert "rate-disagreement" in text
        assert "offset-slope" in text
        assert "SLOPING" in text

    def test_rate_alarm_alone_reaches_the_alert_rung(self, tmp_path):
        """in_violation OR rate_alarm — a pure rate fault (3 ppm walk,
        offset still far inside the wide k*sigma bound) escalates."""
        clock = FakeClock()
        runner = RecordingRunner()
        judge = make_judge(
            clock, tmp_path,
            FakeBench(clock, sigma_ns=1e9, drift_ppm=3.0), runner)
        wedge(judge, clock, error_s=0.0)   # epoch correct
        drive(judge, clock, 1200.0)
        snap = read_json(tmp_path / "offset_judge.json")
        assert snap["sources"][KEY_STR]["in_violation"] is False
        assert snap["sources"][KEY_STR]["rate_alarm"] is True
        assert len(runner.logger_calls()) == 1
        assert "rate-disagreement" in self._alert_text(runner)

    def test_mail_sent_when_env_configured(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TIMESTD_ALERT_EMAIL", "op@example.net")
        clock = FakeClock()
        runner = RecordingRunner()
        judge = make_judge(clock, tmp_path, FakeBench(clock), runner)
        wedge(judge, clock)
        drive(judge, clock, 1000.0)
        mails = runner.mail_calls()
        assert len(mails) == 1
        argv, kwargs = mails[0]
        assert argv[-1] == "op@example.net"
        assert "[hf-timestd]" in argv[2]
        assert "radiod-epoch-fault" in kwargs.get("input", "")

    def test_no_mail_without_env(self, tmp_path, monkeypatch):
        monkeypatch.delenv("TIMESTD_ALERT_EMAIL", raising=False)
        clock = FakeClock()
        runner = RecordingRunner()
        judge = make_judge(clock, tmp_path, FakeBench(clock), runner)
        wedge(judge, clock)
        drive(judge, clock, 1000.0)
        assert runner.mail_calls() == []


# ────────────────────────────────────────────────────────────────────
# Ladder step 3 — the opt-in restart REQUEST
# ────────────────────────────────────────────────────────────────────

class TestRestartRequestGating:
    def test_disabled_by_default_never_writes_even_at_10h(self, tmp_path):
        clock = FakeClock()
        runner = RecordingRunner()
        judge = make_judge(clock, tmp_path, FakeBench(clock), runner)
        assert judge.radiod_restart_request is False   # site-policy default
        wedge(judge, clock)
        drive(judge, clock, 36000.0, step=60.0)        # 10 hours
        assert not (tmp_path / "run" / "radiod-restart-request.json").exists()
        snap = read_json(tmp_path / "offset_judge.json")
        assert snap["escalation"]["restart_request_enabled"] is False
        assert snap["escalation"]["restart_request"]["active"] is False

    def test_not_written_before_threshold(self, tmp_path):
        clock = FakeClock()
        runner = RecordingRunner()
        judge = make_judge(clock, tmp_path, FakeBench(clock), runner,
                           radiod_restart_request=True)
        wedge(judge, clock)
        drive(judge, clock, 3500.0, step=60.0)
        assert not (tmp_path / "run" / "radiod-restart-request.json").exists()

    def test_artifact_schema_at_threshold(self, tmp_path):
        clock = FakeClock()
        runner = RecordingRunner()
        judge = make_judge(clock, tmp_path,
                           FakeBench(clock, tier="T4", sigma_ns=1e5), runner,
                           radiod_restart_request=True)
        wedge(judge, clock)
        drive(judge, clock, 3700.0, step=60.0)
        req_path = tmp_path / "run" / "radiod-restart-request.json"
        req = read_json(req_path)
        assert req["schema"] == "radiod-restart-request-v1"
        assert req["source_key"] == KEY_STR
        assert req["radiod_id"] == "hf-status"
        assert req["offset_ms"] == pytest.approx(2000.0, abs=1.0)
        assert req["sustained_s"] >= 3600.0
        ev = req["evidence"]
        assert ev["tier"] == "T4"
        assert ev["sigma_ns"] == pytest.approx(1e5, rel=0.01)
        assert ev["classification"] == "radiod-epoch-fault"
        assert "rate_ppm" in ev
        # ISO-8601 Z timestamps; cooldown = requested + 6 h
        req_t = datetime.strptime(
            req["requested_utc"], "%Y-%m-%dT%H:%M:%SZ"
        ).replace(tzinfo=timezone.utc).timestamp()
        cd_t = datetime.strptime(
            req["cooldown_until"], "%Y-%m-%dT%H:%M:%SZ"
        ).replace(tzinfo=timezone.utc).timestamp()
        assert cd_t - req_t == pytest.approx(21600.0, abs=1.0)
        snap = read_json(tmp_path / "offset_judge.json")
        assert snap["escalation"]["restart_request"]["active"] is True
        assert snap["escalation"]["restart_request"]["source"] == KEY_STR
        assert snap["sources"][KEY_STR]["escalation"]["restart_requested"] is True

    def test_spec_name_alias_radiod_restart(self, tmp_path):
        clock = FakeClock()
        runner = RecordingRunner()
        judge = make_judge(clock, tmp_path, FakeBench(clock), runner,
                           radiod_restart=True)
        assert judge.radiod_restart_request is True

    def test_withdrawn_when_violation_clears(self, tmp_path):
        clock = FakeClock()
        runner = RecordingRunner()
        judge = make_judge(clock, tmp_path, FakeBench(clock), runner,
                           radiod_restart_request=True)
        wedge(judge, clock)
        drive(judge, clock, 3700.0, step=60.0)
        req_path = tmp_path / "run" / "radiod-restart-request.json"
        assert req_path.exists()
        wedge(judge, clock, error_s=0.0, snap=900000)   # radiod fixed
        drive(judge, clock, 120.0)
        assert not req_path.exists()
        snap = read_json(tmp_path / "offset_judge.json")
        assert snap["escalation"]["restart_request"]["active"] is False

    def test_rerequest_respects_6h_cooldown(self, tmp_path):
        clock = FakeClock()
        runner = RecordingRunner()
        judge = make_judge(clock, tmp_path, FakeBench(clock), runner,
                           radiod_restart_request=True)
        req_path = tmp_path / "run" / "radiod-restart-request.json"
        wedge(judge, clock)
        drive(judge, clock, 3700.0, step=60.0)
        assert req_path.exists()
        first = read_json(req_path)
        # Clears, then re-wedges: sustained crosses the threshold again
        # well inside the 6 h cooldown -> NO new artifact.
        wedge(judge, clock, error_s=0.0, snap=900000)
        drive(judge, clock, 120.0)
        assert not req_path.exists()
        wedge(judge, clock, error_s=-2.0, snap=1800000)
        drive(judge, clock, 4000.0, step=60.0)
        assert not req_path.exists()
        # Past cooldown_until (violation still sustained) -> re-request.
        drive(judge, clock, 21600.0, step=60.0)
        assert req_path.exists()
        second = read_json(req_path)
        assert second["requested_utc"] != first["requested_utc"]

    def test_adopts_existing_request_and_withdraws_when_clear(self, tmp_path):
        req_path = tmp_path / "run" / "radiod-restart-request.json"
        req_path.parent.mkdir(parents=True)
        req_path.write_text(json.dumps({
            "schema": "radiod-restart-request-v1",
            "requested_utc": "2027-01-15T00:00:00Z",
            "source_key": KEY_STR,
            "radiod_id": "hf-status",
            "offset_ms": 2000.0,
            "sustained_s": 3600.0,
            "evidence": {"tier": "T4", "sigma_ns": 1e5,
                         "rate_ppm": None,
                         "classification": "radiod-epoch-fault"},
            "cooldown_until": "2027-01-15T06:00:00Z",
        }))
        clock = FakeClock()
        runner = RecordingRunner()
        judge = make_judge(clock, tmp_path, FakeBench(clock), runner,
                           radiod_restart_request=True)
        wedge(judge, clock, error_s=0.0)   # healthy source
        # Withdrawal waits out the post-adoption re-arm grace
        # (sustain_window_s + 3 ticks) before trusting "clear".
        drive(judge, clock, 200.0)
        # Adopted, owner clear -> withdrawn: a restart never orphans a
        # request artifact.
        assert not req_path.exists()

    def test_adopted_cooldown_survives_judge_restart(self, tmp_path):
        cd_until = datetime.fromtimestamp(
            WALL0 + 20000.0, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        req_path = tmp_path / "run" / "radiod-restart-request.json"
        req_path.parent.mkdir(parents=True)
        req_path.write_text(json.dumps({
            "schema": "radiod-restart-request-v1",
            "requested_utc": "x",
            "source_key": KEY_STR,
            "radiod_id": "hf-status",
            "offset_ms": 2000.0,
            "sustained_s": 3600.0,
            "evidence": {"tier": "T4", "sigma_ns": 1e5, "rate_ppm": None,
                         "classification": "radiod-epoch-fault"},
            "cooldown_until": cd_until,
        }))
        clock = FakeClock()
        runner = RecordingRunner()
        judge = make_judge(clock, tmp_path, FakeBench(clock), runner,
                           radiod_restart_request=True)
        wedge(judge, clock)               # violating from the start
        drive(judge, clock, 60.0)         # adopt; owner still violating…
        # …but this judge's own source key IS the owner, so the artifact
        # stays until it clears; no double-write inside cooldown either.
        drive(judge, clock, 8000.0, step=60.0)
        # cooldown (WALL0+20000) not yet passed at WALL0+~8060: any
        # withdraw/rewrite cycle is still suppressed.
        content = read_json(req_path)
        assert content["requested_utc"] == "x"  # never rewritten

    def test_disabled_config_withdraws_stale_artifact(self, tmp_path):
        req_path = tmp_path / "run" / "radiod-restart-request.json"
        req_path.parent.mkdir(parents=True)
        req_path.write_text(json.dumps({
            "schema": "radiod-restart-request-v1",
            "source_key": KEY_STR,
            "cooldown_until": "2027-01-15T06:00:00Z",
        }))
        clock = FakeClock()
        runner = RecordingRunner()
        judge = make_judge(clock, tmp_path, FakeBench(clock), runner)
        judge.tick()
        assert not req_path.exists()

    def test_unknown_schema_withdrawn_at_startup(self, tmp_path):
        req_path = tmp_path / "run" / "radiod-restart-request.json"
        req_path.parent.mkdir(parents=True)
        req_path.write_text(json.dumps({"schema": "something-else"}))
        clock = FakeClock()
        runner = RecordingRunner()
        judge = make_judge(clock, tmp_path, FakeBench(clock), runner,
                           radiod_restart_request=True)
        judge.tick()
        assert not req_path.exists()


# ────────────────────────────────────────────────────────────────────
# §18 export surface — contract_v07
# ────────────────────────────────────────────────────────────────────

CONTRACT_FIELDS = (
    "utc_anchor_ns", "tier", "sigma_ns", "snapshot_age_s",
    "rtp_anchor_sample", "rate_samples_per_utc_sec", "radiod_id",
    "host_monotonic_at_anchor", "offset_ns", "rate_ppm", "segment_id",
)


class TestContractV07Export:
    def test_field_names_verbatim(self, tmp_path):
        clock = FakeClock()
        runner = RecordingRunner()
        judge = make_judge(clock, tmp_path, FakeBench(clock), runner)
        wedge(judge, clock)
        drive(judge, clock, 60.0)
        snap = read_json(tmp_path / "offset_judge.json")
        assert "_doc" in snap["contract_v07"]
        entry = snap["contract_v07"]["sources"][KEY_STR]
        for f in CONTRACT_FIELDS:
            assert f in entry, f"§18 field {f} missing"

    def test_judged_values(self, tmp_path):
        clock = FakeClock()
        runner = RecordingRunner()
        judge = make_judge(clock, tmp_path,
                           FakeBench(clock, tier="T4", sigma_ns=1e5), runner)
        wedge(judge, clock, error_s=-2.0, snap=5000, rate=24000)
        gps_unix = oj.gps_time_ns_to_unix(unix_to_gps_ns(clock.wall - 2.0))
        drive(judge, clock, 60.0)
        entry = read_json(
            tmp_path / "offset_judge.json")["contract_v07"]["sources"][KEY_STR]
        assert entry["tier"] == "T4"
        assert entry["sigma_ns"] == pytest.approx(1e5, rel=0.01)
        assert entry["rtp_anchor_sample"] == 5000
        assert entry["rate_samples_per_utc_sec"] == 24000
        assert entry["radiod_id"] == "hf-status"
        assert entry["offset_ns"] == pytest.approx(2e9, rel=1e-3)
        # utc_anchor_ns = judge-CORRECTED anchor: raw + offset ≈ truth
        assert entry["utc_anchor_ns"] / 1e9 == pytest.approx(
            gps_unix + 2.0, abs=0.05)
        assert entry["snapshot_age_s"] is not None
        assert isinstance(entry["host_monotonic_at_anchor"], float)
        assert entry["segment_id"] == 1

    def test_nulls_before_verdict_but_structure_present(self, tmp_path):
        """No bench answer yet: the judged fields are null (a subscriber
        must never mistake raw radiod for judged), the structural
        radiod-side fields are present."""
        clock = FakeClock()
        runner = RecordingRunner()
        judge = make_judge(clock, tmp_path,
                           FakeBench(clock, available=False), runner)
        wedge(judge, clock)
        judge.tick()
        entry = read_json(
            tmp_path / "offset_judge.json")["contract_v07"]["sources"][KEY_STR]
        assert entry["utc_anchor_ns"] is None
        assert entry["tier"] is None
        assert entry["sigma_ns"] is None
        assert entry["snapshot_age_s"] is None
        assert entry["rtp_anchor_sample"] == 5000
        assert entry["rate_samples_per_utc_sec"] == 24000
        assert entry["radiod_id"] == "hf-status"

    def test_rate_ppm_exported_when_measured(self, tmp_path):
        clock = FakeClock()
        runner = RecordingRunner()
        judge = make_judge(clock, tmp_path, FakeBench(clock), runner)
        judge.set_t6_rate_provider(lambda: RateEstimate(
            ppm=0.42, sigma_ppm=0.01, n=50, span_s=300.0,
            source="t6-residual"))
        wedge(judge, clock, error_s=0.0)
        drive(judge, clock, 60.0)
        entry = read_json(
            tmp_path / "offset_judge.json")["contract_v07"]["sources"][KEY_STR]
        assert entry["rate_ppm"] == pytest.approx(0.42, abs=1e-3)


# ────────────────────────────────────────────────────────────────────
# Atomicity
# ────────────────────────────────────────────────────────────────────

class TestAtomicity:
    def test_no_tmp_leftovers_anywhere(self, tmp_path):
        clock = FakeClock()
        runner = RecordingRunner()
        judge = make_judge(clock, tmp_path, FakeBench(clock), runner,
                           radiod_restart_request=True)
        wedge(judge, clock)
        drive(judge, clock, 3700.0, step=60.0)
        # publish + request both written via tmp+rename; no droppings.
        assert list(tmp_path.rglob("*.tmp")) == []
        assert (tmp_path / "offset_judge.json").exists()
        assert (tmp_path / "run" / "radiod-restart-request.json").exists()
        # …and both parse (a torn write would fail here)
        read_json(tmp_path / "offset_judge.json")
        read_json(tmp_path / "run" / "radiod-restart-request.json")

    def test_request_write_is_tmp_rename(self, tmp_path, monkeypatch):
        """The artifact must appear atomically: os.replace from a tmp
        in the same directory."""
        clock = FakeClock()
        runner = RecordingRunner()
        judge = make_judge(clock, tmp_path, FakeBench(clock), runner,
                           radiod_restart_request=True)
        replaced = []
        orig_replace = Path.replace

        def spy_replace(self, target):
            replaced.append((str(self), str(target)))
            return orig_replace(self, target)

        monkeypatch.setattr(Path, "replace", spy_replace)
        wedge(judge, clock)
        drive(judge, clock, 3700.0, step=60.0)
        req = str(tmp_path / "run" / "radiod-restart-request.json")
        pairs = [p for p in replaced if p[1] == req]
        assert pairs, "request artifact was not written via tmp+rename"
        assert pairs[0][0].endswith(".json.tmp")

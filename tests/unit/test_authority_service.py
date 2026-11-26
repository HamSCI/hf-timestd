"""The authority service entry point (Plan D Task 2).

Preflight ruling D6 binds the watchdog: a pet proves the service loop
iterates, not that the manager's ticks succeed.  A failing or stale tick
shows in the unit's STATUS and the log; it never withholds the pet.
Ruling D3 binds the imports: the entry point must not load fusion.
"""
from __future__ import annotations

import logging
import subprocess
import sys
import threading

from hf_timestd.core.authority_config import load_station_config
from hf_timestd.core.authority_service import run_authority_service


class FakeRunner:
    def __init__(self, alive=True, fresh=True, interval_sec=30.0, looping=True):
        self.alive = alive
        self.fresh = fresh
        self.looping = looping
        self.loop_asked_with = None
        self.interval_sec = interval_sec
        self.last_tick_ok_monotonic = None
        self.started = False
        self.stopped = False
        self.stop_timeout = None
        self.fresh_asked_with = None

    def start(self):
        self.started = True

    def stop(self, timeout=5.0):
        self.stopped = True
        self.stop_timeout = timeout
        if getattr(self, "stop_raises", False):
            raise RuntimeError("join failed")

    def is_alive(self):
        return self.alive

    def tick_fresh(self, max_age_s, now_monotonic=None):
        self.fresh_asked_with = max_age_s
        return self.fresh

    def loop_fresh(self, max_age_s, now_monotonic=None):
        self.loop_asked_with = max_age_s
        return self.looping


def _stop_after(stop, sent, n_pets):
    # Backstop: a mutation that withholds pets must fail, not hang.
    t = threading.Timer(3.0, stop.set)
    t.daemon = True
    t.start()

    def notify(msg):
        sent.append(msg)
        if sent.count("WATCHDOG=1") >= n_pets:
            stop.set()
    return notify


def test_ready_then_pets_while_fresh(tmp_path):
    sent, stop = [], threading.Event()
    runner = FakeRunner(alive=True, fresh=True, interval_sec=30.0)
    rc = run_authority_service(tmp_path / "cfg.toml",
                               notify=_stop_after(stop, sent, 2),
                               stop_event=stop, pet_interval_s=0.01,
                               build_runner=lambda c: runner,
                               publish=lambda cfg: None)
    assert rc == 0
    assert sent[0] == "READY=1"
    assert sent.count("WATCHDOG=1") == 2
    assert runner.started and runner.stopped
    assert runner.stop_timeout == 5.0
    assert sent[-1] == "STOPPING=1"


def test_stale_tick_still_pets_and_reports_loudly(tmp_path, caplog):
    """D6: a manager whose ticks fail keeps the unit up and loud."""
    sent, stop = [], threading.Event()
    # interval 0.001 s, so the start-up grace (3 intervals) passes at once.
    runner = FakeRunner(alive=True, fresh=False, interval_sec=0.001)
    with caplog.at_level(logging.WARNING, logger="hf_timestd.core.authority_service"):
        rc = run_authority_service(tmp_path / "cfg.toml",
                                   notify=_stop_after(stop, sent, 5),
                                   stop_event=stop, pet_interval_s=0.01,
                                   build_runner=lambda c: runner,
                                   publish=lambda cfg: None)
    assert rc == 0
    assert sent.count("WATCHDOG=1") == 5
    assert runner.fresh_asked_with == 3 * runner.interval_sec
    stale = [m for m in sent if m.startswith("STATUS=") and "STALE" in m]
    assert stale, sent
    # Reported once on the transition, not on every iteration.
    assert len(stale) == 1
    assert any("STALE" in r.getMessage() for r in caplog.records)


def test_status_returns_to_ok_when_ticks_recover(tmp_path):
    sent, stop = [], threading.Event()
    runner = FakeRunner(alive=True, fresh=False, interval_sec=0.001)

    def notify(msg):
        sent.append(msg)
        n = sent.count("WATCHDOG=1")
        if n == 3:
            runner.fresh = True
        if n >= 6:
            stop.set()

    backstop = threading.Timer(3.0, stop.set)
    backstop.daemon = True
    backstop.start()
    run_authority_service(tmp_path / "cfg.toml", notify=notify, stop_event=stop,
                          pet_interval_s=0.01, build_runner=lambda c: runner,
                          publish=lambda cfg: None)
    statuses = [m for m in sent if m.startswith("STATUS=")]
    assert any("STALE" in m for m in statuses), statuses
    assert "ticking" in statuses[-1]


def test_dead_runner_thread_exits_nonzero(tmp_path):
    """A dead manager thread is not a failing tick: the service exits so
    systemd restarts it, rather than petting an empty shell."""
    sent, stop = [], threading.Event()
    runner = FakeRunner(alive=False, fresh=False)
    backstop = threading.Timer(2.0, stop.set)
    backstop.daemon = True
    backstop.start()
    rc = run_authority_service(tmp_path / "cfg.toml", notify=sent.append,
                               stop_event=stop, pet_interval_s=0.01,
                               build_runner=lambda c: runner,
                               publish=lambda cfg: None)
    backstop.cancel()
    assert rc != 0
    assert "WATCHDOG=1" not in sent
    assert runner.stopped


def test_hung_loop_withholds_the_pet(tmp_path, caplog):
    """D6: a tick that never returns stops the loop stamp while the thread
    stays alive.  The pets stop, so systemd's watchdog restarts the unit."""
    sent, stop = [], threading.Event()
    runner = FakeRunner(alive=True, fresh=False, interval_sec=30.0, looping=False)
    t = threading.Timer(0.2, stop.set)
    t.daemon = True
    t.start()
    with caplog.at_level(logging.ERROR, logger="hf_timestd.core.authority_service"):
        rc = run_authority_service(tmp_path / "cfg.toml", notify=sent.append,
                                   stop_event=stop, pet_interval_s=0.01,
                                   build_runner=lambda c: runner,
                                   publish=lambda cfg: None)
    t.cancel()
    assert rc == 0
    assert "WATCHDOG=1" not in sent
    assert runner.loop_asked_with == 90.0
    hung = [m for m in sent if m.startswith("STATUS=") and "HUNG" in m]
    assert len(hung) == 1, sent
    assert any("HUNG" in r.getMessage() for r in caplog.records)


def test_failing_ticks_that_still_iterate_keep_petting(tmp_path):
    sent, stop = [], threading.Event()
    runner = FakeRunner(alive=True, fresh=False, interval_sec=0.001, looping=True)
    run_authority_service(tmp_path / "cfg.toml", notify=_stop_after(stop, sent, 3),
                          stop_event=stop, pet_interval_s=0.01,
                          build_runner=lambda c: runner, publish=lambda cfg: None)
    assert sent.count("WATCHDOG=1") == 3


def test_stop_failure_still_exits_cleanly(tmp_path):
    stop = threading.Event()
    stop.set()
    runner = FakeRunner()
    runner.stop_raises = True
    rc = run_authority_service(tmp_path / "cfg.toml", notify=lambda m: None,
                               stop_event=stop, build_runner=lambda c: runner,
                               publish=lambda cfg: None)
    assert rc == 0
    assert runner.stopped


def test_chain_publish_failure_does_not_stop_service(tmp_path):
    stop = threading.Event()
    stop.set()

    def boom(cfg):
        raise RuntimeError("no chain")

    runner = FakeRunner()
    sent = []
    rc = run_authority_service(tmp_path / "cfg.toml", notify=sent.append,
                               stop_event=stop, build_runner=lambda c: runner,
                               publish=boom)
    assert rc == 0
    assert runner.started
    assert "READY=1" in sent


def test_publish_and_runner_see_the_loaded_config(tmp_path):
    p = tmp_path / "cfg.toml"
    p.write_text('[station]\ngrid_square = "FN31pr"\n')
    stop = threading.Event()
    stop.set()
    seen = {}

    def build(c):
        seen["b"] = c
        return FakeRunner()

    run_authority_service(p, notify=lambda m: None, stop_event=stop,
                          build_runner=build,
                          publish=lambda c: seen.setdefault("p", c))
    assert seen["b"]["station"]["grid_square"] == "FN31pr"
    assert seen["p"] is seen["b"]


def test_load_station_config_missing_is_empty(tmp_path):
    assert load_station_config(tmp_path / "missing.toml") == {}


def test_entry_point_does_not_import_fusion():
    """D3: a fusion import failure must not stop the authority."""
    code = (
        "import sys, hf_timestd.core.authority_service\n"
        "print('hf_timestd.core.multi_broadcast_fusion' in sys.modules)\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                         text=True, check=True)
    assert out.stdout.strip() == "False", out.stderr


def test_fusion_and_authority_share_one_config_loader():
    import hf_timestd.core.multi_broadcast_fusion as mbf
    assert mbf.load_fusion_config is load_station_config


def test_main_wires_signals_to_the_stop_event(tmp_path, monkeypatch):
    import signal
    import hf_timestd.core.authority_service as svc

    saved = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
    seen = {}

    def fake_run(config_path, *, notify, stop_event, **kw):
        seen["config"] = config_path
        for s in (signal.SIGTERM, signal.SIGINT):
            stop_event.clear()
            signal.getsignal(s)(s, None)
            seen[s] = stop_event.is_set()
        notify("READY=1")  # the no-op or systemd notify must accept it
        return 7

    monkeypatch.setattr(svc, "run_authority_service", fake_run)
    try:
        rc = svc.main(["--config", str(tmp_path / "c.toml"), "--log-level", "INFO"])
    finally:
        for s, h in saved.items():
            signal.signal(s, h)
    assert rc == 7
    assert seen["config"] == tmp_path / "c.toml"
    assert seen[signal.SIGTERM] and seen[signal.SIGINT]

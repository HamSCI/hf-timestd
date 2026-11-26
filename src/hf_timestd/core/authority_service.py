"""The authority manager as a service of its own (Plan D, v3.96 Stage 1).

Clients read ``/run/hf-timestd/authority.json``.  Until v3.96 the
manager that writes it ran inside timestd-fusion, so a fusion crash, or
the l2-calibration service failing beneath fusion, took the station's
only timing carrier down with it (audit C1).  This entry point runs the
same ``AuthorityRunner`` in its own process, so authority.json lives or
dies with the manager alone.

It does not import ``multi_broadcast_fusion`` (preflight ruling D3): a
fault at fusion's import time must not stop the authority.  The config
loader both services use lives in ``authority_config``.

The systemd watchdog proves the manager's loop iterates, not that its
ticks succeed (ruling D6).  The pet follows the runner's loop stamp,
which advances after every tick, failed or not.  A tick that fails, or
goes stale, shows in the unit's STATUS and in the log as a fault; the
pet continues, so the unit stays up and loud rather than restarting and
resetting the T3 admission window.  A tick that never returns stops the
loop stamp: after ``LOOP_HUNG_INTERVALS`` intervals (90 s at the default
30 s) the pets stop, and systemd restarts the unit once ``WatchdogSec``
(120 s in Task 3's unit) passes without one, so at most about 210 s
after the loop's last tick.  A dead manager thread ends the service with
a non-zero status at once.

Run:  python -m hf_timestd.core.authority_service --config <path> --log-level INFO
"""
from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Callable, List, Optional

from hf_timestd.core.authority_config import (
    DEFAULT_CONFIG_PATH,
    load_station_config,
)
from hf_timestd.core.authority_runner import build_authority_runner_from_config

log = logging.getLogger(__name__)

# sysexits EX_CONFIG: refused for configuration.  The unit carries
# RestartPreventExitStatus=78, so systemd leaves it failed, not looping.
EX_CONFIG = 78

#: A tick counts as stale after this many manager intervals without a
#: successful one.  STATUS and the log only.
STALE_INTERVALS = 3

#: The loop counts as hung after this many manager intervals without a
#: tick finishing, success or failure.  The loop sleeps one interval
#: between ticks, so this leaves two intervals for a slow tick.  The
#: watchdog pet stops while the loop is hung.
LOOP_HUNG_INTERVALS = 3


def _publish_chains(cfg: dict) -> None:
    """TIMING_PROVENANCE_MODEL §3.2: the station's chain records, for the
    GRAPE packager.  The mode, not a decision: this record goes out once
    at start, so it claims T3 only in mode ON; the per-minute decision
    reaches readers through authority.json."""
    from hf_timestd.core.timing_chain_publisher import chains_from_config, publish_chains
    from hf_timestd.core.t3_admission import t3_admission_mode
    publish_chains(chains_from_config(cfg),
                   t3_admission_mode=t3_admission_mode(cfg))


def _stale_status(runner, limit_s: float, now: float) -> str:
    last = getattr(runner, "last_tick_ok_monotonic", None)
    if last is None:
        since = "no successful tick since start"
    else:
        since = f"last successful tick {now - last:.0f} s ago"
    return (f"authority tick STALE: {since} (limit {limit_s:.0f} s); "
            f"authority.json is not being refreshed")


def run_authority_service(
    config_path: Path,
    *,
    notify: Callable[[str], None],
    stop_event: threading.Event,
    pet_interval_s: float = 10.0,
    build_runner=build_authority_runner_from_config,
    publish: Optional[Callable[[dict], None]] = None,
    monotonic_fn: Callable[[], float] = time.monotonic,
) -> int:
    """Run the authority manager until ``stop_event`` is set.

    Returns 0 on a requested stop, EX_CONFIG (78) when the manager cannot
    be built (the unit's RestartPreventExitStatus=78 leaves it failed
    instead of looping), and 1 when its thread dies (systemd restarts)."""
    cfg = load_station_config(config_path)
    try:
        runner = build_runner(cfg)
    except Exception as exc:  # noqa: BLE001
        log.exception("authority manager could not be built: %s", exc)
        notify(f"STATUS=authority manager could not be built: {exc}")
        return EX_CONFIG

    # Best effort: the timing chain outranks its provenance record.
    try:
        (publish or _publish_chains)(cfg)
    except Exception as exc:  # noqa: BLE001
        log.warning("timing chain not published: %s", exc)

    runner.start()
    notify("READY=1")
    manager = getattr(runner, "manager", None)
    log.info(
        "Authority manager started: probes=%s interval=%.1fs hysteresis=%s",
        [p.t_level for p in getattr(manager, "probes", [])],
        runner.interval_sec, getattr(manager, "upgrade_hysteresis", "?"),
    )

    limit_s = STALE_INTERVALS * runner.interval_sec
    hung_s = LOOP_HUNG_INTERVALS * runner.interval_sec
    started = monotonic_fn()
    state: Optional[str] = None
    while True:
        if not runner.is_alive():
            msg = "authority manager thread died; exiting for a restart"
            log.error(msg)
            notify(f"STATUS={msg}")
            _stop_runner(runner)
            return 1

        now = monotonic_fn()
        if runner.loop_fresh(hung_s):
            notify("WATCHDOG=1")
            looping = True
        else:
            looping = False

        if not looping:
            new_state, text = "hung", (
                f"authority manager loop HUNG: no tick finished in "
                f"{hung_s:.0f} s; watchdog withheld for a restart")
        elif runner.tick_fresh(limit_s):
            new_state, text = "ok", (
                f"authority ticking (interval {runner.interval_sec:.0f} s)")
        elif now - started < limit_s:
            new_state, text = "starting", "authority started; awaiting first tick"
        else:
            new_state, text = "stale", _stale_status(runner, limit_s, now)
        if new_state != state:
            notify(f"STATUS={text}")
            if new_state == "hung":
                log.error(text)
            elif new_state == "stale":
                log.warning(text)
            elif state in ("stale", "hung"):
                log.info("authority tick recovered: %s", text)
            state = new_state

        if stop_event.wait(pet_interval_s):
            break

    log.info("Authority service stopping")
    notify("STOPPING=1")
    _stop_runner(runner)
    return 0


def _stop_runner(runner) -> None:
    """Stop the runner as fusion did: a failure logs, it does not raise."""
    try:
        runner.stop(timeout=5.0)
    except Exception as exc:  # noqa: BLE001
        log.warning("Authority manager stop failed: %s", exc)


def _systemd_notify() -> Callable[[str], None]:
    try:
        from systemd import daemon
    except ImportError:
        return lambda msg: None
    return lambda msg: daemon.notify(msg)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="hf-timestd authority manager service")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH,
                        help="station config TOML (default: %(default)s)")
    parser.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(levelname)s %(name)s: %(message)s",
    )

    stop_event = threading.Event()

    def _on_signal(signum, frame):  # noqa: ARG001
        log.info("Received signal %d", signum)
        stop_event.set()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    return run_authority_service(args.config, notify=_systemd_notify(),
                                 stop_event=stop_event)


if __name__ == "__main__":
    sys.exit(main())

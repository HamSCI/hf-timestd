"""live_vtec must never sleep through its own watchdog (WatchdogSec=60).

AC0G-ND, 2026-10-04: three ways it did, all while the GNSS box was silent or
refusing connections — a 60 s recv() timeout, a reconnect backoff that grows
to 120 s as one time.sleep(), and a 60 s idle sleep when disabled.
"""
import importlib.util
import re
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[2] / 'scripts' / 'live_vtec.py'


def _load():
    spec = importlib.util.spec_from_file_location('live_vtec_under_test', SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_a_long_backoff_pets_the_watchdog_at_least_every_20s():
    lv = _load()
    clock = [0.0]
    pets, naps = [], []

    def fake_sleep(s):
        naps.append(s)
        clock[0] += s

    notifier = mock.Mock(side_effect=lambda m: pets.append(clock[0]))
    with mock.patch.object(lv, 'SYSTEMD_AVAILABLE', True), \
         mock.patch.object(lv, 'systemd_daemon', mock.Mock(notify=notifier), create=True), \
         mock.patch.object(lv.time, 'sleep', fake_sleep), \
         mock.patch.object(lv.time, 'monotonic', lambda: clock[0]):
        lv._sleep_petting_watchdog(120)
    assert sum(naps) == 120
    assert max(naps) <= 20
    gaps = [b - a for a, b in zip(pets, pets[1:])]
    assert pets[0] == 0 and max(gaps) <= 20


def test_no_bare_sleep_or_wait_reaches_the_watchdog_limit():
    """Every remaining bare time.sleep() and socket timeout stays under 60 s."""
    src = SCRIPT.read_text()
    for n in re.findall(r'time\.sleep\((\d+(?:\.\d+)?)\)', src):
        assert float(n) < 60, f'time.sleep({n}) starves WatchdogSec=60'
    for n in re.findall(r'settimeout\((\d+(?:\.\d+)?)\)', src):
        assert float(n) < 60, f'settimeout({n}) can block past WatchdogSec=60'
    assert 'time.sleep(reconnect_delay)' not in src

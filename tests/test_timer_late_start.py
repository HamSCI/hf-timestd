"""A timer that starts late after boot must still fire.

systemd treats an OnBootSec= that lies in the past when the timer starts as
spent, and OnUnitActiveSec= only counts from the service's last activation.
A timer with only those two never fires when it starts more than OnBootSec
after boot and its service has not run since: AB4EJ, 2026-11-18, booted
18:56:40Z, timestd-archive.timer started 19:02:51Z, and the archiver did not
run again until a hand start at 00:27Z.  OnActiveSec= counts from the
timer's own start, so it arms the first run whenever the timer starts.
"""

from pathlib import Path

SYSTEMD = Path(__file__).resolve().parent.parent / "systemd"


def _timer_keys(path: Path) -> set[str]:
    keys = set()
    for line in path.read_text().splitlines():
        line = line.strip()
        if "=" in line and not line.startswith(("#", ";", "[")):
            keys.add(line.split("=", 1)[0].strip())
    return keys


def test_every_relative_timer_arms_on_its_own_start():
    timers = sorted(SYSTEMD.glob("*.timer"))
    assert timers
    stuck = [t.name for t in timers
             if "OnUnitActiveSec" in (k := _timer_keys(t))
             and "OnCalendar" not in k and "OnActiveSec" not in k]
    assert stuck == []

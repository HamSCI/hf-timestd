"""Is chrony's own selection trustworthy right now?

The offset judge trusts its lowest tier without a test: on a station
with no T4 or T5 that lowest tier reads T2, chrony following internet
NTP, and every higher tier must agree with it.  WB6CXC-7, 2026-11-01:
a ``qm snapshot`` froze the guest and stepped its clock about 185 ms
behind.  chrony kept a 17-minute-old sample from the source it had
selected, flagged every server with a fresh sample a falseticker, and
logged "no majority" at 14:44Z before it recovered near 14:50Z.  For
those fifteen minutes the judge refused a correct T6 -- battery passed,
second named by the GPS-mini -- because it disagreed with that T2.

T2 must therefore show its own health before its word can refuse T6.
This module reads health out of ``chronyc -n -c sources``, the output
``ChronyBench`` already fetches to classify T4 against T2, so the judge
gains no chronyc caller.  It reports; it corrects nothing.

The signals, each one a thing chrony itself says about its selection:

* **No selected source.**  No ``*`` row: chrony found no majority, or
  has nothing usable, and follows nothing while its clock coasts.
* **Falsetickers outnumber the combined sources.**  More rows in state
  ``x`` than rows chrony combines (``*`` and ``+``).  One bad server out
  of four is chrony doing its job; most servers voted out is chrony
  stuck on a minority -- the WB6CXC-7 shape after a step.  (With a
  selected source at least one row combines, so a lone falseticker never
  trips this.)
* **A stale selection against fresher falsetickers.**  The selected
  source's last sample older than ``SELECTED_STALE_S`` while some
  falseticker holds a sample at least ``FRESHER_BY_S`` newer: chrony
  clings to an old measurement and rejects the new ones.  Either half
  alone happens on a healthy pool (maxpoll 10 polls every 1024 s), so
  the rule needs both.
"""
from __future__ import annotations

from typing import Dict, Optional

__all__ = [
    "SELECTED_STALE_S", "FRESHER_BY_S",
    "chrony_source_health", "chrony_unhealthy_reason",
]

#: chrony's default maxpoll for a pool is 10 (1024 s); 600 s alone would
#: trip on a healthy pool, which is why the fresher-falseticker half
#: must hold too.
SELECTED_STALE_S = 600.0

#: How much newer a falseticker's sample must be than the selected one.
FRESHER_BY_S = 300.0


def _lastrx(field: str) -> Optional[float]:
    """chrony prints LastRx in seconds, or '-' for never."""
    try:
        return float(field)
    except (TypeError, ValueError):
        return None


def chrony_source_health(sources_csv: Optional[str]) -> Optional[Dict]:
    """Summarise ``chronyc -n -c sources``; None when there is nothing.

    CSV columns: mode, state, name, stratum, poll, reach, LastRx, ...
    """
    if not sources_csv:
        return None
    selected = None
    n_combined = n_falseticker = n_rows = 0
    falseticker_lastrx = []
    for line in sources_csv.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 7:
            continue
        n_rows += 1
        state = parts[1]
        lastrx = _lastrx(parts[6])
        if state == "*":
            selected = {"name": parts[2], "lastrx_s": lastrx}
            n_combined += 1
        elif state == "+":
            n_combined += 1
        elif state == "x":
            n_falseticker += 1
            if lastrx is not None:
                falseticker_lastrx.append(lastrx)
    if n_rows == 0:
        return None
    return {
        "selected": None if selected is None else selected["name"],
        "selected_lastrx_s": None if selected is None
        else selected["lastrx_s"],
        "n_sources": n_rows,
        "n_combined": n_combined,
        "n_falseticker": n_falseticker,
        "freshest_falseticker_lastrx_s": (min(falseticker_lastrx)
                                          if falseticker_lastrx else None),
    }


def chrony_unhealthy_reason(health: Optional[Dict]) -> Optional[str]:
    """None when chrony's selection shows no fault, else the fault.

    No health summary at all is not evidence of a fault: an older bench,
    or a sources call that failed while tracking answered, keeps today's
    behaviour.
    """
    if not health:
        return None
    if health.get("selected") is None:
        return ("chrony selects no source (no majority, or nothing "
                "usable)")
    n_x = int(health.get("n_falseticker") or 0)
    n_ok = int(health.get("n_combined") or 0)
    if n_x > n_ok:
        return (f"chrony calls {n_x} of {health.get('n_sources')} sources "
                f"falsetickers against {n_ok} it combines")
    sel = health.get("selected_lastrx_s")
    fresh = health.get("freshest_falseticker_lastrx_s")
    if (sel is not None and fresh is not None
            and sel > SELECTED_STALE_S and fresh <= sel - FRESHER_BY_S):
        return (f"chrony's selected sample is {sel:.0f} s old while a "
                f"source it calls a falseticker answered {fresh:.0f} s ago")
    return None

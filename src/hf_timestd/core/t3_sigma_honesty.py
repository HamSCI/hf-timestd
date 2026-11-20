"""T3 sigma honesty by UTC hour (10-30 section 9, criterion 3).

For each minute with a reference, z = t3_error_ms / claim, where claim is
T3's stated sigma as the judge acts on it: max(sigma_ms, ORIGIN_SIGMA_FLOOR_MS).
An hour is honest when the rms of z lies in [0.5, 2.0].  ``floor_fraction``
reports how many minutes sat on the floor; Phase A gates nothing on it.

No clock is read.  Minutes arrive stamped, and the window prunes against the
newest stamp seen.
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from typing import Deque, Dict, Iterable, List, Optional, Tuple

from hf_timestd.core.registration_acquirer import ORIGIN_SIGMA_FLOOR_MS
from hf_timestd.core.t3_reference_errors import ReferencePair, reference_errors

# Builder choice (plan table, marked B): spec says "over the last few days".
CALIBRATION_MAX_AGE_S = 259200  # 72 h

HONEST_RMS_Z_MIN = 0.5  # spec, criterion 3
HONEST_RMS_Z_MAX = 2.0


@dataclass(frozen=True)
class HourlyHonesty:
    hour_utc: int
    n: int
    rms_z: float
    floor_fraction: float
    honest: bool


class SigmaHonesty:
    def __init__(self) -> None:
        # (utc_minute, hour, z squared, claim sat on the floor)
        self._samples: Deque[Tuple[int, int, float, bool]] = deque()
        self._last_minute: Optional[int] = None

    def add(self, utc_minute: int, pair: ReferencePair,
            t3_sigma_ms: Optional[float]) -> None:
        try:
            if isinstance(utc_minute, bool) or not isinstance(utc_minute, int):
                return
            if (isinstance(t3_sigma_ms, bool)
                    or not isinstance(t3_sigma_ms, (int, float))
                    or not math.isfinite(t3_sigma_ms) or t3_sigma_ms <= 0):
                return
            err = pair.t3_error_ms
            if isinstance(err, bool) or not isinstance(err, (int, float)) \
                    or not math.isfinite(err):
                return
            # A repeated or earlier minute adds nothing (host clock stepped).
            if self._last_minute is not None and utc_minute <= self._last_minute:
                return
            claim = max(float(t3_sigma_ms), ORIGIN_SIGMA_FLOOR_MS)
            z = float(err) / claim
            self._samples.append((utc_minute, (utc_minute // 3600) % 24,
                                  z * z, claim == ORIGIN_SIGMA_FLOOR_MS))
            self._last_minute = utc_minute
            cutoff = utc_minute - CALIBRATION_MAX_AGE_S
            while self._samples and self._samples[0][0] < cutoff:
                self._samples.popleft()
        except Exception:  # noqa: BLE001 -- never raise into the caller
            return

    def by_hour(self) -> Dict[int, HourlyHonesty]:
        acc: Dict[int, List[float]] = {}
        for _, hour, z2, at_floor in self._samples:
            a = acc.setdefault(hour, [0.0, 0.0, 0.0])
            a[0] += 1
            a[1] += z2
            a[2] += 1.0 if at_floor else 0.0
        out: Dict[int, HourlyHonesty] = {}
        for hour, (n, z2sum, nfloor) in sorted(acc.items()):
            rms = math.sqrt(z2sum / n)
            out[hour] = HourlyHonesty(
                hour_utc=hour, n=int(n), rms_z=float(rms),
                floor_fraction=float(nfloor / n),
                honest=HONEST_RMS_Z_MIN <= rms <= HONEST_RMS_Z_MAX)
        return out

    def seed(self, rows: Iterable[dict]) -> None:
        """Restore from shadow minute rows; malformed rows are skipped."""
        try:
            for row in rows:
                try:
                    pair = reference_errors(row)
                    if pair is None:
                        continue
                    t3 = row.get("t3")
                    sigma = t3.get("sigma_ms") if isinstance(t3, dict) else None
                    self.add(row.get("utc_minute"), pair, sigma)
                except Exception:  # noqa: BLE001
                    continue
        except Exception:  # noqa: BLE001 -- non-iterable input
            return

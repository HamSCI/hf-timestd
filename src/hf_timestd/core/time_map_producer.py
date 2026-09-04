"""From what the recorder knows to the TimeMap it publishes.

TIMING_PROVENANCE_MODEL §3.1: the per-chunk `timing` block publishes the
registration in force.  §3.1.1: every rung fills the shape.  This module
decides which builder applies and never raises into the recording path.

Precedence.  A credible T6 anchor registers the payload-anchored chain.  An
anchor that is not credible registers NOTHING — it does not fall back to the
pair, because that would swap chains without saying so.  No anchor at all
and a radiod pair registers the sysclock chain.  Nothing is absence with a
reason.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable, Optional

from hamsci_dsp.timing import AuthoritySnapshot
from hamsci_dsp.timing_map import TimeMap, native_anchor_map, null_map, sysclock_map

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TimeMapInputs:
    counter_space: str
    counter_epoch_id: str
    f_s_hz: int
    measured_at_utc_ns: int
    gps_time_ns: Optional[int] = None
    rtp_timesnap: Optional[int] = None
    anchor_rtp: Optional[int] = None
    anchor_utc_ns: Optional[int] = None
    anchor_sigma_ns: Optional[int] = None
    lock_credible: bool = False
    judge_tier: Optional[str] = None
    engineering: Optional[dict] = field(default=None)


class TimeMapProducer:
    def __init__(self, snapshot_fn: Callable[[], Optional[AuthoritySnapshot]],
                 a_level_config: str = "A0") -> None:
        self._snapshot_fn = snapshot_fn
        self._a_level_config = str(a_level_config)

    def _snapshot(self) -> Optional[AuthoritySnapshot]:
        try:
            return self._snapshot_fn()
        except Exception as exc:  # noqa: BLE001 — provenance never disturbs recording
            logger.debug("time map: authority snapshot unavailable: %s", exc)
            return None

    def build(self, inputs: TimeMapInputs) -> TimeMap:
        snap = self._snapshot()
        if snap is not None:
            a_level, provenance, host_clock = snap.a_level, "observed", snap.host_clock
        else:
            a_level, provenance, host_clock = self._a_level_config, "assumed", None
        eng = dict(inputs.engineering or {})
        if inputs.judge_tier is not None:
            eng["judge_tier"] = inputs.judge_tier
        common = dict(counter_space=inputs.counter_space, counter_epoch_id=inputs.counter_epoch_id,
                      measured_at_utc_ns=inputs.measured_at_utc_ns,
                      a_level=a_level, a_level_provenance=provenance)
        try:
            if inputs.anchor_rtp is not None and inputs.anchor_utc_ns is not None:
                return native_anchor_map(
                    anchor_rtp=inputs.anchor_rtp, anchor_utc_ns=inputs.anchor_utc_ns,
                    sample_rate_hz=inputs.f_s_hz, sigma_ns=inputs.anchor_sigma_ns,
                    lock_credible=inputs.lock_credible, host_clock=host_clock,
                    engineering=eng, **common)
            if inputs.gps_time_ns is not None and inputs.rtp_timesnap is not None:
                return sysclock_map(
                    gps_time_ns=inputs.gps_time_ns, rtp_timesnap=inputs.rtp_timesnap,
                    f_s_hz=inputs.f_s_hz, host_clock=host_clock, engineering=eng, **common)
            return null_map(f_s_hz=inputs.f_s_hz, reason="no anchor and no radiod pair",
                            engineering=eng, **common)
        except Exception as exc:  # noqa: BLE001
            logger.warning("time map: builder failed (%s); publishing absence", exc)
            return null_map(counter_space=inputs.counter_space, counter_epoch_id=inputs.counter_epoch_id,
                            f_s_hz=inputs.f_s_hz, measured_at_utc_ns=inputs.measured_at_utc_ns,
                            reason=f"builder error: {exc}", a_level=a_level,
                            a_level_provenance=provenance, engineering=eng)

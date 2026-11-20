"""Per-minute T3 and T2 errors against the best reference a shadow row has.

Admission (10-30 section 9, criterion 6) compares T3 with T2 by the absolute
error each makes against the same truth.  A shadow minute row holds several
possible truths.  ``reference_errors`` picks the best one and returns both
tiers' errors against it.  It reads the row only, and never raises.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Optional


@dataclass(frozen=True)
class ReferencePair:
    reference: str                 # "T6", "T5" or "T4": the truth used
    t3_error_ms: float
    t2_error_ms: Optional[float]   # None when the row cannot say


def _num(v: Any) -> Optional[float]:
    """A finite number, else None.  A boolean or a NaN counts as missing."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    return f if math.isfinite(f) else None


def _vs(row: dict, key: str):
    """(shadow_residual_ns or None, vs_tier or None) from a ``*_vs_adopted``
    dict.  (None, None) when the dict is absent or malformed."""
    d = row.get(key)
    if not isinstance(d, dict):
        return None, None
    tier = d.get("vs_tier")
    return _num(d.get("shadow_residual_ns")), (tier if isinstance(tier, str)
                                               else None)


def _t2_host_ms(row: dict) -> Optional[float]:
    """T2 against T4 on the host plane: S2 - S4 = t4_offset - t2_offset
    (chrony offsets read host minus source)."""
    t4, t2 = _num(row.get("t4_offset_ms")), _num(row.get("t2_offset_ms"))
    return None if t4 is None or t2 is None else t4 - t2


def _t2_judge_ms(row: dict, tier: str) -> Optional[float]:
    ns, vs_tier = _vs(row, "t2_vs_adopted")
    return ns / 1e6 if ns is not None and vs_tier == tier else None


def _t2_error(row: dict, tier: str, *, host_when_t4: bool) -> Optional[float]:
    # T4 present: the host plane, as the 11-01 verdict measured T2.
    if host_when_t4 and _num(row.get("t4_offset_ms")) is not None:
        return _t2_host_ms(row)
    return _t2_judge_ms(row, tier)


def reference_errors(row: dict) -> Optional[ReferencePair]:
    """Both tiers' errors for one minute row, against the best reference.

    Every error reads "estimate minus truth".  The best reference comes
    first: T6, then T5, then T4.  The reference is the one the row holds,
    not one a value failed to read: when T3's error against the best
    reference is non-finite, boolean or malformed, the answer is None, and
    no lower reference with a different T3 basis takes over.  Only an
    absent reference (value None, no dict, wrong ``vs_tier``) passes to the
    next one.

    T3's error comes from its sample-plane reference where one exists.
    T2's error comes from the host plane, T2 against T4
    (``t4_offset_ms - t2_offset_ms``), as the 11-01 verdict measured it.

    - T6, T4 present: t3 = ``t3_shadow.t3_minus_t6_ms``; t2 = host plane.
    - T6, no T4: t3 as above; t2 = ``t2_vs_adopted`` / 1e6 when its
      ``vs_tier`` is "T6".  UNVERIFIED until a T6-only station's record
      confirms it: it crosses the host/sample link that carries B4's
      ~16 ms between T6 and T4, and no station without T4 has yet shown
      that this link holds.
    - T5: t3 = ``t3_vs_adopted`` / 1e6 with ``vs_tier`` "T5"; t2 = host
      plane if T4 is present, else ``t2_vs_adopted`` / 1e6 with
      ``vs_tier`` "T5".
    - T4: t3 = ``t3_vs_adopted - t4_vs_adopted`` (/ 1e6) when both name
      the same ``vs_tier``, or ``t3_vs_adopted`` / 1e6 when its
      ``vs_tier`` is "T4"; t2 = host plane.

    A minute with T3's error but no T2 error returns a pair whose
    ``t2_error_ms`` is None; the policy must not count it as a win.
    """
    try:
        if not isinstance(row, dict):
            return None
        shadow = row.get("t3_shadow")
        raw6 = shadow.get("t3_minus_t6_ms") if isinstance(shadow, dict) else None
        if raw6 is not None:
            t3 = _num(raw6)
            return None if t3 is None else ReferencePair(
                "T6", t3, _t2_error(row, "T6", host_when_t4=True))

        t3_ns, t3_tier = _vs(row, "t3_vs_adopted")
        if t3_tier == "T5":
            return None if t3_ns is None else ReferencePair(
                "T5", t3_ns / 1e6, _t2_error(row, "T5", host_when_t4=True))

        t4_ns, t4_tier = _vs(row, "t4_vs_adopted")
        if t3_tier is not None and t3_tier == t4_tier:
            if t3_ns is None or t4_ns is None:
                return None
            return ReferencePair("T4", (t3_ns - t4_ns) / 1e6, _t2_host_ms(row))
        if t3_tier == "T4":
            return None if t3_ns is None else ReferencePair(
                "T4", t3_ns / 1e6, _t2_host_ms(row))
        return None
    except Exception:  # noqa: BLE001 -- a bad row yields no pair
        return None

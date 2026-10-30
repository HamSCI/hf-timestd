"""RegistrationStore — the acquired registration crosses processes.

Metrology runs one process per channel (timestd-metrology@<channel>).
Every channel on one radiod shares one ADC, but since ka9q-radio e5f790dd
each channel starts its OWN RTP counter: ND measured start blocks
k0 = 51, 51, 51, 51, 50 on its 24 kHz channels on 2026-10-06, so one
channel's raw RTP sits 20 ms off the others'.  Two channels' RTP values
therefore never compare directly.  Each process writes its own estimate
to <dir>/<channel>.json, naming the counter it was measured in and that
counter's map to radiod's input-sample index; ``fuse_registrations``
converts every sibling into the caller's counter through both maps and
drops any it cannot convert.  The last writer also refreshes the
station summary /run/hf-timestd/registration.json (spec §7), which the
Offset Judge's HfAcquiredBench and the provenance sidecar read.
"""

from __future__ import annotations

import json
import logging
import math
import os
import tempfile
import time
from pathlib import Path
from typing import Callable, List, Optional, Tuple

import numpy as np

from hamsci_dsp.counter_map import CounterMap

from .registration_acquirer import ORIGIN_SIGMA_FLOOR_MS, Registration

logger = logging.getLogger(__name__)

DEFAULT_DIR = Path("/run/hf-timestd/registration")
DEFAULT_SUMMARY = Path("/run/hf-timestd/registration.json")
FUSE_OUTLIER_MS = 3.0
DEFAULT_STALE_S = 300.0

# The one summary state that means "acquired AND verified".
AUTHORITATIVE_STATE = "ACQUIRED"

# Corroborated minutes a plane must show before any surface acts on it
# (task 16b).  Live on AC0G-ND, 2026-09-07 20:36Z: the shared channels
# fit a WWV artefact 34 ms away as BPM, called the pair unambiguous,
# verified it against its own ticks and anchored the ring, authority.json
# §18, the sidecar and the FUSE chrony feed on it inside ONE minute
# (n_minutes 0) -- FUSE then reported the host 23-28 ms fast against an
# NTP consensus of 5-13 ms until corroboration reset the plane at 20:42.
# Verification asks "do the ticks land where this plane says?" in a
# single minute; corroboration asks it again, and again, against fresh
# ensembles.  Two minutes of it cost two minutes of the pre-registration
# fallback and buy the station a plane that has survived independent
# evidence.
ADOPT_MIN_CORROBORATED_MINUTES = 2


def registration_refusal(
    summary: Optional[dict],
    *,
    now: float,
    stale_s: float = DEFAULT_STALE_S,
    sample_rate: Optional[int] = None,
) -> Optional[str]:
    """``None`` when this summary is the station's AUTHORITATIVE plane,
    else the name of the gate that refuses it.

    THE one gate (fix round 1, review finding C-1).  Every surface that
    lets the registration place UTC asks this function and nothing else:
    the ring anchor, authority.json §18, the archive sidecar (all three
    via :class:`~hf_timestd.core.t3_registration_anchor.T3RegistrationAnchor`)
    and the FUSE chrony feed (via ``HfAcquiredBench``, whose reading
    populates ``label_plane_anchor``).

    Before this existed the FUSE feed ran a looser gate of its own --
    ACQUIRED **or** WITNESS, and it never read ``verified`` -- so an
    unverified plane that the other three surfaces refused was still
    steering the host clock, and on a T6 station a stood-down WITNESS
    plane could speak to chrony while T6 held the metrology.  A plane
    good enough to discipline the clock is a plane good enough to label
    the samples; there is no honest reading on which those differ.

    The gates, in refusal order:

    ``no_summary``
        Nothing published.
    ``state:<S>``
        Not ACQUIRED.  CANDIDATE is the acquired-but-unverified plane;
        WITNESS means a T6 station publishes the plane without it driving
        metrology; BOOTSTRAP/CONFLICT speak for themselves.
    ``incomplete``
        A field the arithmetic needs is missing or unparseable.
    ``unverified``
        The fused plane does not claim its own verification.  Fail-closed
        on an absent flag: an older metrology process wrote the
        untruthful ``false`` (task 14c), and one revalidation tick of
        legacy behaviour beats anchoring on a fold-lattice phantom.
    ``uncorroborated``
        Verified once, but not yet corroborated for
        ``ADOPT_MIN_CORROBORATED_MINUTES`` minutes (task 16b).  A plane
        verifies against the same minute of audio that acquired it; the
        adoption hysteresis makes it survive independent evidence first.
        Fail-closed on an absent field, as ``unverified`` does.
    ``stale``
        ``read_summary`` returns the last file it finds however old, so a
        dead metrology process would otherwise anchor the station
        forever.
    ``sample_rate_mismatch``
        ``rtp_ref`` is stamped in ONE counter domain; relating one domain
        to another needs a measured epoch offset nobody has here
        (``cross_channel_rtp.py``).  Only checked when the caller names a
        domain.
    """
    if not summary:
        return "no_summary"
    if str(summary.get("state")) != AUTHORITATIVE_STATE:
        return f"state:{summary.get('state')}"
    try:
        int(summary["rtp_ref"])
        float(summary["utc_ref"])
        reg_rate = int(summary["sample_rate"])
        float(summary["sigma_ms"])
        written_at = float(summary.get("written_at", 0.0))
        n_minutes = int(summary.get("n_minutes") or 0)
    except (KeyError, TypeError, ValueError):
        return "incomplete"
    if summary.get("verified") is not True:
        return "unverified"
    if n_minutes < ADOPT_MIN_CORROBORATED_MINUTES:
        return "uncorroborated"
    if (float(now) - written_at) > float(stale_s):
        return "stale"
    if sample_rate is not None and reg_rate != int(sample_rate):
        return "sample_rate_mismatch"
    return None


def registration_is_authoritative(
    summary: Optional[dict],
    *,
    now: float,
    stale_s: float = DEFAULT_STALE_S,
    sample_rate: Optional[int] = None,
) -> bool:
    """Is this summary the station's authoritative plane?  See
    :func:`registration_refusal` for the gates and why there is one."""
    return (
        registration_refusal(summary, now=now, stale_s=stale_s, sample_rate=sample_rate)
        is None
    )


def fuse_registrations_with_members(
    regs: List[Registration],
    at_rtp: int,
    at_map: Optional[CounterMap] = None,
    domain: str = "",
) -> Tuple[Optional[Registration], List[str], List[str]]:
    """``(fused, channels_kept, channels_waiting)``.

    ``at_rtp`` is a value in channel ``domain``'s counter, and ``at_map``
    maps that counter to radiod's input-sample index.  Fusion first
    converts every member into that counter (``Registration.in_domain``):
    a member measured in ``domain`` projects directly; any other member
    converts through both counter maps, and only within one radiod run.
    A member that cannot convert -- no map, or a map from another run --
    drops out before the outlier test ever sees it.  This replaces the old
    clustering on ``epoch_offset_s``, which assumed every channel shares
    one counter origin; since ka9q-radio e5f790dd none does.

    The caller must name its counter: ``domain``, ``at_map`` or both.
    Neither raises ``ValueError``.  Fix round 1 removed a default that took
    the first member's counter: on a siblings-only call that projected one
    sibling's plane at the caller's raw RTP, alone, where before three
    agreeing siblings out-voted a wrong one.  A missing plane is safe; a
    wrong plane is not.  The fused plane carries ``rtp_domain`` and
    ``counter_map`` exactly as the caller passed them.

    ``channels_kept`` is what fusion ACTUALLY combined -- the converted
    members that survived the outlier test and the corroboration floor.
    ``metrology_service`` publishes it as the summary's ``contributing``,
    which previously named every sibling read from disk and so claimed
    corroboration the station had not performed (final review, C1).

    ``channels_waiting`` names the members held back by the corroboration
    floor alone (task 16c).  When ANY member clears
    ``ADOPT_MIN_CORROBORATED_MINUTES``, fusion combines only the members
    that clear it: a freshly re-acquired channel serves its two minutes
    outside the plane and joins when it qualifies.  ND re-acquires a
    channel several times an hour, and taking the minimum over every kept
    member would otherwise drop the station's fused ``n_minutes`` to 0
    each time and stand every surface down for two minutes.  When NOBODY
    clears the floor -- the station bootstrapping -- every member fuses as
    before and the summary's own gate holds the plane back instead.
    """
    if not domain and at_map is None:
        raise ValueError(
            "fusion needs the caller's counter: pass domain, at_map or both"
        )
    if not regs:
        return None, [], []
    converted = [r.in_domain(at_rtp, at_map, domain) for r in regs]
    same = [c for c in converted if c is not None]
    if not same:
        return None, [], []
    utc = np.array([c.utc_ref for c in same])
    med = np.median(utc)
    keep = [
        (r, u) for r, u in zip(same, utc) if abs(u - med) * 1000.0 <= FUSE_OUTLIER_MS
    ]
    if not keep:
        return None, [], []
    waiting: List[str] = []
    corroborated = [
        (r, u) for r, u in keep if r.n_minutes >= ADOPT_MIN_CORROBORATED_MINUTES
    ]
    if corroborated and len(corroborated) < len(keep):
        waiting = [
            r.channel for r, _ in keep if r.n_minutes < ADOPT_MIN_CORROBORATED_MINUTES
        ]
        keep = corroborated
    w = np.array(
        [1.0 / max(r.sigma_ms, ORIGIN_SIGMA_FLOOR_MS * 0.1) ** 2 for r, _ in keep]
    )
    u = np.array([u for _, u in keep])
    fused_utc = float(np.sum(w * u) / np.sum(w))
    offsets = [float(getattr(r, "epoch_offset_s", float("nan"))) for r, _ in keep]
    finite = [o for o in offsets if math.isfinite(o)]
    fused = Registration(
        counter_epoch_id=keep[0][0].counter_epoch_id,
        rtp_ref=int(at_rtp),
        utc_ref=fused_utc,
        sample_rate=keep[0][0].sample_rate,
        sigma_ms=float(1.0 / np.sqrt(np.sum(w))),
        # task 16b: the WEAKEST member's corroboration, as ``verified``
        # below already takes the weakest provenance.  ``max`` let one
        # channel's long history vouch for a sibling with a shorter one.
        # Task 16c keeps the members that fall under the floor out of
        # ``keep`` altogether whenever anyone clears it, so this minimum
        # runs over qualified members unless the whole station is still
        # bootstrapping.
        n_minutes=min(r.n_minutes for r, _ in keep),
        channel="fused",
        hypotheses_open=sum(r.hypotheses_open for r, _ in keep),
        stations=tuple(sorted({st for r, _ in keep for st in r.stations})),
        # task-14c: the fused plane is verified exactly when EVERY member
        # it kept is verified.  This field used to be left at its
        # ``False`` default, which made ``verified`` false in every station
        # summary ever written -- so a reader that believed the field
        # could never see a verified registration, and the whole verified
        # predicate had to be inferred from the summary STATE instead
        # (metrology_service publishes ACQUIRED only when a verified plane
        # contributed).  ``all`` and not ``any``: a fused plane inherits
        # the weakest provenance among its members, because an unverified
        # member's origin could be a fold-lattice phantom and
        # inverse-variance combination cannot detect that.
        verified=all(bool(r.verified) for r, _ in keep),
        # provenance only since Task 6 (fusion converts through the
        # counter maps instead of clustering on it); the minimum is the
        # least-late pair any kept member saw
        epoch_offset_s=min(finite) if finite else float("nan"),
        # task 17b: ``any``, as ``verified`` takes the weakest
        # provenance.  One member whose second rests on the label frame
        # makes the fused plane's second rest on it too -- the members
        # agree to the pair skew, so they share whatever whole second
        # the label frame gave them.
        whole_second_unresolved=any(
            bool(getattr(r, "whole_second_unresolved", False)) for r, _ in keep
        ),
        counter_map=at_map,
        rtp_domain=domain,
    )
    return fused, [r.channel for r, _ in keep], waiting


def fuse_registrations(
    regs: List[Registration],
    at_rtp: int,
    at_map: Optional[CounterMap] = None,
    domain: str = "",
) -> Optional[Registration]:
    """The fused plane alone -- see :func:`fuse_registrations_with_members`
    for the channels it kept and the ones still waiting."""
    return fuse_registrations_with_members(regs, at_rtp, at_map, domain)[0]


class RegistrationStore:
    def __init__(
        self,
        directory: Path = DEFAULT_DIR,
        summary_path: Path = DEFAULT_SUMMARY,
        stale_s: float = DEFAULT_STALE_S,
        time_fn: Callable[[], float] = time.time,
    ):
        self.directory = Path(directory)
        self.summary_path = Path(summary_path)
        self.stale_s = float(stale_s)
        self._time = time_fn
        self.write_failures = 0

    # ── writing ────────────────────────────────────────────────────
    def _atomic_write(self, path: Path, payload: dict) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w",
                dir=str(path.parent),
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
                encoding="utf-8",
            ) as tmp:
                json.dump(payload, tmp, separators=(",", ":"))
                tmp.flush()
                os.fsync(tmp.fileno())
                tmp_path = tmp.name
            os.replace(tmp_path, path)
        except OSError as e:
            self.write_failures += 1
            if self.write_failures == 1 or self.write_failures % 60 == 0:
                logger.error(f"registration write failed for {path}: {e}")
            else:
                logger.warning(f"registration write failed for {path}: {e}")

    @staticmethod
    def _payload(reg: Optional[Registration]) -> dict:
        if reg is None:
            return {
                "counter_epoch_id": None,
                "rtp_ref": None,
                "utc_ref": None,
                "sample_rate": None,
                "sigma_ms": None,
                "method": None,
                "n_minutes": 0,
                "hypotheses_open": 0,
                "verified": None,
                "epoch_offset_s": None,
                "whole_second_unresolved": None,
                "counter_map": None,
                "rtp_domain": None,
            }
        # strict JSON: a BOOTSTRAP channel file carries sigma inf -> null
        sigma = float(reg.sigma_ms) if math.isfinite(reg.sigma_ms) else None
        return {
            "counter_epoch_id": reg.counter_epoch_id,
            "rtp_ref": int(reg.rtp_ref),
            "utc_ref": float(reg.utc_ref),
            "sample_rate": int(reg.sample_rate),
            "sigma_ms": sigma,
            "method": reg.method,
            "n_minutes": int(reg.n_minutes),
            "hypotheses_open": int(reg.hypotheses_open),
            "stations": list(reg.stations),
            # task-11b fix round 1 (M5): round-trip verified so a future
            # direct adopt(sibling) doesn't silently reconstruct a
            # permanently-unverified plane from a file that was, in fact,
            # verified when written.
            "verified": bool(reg.verified),
            # C1: the epoch as a measured offset, so another PROCESS can
            # tell "same counter space" from "same id string".  strict
            # JSON again: NaN (no valid pair seen) -> null.
            "epoch_offset_s": (
                float(reg.epoch_offset_s) if math.isfinite(reg.epoch_offset_s) else None
            ),
            # task 17b: the marker implied a whole second and a gate
            # refused it, so this plane's second rests on the label
            # frame.  Provenance for the offline analysis; no gate reads
            # it, because the plane is exactly the one c7b2106 published.
            "whole_second_unresolved": bool(reg.whole_second_unresolved),
            # Task 6: the counter rtp_ref lives in, and its map to radiod's
            # input-sample index, so a sibling process can convert this
            # plane into its own counter instead of reading raw RTP.
            "counter_map": (
                reg.counter_map.to_dict() if reg.counter_map is not None else None
            ),
            "rtp_domain": str(reg.rtp_domain),
        }

    def write_channel(self, reg: Registration, state: str, extra: dict) -> None:
        payload = {
            "channel": reg.channel,
            "state": state,
            "written_at": self._time(),
            **self._payload(reg),
            **extra,
        }
        self._atomic_write(self.directory / f"{reg.channel}.json", payload)

    def write_summary(
        self,
        fused: Optional[Registration],
        contributing: List[str],
        state: str,
        extra: dict,
    ) -> None:
        payload = {
            "state": state,
            "contributing": list(contributing),
            "written_at": self._time(),
            **self._payload(fused),
            **extra,
        }
        self._atomic_write(self.summary_path, payload)

    # ── reading ────────────────────────────────────────────────────
    def read_siblings(
        self,
        exclude_channel: str = "",
        states: Tuple[str, ...] = ("ACQUIRED",),
    ) -> List[Registration]:
        """Fresh sibling planes whose file state is in ``states``.

        The default keeps live behaviour: ACQUIRED only.  On a T6 station
        every verified channel writes WITNESS, so accepting WITNESS by
        default would hand each channel siblings it never had and push it
        into the sibling-only path; Task 10's shadow record asks for
        WITNESS explicitly."""
        out: List[Registration] = []
        if not self.directory.is_dir():
            return out
        now = self._time()
        for p in sorted(self.directory.glob("*.json")):
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if d.get("channel") == exclude_channel or d.get("utc_ref") is None:
                continue
            if d.get("sigma_ms") is None:
                continue
            if now - float(d.get("written_at", 0)) > self.stale_s:
                continue
            if d.get("state") not in states:
                continue
            if d.get("method") == "adopted":
                # A purely derived plane (this channel echoing a sibling
                # fusion back under its own name) is not new evidence;
                # letting it re-enter fusion understates sigma by
                # sqrt(n_adopters+1) (review C1).
                continue
            try:
                out.append(
                    Registration(
                        counter_epoch_id=str(d["counter_epoch_id"]),
                        rtp_ref=int(d["rtp_ref"]),
                        utc_ref=float(d["utc_ref"]),
                        sample_rate=int(d["sample_rate"]),
                        sigma_ms=float(d["sigma_ms"]),
                        method=str(d.get("method") or "fold+template"),
                        n_minutes=int(d.get("n_minutes", 0)),
                        channel=str(d["channel"]),
                        hypotheses_open=int(d.get("hypotheses_open", 0)),
                        stations=tuple(d.get("stations", [])),
                        # M5: default False when absent (older files, or a
                        # schema-incomplete write) rather than raising.
                        verified=bool(d.get("verified", False)),
                        epoch_offset_s=(
                            float(d["epoch_offset_s"])
                            if d.get("epoch_offset_s") is not None
                            else float("nan")
                        ),
                        # task 17b: absent (an older file) reads as
                        # False -- the pre-17b acquirer could not refuse
                        # a whole second, so it never had one to flag.
                        whole_second_unresolved=bool(
                            d.get("whole_second_unresolved") or False
                        ),
                        # Task 6: absent (an older file) reads as no map
                        # and the writer's own channel, so such a plane
                        # never combines with another channel's.
                        counter_map=CounterMap.from_dict(d.get("counter_map")),
                        rtp_domain=str(d.get("rtp_domain") or ""),
                    )
                )
            except (KeyError, TypeError, ValueError):
                logger.debug(f"registration file {p} unreadable; skipped")
                continue
        return out

    def read_summary(self) -> Optional[dict]:
        try:
            return json.loads(self.summary_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def summary_age_s(self, summary: dict) -> float:
        """Seconds since ``summary`` (as returned by :meth:`read_summary`)
        was written, per this store's own clock. A caller compares this
        against ``self.stale_s`` to tell a live summary from one whose
        writer died — ``read_summary`` returns the last file it finds
        exactly as written, however old (review F1/F2, task 10)."""
        return self._time() - float(summary.get("written_at", 0))

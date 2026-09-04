"""The station's chain record: its plane and its budget.

TIMING_PROVENANCE_MODEL §3.2 gives the payload-anchored budget as it stands
and, since 2026-09-04, the sysclock chain.  Both ship as defaults so every
station publishes a chain; `[timing.provenance]` lets a station replace the
terms it has measured — the antenna-to-injector run, the GNSS feed — and
qualify its traceability claim in its own words.  MEASUREMENT_MODEL §9
invariant 4: a term may not leave the budget because it grew small.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import List

from hamsci_dsp.timing_map import PAYLOAD_CHAIN_ID, SCHEMA, SYSCLOCK_CHAIN_ID, BudgetTerm, Chain

logger = logging.getLogger(__name__)

DEFAULT_PATH = Path("/run/hf-timestd/timing_chain.json")


def default_payload_chain() -> Chain:
    return Chain(
        id=PAYLOAD_CHAIN_ID,
        measurand="UTC instant at which sample n was taken, at the antenna terminals",
        measurand_plane="antenna_terminals",
        calibration_plane="ts1_injection_point",
        traceability={"claim": "UTC(USNO) via GPS", "qualified": True,
                      "qualification": "antenna-to-injector path not declared; receiver front end not characterised"},
        budget=(
            BudgetTerm("ts1_modulator_delay", type="B", correction_ns=0, u_ns=200,
                       method="designer statement, P. Elliott WB6CXC, 2026-08-30; standard injector mode"),
            BudgetTerm("antenna_to_injector", type="B", disposition="not_declared",
                       spans=("antenna_terminals", "ts1_injection_point"),
                       method="feed, preamp and filter ahead of the injection point; station-specific"),
            BudgetTerm("injector_to_receiver", disposition="cancels",
                       spans=("ts1_injection_point", "rx888_adc"),
                       method="identical path for signal and injected reference; cancels by construction"),
            BudgetTerm("gnss_antenna_feed", type="B", disposition="not_declared",
                       method="cable length x velocity factor; a sign-known bias, not an uncertainty"),
            BudgetTerm("anchor_origin_dispersion", type="A", correction_ns=0, u_ns=1900,
                       measured_on={"build": "pre-folding", "date": "2026-08-24"}, disposition="historical",
                       method="63 anchors over 4.5 h ACROSS RE-LOCKS; the folded build of 2026-08-29 removed the re-locks"),
            BudgetTerm("edge_estimation", type="B", correction_ns=0, u_ns=5000,
                       method="conservative bound; becomes Type A computed from cn0_db_hz once the fine-stage sweep has run"),
            BudgetTerm("filter_group_delay", type="B", disposition="excluded_by_convention",
                       method="labeling_convention = content: the channel filter's group delay is pipeline latency outside the measurand"),
        ),
        k=2,
    )


def default_sysclock_chain() -> Chain:
    return Chain(
        id=SYSCLOCK_CHAIN_ID,
        measurand="UTC instant at which sample n was taken, at radiod's advertised epoch",
        measurand_plane="radiod_rtp_timesnap",
        calibration_plane="host_system_clock",
        traceability={"claim": "UTC via the host clock's chrony reference", "qualified": True,
                      "qualification": "the registration descends from the host system clock; its reference and discipline are stated per interval in engineering.host_clock"},
        budget=(
            BudgetTerm("pair_non_atomicity", type="A", correction_ns=0, u_ns=8_030_000,
                       measured_on={"build": "pre-anchor-inversion", "date": "2026-08-16"}, disposition="historical",
                       method="p99 of GPS_TIME minus RTP_TIMESNAP-implied epoch, 900 s, T6 channel; the running-minimum estimator of MEASUREMENT_MODEL §6.2 replaces this"),
            BudgetTerm("host_clock_discipline", type="A", disposition="per_interval",
                       method="the largest witnessed disagreement in the authority manager's host_clock block; enters u_epoch_ns directly, per interval, never as a constant"),
            BudgetTerm("channel_filter_group_delay", disposition="excluded_by_convention",
                       method="under labeling_convention = content the label denotes the antenna instant; the processing interval is pipeline latency, outside the measurand"),
        ),
        k=1,
    )


def chains_from_config(cfg: dict) -> List[Chain]:
    prov = ((cfg.get("timing", {}) or {}).get("provenance", {}) or {})
    payload = default_payload_chain()
    overrides = [BudgetTerm.from_dict(t) for t in (prov.get("budget") or [])]
    by_name = {t.term: t for t in payload.budget}
    for t in overrides:
        by_name[t.term] = t
    traceability = dict(payload.traceability)
    if prov.get("traceability_qualification"):
        traceability["qualification"] = str(prov["traceability_qualification"])
    payload = Chain(
        id=payload.id, measurand=payload.measurand,
        measurand_plane=str(prov.get("measurand_plane", payload.measurand_plane)),
        calibration_plane=str(prov.get("calibration_plane", payload.calibration_plane)),
        traceability=traceability, budget=tuple(by_name.values()), k=payload.k,
    )
    return [payload, default_sysclock_chain()]


def publish_chains(chains: List[Chain], path: Path = DEFAULT_PATH) -> None:
    """Atomic, world-readable, never raises."""
    doc = {"schema": SCHEMA,
           "written_utc": datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z"),
           "chains": [c.to_record() for c in chains]}
    try:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", dir=str(path.parent), prefix=f".{path.name}.",
                                         suffix=".tmp", delete=False, encoding="utf-8") as tmp:
            json.dump(doc, tmp, separators=(",", ":"))
            tmp.flush()
            os.fsync(tmp.fileno())
            tmp_path = tmp.name
        os.chmod(tmp_path, 0o644)
        os.replace(tmp_path, path)
    except OSError as exc:
        logger.warning("timing chain: failed to write %s: %s", path, exc)

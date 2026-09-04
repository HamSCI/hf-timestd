"""The station declares its plane and budget; the fusion service publishes
them where the packager can read them (TIMING_PROVENANCE_MODEL §3.2, §6)."""
import json

import pytest

from hf_timestd.core.timing_chain_publisher import (
    chains_from_config, default_payload_chain, default_sysclock_chain, publish_chains,
)


def test_default_payload_chain_matches_the_spec_shape():
    c = default_payload_chain()
    assert c.id == "payload-anchored@1"
    terms = {t.term: t for t in c.budget}
    assert terms["ts1_modulator_delay"].u_ns == 200
    assert terms["antenna_to_injector"].disposition == "not_declared"
    assert terms["filter_group_delay"].disposition == "excluded_by_convention"
    assert c.u_combined_ns == 5353   # sqrt(200^2 + 1900^2 + 5000^2) = 5352.57


def test_default_sysclock_chain_has_no_single_combined_figure():
    c = default_sysclock_chain()
    assert c.id == "sysclock@1" and c.u_combined_ns is None
    assert {t.term for t in c.budget} >= {"pair_non_atomicity", "host_clock_discipline"}


def test_config_override_replaces_a_term_and_keeps_the_rest():
    cfg = {"timing": {"provenance": {
        "traceability_qualification": "feed measured 2026-09-10; front end not characterised",
        "budget": [{"term": "antenna_to_injector", "type": "B", "correction_ns": 41_000, "u_ns": 3_000,
                    "method": "12.3 m LMR-400 at 0.85 VF + preamp datasheet, 2026-09-10"}],
    }}}
    payload, sysclock = chains_from_config(cfg)
    terms = {t.term: t for t in payload.budget}
    assert terms["antenna_to_injector"].correction_ns == 41_000 and terms["antenna_to_injector"].disposition is None
    assert terms["ts1_modulator_delay"].u_ns == 200
    assert payload.traceability["qualification"].startswith("feed measured")
    assert sysclock.id == "sysclock@1"


def test_publish_writes_both_chains_atomically(tmp_path):
    path = tmp_path / "timing_chain.json"
    publish_chains(chains_from_config({}), path=path)
    doc = json.loads(path.read_text())
    assert doc["schema"] == "v2" and [c["id"] for c in doc["chains"]] == ["payload-anchored@1", "sysclock@1"]
    assert not list(tmp_path.glob("*.tmp"))


def test_publish_never_raises(tmp_path):
    publish_chains(chains_from_config({}), path=tmp_path / "no" / "such" / "dir" / "x.json")

#!/usr/bin/env python3
"""Three quantities wore the name `chain_delay_ns`; two of them must stop.

`CLIENT-CONTRACT.md` publishes a per-radiod fact `RADIOD_<id>_CHAIN_DELAY_NS`
and tells every timing-critical client to apply it as

    utc_corrected = utc_raw - chain_delay_ns / 1e9

sourcing the value from "the calibrating hf-timestd instance".  The
hf-timestd field bearing the matching name, `t6_pps.chain_delay_ns`, reads
0.5955 s — where the recovered edge falls inside the named second, not any
analogue path delay.  No path in this station spans half a second.  Nothing
publishes the fact today, so the hazard stays latent, but an implementer
wiring the mechanism as written would reach for the matching name and shift
every client's UTC by 596 ms.

`t6_authority.asserted_chain_delay_ns` reports a third thing again — the
configured budget being applied, an assertion rather than a measurement.

See `docs/design/TIMING_PROVENANCE_MODEL.md` §4.5.  The contract keeps the
name.  These two give it up, through a deprecation window that emits both
keys so any reader outside this repo keeps working for one release.
"""

import pytest

from hf_timestd.core import core_recorder_v2 as crv2


class _Auth:
    state = type('S', (), {'value': 'ASSERTING'})()
    delay_budget_ns = 200
    filter_group_delay_ns = 9800


class _Rec:
    """Minimal stand-in exposing only what the status builders read."""
    _t6_authority = _Auth()
    _t6_fine_stage = None
    _t6_last_decision = None


def _authority_status():
    rec = _Rec()
    return crv2.CoreRecorderV2._t6_authority_status(rec)


class TestAssertedChainDelayRename:

    def test_publishes_applied_delay_budget_ns(self):
        """The new name states what the number holds: an applied budget."""
        status = _authority_status()
        assert status is not None
        assert 'applied_delay_budget_ns' in status
        assert status['applied_delay_budget_ns'] == (
            _Auth.delay_budget_ns + _Auth.filter_group_delay_ns)

    def test_keeps_the_old_key_through_the_deprecation_window(self):
        """Readers outside this repo get one release to move."""
        status = _authority_status()
        assert 'asserted_chain_delay_ns' in status
        assert (status['asserted_chain_delay_ns']
                == status['applied_delay_budget_ns'])


class TestT6PpsEdgePhaseRename:

    def test_status_dict_carries_both_names(self):
        """`t6_pps` publishes the edge phase under a name that describes it.

        Built directly rather than through `_write_status`, which needs a
        whole recorder; the contract under test is the key set the file
        carries, and `_t6_pps_edge_phase_keys` is what puts it there.
        """
        keys = crv2._t6_pps_edge_phase_keys(chain_delay_ns=595_500_000.0)
        assert keys['edge_phase_in_named_second_ns'] == 595_500_000.0
        assert keys['chain_delay_ns'] == 595_500_000.0, (
            "deprecated key must survive the window")

    def test_none_propagates_to_both_names(self):
        keys = crv2._t6_pps_edge_phase_keys(chain_delay_ns=None)
        assert keys['edge_phase_in_named_second_ns'] is None
        assert keys['chain_delay_ns'] is None

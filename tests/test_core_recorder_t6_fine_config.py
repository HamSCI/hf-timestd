"""[timing.t6_pps] fine-stage/authority key parsing (spec §7)."""
import pytest

from hf_timestd.core.core_recorder_v2 import CoreRecorderV2


class TestFineSettings:
    def test_defaults(self):
        s = CoreRecorderV2._t6_fine_settings({})
        assert s == {
            # A label is the antenna instant (content-time convention,
            # 2026-08-24): the radiod channel-filter group delay is
            # pipeline latency downstream of the ADC and is not applied.
            'labeling_convention': 'content',
            'fine_stage_enabled': True,
            'fine_fold_seconds': 30,
            # TS-1 modulator only, and sourced: Paul Elliott (WB6CXC),
            # who designed the TS-1, puts the standard-injector modulator
            # delay under 200 ns (2026-08-30).  The former 10_000 default
            # came from a comment in our own config template and nothing
            # outside the project ever supported it.  The antenna-to-
            # injector run enters this sum NEGATIVELY and stays undeclared;
            # see docs/design/TIMING_PROVENANCE_MODEL.md §4.5.
            'delay_budget_ns': 200,
            'filter_group_delay_ns': 0,
            # What the site configured, applied or not — so a retired
            # value is visible rather than silently dropped.
            'filter_group_delay_ns_configured': 0,
            'edge_period_tolerance_ns': 5_000,
            'fine_coarse_max_ms': 5.0,
            'degraded_unlock_after_sec': 600.0,
        }

    def test_overrides(self):
        s = CoreRecorderV2._t6_fine_settings({
            'fine_stage_enabled': False,
            'fine_fold_seconds': 10,
            'delay_budget_ns': 250_000,
            'edge_period_tolerance_ns': 2_000,
            'fine_coarse_max_ms': 3.5,
            'degraded_unlock_after_sec': 120,
        })
        assert s['fine_stage_enabled'] is False
        assert s['fine_fold_seconds'] == 10
        assert s['delay_budget_ns'] == 250_000
        assert s['fine_coarse_max_ms'] == 3.5
        assert s['degraded_unlock_after_sec'] == 120.0

    def test_delay_budget_beyond_bound_raises(self):
        with pytest.raises(ValueError, match="delay_budget"):
            CoreRecorderV2._t6_fine_settings({'delay_budget_ns': 2_000_000})

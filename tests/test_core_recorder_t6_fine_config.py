"""[timing.t6_pps] fine-stage/authority key parsing (spec §7)."""
import pytest

from hf_timestd.core.core_recorder_v2 import CoreRecorderV2


class TestFineSettings:
    def test_defaults(self):
        s = CoreRecorderV2._t6_fine_settings({})
        assert s == {
            'fine_stage_enabled': True,
            'fine_fold_seconds': 30,
            'delay_budget_ns': 10_000,
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

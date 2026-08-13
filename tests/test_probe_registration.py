"""Tier probes register by DETECTION, not by configuration.

B4 shipped `[timing.t6_pps] enabled = true` and ran T3 for months. The
runner read tier config from `timing.authority`, which on that host is the
string "rtp" — discarded as "not a dict" without a word — so every tier's
config was {}, T6 was never registered, and the authority manager reported
"no probe configured" while the operator's config looked correct.

Registration costs nothing: every probe reports available=False with a
reason when its source is absent. So a probe that cannot see its hardware
says so, instead of vanishing.
"""
import pytest

from hf_timestd.core.authority_runner import build_authority_runner_from_config


def levels(config):
    return [p.t_level for p in build_authority_runner_from_config(config=config).manager.probes]


def test_b4_as_deployed_registers_t6():
    """The exact config shape that produced the bug."""
    got = levels({"timing": {"authority": "rtp", "t6_pps": {"enabled": True}}})
    assert "T6" in got, "TS-1 present and configured, yet no T6 probe"


def test_scalar_authority_key_does_not_suppress_tiers():
    """`timing.authority = "rtp"` is a scalar. It must be ignored for tier
    config WITHOUT taking the tiers down with it."""
    assert "T6" in levels({"timing": {"authority": "rtp"}})
    assert "T5" in levels({"timing": {"authority": "rtp"}})


@pytest.mark.parametrize("config", [
    {}, {"timing": {}}, {"timing": {"authority": None}},
    {"timing": {"authority": []}}, {"timing": {"authority_manager": "nonsense"}},
])
def test_t3_and_hardware_tiers_survive_any_config_shape(config):
    """T3 is the floor and T6/T5 are hardware-detected: none of them may
    depend on config being well-formed."""
    got = levels(config)
    assert "T3" in got and "T6" in got and "T5" in got


def test_explicit_off_switch_still_works():
    """Detection-based registration must not take away operator control."""
    assert "T6" not in levels({"timing": {"t6_pps": {"enabled": False}}})
    assert "T5" not in levels({"timing": {"lb1421_enabled": False}})


def test_t4_still_requires_configured_peers():
    """T4 is the one tier that genuinely needs outside information — there
    is nothing to detect about a LAN timeserver."""
    assert "T4" not in levels({"timing": {}})
    got = levels({"timing": {"authority_manager": {"t4": {"peers": ["10.0.0.1"]}}}})
    assert "T4" in got


def test_modern_config_overrides_the_deployed_spelling():
    cfg = {"timing": {"t6_pps": {"enabled": True},
                      "authority_manager": {"t6": {"enabled": False}}}}
    assert "T6" not in levels(cfg)

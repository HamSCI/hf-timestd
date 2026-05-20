"""Regression coverage for P-H24.

RaytraceEngine processed PHaRLAP raytrace_2d output by extrapolating
every hop count from the hop-0 group/ground ratio and reusing the hop-0
apogee for all modes — an approximation of an approximation on what is
meant to be the authoritative ray-traced path.

``_modes_from_ray_list`` now uses PHaRLAP's per-hop ``group_range[k]``
and ``apogee[k]`` directly, and skips a ray whose per-hop arrays
disagree in length (so index k cannot silently mean different hops).
"""

import pytest

from hf_timestd.core.raytrace_engine import RaytraceEngine, _C_KM_S


def _ray(ground, group, apogee, label):
    return {'ground_range': ground, 'group_range': group,
            'apogee': apogee, 'ray_label': label}


def test_per_hop_group_range_and_apogee_used_directly():
    # 2-hop ray. The hop-1 group/ground ratio (1530/1500 = 1.020) differs
    # from hop-2's (3120/3000 = 1.040): extrapolating from hop 0 would
    # give 3000*1.020 = 3060 km, not the true 3120 km. apogee likewise
    # differs per hop (250 vs 400 km).
    ray_list = [_ray(ground=[1500.0, 3000.0], group=[1530.0, 3120.0],
                      apogee=[250.0, 400.0], label=[1, 1])]
    modes = RaytraceEngine._modes_from_ray_list(
        ray_list, elevs=[15.0], target_range_km=3000.0, tolerance_km=300.0,
    )
    assert len(modes) == 1
    m = modes[0]
    assert m.n_hops == 2
    # group delay from group_range[1] directly — not ground_range[1]×ratio
    assert m.group_delay_ms == pytest.approx(3120.0 / _C_KM_S * 1000.0)
    assert m.group_delay_ms != pytest.approx(3060.0 / _C_KM_S * 1000.0)
    # apogee from apogee[1], not the hop-0 apogee
    assert m.apogee_km == pytest.approx(400.0)


def test_single_hop_ray():
    ray_list = [_ray(ground=[1500.0], group=[1530.0],
                     apogee=[250.0], label=[1])]
    modes = RaytraceEngine._modes_from_ray_list(
        ray_list, elevs=[20.0], target_range_km=1500.0, tolerance_km=300.0,
    )
    assert len(modes) == 1
    assert modes[0].n_hops == 1
    assert modes[0].apogee_km == pytest.approx(250.0)


def test_incomplete_hop_label_skipped():
    # hop 2 did not complete cleanly (label 0) — only the 1-hop mode.
    ray_list = [_ray(ground=[1500.0, 3000.0], group=[1530.0, 3120.0],
                     apogee=[250.0, 400.0], label=[1, 0])]
    modes = RaytraceEngine._modes_from_ray_list(
        ray_list, elevs=[15.0], target_range_km=1500.0, tolerance_km=300.0,
    )
    assert [m.n_hops for m in modes] == [1]


def test_length_mismatched_ray_is_skipped():
    # group_range shorter than ground_range — index k would mis-index.
    ray_list = [_ray(ground=[1500.0, 3000.0], group=[1530.0],
                     apogee=[250.0, 400.0], label=[1, 1])]
    modes = RaytraceEngine._modes_from_ray_list(
        ray_list, elevs=[15.0], target_range_km=3000.0, tolerance_km=300.0,
    )
    assert modes == []


def test_ray_not_landing_on_receiver_skipped():
    ray_list = [_ray(ground=[1500.0], group=[1530.0],
                     apogee=[250.0], label=[1])]
    modes = RaytraceEngine._modes_from_ray_list(
        ray_list, elevs=[15.0], target_range_km=3000.0, tolerance_km=300.0,
    )
    assert modes == []

"""
Unit tests for hf_timestd.service_profile

Covers:
- Profile resolution from config (profile name, overrides, vtec gating)
- active_services / inactive_services / systemd_units
- core_recorder is always-on (cannot be overridden off)
- vtec requires gnss_vtec.enabled even when the profile/override would enable it
- summary() shape and source attribution
- apply_profile() dry-run semantics and subprocess interaction (mocked)
- get_unit_status() handles missing systemctl
"""

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from hf_timestd.service_profile import (
    ALL_SERVICES,
    PROFILE_DESCRIPTIONS,
    PROFILE_NAMES,
    PROFILES,
    SERVICE_UNIT_MAP,
    ServiceProfile,
    apply_profile,
    get_unit_status,
)


# =============================================================================
# Module-level invariants
# =============================================================================


class TestModuleConstants:
    def test_every_profile_includes_core_recorder(self):
        for name, services in PROFILES.items():
            assert 'core_recorder' in services, f"profile '{name}' missing core_recorder"

    def test_every_profile_service_has_a_unit(self):
        for name, services in PROFILES.items():
            for svc in services:
                assert svc in SERVICE_UNIT_MAP, \
                    f"profile '{name}' references unknown service '{svc}'"

    def test_every_profile_has_a_description(self):
        assert set(PROFILE_DESCRIPTIONS) == set(PROFILES)

    def test_profile_names_sorted(self):
        assert PROFILE_NAMES == sorted(PROFILES.keys())

    def test_all_services_sorted(self):
        assert ALL_SERVICES == sorted(SERVICE_UNIT_MAP.keys())


# =============================================================================
# from_config()
# =============================================================================


class TestFromConfig:
    def test_default_profile_when_unspecified(self):
        profile = ServiceProfile.from_config({})
        assert profile.profile_name == 'rtp'
        assert profile.overrides == {}
        assert profile.vtec_available is False

    def test_explicit_known_profile(self):
        profile = ServiceProfile.from_config({'services': {'profile': 'fusion'}})
        assert profile.profile_name == 'fusion'

    def test_unknown_profile_falls_back_to_rtp(self, caplog):
        profile = ServiceProfile.from_config({'services': {'profile': 'bogus'}})
        assert profile.profile_name == 'rtp'
        # And there's a warning about it
        assert any('bogus' in rec.message for rec in caplog.records)

    def test_overrides_collected_only_for_bool_values(self):
        profile = ServiceProfile.from_config({
            'services': {
                'profile': 'rtp',
                'physics': True,         # override-on
                'web_api': False,        # override-off
                'metrology': 'sometimes',  # not bool — ignored
                'unknown_service': True,   # not in registry — ignored
            }
        })
        assert profile.overrides == {'physics': True, 'web_api': False}

    def test_vtec_flag_picked_up_from_gnss_vtec_section(self):
        profile = ServiceProfile.from_config({
            'gnss_vtec': {'enabled': True},
        })
        assert profile.vtec_available is True


# =============================================================================
# active_services / inactive_services
# =============================================================================


class TestActiveServices:
    def test_archive_baseline(self):
        profile = ServiceProfile(profile_name='archive')
        assert profile.active_services() == {'core_recorder', 'prune'}

    def test_full_baseline(self):
        profile = ServiceProfile(profile_name='full')
        active = profile.active_services()
        # Spot-check a few characteristic members
        assert 'physics' in active
        assert 'fusion' in active
        assert 'iono_reanalysis' in active
        assert 'core_recorder' in active

    def test_override_can_enable_extra_service(self):
        profile = ServiceProfile(
            profile_name='archive',
            overrides={'web_api': True},
        )
        active = profile.active_services()
        assert 'web_api' in active
        assert 'core_recorder' in active

    def test_override_can_disable_profile_service(self):
        profile = ServiceProfile(
            profile_name='full',
            overrides={'physics': False},
        )
        assert 'physics' not in profile.active_services()

    def test_core_recorder_cannot_be_overridden_off(self):
        profile = ServiceProfile(
            profile_name='full',
            overrides={'core_recorder': False},
        )
        # Always-on guarantee
        assert 'core_recorder' in profile.active_services()

    def test_vtec_requires_gnss_vtec_available(self):
        # Override-on but vtec not available → still inactive
        profile = ServiceProfile(
            profile_name='archive',
            overrides={'vtec': True},
            vtec_available=False,
        )
        assert 'vtec' not in profile.active_services()

    def test_vtec_active_when_available(self):
        profile = ServiceProfile(
            profile_name='archive',
            overrides={'vtec': True},
            vtec_available=True,
        )
        assert 'vtec' in profile.active_services()

    def test_inactive_is_complement_of_active(self):
        profile = ServiceProfile(profile_name='fusion')
        active = profile.active_services()
        inactive = profile.inactive_services()
        # No overlap
        assert active.isdisjoint(inactive)
        # Together cover the full registry
        assert active | inactive == set(ALL_SERVICES)


# =============================================================================
# systemd_units()
# =============================================================================


class TestSystemdUnits:
    def test_active_units_are_sorted_unit_names(self):
        profile = ServiceProfile(profile_name='archive')
        units = profile.systemd_units(active=True)
        assert units == sorted(units)
        # archive baseline is core_recorder + prune
        assert 'timestd-core-recorder.service' in units
        assert 'timestd-prune.timer' in units

    def test_inactive_units_complement_active(self):
        profile = ServiceProfile(profile_name='archive')
        active = set(profile.systemd_units(active=True))
        inactive = set(profile.systemd_units(active=False))
        assert active.isdisjoint(inactive)
        assert active | inactive == set(SERVICE_UNIT_MAP.values())


# =============================================================================
# summary()
# =============================================================================


class TestSummary:
    def test_shape_and_source_attribution(self):
        profile = ServiceProfile(
            profile_name='rtp',
            overrides={'physics': True},
            vtec_available=False,
        )
        summary = profile.summary()

        assert summary['profile'] == 'rtp'
        assert summary['description'] == PROFILE_DESCRIPTIONS['rtp']
        assert summary['vtec_available'] is False
        assert set(summary['services']) == set(ALL_SERVICES)

        # core_recorder source is 'always'
        assert summary['services']['core_recorder']['source'] == 'always'
        assert summary['services']['core_recorder']['enabled'] is True

        # An overridden service is sourced from 'override'
        assert summary['services']['physics']['source'] == 'override'
        assert summary['services']['physics']['enabled'] is True

        # A profile-baseline service is sourced from 'profile'
        assert summary['services']['web_api']['source'] == 'profile'
        assert summary['services']['web_api']['enabled'] is True

        # A non-active service is reported as enabled=False
        assert summary['services']['iono_reanalysis']['enabled'] is False


# =============================================================================
# get_unit_status()
# =============================================================================


class TestGetUnitStatus:
    def test_parses_systemctl_show_output(self):
        fake_stdout = (
            "ActiveState=active\n"
            "SubState=running\n"
            "LoadState=loaded\n"
        )
        with patch('hf_timestd.service_profile.subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(stdout=fake_stdout)
            status = get_unit_status('timestd-core-recorder.service')

        assert status == {
            'active_state': 'active',
            'sub_state': 'running',
            'load_state': 'loaded',
        }

    def test_missing_systemctl_returns_unknown(self):
        with patch('hf_timestd.service_profile.subprocess.run',
                   side_effect=FileNotFoundError):
            status = get_unit_status('whatever.service')
        assert status == {
            'active_state': 'unknown',
            'sub_state': 'unknown',
            'load_state': 'unknown',
        }

    def test_timeout_returns_unknown(self):
        with patch('hf_timestd.service_profile.subprocess.run',
                   side_effect=subprocess.TimeoutExpired(cmd='systemctl', timeout=5)):
            status = get_unit_status('whatever.service')
        assert status['active_state'] == 'unknown'

    def test_missing_keys_default_to_unknown(self):
        # If systemctl returns partial output, missing keys → 'unknown'
        with patch('hf_timestd.service_profile.subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(stdout="ActiveState=active\n")
            status = get_unit_status('partial.service')
        assert status['active_state'] == 'active'
        assert status['sub_state'] == 'unknown'
        assert status['load_state'] == 'unknown'


# =============================================================================
# apply_profile()
# =============================================================================


class TestApplyProfile:
    def test_dry_run_reports_intended_actions_without_running_systemctl(self):
        profile = ServiceProfile(profile_name='archive')
        with patch('hf_timestd.service_profile.subprocess.run') as mock_run:
            actions = apply_profile(profile, dry_run=True)
        # No subprocess calls in dry-run
        mock_run.assert_not_called()

        # Active units report 'would enable', inactive 'would disable'
        for unit in profile.systemd_units(active=True):
            assert actions[unit] == 'would enable'
        for unit in profile.systemd_units(active=False):
            assert actions[unit] == 'would disable'

    def test_real_run_reports_enabled_and_disabled(self):
        profile = ServiceProfile(profile_name='archive')
        with patch('hf_timestd.service_profile.subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stderr='')
            actions = apply_profile(profile, dry_run=False)

        # Every active unit was attempted with `enable --now`
        active_units = profile.systemd_units(active=True)
        for unit in active_units:
            assert actions[unit] == 'enabled'

        inactive_units = profile.systemd_units(active=False)
        for unit in inactive_units:
            assert actions[unit] == 'disabled'

    def test_disable_failure_for_unknown_unit_is_treated_as_skipped(self):
        profile = ServiceProfile(profile_name='archive')

        def fake_run(cmd, *args, **kwargs):
            if 'enable' in cmd:
                return MagicMock(returncode=0, stderr='')
            # disable raises with a "not loaded" stderr → treated as skipped
            err = subprocess.CalledProcessError(1, cmd)
            err.stderr = 'Failed: unit not loaded'
            raise err

        with patch('hf_timestd.service_profile.subprocess.run',
                   side_effect=fake_run):
            actions = apply_profile(profile, dry_run=False)

        # Some inactive unit should have been skipped
        skipped = [u for u, a in actions.items() if a == 'skipped (not installed)']
        assert skipped, "expected at least one 'skipped' action"

    def test_enable_failure_recorded_as_error(self):
        profile = ServiceProfile(profile_name='archive')

        def fake_run(cmd, *args, **kwargs):
            if 'enable' in cmd:
                err = subprocess.CalledProcessError(1, cmd)
                err.stderr = 'permission denied'
                raise err
            return MagicMock(returncode=0, stderr='')

        with patch('hf_timestd.service_profile.subprocess.run',
                   side_effect=fake_run):
            actions = apply_profile(profile, dry_run=False)

        # Active units now have an 'error: ...' entry
        active_units = profile.systemd_units(active=True)
        for unit in active_units:
            assert actions[unit].startswith('error:')
            assert 'permission denied' in actions[unit]

"""
Service Profile Manager

Resolves the [services] config section into a concrete set of systemd units
to enable/disable.  Profiles define baseline service sets; per-service
overrides allow fine-grained control on top.

Usage:
    from hf_timestd.service_profile import ServiceProfile
    profile = ServiceProfile.from_config(config)
    profile.active_services()   # → {'core_recorder', 'web_api', ...}
    profile.systemd_units()     # → {'timestd-core-recorder.service', ...}
"""

import logging
import subprocess
from dataclasses import dataclass, field
from typing import Dict, Optional, Set, List, Any

logger = logging.getLogger(__name__)


# ── Service registry ──────────────────────────────────────────────────
# Maps config key → systemd unit name(s).
# Services with multiple units (metrology template instances) are handled
# specially in systemd_units().

SERVICE_UNIT_MAP: Dict[str, str] = {
    'core_recorder':     'timestd-core-recorder.service',
    'metrology':         'timestd-metrology.target',
    'l2_calibration':    'timestd-l2-calibration.service',
    'fusion':            'timestd-fusion.service',
    'physics':           'timestd-physics.service',
    'vtec':              'timestd-vtec.service',
    'web_api':           'timestd-web-api.service',
    'radiod_monitor':    'timestd-radiod-monitor.service',
    'grape_daily':       'grape-daily.timer',
    'chrony_monitor':    'timestd-chrony-monitor.timer',
    'ionex_download':    'timestd-ionex-download.timer',
    'iono_reanalysis':   'timestd-iono-reanalysis.timer',
    'pipeline_watchdog': 'timestd-pipeline-watchdog.timer',
    'prune':             'timestd-prune.timer',
}

# All known service keys (sorted for stable output)
ALL_SERVICES = sorted(SERVICE_UNIT_MAP.keys())


# ── Profile definitions ───────────────────────────────────────────────
# Each profile is a set of service keys that are ON by default.
# core_recorder is always on — it's the irreplaceable data source.

_ALWAYS_ON = {'core_recorder'}

PROFILES: Dict[str, Set[str]] = {
    'archive': _ALWAYS_ON | {'prune'},

    'rtp': _ALWAYS_ON | {
        'web_api', 'radiod_monitor', 'pipeline_watchdog',
        'grape_daily', 'prune',
    },

    'fusion': _ALWAYS_ON | {
        'metrology', 'l2_calibration', 'fusion',
        'vtec',  # gated by [gnss_vtec].enabled — suppressed on hosts
                 # without a GNSS receiver (see active_services()).
                 # Fusion reads gnss_vtec data via
                 # multi_broadcast_fusion._read_gnss_vtec() when present,
                 # so this is a natural augmentation of timing production.
        'web_api', 'radiod_monitor', 'pipeline_watchdog',
        'chrony_monitor', 'grape_daily', 'prune',
    },

    'full': _ALWAYS_ON | {
        'metrology', 'l2_calibration', 'fusion',
        'physics', 'ionex_download', 'iono_reanalysis',
        'vtec',  # gated by [gnss_vtec].enabled — suppressed on hosts
                 # without a GNSS receiver (see active_services())
        'web_api', 'radiod_monitor', 'pipeline_watchdog',
        'chrony_monitor', 'grape_daily', 'prune',
    },
}

PROFILE_NAMES = sorted(PROFILES.keys())


# ── Profile description (for help text) ──────────────────────────────

PROFILE_DESCRIPTIONS: Dict[str, str] = {
    'archive': 'Core recorder only — raw IQ preservation, minimal resources',
    'rtp':     'Archive + web-api + monitoring — standard RTP/GPSDO mode',
    'fusion':  'RTP + metrology + fusion — GPS-denied timing from HF broadcasts',
    'full':    'Fusion + physics + ionospheric — full science and timing',
}


@dataclass
class ServiceProfile:
    """Resolved service profile with per-service overrides applied."""

    profile_name: str
    overrides: Dict[str, bool] = field(default_factory=dict)
    vtec_available: bool = False  # gnss_vtec.enabled in config

    @classmethod
    def from_config(cls, config: Dict[str, Any]) -> 'ServiceProfile':
        """Build a ServiceProfile from the parsed TOML config dict."""
        services_section = config.get('services', {})
        profile_name = services_section.get('profile', 'rtp')

        if profile_name not in PROFILES:
            logger.warning(
                f"Unknown profile '{profile_name}', falling back to 'rtp'. "
                f"Valid profiles: {', '.join(PROFILE_NAMES)}"
            )
            profile_name = 'rtp'

        # Collect per-service overrides (anything that's a bool)
        overrides = {}
        for key in ALL_SERVICES:
            val = services_section.get(key)
            if isinstance(val, bool):
                overrides[key] = val

        vtec_available = config.get('gnss_vtec', {}).get('enabled', False)

        return cls(
            profile_name=profile_name,
            overrides=overrides,
            vtec_available=vtec_available,
        )

    def active_services(self) -> Set[str]:
        """Return the set of service keys that should be running."""
        base = set(PROFILES[self.profile_name])

        # Apply overrides
        for key, enabled in self.overrides.items():
            if enabled:
                base.add(key)
            else:
                base.discard(key)

        # core_recorder is always on — cannot be overridden off
        base.add('core_recorder')

        # vtec requires gnss_vtec.enabled in addition to profile/override
        if 'vtec' in base and not self.vtec_available:
            base.discard('vtec')

        return base

    def inactive_services(self) -> Set[str]:
        """Return the set of service keys that should NOT be running."""
        return set(ALL_SERVICES) - self.active_services()

    def systemd_units(self, active: bool = True) -> List[str]:
        """Return systemd unit names for active (or inactive) services.

        Args:
            active: If True, return units that should be enabled/started.
                    If False, return units that should be disabled/stopped.
        """
        services = self.active_services() if active else self.inactive_services()
        units = []
        for svc in sorted(services):
            unit = SERVICE_UNIT_MAP.get(svc)
            if unit:
                units.append(unit)
        return units

    def summary(self) -> Dict[str, Any]:
        """Return a dict suitable for JSON output or display."""
        active = self.active_services()
        rows = {}
        for svc in ALL_SERVICES:
            source = 'profile'
            if svc in self.overrides:
                source = 'override'
            elif svc == 'core_recorder':
                source = 'always'
            rows[svc] = {
                'enabled': svc in active,
                'source': source,
                'unit': SERVICE_UNIT_MAP.get(svc, ''),
            }
        return {
            'profile': self.profile_name,
            'description': PROFILE_DESCRIPTIONS.get(self.profile_name, ''),
            'vtec_available': self.vtec_available,
            'services': rows,
        }


# ── Systemd helpers ───────────────────────────────────────────────────

def get_unit_status(unit: str) -> Dict[str, str]:
    """Query systemd for a unit's current state.

    Returns dict with keys: active_state, sub_state, load_state.
    Returns placeholder values if systemctl is unavailable.
    """
    try:
        result = subprocess.run(
            ['systemctl', 'show', unit,
             '--property=ActiveState,SubState,LoadState',
             '--no-pager'],
            capture_output=True, text=True, timeout=5,
        )
        props = {}
        for line in result.stdout.strip().splitlines():
            if '=' in line:
                k, v = line.split('=', 1)
                props[k] = v
        return {
            'active_state': props.get('ActiveState', 'unknown'),
            'sub_state': props.get('SubState', 'unknown'),
            'load_state': props.get('LoadState', 'unknown'),
        }
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return {
            'active_state': 'unknown',
            'sub_state': 'unknown',
            'load_state': 'unknown',
        }


def apply_profile(profile: ServiceProfile, dry_run: bool = False) -> Dict[str, str]:
    """Enable/disable systemd units to match the profile.

    Returns a dict of {unit: action} where action is 'enabled',
    'disabled', or 'skipped'.

    Requires root privileges unless dry_run=True.

    Implementation notes:
      * `--no-reload` is passed to every enable/disable so we don't
        trigger systemd's implicit daemon-reload after each call.  A
        single `systemctl daemon-reload` at the start (below) ensures
        systemd's view of unit files is current; per-unit reloads
        afterward are redundant and were observed to cascade-bounce
        long-running services like timestd-core-recorder.service
        (one operator-visible install.sh run produced 10 reloads in
        3 seconds, which coincided with a core-recorder restart that
        in turn cascade-stopped all 9 metrology@* instances via
        Requires=).
      * `timeout=120` (not 30) accommodates Type=notify services
        whose ExecStart legitimately takes >30 seconds to send
        READY=1.  timestd-core-recorder.service is the prime
        example: IRI2020 init + multicast joins + ring-buffer
        allocation routinely take 35–45 seconds.  At timeout=30 the
        subprocess.run would fire while systemd was still mid-start
        and we'd report a false-positive error; downstream units
        with Requires= on the still-pending unit would then queue
        behind it and also time out.
    """
    actions = {}

    # Reload once up front so systemd has the current view of all unit
    # files before we start enabling/disabling.  Suppresses the implicit
    # reload that `systemctl enable/disable` would otherwise do per call.
    if not dry_run:
        try:
            subprocess.run(
                ['systemctl', 'daemon-reload'],
                capture_output=True, text=True, timeout=10, check=True,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired,
                FileNotFoundError) as e:
            logger.warning(f"daemon-reload failed (continuing): {e}")

    for unit in profile.systemd_units(active=True):
        if dry_run:
            actions[unit] = 'would enable'
        else:
            try:
                subprocess.run(
                    ['systemctl', 'enable', '--no-reload', '--now', unit],
                    capture_output=True, text=True, timeout=120,
                    check=True,
                )
                actions[unit] = 'enabled'
            except subprocess.CalledProcessError as e:
                logger.error(f"Failed to enable {unit}: {e.stderr.strip()}")
                actions[unit] = f'error: {e.stderr.strip()}'
            except (subprocess.TimeoutExpired, FileNotFoundError) as e:
                actions[unit] = f'error: {e}'

    for unit in profile.systemd_units(active=False):
        if dry_run:
            actions[unit] = 'would disable'
        else:
            try:
                subprocess.run(
                    ['systemctl', 'disable', '--no-reload', '--now', unit],
                    capture_output=True, text=True, timeout=120,
                    check=True,
                )
                actions[unit] = 'disabled'
            except subprocess.CalledProcessError as e:
                # Not-found or already disabled is fine
                stderr = e.stderr.strip()
                if 'not found' in stderr or 'not loaded' in stderr:
                    actions[unit] = 'skipped (not installed)'
                else:
                    logger.error(f"Failed to disable {unit}: {stderr}")
                    actions[unit] = f'error: {stderr}'
            except (subprocess.TimeoutExpired, FileNotFoundError) as e:
                actions[unit] = f'error: {e}'

    return actions

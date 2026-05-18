#!/usr/bin/env python3
"""
Command Line Interface for hf-timestd

Subcommands for wsprdaemon / external client integration:
    version   — machine-readable version info (--json)
    status    — pipeline health check (exit codes 0/1/2)
    calibrate — run fusion with JSON calibration file output
"""

import sys
import json
import logging
import argparse
import time
from pathlib import Path
from .core.core_recorder_v2 import CoreRecorderV2


# ============================================================================
# Client API handlers (version, status, calibrate)
# ============================================================================

def _handle_version(args):
    """Print hf-timestd version information."""
    try:
        from importlib.metadata import version as pkg_version
        ver = pkg_version('hf-timestd')
    except Exception:
        ver = 'unknown (not installed as package)'

    from .version import COMPONENT_VERSIONS, GIT_INFO

    info = {
        'name': 'hf-timestd',
        'version': ver,
        'git': GIT_INFO,
        'components': COMPONENT_VERSIONS,
        'python': sys.version.split()[0],
        'schemas': {
            'calibration': '1.0.0',
        },
    }

    if getattr(args, 'json', False):
        print(json.dumps(info, indent=2))
    else:
        print(f"hf-timestd {ver}")
        if GIT_INFO.get('short'):
            dirty = ' (dirty)' if GIT_INFO.get('dirty') else ''
            ref = GIT_INFO.get('ref') or '?'
            print(f"  Git: {GIT_INFO['short']} on {ref}{dirty}")
            print(f"  Source: {GIT_INFO.get('source')}")
        print(f"  Python: {info['python']}")
        print(f"  Calibration schema: {info['schemas']['calibration']}")
        print(f"  Components:")
        for k, v in COMPONENT_VERSIONS.items():
            print(f"    {k}: {v}")


def _handle_inventory(args):
    """`hf-timestd inventory --json` — sigmond client-contract surface.

    Emits a clean JSON document to stdout describing every hf-timestd
    instance on this host: which radiod each one binds to, the channels
    it will request, the disk it writes, and what it provides to other
    clients (timing calibration).  Sigmond consumes this via subprocess
    to learn about hf-timestd without importing any of its code.

    See sigmond/docs/CLIENT-CONTRACT.md for the full schema.
    """
    import os
    import toml as _toml
    from importlib.metadata import version as pkg_version, PackageNotFoundError

    config_path = Path(getattr(args, 'config', None) or
                       os.environ.get('TIMESTD_CONFIG') or
                       '/etc/hf-timestd/timestd-config.toml')

    instances = []
    issues    = []

    if not config_path.exists():
        issues.append({
            'severity': 'warn',
            'instance': None,
            'message':  f'{config_path} not found',
        })
    else:
        try:
            with open(config_path, 'r') as f:
                cfg = _toml.load(f)
        except Exception as exc:
            issues.append({
                'severity': 'fail',
                'instance': None,
                'message':  f'failed to parse {config_path}: {exc}',
            })
            cfg = None

        if cfg is not None:
            recorder = cfg.get('recorder', {}) or {}
            ka9q     = cfg.get('ka9q', {})     or {}
            timing   = cfg.get('timing', {})   or {}
            station  = cfg.get('station', {})  or {}

            freqs = []
            for group in (recorder.get('channel_group', {}) or {}).values():
                for ch in (group.get('channels', []) or []):
                    hz = ch.get('frequency_hz')
                    if hz:
                        freqs.append(int(hz))

            data_root = recorder.get('production_data_root', '/var/lib/timestd')
            mode      = recorder.get('mode', 'production')
            if mode != 'production':
                data_root = recorder.get('test_data_root', data_root)

            # Contract v0.3 §7: ka9q-python owns data multicast derivation.
            # Inventory reports null here and the running daemon resolves
            # it from ChannelInfo at runtime.  Warn if a deprecated
            # override key is present.
            if ka9q.get('data_destination') or cfg.get('radiod_multicast_group'):
                issues.append({
                    'severity': 'warn',
                    'instance': 'default',
                    'message':  ('[ka9q].data_destination / radiod_multicast_group '
                                 'is deprecated under contract v0.3 §7; ka9q-python '
                                 'now derives the multicast group automatically'),
                })
            data_destination = None

            # Output sinks per instance. HDF5 is the canonical L1/L2
            # artefact. (The CONTRACT v0.6 §17 ClickHouse staging sink was
            # removed — hf-timestd stages L2 events to SQLite, not ClickHouse.)
            data_sinks = [
                {
                    'kind':           'file',
                    'target':         data_root,
                    'schema_ref':     None,
                    'retention_days': 0,        # operator-managed
                    'mb_per_day':     0,        # not estimated yet
                },
            ]

            instances.append({
                'instance':                    'default',
                'radiod_id':                   None,    # set by sigmond via coordination.toml
                'host':                        'localhost',
                'required_cores':              [],
                'preferred_cores':             'worker',
                'frequencies_hz':              freqs,
                'ka9q_channels':               len(freqs),
                'data_destination':            data_destination,
                'data_sinks':                  data_sinks,
                'uses_timing_calibration':     False,
                'provides_timing_calibration': bool(timing.get('authority')),
                # Standalone fallback: clients can read these from their own
                # config file when sigmond/coordination.env are absent.
                'radiod_status_dns':           ka9q.get('status_address', ''),
                # CONTRACT-v0.5 §16.3: declare data source.  hf-timestd
                # uses ka9q-python; radiod_id is null at inventory time
                # because sigmond resolves it via coordination.toml.
                'data_path': {
                    'kind':      'radiod-ka9q-python',
                    'radiod_id': None,
                },
                # CONTRACT-v0.5 §3 amendment + §13.1: per-instance
                # control-socket path.  hf-timestd already exposes a web
                # API (port 8000) for deep debug per §13.5; the unix
                # socket is a parallel surface that will be served when
                # the §13 control surface phase ships.  The path here is
                # advisory.
                'control_socket': '/run/hf-timestd/control.sock',
            })

    try:
        version = pkg_version('hf-timestd')
    except PackageNotFoundError:
        version = 'unknown'

    from .version import GIT_INFO

    payload = {
        'client':           'hf-timestd',
        'version':          version,
        'git':              GIT_INFO,
        'contract_version': '0.6',
        'config_path':      str(config_path),
        'log_paths': {
            # As of v6.12 every timestd-* unit writes to journald.
            # Clients read via `journalctl -u <unit>` (or the /logs
            # web-api endpoint).  `file_dir` is retained for a small
            # number of non-systemd helper scripts (freshness,
            # data-retention cron) that still log to files.
            'journal':  'timestd-*',
            'file_dir': '/var/log/hf-timestd',
        },
        'log_level':        os.environ.get('HF_TIMESTD_LOG_LEVEL')
                             or os.environ.get('CLIENT_LOG_LEVEL')
                             or 'INFO',
        'instances':        instances,
        'deps': {
            'git': [],
            'pypi': [],
        },
        'issues': issues,
    }
    print(json.dumps(payload, indent=2))


def _handle_validate_contract(args):
    """`hf-timestd validate --json` — sigmond client-contract surface.

    Self-validates every hf-timestd instance on this host.  Returns
    {ok: bool, issues: [...]} per the contract.  Exit 0 on ok, 1 on
    issues with severity == "fail".
    """
    import os
    import toml as _toml

    config_path = Path(getattr(args, 'config', None) or
                       os.environ.get('TIMESTD_CONFIG') or
                       '/etc/hf-timestd/timestd-config.toml')
    issues = []

    if not config_path.exists():
        issues.append({
            'severity': 'fail',
            'instance': None,
            'message':  f'{config_path} not found',
        })
    else:
        try:
            with open(config_path, 'r') as f:
                cfg = _toml.load(f)
        except Exception as exc:
            issues.append({
                'severity': 'fail',
                'instance': None,
                'message':  f'failed to parse {config_path}: {exc}',
            })
            cfg = None

        if cfg is not None:
            station = cfg.get('station', {}) or {}
            if not station.get('callsign'):
                issues.append({
                    'severity': 'warn',
                    'instance': 'default',
                    'message':  'station.callsign is empty',
                })
            if not (cfg.get('ka9q', {}) or {}).get('status_address'):
                issues.append({
                    'severity': 'warn',
                    'instance': 'default',
                    'message':  'ka9q.status_address is empty (no radiod binding)',
                })
            recorder = cfg.get('recorder', {}) or {}
            channels_count = sum(
                len((g.get('channels', []) or []))
                for g in (recorder.get('channel_group', {}) or {}).values()
            )
            if channels_count == 0:
                issues.append({
                    'severity': 'warn',
                    'instance': 'default',
                    'message':  'no channels configured under recorder.channel_group',
                })

            # §12.2 (v0.4): SSRC uniqueness within a radiod block.
            # (freq, preset, sample_rate, encoding) collides on SSRC;
            # ka9q-python's MultiStream silently drops duplicates.
            seen = {}
            for gname, group in (recorder.get('channel_group', {}) or {}).items():
                preset = group.get('preset', 'iq')
                rate   = group.get('sample_rate')
                enc    = group.get('encoding', 's16be')
                for ch in (group.get('channels', []) or []):
                    hz = ch.get('frequency_hz')
                    if hz is None:
                        continue
                    key = (int(hz), preset, rate, enc)
                    if key in seen:
                        issues.append({
                            'severity': 'fail',
                            'instance': 'default',
                            'message': (
                                f'SSRC collision: channels in groups '
                                f'{seen[key]!r} and {gname!r} share '
                                f'(freq={hz}, preset={preset}, '
                                f'rate={rate}, enc={enc}) — '
                                f'ka9q-python will silently drop one'
                            ),
                        })
                    else:
                        seen[key] = gname

    ok = not any(i['severity'] == 'fail' for i in issues)
    payload = {
        'ok':          ok,
        'config_path': str(config_path),
        'issues':      issues,
    }
    print(json.dumps(payload, indent=2))
    sys.exit(0 if ok else 1)


def _handle_quality(args):
    """`hf-timestd quality --json` — sigmond-readable runtime stream quality.

    Reads the snapshot the running daemon writes via QualitySnapshotWriter
    (every ~5s, atomic).  Always exits 0 — a missing or stale snapshot
    is not a CLI failure, just data the consumer of the JSON should
    interpret.  Caller distinguishes via:

      * payload["error"] == "snapshot_missing"  → daemon never started
      * payload["stale_seconds"] > expected     → daemon hung / stopped

    See sigmond/tasks/plan-stream-quality-surface.md for the contract.
    """
    snapshot_path = Path(getattr(args, 'snapshot_path', None) or
                         '/run/hf-timestd/quality.json')

    if not snapshot_path.exists():
        print(json.dumps({
            "client":        "hf-timestd",
            "error":         "snapshot_missing",
            "snapshot_path": str(snapshot_path),
        }, indent=2))
        return

    try:
        payload = json.loads(snapshot_path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        print(json.dumps({
            "client":        "hf-timestd",
            "error":         f"snapshot_unreadable: {e.__class__.__name__}",
            "snapshot_path": str(snapshot_path),
        }, indent=2))
        return

    captured_at = float(payload.get("captured_at", 0.0) or 0.0)
    payload["stale_seconds"] = round(time.time() - captured_at, 2) \
        if captured_at > 0 else None
    payload["snapshot_path"] = str(snapshot_path)
    print(json.dumps(payload, indent=2))


def _handle_status(args):
    """
    Check pipeline health.  Returns JSON and sets exit code:
      0 = OK (usable), 1 = WARN (running but not usable), 2 = CRIT (stale/down)
    """
    result = {
        'status': 'CRIT',
        'exit_code': 2,
        'calibration': None,
        'data_freshness': {},
    }

    # 1. Check calibration file if provided
    calib_path = getattr(args, 'calib_file', None)
    if calib_path:
        calib_path = Path(calib_path)
        if calib_path.exists():
            try:
                calib = json.loads(calib_path.read_text())
                age_sec = time.time() - calib.get('last_update_unix', 0)
                result['calibration'] = {
                    'file': str(calib_path),
                    'usable': calib.get('usable', False),
                    'convergence_state': calib.get('convergence_state', 'UNKNOWN'),
                    'offset_ms': calib.get('offset_ms'),
                    'uncertainty_ms': calib.get('uncertainty_ms'),
                    'quality_grade': calib.get('quality_grade'),
                    'age_seconds': round(age_sec, 1),
                    'stale': age_sec > 300,
                }
                if calib.get('usable') and age_sec < 300:
                    result['status'] = 'OK'
                    result['exit_code'] = 0
                elif age_sec < 300:
                    result['status'] = 'WARN'
                    result['exit_code'] = 1
                # else: CRIT (stale or missing)
            except Exception as e:
                result['calibration'] = {
                    'file': str(calib_path),
                    'error': str(e),
                }
        else:
            result['calibration'] = {
                'file': str(calib_path),
                'error': 'file not found',
            }

    # 2. Check HDF5 data freshness
    data_root = Path(getattr(args, 'data_root', '/var/lib/timestd'))
    phase2_dir = data_root / 'phase2'
    if phase2_dir.exists():
        # Check fusion output
        fusion_dir = phase2_dir / 'fusion'
        if fusion_dir.exists():
            h5_files = sorted(fusion_dir.glob('fusion_fusion_timing_*.h5'))
            if h5_files:
                latest = h5_files[-1]
                age = time.time() - latest.stat().st_mtime
                result['data_freshness']['fusion_hdf5'] = {
                    'file': latest.name,
                    'age_seconds': round(age, 1),
                    'stale': age > 600,
                }
                # If no calib file was given, infer status from HDF5 freshness
                if not calib_path and age < 600:
                    result['status'] = 'WARN'
                    result['exit_code'] = 1

        # Check how many metrology channels have recent data
        channel_dirs = [d for d in phase2_dir.iterdir()
                        if d.is_dir() and d.name != 'fusion' and d.name != 'science']
        active_channels = 0
        for ch_dir in channel_dirs:
            h5s = sorted(ch_dir.glob('*.h5'))
            if h5s:
                age = time.time() - h5s[-1].stat().st_mtime
                if age < 300:
                    active_channels += 1
        result['data_freshness']['active_metrology_channels'] = active_channels
        result['data_freshness']['total_metrology_channels'] = len(channel_dirs)

    print(json.dumps(result, indent=2))
    sys.exit(result['exit_code'])


# ============================================================================
# Profile and service handlers
# ============================================================================

def _load_config_for_profile(args):
    """Load TOML config, returning (config_dict, config_path)."""
    import toml
    config_path = Path(getattr(args, 'config', '/etc/hf-timestd/timestd-config.toml'))
    if not config_path.exists():
        print(f"Config not found: {config_path}", file=sys.stderr)
        sys.exit(1)
    with open(config_path, 'r') as f:
        return toml.load(f), config_path


def _handle_profile(args, parser):
    """Handle 'hf-timestd profile' subcommands."""
    from .service_profile import (
        ServiceProfile, PROFILE_NAMES, PROFILE_DESCRIPTIONS,
        ALL_SERVICES, get_unit_status, apply_profile,
    )

    if not args.profile_command:
        if parser:
            parser.print_help()
        sys.exit(1)

    if args.profile_command == 'list':
        for name in PROFILE_NAMES:
            print(f"  {name:10s} {PROFILE_DESCRIPTIONS[name]}")
        return

    if args.profile_command == 'show':
        config, _ = _load_config_for_profile(args)
        profile = ServiceProfile.from_config(config)
        active = profile.active_services()

        if getattr(args, 'json', False):
            info = profile.summary()
            # Enrich with live systemd state
            for svc, row in info['services'].items():
                row['systemd'] = get_unit_status(row['unit']) if row['unit'] else {}
            print(json.dumps(info, indent=2))
        else:
            print(f"Profile: {profile.profile_name}  ({PROFILE_DESCRIPTIONS[profile.profile_name]})")
            print()
            print(f"  {'SERVICE':<22s} {'ENABLED':>8s}  {'SOURCE':>10s}  {'SYSTEMD UNIT'}")
            print(f"  {'─'*22} {'─'*8}  {'─'*10}  {'─'*38}")
            for svc in ALL_SERVICES:
                enabled = svc in active
                source = 'override' if svc in profile.overrides else (
                    'always' if svc == 'core_recorder' else 'profile')
                unit = profile.summary()['services'][svc]['unit']
                marker = 'on' if enabled else 'off'
                print(f"  {svc:<22s} {marker:>8s}  {source:>10s}  {unit}")
        return

    if args.profile_command == 'set':
        config, config_path = _load_config_for_profile(args)
        new_name = args.name

        # Build profile to show what will change
        old_profile = ServiceProfile.from_config(config)
        old_active = old_profile.active_services()

        # Update config in memory
        if 'services' not in config:
            config['services'] = {}
        config['services']['profile'] = new_name

        new_profile = ServiceProfile.from_config(config)
        new_active = new_profile.active_services()

        added = new_active - old_active
        removed = old_active - new_active

        print(f"Profile: {old_profile.profile_name} -> {new_name}")
        if added:
            print(f"  + enable:  {', '.join(sorted(added))}")
        if removed:
            print(f"  - disable: {', '.join(sorted(removed))}")
        if not added and not removed:
            print(f"  (no change)")

        if args.dry_run:
            print("\n(dry run — no changes applied)")
            return

        # Write updated config
        import toml
        with open(config_path, 'w') as f:
            toml.dump(config, f)
        print(f"\nConfig updated: {config_path}")

        # Apply to systemd
        print("Applying to systemd...")
        actions = apply_profile(new_profile, dry_run=False)
        for unit, action in sorted(actions.items()):
            print(f"  {unit}: {action}")
        return


def _handle_service(args, parser):
    """Handle 'hf-timestd service' subcommands."""
    from .service_profile import (
        ServiceProfile, ALL_SERVICES, SERVICE_UNIT_MAP,
        get_unit_status, apply_profile,
    )

    if not args.service_command:
        if parser:
            parser.print_help()
        sys.exit(1)

    if args.service_command == 'status':
        config, _ = _load_config_for_profile(args)
        profile = ServiceProfile.from_config(config)
        active = profile.active_services()

        rows = []
        for svc in ALL_SERVICES:
            unit = SERVICE_UNIT_MAP.get(svc, '')
            enabled = svc in active
            state = get_unit_status(unit) if unit else {}
            rows.append({
                'service': svc,
                'unit': unit,
                'config_enabled': enabled,
                'active_state': state.get('active_state', ''),
                'sub_state': state.get('sub_state', ''),
            })

        if getattr(args, 'json', False):
            print(json.dumps({'profile': profile.profile_name, 'services': rows}, indent=2))
        else:
            print(f"Profile: {profile.profile_name}")
            print()
            print(f"  {'SERVICE':<22s} {'CONFIG':>7s}  {'STATE':<12s} {'SYSTEMD UNIT'}")
            print(f"  {'─'*22} {'─'*7}  {'─'*12} {'─'*38}")
            for r in rows:
                cfg = 'on' if r['config_enabled'] else 'off'
                st = r['active_state']
                if st == 'active':
                    state_str = f"{st}({r['sub_state']})"
                elif st == 'unknown':
                    state_str = '-'
                else:
                    state_str = st
                print(f"  {r['service']:<22s} {cfg:>7s}  {state_str:<12s} {r['unit']}")
        return

    if args.service_command in ('enable', 'disable'):
        svc_name = args.name.replace('-', '_')
        if svc_name not in ALL_SERVICES:
            print(f"Unknown service: {args.name}", file=sys.stderr)
            print(f"Available: {', '.join(ALL_SERVICES)}", file=sys.stderr)
            sys.exit(1)

        if svc_name == 'core_recorder' and args.service_command == 'disable':
            print("Cannot disable core_recorder — it is always on.", file=sys.stderr)
            sys.exit(1)

        enable = args.service_command == 'enable'
        config, config_path = _load_config_for_profile(args)

        if 'services' not in config:
            config['services'] = {}
        config['services'][svc_name] = enable

        new_profile = ServiceProfile.from_config(config)

        action_word = 'Enabling' if enable else 'Disabling'
        unit = SERVICE_UNIT_MAP.get(svc_name, '')
        print(f"{action_word} {svc_name} ({unit})")

        if args.dry_run:
            print("(dry run — no changes applied)")
            return

        # Write config
        import toml
        with open(config_path, 'w') as f:
            toml.dump(config, f)
        print(f"Config updated: {config_path}")

        # Apply to systemd
        actions = apply_profile(new_profile, dry_run=False)
        for u, a in sorted(actions.items()):
            if u == unit or a.startswith('error'):
                print(f"  {u}: {a}")
        return


def _resolve_log_level(default=logging.INFO):
    """§11 (v0.3): honor HF_TIMESTD_LOG_LEVEL / CLIENT_LOG_LEVEL env vars."""
    import os
    lvl = (os.environ.get('HF_TIMESTD_LOG_LEVEL')
           or os.environ.get('CLIENT_LOG_LEVEL'))
    if not lvl:
        return default
    try:
        return getattr(logging, lvl.upper())
    except AttributeError:
        return default


def _install_sighup_log_handler():
    """§11 (v0.3): SIGHUP re-reads log level env and applies it live."""
    import signal

    def _reload(_signum, _frame):
        new_level = _resolve_log_level()
        root = logging.getLogger()
        root.setLevel(new_level)
        for h in root.handlers:
            h.setLevel(new_level)
        logging.info(f"SIGHUP: log level → {logging.getLevelName(new_level)}")

    signal.signal(signal.SIGHUP, _reload)


def main():
    """Main entry point for hf-timestd command"""
    # Quiet stderr for sigmond client-contract subcommands so they emit
    # exactly one JSON document on stdout and nothing on stderr unless
    # something is wrong.  This must run before any logging.info() calls.
    _contract_quiet = any(arg in ('inventory', 'validate') for arg in sys.argv[1:3])

    # §11: startup log level from env (overrides hard-coded INFO default).
    _env_level = _resolve_log_level(default=logging.INFO)

    # Configure logging to show INFO level and above
    # Force level on root logger in case it was already configured
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.WARNING if _contract_quiet else _env_level)

    # Add handler if none exists
    if not root_logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter('%(levelname)s:%(name)s:%(message)s'))
        root_logger.addHandler(handler)
    else:
        # Set level on existing handlers too
        for handler in root_logger.handlers:
            handler.setLevel(logging.WARNING if _contract_quiet else _env_level)

    if not _contract_quiet:
        logging.info("✓ Logging configured at INFO level")
    
    parser = argparse.ArgumentParser(
        description='hf-timestd',
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    
    # Create subparsers for different commands
    subparsers = parser.add_subparsers(dest='command', help='Command to run')
    
    # Version command
    version_parser = subparsers.add_parser('version',
        help='Show hf-timestd version and component info')
    version_parser.add_argument('--json', action='store_true',
        help='Machine-readable JSON output (for wsprdaemon components.ini)')

    # Inventory command — sigmond client-contract surface
    inventory_parser = subparsers.add_parser('inventory',
        help='Emit machine-readable inventory of hf-timestd instances (for sigmond)')
    inventory_parser.add_argument('--json', action='store_true', default=True,
        help='JSON output (default and only mode)')
    inventory_parser.add_argument('--config', '-c',
        help='Configuration file path (default: $TIMESTD_CONFIG or /etc/hf-timestd/timestd-config.toml)')

    # Validate command — sigmond client-contract surface
    validate_parser = subparsers.add_parser('validate',
        help='Self-validate every hf-timestd instance configuration (for sigmond)')
    validate_parser.add_argument('--json', action='store_true', default=True,
        help='JSON output (default and only mode)')
    validate_parser.add_argument('--config', '-c',
        help='Configuration file path (default: $TIMESTD_CONFIG or /etc/hf-timestd/timestd-config.toml)')
    
    # Quality command — sigmond client-contract surface (runtime data)
    quality_parser = subparsers.add_parser('quality',
        help='Emit per-recorder StreamQuality snapshot (for sigmond)')
    quality_parser.add_argument('--json', action='store_true', default=True,
        help='JSON output (default and only mode)')
    quality_parser.add_argument('--snapshot-path',
        help='Path to the snapshot file (default: /run/hf-timestd/quality.json)')

    # Status command (machine-readable health check)
    status_parser = subparsers.add_parser('status',
        help='Show pipeline health status (machine-readable JSON)',
        description='''
Query the current health of the hf-timestd pipeline.

Reads the latest calibration JSON file (if it exists) and checks data
freshness across all pipeline stages.  Returns a JSON document suitable
for wsprdaemon's wd-ctl status or Nagios-style monitoring.

Exit codes:
  0  OK — pipeline healthy, calibration usable
  1  WARN — pipeline running but calibration not yet usable
  2  CRIT — pipeline stale or not running
''',
        formatter_class=argparse.RawDescriptionHelpFormatter)
    status_parser.add_argument('--calib-file',
        help='Path to calibration JSON file to check')
    status_parser.add_argument('--data-root',
        default='/var/lib/timestd',
        help='Data root directory (checks HDF5 freshness)')
    status_parser.add_argument('--json', action='store_true', default=True,
        help='JSON output (default)')
    
    # Daemon command
    daemon_parser = subparsers.add_parser('daemon', help='Run recorder daemon')
    daemon_parser.add_argument('--config', '-c', help='Configuration file path')
    daemon_parser.add_argument('--debug', '-d', action='store_true', help='Enable DEBUG logging')
    daemon_parser.add_argument('--archive-root', type=Path, default=None,
        help='Archive root for moving evicted data instead of deleting (overrides config)')
    daemon_parser.add_argument('--max-derived-days', type=int, default=None,
        help='Max retention days for derived data (phase2, products). Overrides config; default: 7')
    
    # Discover command
    discover_parser = subparsers.add_parser('discover', help='Discover available channels')
    discover_parser.add_argument('--config', '-c', help='Configuration file path')
    discover_parser.add_argument('--radiod', '-r', help='RadioD address for discovery')
    discover_parser.add_argument('--debug', '-d', action='store_true', help='Enable DEBUG logging')
    
    # Create channels command
    create_parser = subparsers.add_parser('create-channels', help='Create channels in radiod')
    create_parser.add_argument('--config', '-c', help='Configuration file path')
    create_parser.add_argument('--debug', '-d', action='store_true', help='Enable DEBUG logging')
    
    # Data management command
    data_parser = subparsers.add_parser('data', help='Manage recorded data')
    data_subparsers = data_parser.add_subparsers(dest='data_command', help='Data management command')
    
    # Data summary
    summary_parser = data_subparsers.add_parser('summary', help='Show data storage summary')
    summary_parser.add_argument('--config', '-c', default='/etc/signal-recorder/config.toml',
                               help='Configuration file path')
    summary_parser.add_argument('--dev', action='store_true', help='Use development paths')
    
    # Clean data
    clean_data_parser = data_subparsers.add_parser('clean-data', help='Delete RTP recordings')
    clean_data_parser.add_argument('--config', '-c', default='/etc/signal-recorder/config.toml',
                                   help='Configuration file path')
    clean_data_parser.add_argument('--dry-run', action='store_true',
                                   help='Show what would be deleted without deleting')
    clean_data_parser.add_argument('--yes', '-y', action='store_true',
                                   help='Skip confirmation prompts')
    clean_data_parser.add_argument('--dev', action='store_true', help='Use development paths')
    
    # Clean analytics
    clean_analytics_parser = data_subparsers.add_parser('clean-analytics', 
                                                         help='Delete analytics (can be regenerated)')
    clean_analytics_parser.add_argument('--config', '-c', default='/etc/signal-recorder/config.toml',
                                        help='Configuration file path')
    clean_analytics_parser.add_argument('--dry-run', action='store_true',
                                        help='Show what would be deleted without deleting')
    clean_analytics_parser.add_argument('--yes', '-y', action='store_true',
                                        help='Skip confirmation prompts')
    clean_analytics_parser.add_argument('--dev', action='store_true', help='Use development paths')
    
    # Clean uploads
    clean_uploads_parser = data_subparsers.add_parser('clean-uploads', help='Clear upload queue')
    clean_uploads_parser.add_argument('--config', '-c', default='/etc/signal-recorder/config.toml',
                                      help='Configuration file path')
    clean_uploads_parser.add_argument('--dry-run', action='store_true',
                                      help='Show what would be deleted without deleting')
    clean_uploads_parser.add_argument('--yes', '-y', action='store_true',
                                      help='Skip confirmation prompts')
    clean_uploads_parser.add_argument('--dev', action='store_true', help='Use development paths')
    
    # Clean all
    clean_all_parser = data_subparsers.add_parser('clean-all', 
                                                   help='Delete all RTP data, analytics, and uploads')
    clean_all_parser.add_argument('--config', '-c', default='/etc/signal-recorder/config.toml',
                                  help='Configuration file path')
    clean_all_parser.add_argument('--dry-run', action='store_true',
                                  help='Show what would be deleted without deleting')
    clean_all_parser.add_argument('--yes', '-y', action='store_true',
                                  help='Skip confirmation prompts')
    clean_all_parser.add_argument('--dev', action='store_true', help='Use development paths')
    
    # GRAPE command group
    grape_parser = subparsers.add_parser('grape', help='GRAPE data products (decimation, spectrograms, packaging)')
    grape_subparsers = grape_parser.add_subparsers(dest='grape_command', help='GRAPE command')
    
    # GRAPE daily (full orchestrated pipeline)
    grape_daily_parser = grape_subparsers.add_parser('daily', help='Run full daily pipeline: decimate → spectrogram → package → upload')
    grape_daily_parser.add_argument('--data-root', default='/var/lib/timestd', help='Data root directory')
    grape_daily_parser.add_argument('--config', '-c', default='/etc/hf-timestd/timestd-config.toml', help='Config file')
    grape_daily_parser.add_argument('--date', help='Date (YYYY-MM-DD or YYYYMMDD, default: yesterday)')
    grape_daily_parser.add_argument('--no-upload', action='store_true', help='Skip upload stage (decimate, spectrogram, package only)')
    grape_daily_parser.add_argument('--debug', '-d', action='store_true', help='Enable DEBUG logging')

    # GRAPE decimate
    grape_decimate_parser = grape_subparsers.add_parser('decimate', help='Decimate 24/20 kHz IQ to 10 Hz')
    grape_decimate_parser.add_argument('--data-root', default='/var/lib/timestd', help='Data root directory')
    grape_decimate_parser.add_argument('--channel', help='Channel name (e.g., "WWV 10 MHz")')
    grape_decimate_parser.add_argument('--date', help='Date (YYYY-MM-DD or YYYYMMDD)')
    grape_decimate_parser.add_argument('--all-channels', action='store_true', help='Process all channels')
    grape_decimate_parser.add_argument('--debug', '-d', action='store_true', help='Enable DEBUG logging')
    
    # GRAPE spectrogram
    grape_spec_parser = grape_subparsers.add_parser('spectrogram', help='Generate carrier spectrograms')
    grape_spec_parser.add_argument('--data-root', default='/var/lib/timestd', help='Data root directory')
    grape_spec_parser.add_argument('--channel', required=True, help='Channel name')
    grape_spec_parser.add_argument('--date', help='Date (YYYY-MM-DD or YYYYMMDD)')
    grape_spec_parser.add_argument('--rolling', type=int, choices=[6, 12, 24], help='Rolling spectrogram (hours)')
    grape_spec_parser.add_argument('--grid', help='Receiver grid square for solar zenith overlay')
    grape_spec_parser.add_argument('--debug', '-d', action='store_true', help='Enable DEBUG logging')
    
    # GRAPE package
    grape_package_parser = grape_subparsers.add_parser('package', help='Package as Digital RF for upload')
    grape_package_parser.add_argument('--data-root', default='/var/lib/timestd', help='Data root directory')
    grape_package_parser.add_argument('--date', help='Date to package (default: yesterday)')
    grape_package_parser.add_argument('--callsign', required=True, help='Station callsign')
    grape_package_parser.add_argument('--grid', required=True, help='Grid square')
    grape_package_parser.add_argument('--debug', '-d', action='store_true', help='Enable DEBUG logging')
    
    # GRAPE upload
    grape_upload_parser = grape_subparsers.add_parser('upload', help='Upload to PSWS repository')
    grape_upload_parser.add_argument('--data-root', default='/var/lib/timestd', help='Data root directory')
    grape_upload_parser.add_argument('--date', help='Date to upload (default: yesterday)')
    grape_upload_parser.add_argument('--resume', action='store_true',
                                     help="Drain every undelivered date directory under "
                                          "<data-root>/upload/ and reset failed-status tasks "
                                          "back to pending.  Used by grape-upload-retry.timer; "
                                          "ignores --date.  Exits 0 even when there is nothing "
                                          "to do, so the timer no-ops cleanly.")
    grape_upload_parser.add_argument('--dry-run', action='store_true', help='Show what would be uploaded')
    grape_upload_parser.add_argument('--debug', '-d', action='store_true', help='Enable DEBUG logging')
    
    # GRAPE test-upload (preflight connectivity check)
    grape_test_upload_parser = grape_subparsers.add_parser('test-upload', help='Test PSWS SFTP connectivity and SSH key')
    grape_test_upload_parser.add_argument('--config', '-c', default='/etc/hf-timestd/timestd-config.toml', help='Config file')
    grape_test_upload_parser.add_argument('--debug', '-d', action='store_true', help='Enable DEBUG logging')

    # GRAPE status
    grape_status_parser = grape_subparsers.add_parser('status', help='Show upload status and history')
    grape_status_parser.add_argument('--data-root', default='/var/lib/timestd', help='Data root directory')
    grape_status_parser.add_argument('--days', type=int, default=7, help='Days of history to show')
    grape_status_parser.add_argument('--debug', '-d', action='store_true', help='Enable DEBUG logging')
    
    # Calibrate command (wsprdaemon integration)
    calibrate_parser = subparsers.add_parser('calibrate',
        help='Run fusion service with JSON calibration output (wsprdaemon integration)',
        description='''\
Run the multi-broadcast fusion engine and write a JSON calibration file
that wsprdaemon's wd-ka9q-record service reads to align wav start times.

IMPORTANT: This command runs the FUSION layer only (step 5 of the
hf-timestd pipeline).  The upstream services must already be running:
  1. timestd-core-recorder  — IQ capture from radiod
  2. timestd-metrology@*    — per-channel tone detection → L1 HDF5
  3. timestd-l2-calibration — cross-station calibration → L2 HDF5
  4. timestd-physics         — propagation model (optional but recommended)

If you are deploying hf-timestd for the first time, install the full
service suite first (see docs/INTEGRATION.md), then add --calib-file to
the existing timestd-fusion.service unit, OR use this subcommand as a
separate systemd service that reads the same data root.

The calibration file is written atomically (tmp + rename) so readers never
see a partial write.  On SIGTERM the file is removed so consumers do not
read stale data after shutdown.

Example wsprdaemon systemd unit:
  ExecStart=/opt/wsprdaemon/python/bin/python3 -m hf_timestd calibrate \\
      --config /etc/hf-timestd/timestd-config.toml \\
      --calib-file /run/wsprdaemon/KA9Q_0/hftime.json

Health check:
  hf-timestd status --calib-file /run/wsprdaemon/KA9Q_0/hftime.json
''',
        formatter_class=argparse.RawDescriptionHelpFormatter)
    calibrate_parser.add_argument('--config', '-c',
        default='/etc/hf-timestd/timestd-config.toml',
        help='Configuration file path (TOML)')
    calibrate_parser.add_argument('--calib-file', required=True,
        help='Path to JSON calibration output file (e.g. /run/wsprdaemon/KA9Q_0/hftime.json)')
    calibrate_parser.add_argument('--data-root',
        default='/var/lib/timestd',
        help='Data root directory for HDF5 storage')
    calibrate_parser.add_argument('--interval', type=float, default=8.0,
        help='Fusion cycle interval in seconds (default: 8)')
    calibrate_parser.add_argument('--enable-chrony', action='store_true', default=False,
        help='Also write to Chrony SHM (default: disabled in calibrate mode)')
    calibrate_parser.add_argument('--timing-level',
        default='L5', choices=['L1', 'L2', 'L3', 'L4', 'L5', 'L6'],
        help='Timing authority level (default: L5)')
    calibrate_parser.add_argument('--debug', '-d', action='store_true',
        help='Enable DEBUG logging')
    
    # ── Profile command group ──────────────────────────────────────────
    profile_parser = subparsers.add_parser('profile',
        help='Manage service profiles (archive, rtp, fusion, full)',
        description='''\
Service profiles control which systemd services are enabled.

Profiles (least → most services):
  archive — core-recorder only (raw IQ preservation)
  rtp     — archive + web-api + monitoring (GPSDO timing)
  fusion  — rtp + metrology + fusion (GPS-denied timing)
  full    — fusion + physics + ionospheric (full science)

Per-service overrides in [services] take precedence over the profile.
''',
        formatter_class=argparse.RawDescriptionHelpFormatter)
    profile_sub = profile_parser.add_subparsers(dest='profile_command')

    # profile show
    profile_show = profile_sub.add_parser('show',
        help='Show active profile and service states')
    profile_show.add_argument('--config', '-c',
        default='/etc/hf-timestd/timestd-config.toml',
        help='Configuration file path')
    profile_show.add_argument('--json', action='store_true',
        help='Machine-readable JSON output')

    # profile list
    profile_sub.add_parser('list',
        help='List available profiles and their descriptions')

    # profile set
    profile_set = profile_sub.add_parser('set',
        help='Set the active profile (updates config and applies to systemd)')
    profile_set.add_argument('name', choices=['archive', 'rtp', 'fusion', 'full'],
        help='Profile name')
    profile_set.add_argument('--config', '-c',
        default='/etc/hf-timestd/timestd-config.toml',
        help='Configuration file path')
    profile_set.add_argument('--dry-run', action='store_true',
        help='Show what would change without applying')

    # ── Service command group ─────────────────────────────────────────
    service_parser = subparsers.add_parser('service',
        help='View and control individual services')
    service_sub = service_parser.add_subparsers(dest='service_command')

    # service status
    svc_status = service_sub.add_parser('status',
        help='Show status of all hf-timestd services')
    svc_status.add_argument('--config', '-c',
        default='/etc/hf-timestd/timestd-config.toml',
        help='Configuration file path')
    svc_status.add_argument('--json', action='store_true',
        help='Machine-readable JSON output')

    # service enable
    svc_enable = service_sub.add_parser('enable',
        help='Enable a service (adds override to config)')
    svc_enable.add_argument('name', help='Service name (e.g., metrology, physics)')
    svc_enable.add_argument('--config', '-c',
        default='/etc/hf-timestd/timestd-config.toml',
        help='Configuration file path')
    svc_enable.add_argument('--dry-run', action='store_true',
        help='Show what would change without applying')

    # service disable
    svc_disable = service_sub.add_parser('disable',
        help='Disable a service (adds override to config)')
    svc_disable.add_argument('name', help='Service name (e.g., metrology, physics)')
    svc_disable.add_argument('--config', '-c',
        default='/etc/hf-timestd/timestd-config.toml',
        help='Configuration file path')
    svc_disable.add_argument('--dry-run', action='store_true',
        help='Show what would change without applying')

    args = parser.parse_args()

    # If no command specified, show help
    if not args.command:
        parser.print_help()
        sys.exit(1)
    
    # Update logging level if debug flag is set
    if hasattr(args, 'debug') and args.debug:
        root_logger.setLevel(logging.DEBUG)
        for handler in root_logger.handlers:
            handler.setLevel(logging.DEBUG)
        logging.info("DEBUG logging enabled")
    
    # Handle commands
    if args.command == 'version':
        _handle_version(args)
    elif args.command == 'inventory':
        _handle_inventory(args)
    elif args.command == 'validate':
        _handle_validate_contract(args)
    elif args.command == 'quality':
        _handle_quality(args)
    elif args.command == 'status':
        _handle_status(args)
    elif args.command == 'daemon':
        import toml
        # Load configuration
        try:
            with open(args.config, 'r') as f:
                config = toml.load(f)
        except FileNotFoundError:
            print(f"❌ Configuration file not found: {args.config}")
            print(f"   Use --config to specify a different file")
            sys.exit(1)
        except Exception as e:
            print(f"❌ Error loading configuration: {e}")
            sys.exit(1)

        # Build config for CoreRecorder
        # Determine output directory based on mode
        recorder_section = config.get('recorder', {})
        mode = recorder_section.get('mode', 'test')
        
        if mode == 'test':
            output_dir = recorder_section.get('test_data_root', '/tmp/timestd-test')
        else:
            output_dir = recorder_section.get('production_data_root', '/var/lib/signal-recorder')
        
        from .core.core_recorder_v2 import _expand_channel_groups
        # Resolve archive_root: CLI flag overrides TOML config
        archive_root = getattr(args, 'archive_root', None)
        if archive_root is None:
            ar = recorder_section.get('archive_root')
            archive_root = Path(ar) if ar else None

        # Resolve derived_max_days: CLI flag overrides TOML config
        derived_max_days = getattr(args, 'max_derived_days', None)
        if derived_max_days is None:
            derived_max_days = recorder_section.get('derived_max_days', 7)

        recorder_config = {
            'multicast_address': config.get('ka9q', {}).get('data_address', '239.103.26.231'),
            'port': 5004,
            'output_dir': output_dir,
            'station': config.get('station', {}),
            'channels': _expand_channel_groups(recorder_section),
            'status_address': config.get('ka9q', {}).get('status_address', '239.192.152.141'),
            'storage_quota': recorder_section.get('storage_quota', '75%'),
            'archive_root': archive_root,
            'derived_max_days': derived_max_days,
        }
        
        # §11: SIGHUP re-reads log level env without restart.
        _install_sighup_log_handler()

        # Start daemon mode
        recorder = CoreRecorderV2(recorder_config)
        recorder.run()
    elif args.command == 'discover':
        import toml
        from .channel_manager import ChannelManager
        
        # Load configuration
        try:
            with open(args.config, 'r') as f:
                config = toml.load(f)
        except FileNotFoundError:
            print(f"❌ Configuration file not found: {args.config}")
            sys.exit(1)
        except Exception as e:
            print(f"❌ Error loading configuration: {e}")
            sys.exit(1)
        
        # Discovery mode
        status_address = args.radiod or config.get('ka9q', {}).get('status_address', '239.192.152.141')
        manager = ChannelManager(status_address)
        channels = manager.discover_channels()
        
        print(f"\n📡 Discovered {len(channels)} channels from radiod at {status_address}:")
        for ch in channels:
            print(f"  • SSRC {ch['ssrc']:08x}: {ch.get('frequency_hz', 0)/1e6:.3f} MHz - {ch.get('description', 'Unknown')}")
    elif args.command == 'create-channels':
        import toml
        from .channel_manager import ChannelManager
        
        # Load configuration
        try:
            with open(args.config, 'r') as f:
                config = toml.load(f)
        except FileNotFoundError:
            print(f"❌ Configuration file not found: {args.config}")
            sys.exit(1)
        except Exception as e:
            print(f"❌ Error loading configuration: {e}")
            sys.exit(1)
        
        # Create channels mode
        status_address = config.get('ka9q', {}).get('status_address', '239.192.152.141')
        manager = ChannelManager(status_address)
        
        # Build channel specifications
        required_channels = []
        for ch_cfg in config.get('recorder', {}).get('channels', []):
            if ch_cfg.get('enabled', True):
                required_channels.append({
                    'ssrc': ch_cfg['ssrc'],
                    'frequency_hz': ch_cfg['frequency_hz'],
                    'preset': ch_cfg.get('preset', 'iq'),
                    'sample_rate': ch_cfg.get('sample_rate', 16000),
                    'agc': ch_cfg.get('agc', 0),
                    'gain': ch_cfg.get('gain', 0),
                    'description': ch_cfg['description']
                })
        
        if not required_channels:
            print("❌ No enabled channels found in configuration")
            sys.exit(1)
        
        print(f"\n🔧 Creating {len(required_channels)} channels in radiod at {status_address}...")
        success = manager.ensure_channels_exist(required_channels, update_existing=False)
        
        if success:
            print("✅ All channels created successfully")
        else:
            print("⚠️ Some channels may have failed to create")
            sys.exit(1)
    elif args.command == 'data':
        # Data management mode
        from .data_management import DataManager
        from .config_utils import load_config_with_paths
        import toml
        
        # Load configuration
        try:
            with open(args.config, 'r') as f:
                config = toml.load(f)
        except FileNotFoundError:
            print(f"❌ Configuration file not found: {args.config}")
            print(f"   Use --config to specify a different file")
            sys.exit(1)
        except Exception as e:
            print(f"❌ Error loading configuration: {e}")
            sys.exit(1)
        
        # Create path resolver
        from .config_utils import PathResolver
        path_resolver = PathResolver(config, development_mode=args.dev)
        
        # Create data manager
        manager = DataManager(path_resolver)
        
        # Execute data command
        if args.data_command == 'summary':
            manager.print_data_summary()
        elif args.data_command == 'clean-data':
            manager.clean_data(dry_run=args.dry_run, confirm=args.yes)
        elif args.data_command == 'clean-analytics':
            manager.clean_analytics(dry_run=args.dry_run, confirm=args.yes)
        elif args.data_command == 'clean-uploads':
            manager.clean_uploads(dry_run=args.dry_run, confirm=args.yes)
        elif args.data_command == 'clean-all':
            manager.clean_all(dry_run=args.dry_run, confirm=args.yes)
        else:
            data_parser.print_help()
            sys.exit(1)
    elif args.command == 'grape':
        # GRAPE data products mode
        from datetime import datetime, timedelta, timezone
        
        if not args.grape_command:
            grape_parser.print_help()
            sys.exit(1)
        
        data_root = Path(args.data_root) if hasattr(args, 'data_root') else None
        
        def resolve_date(date_arg):
            """Resolve date argument to YYYYMMDD string."""
            if not date_arg or date_arg.lower() == 'yesterday':
                return (datetime.now(tz=timezone.utc) - timedelta(days=1)).strftime('%Y%m%d')
            return date_arg.replace('-', '')
        
        if args.grape_command == 'daily':
            from .grape.decimation_pipeline import DecimationPipeline
            from .grape.spectrogram import CarrierSpectrogramGenerator
            from .grape.packager import DailyDRFPackager, StationConfig, STANDARD_CHANNELS
            from .grape.uploader import UploadManager
            import toml
            import os
            import json as _json
            import shutil

            date_str = resolve_date(args.date)

            # Load config
            config_path = Path(args.config)
            if not config_path.exists():
                print(f"❌ Config not found: {config_path}")
                sys.exit(1)
            with open(config_path, 'r') as f:
                config = toml.load(f)

            station = config.get('station', {})
            callsign = station.get('callsign', 'AC0G')
            grid = station.get('grid_square', 'EM38ww')

            # Use canonical 9 GRAPE channels — not dir scanning which picks up
            # legacy aliases (BPM_10000, WWV_10000, WWVH_10000, etc.)
            all_channels = [name for name, _freq in STANDARD_CHANNELS]
            expected_count = len(all_channels)
            print(f"📡 GRAPE daily pipeline for {date_str}")
            print(f"   Channels: {expected_count} ({', '.join(all_channels)})")

            # Status file for health dashboard
            status_file = data_root / 'upload' / 'grape_status.json'
            pipeline_status = {
                'date': date_str,
                'started_at': datetime.now(tz=timezone.utc).isoformat(),
                'completed_at': None,
                'status': 'running',
                'channels_expected': expected_count,
                'channels_decimated': 0,
                'channels_spectrogram': 0,
                'upload_status': 'pending',
                'upload_completed': 0,
                'upload_failed': 0,
                'error': None,
            }

            def _save_status():
                try:
                    status_file.parent.mkdir(parents=True, exist_ok=True)
                    with open(status_file, 'w') as sf:
                        _json.dump(pipeline_status, sf, indent=2)
                except Exception:
                    pass

            _save_status()

            # === Stage 1: Decimate all channels ===
            print(f"\n━━━ Stage 1: Decimation ({expected_count} channels) ━━━")
            pipeline = DecimationPipeline(data_root)
            decimated = []
            failed_decimate = []

            for ch in all_channels:
                try:
                    print(f"   [{len(decimated)+len(failed_decimate)+1}/{expected_count}] {ch}...")
                    pipeline.process_day(date_str, ch)
                    # Verify output exists
                    ch_dir = ch.replace(' ', '_')
                    dec_file = data_root / 'products' / ch_dir / 'decimated' / f'{date_str}.bin'
                    if dec_file.exists() and dec_file.stat().st_size > 0:
                        decimated.append(ch)
                    else:
                        failed_decimate.append(ch)
                        print(f"   ⚠️  {ch}: decimation produced no output")
                except Exception as e:
                    failed_decimate.append(ch)
                    print(f"   ❌ {ch}: {e}")

            print(f"\n   Decimation: {len(decimated)}/{expected_count} channels")
            pipeline_status['channels_decimated'] = len(decimated)

            # === Gate 1: At least one channel must be decimated ===
            if len(decimated) == 0:
                print(f"   ❌ GATE FAILED: 0/{expected_count} channels decimated")
                print(f"   Aborting — no data to package/upload")
                pipeline_status['status'] = 'failed'
                pipeline_status['error'] = f'0/{expected_count} channels decimated'
                pipeline_status['completed_at'] = datetime.now(tz=timezone.utc).isoformat()
                _save_status()
                sys.exit(1)
            if failed_decimate:
                print(f"   ⚠️  {len(failed_decimate)} channels had no data: {', '.join(failed_decimate)}")
            print(f"   ✅ GATE PASSED: {len(decimated)}/{expected_count} channels decimated")

            # === Stage 2: Generate spectrograms ===
            print(f"\n━━━ Stage 2: Spectrograms ({len(decimated)} channels) ━━━")
            spectrograms = []
            failed_spec = []

            for ch in decimated:
                try:
                    gen = CarrierSpectrogramGenerator(
                        data_root=data_root,
                        channel_name=ch,
                        receiver_grid=grid
                    )
                    result = gen.generate_daily(date_str)
                    if result and result.exists():
                        spectrograms.append(ch)
                        print(f"   ✅ {ch}: {result.name}")
                    else:
                        failed_spec.append(ch)
                        print(f"   ⚠️  {ch}: no spectrogram generated")
                except Exception as e:
                    failed_spec.append(ch)
                    print(f"   ❌ {ch}: {e}")

            print(f"\n   Spectrograms: {len(spectrograms)}/{len(decimated)} channels")
            pipeline_status['channels_spectrogram'] = len(spectrograms)
            _save_status()

            # === Gate 2: At least one spectrogram must exist ===
            if len(spectrograms) == 0:
                print(f"   ❌ GATE FAILED: 0/{len(decimated)} spectrograms generated")
                print(f"   Aborting — no spectrograms to package/upload")
                pipeline_status['status'] = 'failed'
                pipeline_status['error'] = f'0/{len(decimated)} spectrograms generated'
                pipeline_status['completed_at'] = datetime.now(tz=timezone.utc).isoformat()
                _save_status()
                sys.exit(1)
            if failed_spec:
                print(f"   ⚠️  {len(failed_spec)} spectrograms missing: {', '.join(failed_spec)}")
            print(f"   ✅ GATE PASSED: {len(spectrograms)}/{len(decimated)} spectrograms generated")

            # === Stage 3: Package into Digital RF ===
            print(f"\n━━━ Stage 3: Package ━━━")
            try:
                station_config = StationConfig(callsign=callsign, grid_square=grid)
                packager = DailyDRFPackager(data_root=data_root, station_config=station_config)
                packager.package_day(date_str)
                print(f"   ✅ Package complete")
            except Exception as e:
                print(f"   ❌ Package failed: {e}")
                print(f"   Aborting — will not upload without valid package")
                pipeline_status['status'] = 'failed'
                pipeline_status['error'] = f'Package failed: {e}'
                pipeline_status['completed_at'] = datetime.now(tz=timezone.utc).isoformat()
                _save_status()
                sys.exit(1)

            # === Gate 3: Verify OBS directory exists ===
            upload_dir = data_root / 'upload' / date_str
            obs_dirs = list(upload_dir.rglob('OBS*')) if upload_dir.exists() else []
            if not obs_dirs:
                print(f"   ❌ GATE FAILED: no OBS directory in {upload_dir}")
                pipeline_status['status'] = 'failed'
                pipeline_status['error'] = 'No OBS directory after packaging'
                pipeline_status['completed_at'] = datetime.now(tz=timezone.utc).isoformat()
                _save_status()
                sys.exit(1)
            print(f"   ✅ GATE PASSED: {len(obs_dirs)} dataset(s) ready")

            # === Stage 4: Upload to PSWS ===
            upload_attempted = False
            upload_ok = False

            if args.no_upload:
                print(f"\n━━━ Stage 4: Upload (skipped via --no-upload) ━━━")
                print(f"   Packaged data ready at: {upload_dir}")
                print(f"   Upload later with: hf-timestd grape upload --date {date_str}")
                pipeline_status['upload_status'] = 'skipped'
            else:
                print(f"\n━━━ Stage 4: Upload ━━━")
                upload_attempted = True
                uploader_config = config.get('uploader', {})
                sftp_config = uploader_config.get('sftp', {})
                ssh_key = os.path.expanduser(sftp_config.get('ssh_key', '~/.ssh/psws_key'))

                upload_config = {
                    'protocol': uploader_config.get('protocol', 'sftp'),
                    'host': sftp_config.get('host', 'pswsnetwork.eng.ua.edu'),
                    'user': sftp_config.get('user', station.get('id', '')),
                    'ssh': {'key_file': ssh_key},
                    'bandwidth_limit_kbps': sftp_config.get('bandwidth_limit_kbps', 100),
                    'max_retries': uploader_config.get('max_retries', 5),
                    'queue_file': data_root / 'upload' / 'queue.json'
                }

                manager = UploadManager(upload_config)

                for obs_dir in obs_dirs:
                    metadata = {
                        'date': f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}",
                        'callsign': callsign,
                        'grid_square': grid,
                        'station_id': station.get('id', 'S000171'),
                        'instrument_id': station.get('instrument_id', '172')
                    }
                    manager.enqueue(obs_dir, metadata)

                try:
                    manager.process_queue()
                except Exception as e:
                    print(f"   ⚠️  Upload error: {e}")

                status = manager.get_status()
                print(f"   Queue: {status['completed']} completed, {status['pending']} pending, {status['failed']} failed")

                report_file = manager.write_upload_report()
                print(f"   Report: {report_file}")

                pipeline_status['upload_completed'] = status['completed']
                pipeline_status['upload_failed'] = status['failed']

                if status['failed'] > 0:
                    print(f"   ⚠️  Upload had failures — data is queued for retry")
                    print(f"   Retry with: hf-timestd grape upload --date {date_str}")
                    pipeline_status['upload_status'] = 'failed'
                else:
                    upload_ok = True
                    pipeline_status['upload_status'] = 'completed'

            # === Stage 5: Post-upload cleanup ===
            if upload_ok:
                print(f"\n━━━ Stage 5: Cleanup ━━━")
                # Delete decimated .bin files (regenerable from raw if needed)
                cleaned_dec = 0
                for ch in decimated:
                    ch_dir = ch.replace(' ', '_')
                    dec_file = data_root / 'products' / ch_dir / 'decimated' / f'{date_str}.bin'
                    meta_file = data_root / 'products' / ch_dir / 'decimated' / f'{date_str}_meta.json'
                    for f in [dec_file, meta_file]:
                        try:
                            if f.exists():
                                f.unlink()
                                cleaned_dec += 1
                        except Exception as e:
                            print(f"   ⚠️  Could not delete {f.name}: {e}")
                if cleaned_dec > 0:
                    print(f"   Removed {cleaned_dec} decimated files")

                # Delete DRF upload package (already uploaded)
                if upload_dir.exists():
                    try:
                        shutil.rmtree(upload_dir)
                        print(f"   Removed upload package: {upload_dir.name}")
                    except Exception as e:
                        print(f"   ⚠️  Could not delete {upload_dir}: {e}")

                # Spectrograms are KEPT — they're the permanent visual record
                print(f"   Spectrograms retained")

            # Finalize status
            pipeline_status['status'] = 'completed' if upload_ok or args.no_upload else 'upload_pending'
            pipeline_status['completed_at'] = datetime.now(tz=timezone.utc).isoformat()
            _save_status()

            print(f"\n✅ GRAPE daily pipeline complete for {date_str}")
            print(f"   {len(decimated)}/{expected_count} channels decimated")
            print(f"   {len(spectrograms)} spectrograms generated")
            if upload_attempted:
                if upload_ok:
                    print(f"   {status['completed']} dataset(s) uploaded to PSWS")
                else:
                    print(f"   ⚠️  Upload pending — queued for retry")

        elif args.grape_command == 'decimate':
            from .grape.decimation_pipeline import DecimationPipeline
            
            date_str = resolve_date(args.date)
            
            pipeline = DecimationPipeline(data_root)
            
            if args.all_channels:
                # Get all channels from raw_buffer (tiered storage) and raw_archive (legacy)
                channel_set = set()
                for subdir in ['raw_buffer', 'raw_archive']:
                    channels_dir = data_root / subdir
                    if channels_dir.exists():
                        for d in channels_dir.iterdir():
                            if d.is_dir():
                                channel_set.add(d.name.replace('_', ' '))
                if not channel_set:
                    print(f"❌ No raw data found in {data_root}/raw_buffer/ or {data_root}/raw_archive/")
                    sys.exit(1)
                for channel_name in sorted(channel_set):
                    print(f"Processing {channel_name}...")
                    pipeline.process_day(date_str, channel_name)
            elif args.channel:
                pipeline.process_day(date_str, args.channel)  # FIXED: date first, then channel
            else:
                print("❌ Specify --channel or --all-channels")
                sys.exit(1)

                
        elif args.grape_command == 'spectrogram':
            from .grape.spectrogram import CarrierSpectrogramGenerator
            import toml
            
            # Get grid from args or config file
            receiver_grid = args.grid
            if not receiver_grid:
                config_path = Path('/etc/hf-timestd/timestd-config.toml')
                if config_path.exists():
                    with open(config_path, 'r') as f:
                        config = toml.load(f)
                    receiver_grid = config.get('station', {}).get('grid_square', '')
                    if receiver_grid:
                        print(f"Using grid from config: {receiver_grid}")
            
            gen = CarrierSpectrogramGenerator(
                data_root=data_root,
                channel_name=args.channel,
                receiver_grid=receiver_grid or ''
            )
            
            if args.date:
                date_str = args.date.replace('-', '')
                gen.generate_daily(date_str)
            elif args.rolling:
                gen.generate_rolling(hours=args.rolling)
            else:
                # Default to yesterday
                date_str = (datetime.now(tz=timezone.utc) - timedelta(days=1)).strftime('%Y%m%d')
                gen.generate_daily(date_str)
                
        elif args.grape_command == 'package':
            from .grape.packager import DailyDRFPackager, StationConfig
            
            date_str = resolve_date(args.date)
            station_config = StationConfig(
                callsign=args.callsign,
                grid_square=args.grid
            )
            packager = DailyDRFPackager(
                data_root=data_root,
                station_config=station_config
            )
            packager.package_day(date_str)
            
        elif args.grape_command == 'upload':
            from .grape.uploader import UploadManager, SFTPUpload
            import toml

            # Load config for station info (shared by --date and --resume).
            config_path = Path('/etc/hf-timestd/timestd-config.toml')
            if config_path.exists():
                with open(config_path, 'r') as f:
                    config = toml.load(f)
            else:
                print(f"❌ Config not found: {config_path}")
                sys.exit(1)

            station = config.get('station', {})

            # --resume: scan every undelivered <data-root>/upload/<YYYYMMDD>/
            # directory and feed everything found into the queue.  Used by
            # grape-upload-retry.timer to drain stuck failures.
            if args.resume:
                upload_root = data_root / 'upload'
                if not upload_root.exists():
                    print(f"📤 Resume: nothing to do (no {upload_root})")
                    sys.exit(0)

                date_dirs = sorted(
                    p for p in upload_root.iterdir()
                    if p.is_dir() and len(p.name) == 8 and p.name.isdigit()
                )
                if not date_dirs:
                    print(f"📤 Resume: queue drained — no date directories under {upload_root}")
                    sys.exit(0)

                obs_dirs = []
                date_strs = {}
                for d in date_dirs:
                    obs_in_d = list(d.rglob('OBS*'))
                    for o in obs_in_d:
                        obs_dirs.append(o)
                        date_strs[o] = d.name
                if not obs_dirs:
                    print(f"📤 Resume: no OBS directories under {upload_root}")
                    sys.exit(0)
                print(f"📤 Resume: found {len(obs_dirs)} dataset(s) "
                      f"across {len(date_dirs)} day(s)")
                date_str = None  # signals downstream that we're in resume mode
            else:
                date_str = resolve_date(args.date)
                upload_dir = data_root / 'upload' / date_str
                if not upload_dir.exists():
                    print(f"❌ No packaged data for {date_str} at {upload_dir}")
                    print(f"   Run 'grape package --date {date_str}' first")
                    sys.exit(1)

                obs_dirs = list(upload_dir.rglob('OBS*'))
                if not obs_dirs:
                    print(f"❌ No OBS directories found in {upload_dir}")
                    sys.exit(1)
                date_strs = {o: date_str for o in obs_dirs}

                print(f"📤 Upload for {date_str}")
                print(f"   Found {len(obs_dirs)} dataset(s)")
            
            if args.dry_run:
                print("   (Dry run - no actual upload)")
                for obs_dir in obs_dirs:
                    print(f"   Would upload: {obs_dir}")
                sys.exit(0)
            
            # Create uploader from config
            uploader_config = config.get('uploader', {})
            sftp_config = uploader_config.get('sftp', {})
            
            # Expand ~ in ssh_key path
            import os
            ssh_key = os.path.expanduser(sftp_config.get('ssh_key', '~/.ssh/psws_key'))
            
            upload_config = {
                'protocol': uploader_config.get('protocol', 'sftp'),
                'host': sftp_config.get('host', 'pswsnetwork.eng.ua.edu'),
                'user': sftp_config.get('user', station.get('id', '')),
                'ssh': {'key_file': ssh_key},
                'bandwidth_limit_kbps': sftp_config.get('bandwidth_limit_kbps', 100),
                'max_retries': uploader_config.get('max_retries', 5),
                'queue_file': data_root / 'upload' / 'queue.json'
            }
            
            manager = UploadManager(upload_config)

            # In --resume mode, reset any task in the persistent queue
            # whose status is "failed" but whose dataset_path still
            # exists on disk back to "pending" with attempts=0.  Without
            # this, the retry timer is a no-op for everything stuck at
            # max_retries — defeating its purpose.  Cleanup-deleted
            # datasets stay "failed" because their disk path is gone.
            if args.resume:
                resurrected = 0
                for task in manager.queue:
                    if task.status != "failed":
                        continue
                    if Path(task.dataset_path).exists():
                        task.status = "pending"
                        task.attempts = 0
                        task.error_message = ""
                        task.last_attempt = None
                        resurrected += 1
                if resurrected:
                    print(f"   Reset {resurrected} failed task(s) → pending for retry")
                    manager._save_queue()

            # Enqueue and process.  enqueue() dedupes on dataset_path,
            # so calling it twice for a task already in the queue
            # leaves the existing attempt counter intact.
            for obs_dir in obs_dirs:
                ds = date_strs[obs_dir]
                metadata = {
                    'date': f"{ds[:4]}-{ds[4:6]}-{ds[6:8]}",
                    'callsign': station.get('callsign', 'AC0G'),
                    'grid_square': station.get('grid_square', 'EM38ww'),
                    'station_id': station.get('id', 'S000171'),
                    'instrument_id': station.get('instrument_id', '172')
                }
                manager.enqueue(obs_dir, metadata)

            manager.process_queue()
            
            status = manager.get_status()
            print(f"   Queue status: {status['completed']} completed, {status['pending']} pending, {status['failed']} failed")
            
            # Write upload report
            report_file = manager.write_upload_report()
            print(f"   Report: {report_file}")
            
        elif args.grape_command == 'test-upload':
            from .grape.uploader import test_psws_connectivity
            import toml

            config_path = Path(args.config)
            if not config_path.exists():
                print(f"Config not found: {config_path}")
                sys.exit(1)
            with open(config_path, 'r') as f:
                config = toml.load(f)

            ok = test_psws_connectivity(config)
            sys.exit(0 if ok else 1)

        elif args.grape_command == 'status':
            from .grape.uploader import UploadManager
            
            # Create minimal config just to read queue
            upload_config = {
                'protocol': 'sftp',
                'host': 'pswsnetwork.eng.ua.edu',
                'user': 'status_check',
                'ssh': {'key_file': '/dev/null'},
                'queue_file': data_root / 'upload' / 'queue.json'
            }
            
            manager = UploadManager(upload_config)
            
            # Current queue status
            status = manager.get_status()
            print(f"\n📊 GRAPE Upload Status")
            print(f"   Queue: {status['total']} total")
            print(f"   ├─ Completed: {status['completed']}")
            print(f"   ├─ Pending:   {status['pending']}")
            print(f"   ├─ Uploading: {status['uploading']}")
            print(f"   └─ Failed:    {status['failed']}")
            
            # History
            history = manager.get_upload_history(days=args.days)
            if history:
                print(f"\n📅 Upload History (last {args.days} days):")
                for day in history:
                    summary = day.get('summary', {})
                    print(f"   {day['date']}: "
                          f"{summary.get('completed', 0)} completed, "
                          f"{summary.get('failed', 0)} failed")
            else:
                print(f"\n   No upload history found")
        else:
            grape_parser.print_help()
            sys.exit(1)
    elif args.command == 'profile':
        _handle_profile(args, locals().get('profile_parser'))
    elif args.command == 'service':
        _handle_service(args, locals().get('service_parser'))
    elif args.command == 'calibrate':
        import toml

        # Load configuration (for receiver coordinates, timing authority, etc.)
        config_path = Path(args.config)
        receiver_lat = None
        receiver_lon = None
        timing_level = args.timing_level

        if config_path.exists():
            try:
                with open(config_path, 'r') as f:
                    config = toml.load(f)
                receiver_lat = config.get('station', {}).get('latitude')
                receiver_lon = config.get('station', {}).get('longitude')
                cfg_level = config.get('fusion', {}).get('timing_authority_level')
                if cfg_level and timing_level == 'L5':
                    timing_level = cfg_level
                logging.info(f"Calibrate: loaded config from {config_path}")
            except Exception as e:
                logging.warning(f"Calibrate: could not read config: {e}")
        else:
            logging.info(f"Calibrate: no config at {config_path}, using defaults")

        from .core.multi_broadcast_fusion import run_fusion_service
        run_fusion_service(
            data_root=Path(args.data_root),
            interval_sec=args.interval,
            enable_chrony=args.enable_chrony,
            lookback_minutes=30,
            receiver_lat=receiver_lat,
            receiver_lon=receiver_lon,
            timing_authority_level=timing_level,
            calib_file=args.calib_file
        )

if __name__ == '__main__':
    main()

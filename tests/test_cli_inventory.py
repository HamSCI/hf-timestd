"""Tests for `hf-timestd inventory --json` CLI handler.

Pins the v0.7 shape of the inventory payload that sigmond's
ContractAdapter consumes. The handler is invoked directly via
_handle_inventory(args) with a SimpleNamespace mocking argparse,
stdout is captured, and the resulting JSON is asserted against
the CLIENT-CONTRACT.md v0.7 expectations.

These are the *producer-side* contract tests; the *consumer-side*
tests live in sigmond/tests/test_contract_adapter.py.
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))

from hf_timestd.cli import _handle_inventory


MINIMAL_TOML = """\
[station]
callsign = "AI6VN"

[ka9q]
status = "bee3-status.local"

[recorder]
mode = "production"
production_data_root = "/var/lib/timestd"

[recorder.channel_group.wwv]
[[recorder.channel_group.wwv.channels]]
frequency_hz = 2500000

[[recorder.channel_group.wwv.channels]]
frequency_hz = 5000000

[services]
profile = "full"
"""


def _run_inventory(config_path: Path) -> dict:
    """Invoke _handle_inventory with the given config path; return parsed stdout."""
    args = SimpleNamespace(config=str(config_path))
    buf = io.StringIO()
    with redirect_stdout(buf):
        _handle_inventory(args)
    return json.loads(buf.getvalue())


class V07ContractVersionTests(unittest.TestCase):
    """Top-level contract_version field must match the version of
    CLIENT-CONTRACT.md this hf-timestd build was written against.
    Sigmond's ContractAdapter compares its own SUPPORTED_CONTRACT_VERSION
    against this and emits a warn-level mismatch if they differ —
    keeping them in lockstep avoids spurious warnings on every
    `smd diag` run."""

    def test_contract_version_is_08(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "timestd-config.toml"
            cfg.write_text(MINIMAL_TOML)
            payload = _run_inventory(cfg)
        self.assertEqual(payload['contract_version'], '0.8')


class V07TimingAuthorityFieldTests(unittest.TestCase):
    """v0.7 §3/§18 — `timing_authority_applied` per instance.

    hf-timestd is a producer (`provides_timing_calibration: true`),
    not a subscriber, so it never *applies* a peer's authority — the
    field is therefore always null in hf-timestd's own inventory.
    A consumer reading hf-timestd's inventory shouldn't expect to
    discover a sibling authority through it; the only thing the
    publish-side declares is `provides_timing_calibration`."""

    def _emit(self) -> dict:
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "timestd-config.toml"
            cfg.write_text(MINIMAL_TOML)
            return _run_inventory(cfg)

    def test_timing_authority_applied_is_explicitly_null(self):
        """The field is *present* and explicitly null — not missing.
        Sigmond's adapter treats missing and null identically (both →
        §18 default mode), but being explicit removes ambiguity on
        whether the producer is contract-aware."""
        payload = self._emit()
        self.assertEqual(len(payload['instances']), 1)
        inst = payload['instances'][0]
        self.assertIn('timing_authority_applied', inst)
        self.assertIsNone(inst['timing_authority_applied'])

    def test_provides_timing_calibration_true_when_fusion_service_runs(self):
        """The producer-side declaration the contract has had since
        v0.2; v0.7 just gives it semantics ([[project_client_contract_v07]]).
        The authority manager lives in the fusion service and writes
        /run/hf-timestd/authority.json, so a profile that activates
        `fusion` declares the instance a producer.  (Until 2026-09-04
        this keyed on the presence of `[timing] authority`, a key the
        measurement model retired — RESIDUE_AUDIT_2026-09-04 §3.5.)"""
        payload = self._emit()
        inst = payload['instances'][0]
        self.assertTrue(inst['provides_timing_calibration'])
        self.assertFalse(inst['uses_timing_calibration'])

    def test_provides_timing_calibration_false_without_fusion_service(self):
        """A profile without the fusion service runs no authority
        manager, so provides_timing_calibration must report false
        honestly.  The default profile is `rtp` (recorder, web API and
        monitoring; no metrology, no fusion) — a station may run
        hf-timestd purely for its archive without serving any timing
        authority."""
        toml_no_fusion = MINIMAL_TOML.replace(
            '[services]\nprofile = "full"\n', ''
        )
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "timestd-config.toml"
            cfg.write_text(toml_no_fusion)
            payload = _run_inventory(cfg)
        inst = payload['instances'][0]
        self.assertFalse(inst['provides_timing_calibration'])
        # timing_authority_applied stays null regardless — hf-timestd
        # is never a subscriber.
        self.assertIsNone(inst['timing_authority_applied'])

    def test_retired_authority_key_does_not_make_a_producer(self):
        """Deployed configs still carry `[timing] authority = "rtp"`
        for a while after its retirement.  The stale key must not
        resurrect the old derivation."""
        toml_stale = MINIMAL_TOML.replace(
            '[services]\nprofile = "full"\n',
            '[timing]\nauthority = "rtp"\n'
        )
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "timestd-config.toml"
            cfg.write_text(toml_stale)
            payload = _run_inventory(cfg)
        self.assertFalse(payload['instances'][0]['provides_timing_calibration'])


class StdoutCleanlinessTests(unittest.TestCase):
    """CONTRACT-v0.2 §3 stdout-cleanliness — the output must parse as
    a single JSON document, no banners or log lines on stdout."""

    def test_stdout_is_pure_json(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "timestd-config.toml"
            cfg.write_text(MINIMAL_TOML)
            args = SimpleNamespace(config=str(cfg))
            buf = io.StringIO()
            with redirect_stdout(buf):
                _handle_inventory(args)
            out = buf.getvalue()
        # The whole stdout must be one parseable JSON document —
        # json.loads on the entire string succeeds without trailing
        # data.
        payload = json.loads(out)
        self.assertIsInstance(payload, dict)
        self.assertEqual(payload['client'], 'hf-timestd')


if __name__ == '__main__':
    unittest.main()

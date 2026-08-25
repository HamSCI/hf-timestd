"""`hf-timestd validate` must enforce the two-axis timing model.

docs/design/TIMING_AUTHORITY_TWO_AXIS.md: authority is a matrix (A-axis =
is the ADC clock GPSDO-disciplined; T-axis = what names and places the
second), not the L1-L6 rank the old architecture doc used.

The specific hazard these checks cover is that the A-axis can be
ASSERTED rather than observed.  authority_runner.py:

    a_level_cfg = auth_cfg.get("a_level", "A1")
    if gpsdo_cfg.get("enabled"):
        a_level_provider = probe.poll          # observed, freshness-gated
    else:
        a_level_provider = lambda: a_level_cfg # asserted, DEFAULTS TO A1

so a station with no GPSDO at all claims A1 by default, and every
uncertainty quoted downstream assumes that claim was measured.
"""
from __future__ import annotations

from hf_timestd.cli import resolve_a_level, timing_axis_issues


def _cfg(*, gpsdo_enabled=None, a_level=None, t6_enabled=False):
    auth = {}
    if gpsdo_enabled is not None:
        auth['gpsdo'] = {'enabled': gpsdo_enabled}
    if a_level is not None:
        auth['a_level'] = a_level
    return {'timing': {'authority_manager': auth,
                       't6_pps': {'enabled': t6_enabled}}}


def _messages(cfg):
    return " ".join(i['message'] for i in timing_axis_issues(cfg))


class TestAAxisObservability:
    def test_probe_enabled_is_clean(self):
        """The probe freshness-gates and degrades to A0 on its own."""
        assert timing_axis_issues(_cfg(gpsdo_enabled=True)) == []

    def test_bare_config_claims_a1_without_observing_it(self):
        """The default path: no gpsdo block at all => asserted A1."""
        msg = _messages(_cfg())
        assert 'ASSERTED as A1' in msg
        assert 'defaults to "A1"' in msg

    def test_explicit_a1_without_the_probe_still_warns(self):
        assert 'ASSERTED as A1' in _messages(
            _cfg(gpsdo_enabled=False, a_level='A1'))

    def test_probe_enabled_overrides_a_stale_config_assertion(self):
        """a_level in config is dead when the probe is on -- no warning."""
        assert timing_axis_issues(
            _cfg(gpsdo_enabled=True, a_level='A1')) == []


class TestA0Holdover:
    def test_a0_warns_that_the_coast_sigma_is_a1_calibrated(self):
        """UNMEASURED_RATE_SIGMA_PPM = 0.01 is 25x B4's GPSDO residual.
        A free-running TCXO is 0.5-2 ppm -- 50-200x larger."""
        msg = _messages(_cfg(a_level='A0'))
        assert 'UNMEASURED_RATE_SIGMA_PPM' in msg
        assert '50-200x' in msg

    def test_a0_does_not_also_claim_a1(self):
        assert 'ASSERTED as A1' not in _messages(_cfg(a_level='A0'))


class TestT6NeedsARuler:
    def test_t6_without_a_disciplined_ruler_warns(self):
        """T6 places the second to ns; the RTP counter carries that
        forward at whatever rate the ADC runs."""
        msg = _messages(_cfg(a_level='A0', t6_enabled=True))
        assert 'without a disciplined ruler' in msg

    def test_t6_with_the_probe_on_is_clean(self):
        assert timing_axis_issues(
            _cfg(gpsdo_enabled=True, t6_enabled=True)) == []

    def test_axes_are_independent_a0_plus_t6_is_expressible(self):
        """The case the L1-L6 ladder could not express at all: a good
        origin on a bad ruler.  It must produce advice, not a crash."""
        issues = timing_axis_issues(_cfg(a_level='A0', t6_enabled=True))
        assert len(issues) == 2
        assert all(i['severity'] == 'warn' for i in issues)


class TestRemoteReceiverAttestation:
    """A REMOTE RX-888 may well be GPSDO-disciplined, but its
    /run/gpsdo lives on another machine — this host cannot probe it.

    mjh, 2026-08-25: the operator must be able to say so.  Attestation is
    legitimate evidence, is as good as the operator, and must be carried
    into the sidecar as `attested` — never laundered into `observed`.
    """

    def _remote(self, level='A1', by='AC0G: remote RX-888 on LBE-1421'):
        return {'timing': {'authority_manager': {
            'a_level': level, 'a_level_attested_by': by}}}

    def test_attested_a1_is_not_warned_about(self):
        assert timing_axis_issues(self._remote()) == []

    def test_attestation_is_reported_as_attested_not_observed(self):
        level, prov, detail = resolve_a_level(self._remote())
        assert (level, prov) == ('A1', 'attested')
        assert 'remote RX-888' in detail

    def test_empty_attestation_does_not_count(self):
        """Whitespace is not a reason."""
        level, prov, _ = resolve_a_level(self._remote(by='   '))
        assert prov == 'assumed'
        assert 'ASSERTED as A1' in _messages(self._remote(by='   '))

    def test_attested_a0_still_gets_the_holdover_warning(self):
        """Attestation says WHICH level, not that the level is harmless."""
        msg = _messages(self._remote(level='A0', by='no GPSDO at the remote site'))
        assert 'UNMEASURED_RATE_SIGMA_PPM' in msg

    def test_probe_beats_attestation(self):
        """A local probe is observed evidence; stale config text must not
        override it."""
        cfg = self._remote()
        cfg['timing']['authority_manager']['gpsdo'] = {'enabled': True}
        assert resolve_a_level(cfg)[1] == 'observed'
        assert timing_axis_issues(cfg) == []

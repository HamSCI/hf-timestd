"""Tests for FrontendProbe — the receiver operating point recorder.

radiod's RX888 AGC re-adjusts the analog front-end gain once per second
from TOTAL 0-64.8 MHz power, dominated by shortwave broadcast far from
any timing pilot.  radiod digitally undoes the level change, so the
recorded signal level is unaffected — but the noise floor beneath it is
not (measured 0.52 dB of T6 C/N0 per dB of gain, B4 2026-08-28).  The
probe samples that operating point once per authority tick so a T6
residual can be attributed to it afterwards.

The probe is BEST EFFORT by contract: it must never raise into the
authority tick, because a radiod hiccup taking down timing authority
would be a far worse failure than a missing provenance row.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from hf_timestd.core.frontend_probe import FrontendProbe, SsrcByFrequency


class _Frontend:
    def __init__(self, rf_gain=11.9, rf_agc=True, if_power=-19.52):
        self.rf_gain = rf_gain
        self.rf_agc = rf_agc
        self.if_power = if_power


class _Status:
    def __init__(self, frontend=None, baseband_power=-99.08,
                 noise_density=-156.07):
        self.frontend = frontend if frontend is not None else _Frontend()
        self.baseband_power = baseband_power
        self.noise_density = noise_density


class _FakeControl:
    """Stands in for ka9q RadiodControl."""

    def __init__(self, status=None, raises=None):
        self._status = status if status is not None else _Status()
        self._raises = raises
        self.polls = []

    def poll_status(self, ssrc, timeout=2.0):
        self.polls.append(ssrc)
        if self._raises is not None:
            raise self._raises
        return self._status


class TestSample(unittest.TestCase):

    def test_returns_operating_point_from_status(self):
        probe = FrontendProbe(lambda: _FakeControl(), resolve_ssrc=lambda: 2072147062)
        self.assertEqual(
            probe.sample(),
            {
                "rf_gain": 11.9,
                "rf_agc": 1,
                "if_power": -19.52,
                "t6_baseband_power": -99.08,
                "t6_n0": -156.07,
            },
        )

    def test_polls_the_resolved_t6_ssrc(self):
        """The frontend state rides on every channel's status packet, but
        polling the T6 channel gets the pilot's own levels in the same
        exchange — that is the whole reason for one poll rather than two."""
        control = _FakeControl()
        FrontendProbe(lambda: control, resolve_ssrc=lambda: 2072147062).sample()
        self.assertEqual(control.polls, [2072147062])

    def test_rf_agc_recorded_as_int(self):
        """Stored in an INTEGER column; a raw bool would round-trip but
        reads back as a type the correlation queries do not expect."""
        control = _FakeControl(_Status(frontend=_Frontend(rf_agc=False)))
        sample = FrontendProbe(lambda: control, resolve_ssrc=lambda: 1).sample()
        self.assertEqual(sample["rf_agc"], 0)
        self.assertIsInstance(sample["rf_agc"], int)
        self.assertNotIsInstance(sample["rf_agc"], bool)


class TestBestEffort(unittest.TestCase):
    """Every failure path must yield {} — the caller updates the snapshot
    dict with it unconditionally, and the store lands missing keys as NULL."""

    def test_empty_when_poll_raises(self):
        control = _FakeControl(raises=OSError("no route to host"))
        probe = FrontendProbe(lambda: control, resolve_ssrc=lambda: 1)
        self.assertEqual(probe.sample(), {})

    def test_empty_when_poll_times_out(self):
        control = _FakeControl()
        control.poll_status = lambda ssrc, timeout=2.0: None
        probe = FrontendProbe(lambda: control, resolve_ssrc=lambda: 1)
        self.assertEqual(probe.sample(), {})

    def test_empty_when_ssrc_unresolvable(self):
        probe = FrontendProbe(lambda: _FakeControl(), resolve_ssrc=lambda: None)
        self.assertEqual(probe.sample(), {})

    def test_empty_when_resolver_raises(self):
        def boom():
            raise RuntimeError("discovery failed")
        probe = FrontendProbe(lambda: _FakeControl(), resolve_ssrc=boom)
        self.assertEqual(probe.sample(), {})

    def test_absent_fields_are_omitted_not_none(self):
        """A partial status must not write NULLs over nothing — omit the
        key so the column simply stays NULL, and keep what did arrive."""
        control = _FakeControl(_Status(
            frontend=_Frontend(if_power=None), noise_density=None,
        ))
        sample = FrontendProbe(lambda: control, resolve_ssrc=lambda: 1).sample()
        self.assertNotIn("if_power", sample)
        self.assertNotIn("t6_n0", sample)
        self.assertEqual(sample["rf_gain"], 11.9)


class TestLazyControl(unittest.TestCase):
    """``RadiodControl.__init__`` resolves the radiod mDNS name — a
    network side effect.  Building an AuthorityManager to inspect it must
    never touch the network, so the control is constructed on first use."""

    def test_control_not_constructed_until_first_sample(self):
        built = []

        def factory():
            built.append(1)
            return _FakeControl()

        probe = FrontendProbe(factory, resolve_ssrc=lambda: 1)
        self.assertEqual(built, [])
        probe.sample()
        self.assertEqual(len(built), 1)

    def test_control_constructed_once_and_reused(self):
        built = []

        def factory():
            built.append(1)
            return _FakeControl()

        probe = FrontendProbe(factory, resolve_ssrc=lambda: 1)
        probe.sample()
        probe.sample()
        self.assertEqual(len(built), 1)

    def test_empty_when_control_construction_fails(self):
        def factory():
            raise OSError("Failed to resolve AC0G-B4-status.local")
        probe = FrontendProbe(factory, resolve_ssrc=lambda: 1)
        self.assertEqual(probe.sample(), {})


class TestSsrcResolution(unittest.TestCase):

    def test_resolved_once_then_cached(self):
        """Discovery is a multi-second multicast listen; doing it every
        30 s tick would be wasteful and noisy."""
        calls = []

        def resolver():
            calls.append(1)
            return 2072147062

        probe = FrontendProbe(lambda: _FakeControl(), resolve_ssrc=resolver)
        probe.sample()
        probe.sample()
        probe.sample()
        self.assertEqual(len(calls), 1)

    def test_retries_resolution_after_failure(self):
        """A channel that did not exist yet at startup must be picked up
        later — caching a failure would make the probe dead for the life
        of the process."""
        results = [None, 2072147062]

        def resolver():
            return results.pop(0)

        probe = FrontendProbe(lambda: _FakeControl(), resolve_ssrc=resolver)
        self.assertEqual(probe.sample(), {})
        self.assertIn("rf_gain", probe.sample())


class _Chan:
    def __init__(self, frequency):
        self.frequency = frequency


class TestSsrcByFrequency(unittest.TestCase):
    """radiod hash-assigns SSRCs — they are NEVER computable from a
    frequency (fleet invariant).  Resolution is by discovery only."""

    def test_returns_ssrc_of_the_matching_channel(self):
        channels = {
            1529789166: _Chan(40680000.0),
            2072147062: _Chan(45375000.0),
        }
        resolve = SsrcByFrequency(
            "AC0G-B4-status.local", 45375000.0,
            discover=lambda addr: channels,
        )
        self.assertEqual(resolve(), 2072147062)

    def test_matches_within_tolerance(self):
        resolve = SsrcByFrequency(
            "s.local", 45375000.0,
            discover=lambda addr: {7: _Chan(45375000.4)},
        )
        self.assertEqual(resolve(), 7)

    def test_none_when_no_channel_matches(self):
        resolve = SsrcByFrequency(
            "s.local", 45375000.0,
            discover=lambda addr: {7: _Chan(10000000.0)},
        )
        self.assertIsNone(resolve())

    def test_none_when_discovery_raises(self):
        def boom(addr):
            raise OSError("no radiod")
        resolve = SsrcByFrequency("s.local", 45375000.0, discover=boom)
        self.assertIsNone(resolve())


if __name__ == "__main__":
    unittest.main()

"""A stream's identity must include the filters that shape it.

`StreamSpec.__hash__`/`__eq__` compared {frequency, preset, sample_rate, agc,
gain} and ignored the filter edges — so two channels differing only in
bandwidth hashed the same and compared equal.  Stream sharing keys on that
equality, so the second requester would be handed the first one's stream and
receive a different bandwidth than it asked for.

Phil Karn (ka9q-radio author), 2026-09-21:

    you send PRESET USB followed by retuning the filters to -3000, -50, you
    actually get the lower sideband.  So now the preset actively lies about
    the channel.

    The parameter vector {demod_type, channels, frequency, offset, filter
    high/low, etc} gives you everything you need to interpret the data
    stream from a channel.

`core_recorder_v2` passes per-channel `low_edge`/`high_edge` on creation, so
this is not hypothetical here.  The 2026-09-20 T6 bandwidth experiment built
exactly such a pair — same frequency, same preset "iq", same rate, ±25 kHz
against ±45 kHz — and measured a 1.31x difference in timing scatter between
them.  Two objects that differ enough to change the measurement must not
compare equal.

⚠ `preset` stays in the identity.  A spec records what a caller ASKED FOR,
and "iq" remains a legitimate request even after Phil's change; what it can
no longer do is describe a channel on its own.  Adding the filters fixes
that without removing anything callers already rely on.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from hf_timestd.stream.stream_spec import StreamSpec  # noqa: E402


def _spec(**kw):
    base = dict(frequency_hz=45_375_000.0, preset="iq", sample_rate=96_000)
    base.update(kw)
    return StreamSpec(**base)


class TestFilterEdgesAreIdentity:

    def test_different_bandwidths_are_different_streams(self):
        """THE case: the T6 A/B pair, which measured 1.31x apart."""
        narrow = _spec(low_edge=-25_000.0, high_edge=25_000.0)
        wide = _spec(low_edge=-45_000.0, high_edge=45_000.0)
        assert narrow != wide
        assert hash(narrow) != hash(wide)

    def test_an_asymmetric_filter_differs_from_a_symmetric_one(self):
        """Phil's example: USB retuned to -3000,-50 is really LSB."""
        usb = _spec(preset="usb", low_edge=50.0, high_edge=3000.0)
        really_lsb = _spec(preset="usb", low_edge=-3000.0, high_edge=-50.0)
        assert usb != really_lsb

    def test_identical_specs_still_share(self):
        """Stream sharing must keep working for genuinely identical asks."""
        a = _spec(low_edge=-25_000.0, high_edge=25_000.0)
        b = _spec(low_edge=-25_000.0, high_edge=25_000.0)
        assert a == b
        assert hash(a) == hash(b)
        assert len({a, b}) == 1

    def test_unspecified_edges_still_compare_equal(self):
        """Most callers pass no edges at all; they must still share."""
        assert _spec() == _spec()
        assert hash(_spec()) == hash(_spec())

    def test_specified_differs_from_unspecified(self):
        """'whatever radiod defaults to' is not the same ask as '±25 kHz'."""
        assert _spec() != _spec(low_edge=-25_000.0, high_edge=25_000.0)


class TestExistingContractIsPreserved:
    """⚠ These must pass before AND after. They pin what callers already
    depend on, and the filter work must not disturb any of it."""

    def test_frequency_tolerance_still_one_hz(self):
        assert _spec(frequency_hz=45_375_000.0) == _spec(frequency_hz=45_375_000.4)
        assert _spec(frequency_hz=45_375_000.0) != _spec(frequency_hz=45_375_002.0)

    def test_preset_still_distinguishes(self):
        assert _spec(preset="iq") != _spec(preset="usb")

    def test_preset_comparison_stays_case_insensitive(self):
        assert _spec(preset="IQ") == _spec(preset="iq")

    def test_sample_rate_still_distinguishes(self):
        assert _spec(sample_rate=96_000) != _spec(sample_rate=48_000)

    def test_gain_tolerance_still_point_one_db(self):
        assert _spec(gain=10.0) == _spec(gain=10.05)
        assert _spec(gain=10.0) != _spec(gain=11.0)

    def test_agc_still_distinguishes(self):
        assert _spec(agc=True) != _spec(agc=False)

    def test_construction_without_edges_still_works(self):
        """Every existing call site omits them."""
        s = StreamSpec(frequency_hz=1e6, preset="usb", sample_rate=12_000)
        assert s.low_edge is None and s.high_edge is None


class TestMatches:
    """`matches` is the actual sharing decision — "could share
    stream" — and it carried the same blind spot as __eq__.  Fixing one and
    not the other would leave equality and shareability disagreeing, which is
    worse than either bug alone."""

    def test_different_bandwidths_do_not_match(self):
        narrow = _spec(low_edge=-25_000.0, high_edge=25_000.0)
        wide = _spec(low_edge=-45_000.0, high_edge=45_000.0)
        assert not narrow.matches(wide)

    def test_identical_specs_remain_shareable(self):
        a = _spec(low_edge=-25_000.0, high_edge=25_000.0)
        b = _spec(low_edge=-25_000.0, high_edge=25_000.0)
        assert a.matches(b)

    def test_unspecified_edges_remain_shareable(self):
        assert _spec().matches(_spec())

    def test_it_still_honours_its_frequency_tolerance_argument(self):
        a = _spec(frequency_hz=45_375_000.0)
        b = _spec(frequency_hz=45_375_050.0)
        assert not a.matches(b)
        assert a.matches(b, frequency_tolerance_hz=100.0)

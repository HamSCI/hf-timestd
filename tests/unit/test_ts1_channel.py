"""Where the TS-1's PPS lands, given the ADC clock — and who gets to say.

A wrong alias creates a T6 channel over empty spectrum. Every step
succeeds, the detector never locks, and nothing reports a fault: the
worst shape a defect can take. These tests pin the arithmetic against
the two rates the fleet actually runs, and pin the precedence that
decides which rate is used.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from hf_timestd.core.ts1_channel import (
    DESIGNER_ALIASES_HZ,
    DEFAULT_ADC_HZ,
    FLEET_TS1_TX_HZ,
    adc_rate_from_status,
    nyquist_alias_hz,
    resolve_t6_frequency_hz,
)

RATE_129 = 129_600_000
RATE_64 = 64_800_000


def _status(*, input_samprate=None, output_samprate=None, frontend=True):
    """A radiod status object shaped like ka9q-python's ChannelStatus."""
    fe = SimpleNamespace(input_samprate=input_samprate) if frontend else None
    return SimpleNamespace(frontend=fe, output_samprate=output_samprate)


class TestAgainstTheDesignersDocumentation:
    """P. Elliott WB6CXC, TS-1 TimeSync injector mode, verbatim:

        • Freq: 84.225 MHz
        • Alias (fSample  64.800 MHz) : 19.425 MHz
        • Alias (fSample 129.600 MHz) : 45.375 MHz

    This is the source of truth for the arithmetic — not our own template,
    which quotes it, and not this implementation, which must reproduce it.
    """

    def test_the_default_tx_frequency_matches_the_designer(self):
        assert FLEET_TS1_TX_HZ == 84_225_000

    @pytest.mark.parametrize("adc_hz,expected", sorted(DESIGNER_ALIASES_HZ.items()))
    def test_every_published_alias_is_reproduced(self, adc_hz, expected):
        assert nyquist_alias_hz(FLEET_TS1_TX_HZ, adc_hz) == expected

    def test_the_64_8_case_is_the_one_that_was_WRONG(self):
        """⛔ The regression, stated against the designer's own number.
        `adc - tx` returned -19_425_000 — the negation of the published
        value — at one of the two sample rates the TS-1 is built for."""
        assert nyquist_alias_hz(FLEET_TS1_TX_HZ, RATE_64) == 19_425_000
        assert RATE_64 - FLEET_TS1_TX_HZ == -19_425_000   # what it used to do


class TestTheAliasArithmetic:
    def test_129_6_msps_gives_45_375_mhz(self):
        assert nyquist_alias_hz(FLEET_TS1_TX_HZ, RATE_129) == 45_375_000

    def test_the_alias_is_never_negative(self):
        """Whatever else changes, this must hold for every zone."""
        for zone in range(1, 12):
            tx = int(RATE_64 * zone * 0.37)
            assert nyquist_alias_hz(tx, RATE_64) >= 0, f"zone {zone}"

    def test_the_alias_never_exceeds_nyquist(self):
        for zone in range(1, 12):
            tx = int(RATE_64 * zone * 0.37)
            assert nyquist_alias_hz(tx, RATE_64) * 2 <= RATE_64, f"zone {zone}"

    def test_a_frequency_already_inside_the_first_zone_is_unchanged(self):
        assert nyquist_alias_hz(10_000_000, RATE_129) == 10_000_000

    def test_exactly_nyquist_folds_to_nyquist(self):
        """The boundary belongs to the zone below it, not above."""
        assert nyquist_alias_hz(RATE_64 // 2, RATE_64) == RATE_64 // 2

    def test_a_multiple_of_the_sample_rate_aliases_to_dc(self):
        assert nyquist_alias_hz(RATE_64 * 3, RATE_64) == 0

    @pytest.mark.parametrize("bad_rate", [0, -1, -129_600_000])
    def test_a_nonpositive_rate_refuses(self, bad_rate):
        """Returning a plausible number here is how a bad channel gets
        created quietly. Refuse instead."""
        with pytest.raises(ValueError):
            nyquist_alias_hz(FLEET_TS1_TX_HZ, bad_rate)

    def test_a_negative_tx_refuses(self):
        with pytest.raises(ValueError):
            nyquist_alias_hz(-1, RATE_129)


class TestReadingTheRateFromRadiod:
    def test_it_reads_the_frontend_input_rate(self):
        assert adc_rate_from_status(_status(input_samprate=RATE_129)) == RATE_129

    def test_it_does_NOT_read_the_channel_output_rate(self):
        """⚠ output_samprate is the CHANNEL rate — 12 kHz on B4's WSPR
        channels. Reading it is the same mistake as taking the first
        `samprate` out of radiod's config, which on B4 is 12000."""
        got = adc_rate_from_status(
            _status(input_samprate=None, output_samprate=12_000))
        assert got is None

    def test_no_frontend_block_is_none_not_a_crash(self):
        assert adc_rate_from_status(_status(frontend=False)) is None

    def test_a_zero_rate_is_none(self):
        """radiod before the front end is up. Not evidence of anything."""
        assert adc_rate_from_status(_status(input_samprate=0)) is None

    def test_an_object_that_is_nothing_like_a_status_is_none(self):
        assert adc_rate_from_status(object()) is None
        assert adc_rate_from_status(None) is None


class TestPrecedence:
    """Measured beats declared; declared beats default. As for T5."""

    def test_radiod_is_used_when_nothing_is_configured(self):
        freq, rate, source = resolve_t6_frequency_hz(
            FLEET_TS1_TX_HZ, probe=lambda: RATE_64)
        assert (freq, rate, source) == (19_425_000, RATE_64, "radiod")

    def test_an_explicit_rate_beats_the_probe(self):
        """A site that states its rate is not overruled by a poll."""
        freq, rate, source = resolve_t6_frequency_hz(
            FLEET_TS1_TX_HZ, adc_hz=RATE_64, probe=lambda: RATE_129)
        assert (freq, rate, source) == (19_425_000, RATE_64, "configured")

    def test_no_probe_and_no_config_falls_back_but_SAYS_SO(self):
        """The silent 129.6 assumption is the defect being fixed. It still
        produces an answer — refusing would strand every site whose radiod
        is not up yet — but it must be labelled a guess."""
        freq, rate, source = resolve_t6_frequency_hz(FLEET_TS1_TX_HZ)
        assert rate == DEFAULT_ADC_HZ
        assert freq == 45_375_000
        assert source == "default", "a guess must not be reported as measured"

    def test_a_probe_that_returns_nothing_falls_back(self):
        _, rate, source = resolve_t6_frequency_hz(
            FLEET_TS1_TX_HZ, probe=lambda: None)
        assert (rate, source) == (DEFAULT_ADC_HZ, "default")

    def test_a_probe_that_RAISES_falls_back_instead_of_propagating(self):
        """radiod being down must not abort provisioning."""
        def boom():
            raise OSError("no route to host")
        _, rate, source = resolve_t6_frequency_hz(FLEET_TS1_TX_HZ, probe=boom)
        assert (rate, source) == (DEFAULT_ADC_HZ, "default")

    def test_the_source_distinguishes_all_three_origins(self):
        """An operator reading a log has to be able to tell a measurement
        from a guess; collapsing these was the original sin."""
        sources = {
            resolve_t6_frequency_hz(FLEET_TS1_TX_HZ, adc_hz=RATE_129)[2],
            resolve_t6_frequency_hz(FLEET_TS1_TX_HZ, probe=lambda: RATE_129)[2],
            resolve_t6_frequency_hz(FLEET_TS1_TX_HZ)[2],
        }
        assert sources == {"configured", "radiod", "default"}


class TestTheCaseThatMotivatedThis:
    def test_a_64_8_site_that_stays_silent_gets_the_WRONG_channel(self):
        """Documents the live failure mode, so the guard has a reason.

        A 64.8 Msps site whose operator skips the prompt is handed the
        129.6 answer: a channel 26 MHz away from the pilot, which locks
        never and complains never.
        """
        guessed, _, source = resolve_t6_frequency_hz(FLEET_TS1_TX_HZ)
        truth = nyquist_alias_hz(FLEET_TS1_TX_HZ, RATE_64)
        assert source == "default"
        assert abs(guessed - truth) == 25_950_000

    def test_asking_radiod_gets_that_site_right(self):
        freq, _, source = resolve_t6_frequency_hz(
            FLEET_TS1_TX_HZ, probe=lambda: RATE_64)
        assert source == "radiod"
        assert freq == nyquist_alias_hz(FLEET_TS1_TX_HZ, RATE_64)


class TestTheShellSitesAgreeWithThisModule:
    """The alias is computed in THREE places. Keep them one answer.

    ⚠ Written because fixing two of three is exactly what happened on
    2026-09-17: `ts1-probe.sh` was corrected, `setup-station.sh` was not,
    and the commit message claimed both. `setup-station.sh` is the one
    that writes the config, so the defect survived its own fix. Only
    reading the files caught it.

    Both shell sites must reduce modulo the sample rate BEFORE reflecting.
    The defect is the bare `rate - tx` form with no `%` in sight.
    """

    import pathlib
    _ROOT = pathlib.Path(__file__).parents[2]
    SHELL_SITES = ("scripts/ts1-probe.sh", "scripts/setup-station.sh")

    def _alias_region(self, rel):
        """Lines around the alias computation in one shell file."""
        text = (self._ROOT / rel).read_text().splitlines()
        live = [l.strip() for l in text if l.strip() and not l.strip().startswith("#")]
        return [l for l in live if "INJECTED_HZ" in l or "L6_PPS_FREQUENCY" in l
                or "%" in l and "ADC" in l.upper()]

    @pytest.mark.parametrize("rel", SHELL_SITES)
    def test_the_site_reduces_modulo_before_reflecting(self, rel):
        region = "\n".join(self._alias_region(rel))
        assert "%" in region, (
            f"{rel} computes the alias without a modulo reduction. That is "
            f"the second-Nyquist-zone-only form, which returns a NEGATIVE "
            f"frequency at 64.8 Msps. See nyquist_alias_hz."
        )

    @pytest.mark.parametrize("rel", SHELL_SITES)
    def test_the_site_does_not_assign_a_bare_subtraction(self, rel):
        """`X=$(( ADC - tx ))` with tx the raw TX, not the reduced remainder."""
        import re
        bad = re.compile(
            r'(INJECTED_HZ|L6_PPS_FREQUENCY)=\$\(\(\s*\w*ADC\w*\s*-\s*'
            r'(?!_?r\b)[_a-zA-Z]*t[sx]\w*\s*\)\)', re.IGNORECASE)
        text = (self._ROOT / rel).read_text()
        live = "\n".join(l for l in text.splitlines()
                         if not l.strip().startswith("#"))
        hits = bad.findall(live)
        assert not hits, (
            f"{rel} assigns the alias as a bare rate-minus-TX subtraction "
            f"{hits}; it must subtract the REMAINDER (tx % rate)."
        )

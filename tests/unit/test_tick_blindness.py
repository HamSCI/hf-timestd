"""A channel must be able to report that it is BLIND (hf-timestd#24).

WWV 20 and 25 MHz produce a confident tick measurement every minute of
the day — 1435 of 1440 — at a constant ~9 dB SNR, including hours when
those bands are closed over a 1,100 km path.  Nothing downstream can
tell those minutes from real ones, so they contribute confident noise
to the fusion.

The existing per-minute statistics cannot separate the cases.  Measured
over ~8,780 minutes per channel on AC0G-B4:

    MHz   unc ms   SNR dB   confidence   edges
    5.0     1.30     13.9        0.579    56.6
    10.0    1.17     16.0        0.567    56.3
    20.0    1.88     10.0        0.935    56.7
    25.0    2.01      9.4        0.930    56.7

``ensemble_n_edges`` is ~56.6 on every channel whether or not a signal
is present, ``overall_confidence`` is INVERTED (higher on the suspect
channels), and doppler uncertainty is ~0.0022 Hz everywhere.

SNR is the one field that separates, because the bands really do open:
20 MHz reaches 37.4 dB and 25 MHz 33.3 dB when they work, against a
~9 dB floor when they do not.
"""
from hf_timestd.core.metrology_service import MetrologyService


def _admissible(n_edges=56, snr_db=20.0, min_snr_db=12.0):
    return MetrologyService.tick_measurement_admissible(
        n_edges=n_edges, mean_snr_db=snr_db, min_snr_db=min_snr_db)


class TestReportsBlindness:
    def test_noise_floor_detection_is_refused(self):
        """~9 dB with a full complement of edges is the blind case."""
        ok, reason = _admissible(n_edges=57, snr_db=9.4)
        assert ok is False
        assert "snr" in reason.lower()

    def test_a_real_opening_is_admitted(self):
        """20/25 MHz reach 33-37 dB when the band is genuinely open."""
        ok, reason = _admissible(n_edges=57, snr_db=33.3)
        assert ok is True
        assert reason == "ok"

    def test_edge_count_alone_cannot_rescue_a_blind_minute(self):
        """56 edges are found whether or not there is a signal, so a
        full complement must NOT imply a valid measurement."""
        ok, _r = _admissible(n_edges=60, snr_db=9.0)
        assert ok is False

    def test_too_few_edges_is_still_refused(self):
        ok, reason = _admissible(n_edges=4, snr_db=30.0)
        assert ok is False
        assert "edge" in reason.lower()

    def test_the_reason_distinguishes_the_two_refusals(self):
        _ok, snr_reason = _admissible(n_edges=57, snr_db=9.0)
        _ok2, edge_reason = _admissible(n_edges=2, snr_db=30.0)
        assert snr_reason != edge_reason


class TestThresholdBehaviour:
    def test_at_the_threshold_is_admitted(self):
        assert _admissible(snr_db=12.0, min_snr_db=12.0)[0] is True

    def test_just_below_is_refused(self):
        assert _admissible(snr_db=11.9, min_snr_db=12.0)[0] is False

    def test_threshold_is_configurable(self):
        """Sites differ; the default must not be the only option."""
        assert _admissible(snr_db=10.0, min_snr_db=9.0)[0] is True
        assert _admissible(snr_db=10.0, min_snr_db=15.0)[0] is False

    def test_zero_threshold_admits_everything_measurable(self):
        """Opting out must remain possible for diagnosis."""
        assert _admissible(snr_db=1.0, min_snr_db=0.0)[0] is True


class TestMissingData:
    def test_absent_snr_is_refused_not_assumed_good(self):
        ok, reason = _admissible(snr_db=None)
        assert ok is False
        assert "snr" in reason.lower()

    def test_absent_edge_count_is_refused(self):
        assert _admissible(n_edges=None)[0] is False


class TestDefaultThreshold:
    def test_default_is_chosen_from_coverage_data(self):
        """10 dB keeps 94.6% of minutes covered while discarding ~60% of
        measurements as noise.  12 dB costs a THIRD of all minutes for
        almost no further quality — and a fusion input that goes silent
        for long stretches is hf-timestd#16."""
        assert MetrologyService.DEFAULT_MIN_TICK_SNR_DB == 10.0

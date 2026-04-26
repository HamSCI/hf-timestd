"""
Unit tests for hf_timestd.core.probabilistic_discriminator

Logistic-regression-based probabilistic WWV/WWVH discriminator. Tests cover:
- Module-level ground-truth tables (no overlap, label consistency)
- DiscriminationFeatures: defaults, NaN handling in to_vector(),
  ground-truth indicator polarity, feature_names ordering
- ProbabilisticResult: margin and Shannon entropy edge cases
- LogisticRegressionModel:
  * sigmoid numerical stability and bounds
  * predict_proba on vectors and matrices
  * fit() converges, learns boundary, can be serialized + restored
  * to_dict/from_dict round-trip
- ProbabilisticDiscriminator:
  * extract_features feature normalization (power, BCD log ratio,
    Doppler log ratio, delay)
  * classify() returns probabilities, uses min_confidence_threshold
    to short-circuit to UNCERTAIN, respects return_uncertain=False
  * Auto-train on ground-truth minutes pushes a sample with the right label
  * fit() requires min_samples
  * Statistics, learned weights, feature_importance
  * Model save/load round-trip via on-disk JSON
- Convenience functions: get_discriminator() singleton, discriminate_probabilistic()
"""

from pathlib import Path
from unittest.mock import patch

import math

import numpy as np
import pytest

import hf_timestd.core.probabilistic_discriminator as pd
from hf_timestd.core.probabilistic_discriminator import (
    GROUND_TRUTH_MINUTES,
    WWVH_ONLY_MINUTES,
    WWVH_SILENT_MINUTES,
    WWV_ONLY_MINUTES,
    WWV_SILENT_MINUTES,
    DiscriminationFeatures,
    LogisticRegressionModel,
    ProbabilisticDiscriminator,
    ProbabilisticResult,
    TrainingSample,
    discriminate_probabilistic,
    get_discriminator,
)


# =============================================================================
# Module constants & ground-truth tables
# =============================================================================


class TestGroundTruthTables:
    def test_silent_minute_sets_disjoint(self):
        # WWV-silent and WWVH-silent must not share a minute
        assert WWV_SILENT_MINUTES.isdisjoint(WWVH_SILENT_MINUTES)

    def test_exclusive_minute_sets_disjoint(self):
        assert WWV_ONLY_MINUTES.isdisjoint(WWVH_ONLY_MINUTES)

    def test_silent_minute_labels_match_inverse_station(self):
        # WWV silent → label 0 (WWVH transmitting)
        for m in WWV_SILENT_MINUTES:
            assert GROUND_TRUTH_MINUTES[m] == 0
        for m in WWVH_SILENT_MINUTES:
            assert GROUND_TRUTH_MINUTES[m] == 1

    def test_exclusive_minute_labels_match_active_station(self):
        for m in WWV_ONLY_MINUTES:
            assert GROUND_TRUTH_MINUTES[m] == 1
        for m in WWVH_ONLY_MINUTES:
            assert GROUND_TRUTH_MINUTES[m] == 0


# =============================================================================
# DiscriminationFeatures
# =============================================================================


class TestDiscriminationFeatures:
    def test_defaults_initialise_with_nan_secondaries(self):
        f = DiscriminationFeatures()
        assert f.power_ratio_norm == 0.0
        assert math.isnan(f.bcd_ratio_norm)
        assert math.isnan(f.doppler_ratio_norm)
        assert math.isnan(f.delay_ratio_norm)
        assert f.is_440hz_minute is False

    def test_to_vector_replaces_nan_with_zero(self):
        f = DiscriminationFeatures(power_ratio_norm=1.5)
        # secondaries default to NaN → must come out as 0.0
        vec = f.to_vector()
        assert vec[0] == pytest.approx(1.5)
        assert vec[1] == 0.0  # bcd
        assert vec[2] == 0.0  # doppler
        assert vec[3] == 0.0  # delay
        assert vec.shape == (7,)

    def test_to_vector_tone_440_polarity(self):
        wwv_tone = DiscriminationFeatures(tone_440_detected_wwv=True).to_vector()
        wwvh_tone = DiscriminationFeatures(tone_440_detected_wwvh=True).to_vector()
        assert wwv_tone[4] == 1.0
        assert wwv_tone[5] == 0.0
        assert wwvh_tone[4] == 0.0
        assert wwvh_tone[5] == -1.0

    def test_ground_truth_indicator_polarity(self):
        # Minute 8 is WWV-only → label 1 → indicator +1.
        # Minute 1 is WWVH-only → label 0 → indicator -1.
        # Minute 15 is not in the ground-truth table → indicator 0.
        wwv = DiscriminationFeatures(minute=8, is_ground_truth_minute=True)
        wwvh = DiscriminationFeatures(minute=1, is_ground_truth_minute=True)
        neither = DiscriminationFeatures(minute=15, is_ground_truth_minute=False)
        assert wwv.to_vector()[6] == 1.0
        assert wwvh.to_vector()[6] == -1.0
        assert neither.to_vector()[6] == 0.0

    def test_feature_names_match_vector_length(self):
        f = DiscriminationFeatures()
        assert len(f.feature_names) == len(f.to_vector())


# =============================================================================
# ProbabilisticResult
# =============================================================================


class TestProbabilisticResult:
    def test_margin_at_certain_extremes(self):
        r0 = ProbabilisticResult(p_wwv=0.0, p_wwvh=1.0, station='WWVH', confidence=1.0)
        r1 = ProbabilisticResult(p_wwv=1.0, p_wwvh=0.0, station='WWV', confidence=1.0)
        assert r0.margin == 1.0
        assert r1.margin == 1.0

    def test_margin_at_uncertain_midpoint(self):
        r = ProbabilisticResult(p_wwv=0.5, p_wwvh=0.5, station='UNCERTAIN', confidence=0.0)
        assert r.margin == 0.0

    def test_entropy_zero_at_extremes(self):
        # log(0) avoided via the early-return guard
        r0 = ProbabilisticResult(p_wwv=0.0, p_wwvh=1.0, station='WWVH', confidence=1.0)
        r1 = ProbabilisticResult(p_wwv=1.0, p_wwvh=0.0, station='WWV', confidence=1.0)
        assert r0.entropy == 0.0
        assert r1.entropy == 0.0

    def test_entropy_max_at_midpoint(self):
        r = ProbabilisticResult(p_wwv=0.5, p_wwvh=0.5, station='UNCERTAIN', confidence=0.0)
        assert r.entropy == pytest.approx(1.0)


# =============================================================================
# LogisticRegressionModel
# =============================================================================


class TestSigmoidStability:
    def test_sigmoid_at_zero(self):
        assert LogisticRegressionModel.sigmoid(np.array([0.0]))[0] == pytest.approx(0.5)

    def test_sigmoid_clipped_for_extreme_inputs(self):
        # Very large positive → ≈ 1; very large negative → ≈ 0; no overflow
        out = LogisticRegressionModel.sigmoid(np.array([10_000.0, -10_000.0]))
        assert out[0] == pytest.approx(1.0)
        assert out[1] == pytest.approx(0.0)

    def test_sigmoid_monotonic(self):
        out = LogisticRegressionModel.sigmoid(np.linspace(-5, 5, 11))
        assert all(np.diff(out) > 0)


class TestLogisticPredict:
    def test_predict_proba_accepts_vector_or_matrix(self):
        m = LogisticRegressionModel(n_features=3)
        m.weights = np.array([1.0, 0.0, 0.0])
        m.bias = 0.0

        # Single vector
        p = m.predict_proba(np.array([0.0, 0.0, 0.0]))
        assert p.shape == (1,)
        assert p[0] == pytest.approx(0.5)

        # Batch of vectors
        X = np.array([[0.0, 0.0, 0.0], [10.0, 0.0, 0.0]])
        p = m.predict_proba(X)
        assert p.shape == (2,)
        assert p[1] > p[0]  # larger w·x → larger probability

    def test_predict_uses_threshold(self):
        m = LogisticRegressionModel(n_features=2)
        m.weights = np.array([1.0, 0.0])
        X = np.array([[-5.0, 0.0], [5.0, 0.0]])
        labels = m.predict(X)
        assert labels.tolist() == [0, 1]


class TestLogisticFit:
    def test_fit_separates_linearly_separable_data(self):
        # Two clusters at x=-3 and x=+3 → trivially separable
        np.random.seed(0)
        X = np.vstack([
            np.random.randn(50, 2) + np.array([-3.0, 0.0]),
            np.random.randn(50, 2) + np.array([+3.0, 0.0]),
        ])
        y = np.array([0] * 50 + [1] * 50)

        m = LogisticRegressionModel(n_features=2, regularization=0.001)
        m.fit(X, y, n_iterations=2000)

        # On training data, accuracy should be very high
        preds = m.predict(X)
        assert (preds == y).mean() > 0.95
        assert m.is_trained
        assert m.training_samples == 100

    def test_fit_stops_early_on_convergence(self):
        # Trivial single-sample fit converges almost immediately
        m = LogisticRegressionModel(n_features=1, regularization=0.0)
        X = np.array([[1.0]])
        y = np.array([1])
        # This should not run all 1000 iterations — convergence guard fires
        loss = m.fit(X, y, n_iterations=1000, tolerance=1e-3)
        assert loss < float('inf')

    def test_fit_records_loss_history(self):
        np.random.seed(1)
        X = np.random.randn(100, 3)
        y = (X[:, 0] > 0).astype(int)
        m = LogisticRegressionModel(n_features=3)
        m.fit(X, y, n_iterations=500)
        # History records every 100 iterations
        assert len(m.training_loss_history) >= 1


class TestLogisticSerialization:
    def test_to_dict_from_dict_round_trip(self):
        m = LogisticRegressionModel(n_features=4, regularization=0.05)
        m.weights = np.array([0.1, -0.2, 0.3, -0.4])
        m.bias = 0.5
        m.is_trained = True
        m.training_samples = 123

        restored = LogisticRegressionModel.from_dict(m.to_dict())
        assert restored.n_features == 4
        assert restored.regularization == pytest.approx(0.05)
        assert restored.weights.tolist() == m.weights.tolist()
        assert restored.bias == m.bias
        assert restored.is_trained
        assert restored.training_samples == 123

    def test_from_dict_with_minimal_payload(self):
        # Missing optional keys → safe defaults
        m = LogisticRegressionModel.from_dict({})
        assert m.n_features == 7
        assert m.weights.tolist() == [0.0] * 7
        assert m.bias == 0.0


# =============================================================================
# ProbabilisticDiscriminator — feature extraction
# =============================================================================


class TestExtractFeatures:
    @pytest.fixture
    def disc(self):
        return ProbabilisticDiscriminator(auto_train=False)

    def test_power_ratio_normalized_by_10(self, disc):
        f = disc.extract_features(power_ratio_db=15.0, minute=0)
        assert f.power_ratio_norm == pytest.approx(1.5)

    def test_bcd_ratio_log_normalized(self, disc):
        f = disc.extract_features(
            bcd_wwv_amplitude=4.0, bcd_wwvh_amplitude=1.0, minute=0)
        # log(4/1) / 2 = 0.693
        assert f.bcd_ratio_norm == pytest.approx(math.log(4.0) / 2)

    def test_doppler_ratio_log_normalized(self, disc):
        # Lower WWV Doppler std → log(WWVH/WWV) > 0 → toward WWV
        f = disc.extract_features(
            doppler_std_wwv=0.1, doppler_std_wwvh=1.0, minute=0)
        assert f.doppler_ratio_norm == pytest.approx(math.log(10.0))

    def test_zero_amplitudes_skip_bcd(self, disc):
        f = disc.extract_features(
            bcd_wwv_amplitude=0.0, bcd_wwvh_amplitude=1.0, minute=0)
        # Source skips bcd_ratio when either amplitude is 0
        assert math.isnan(f.bcd_ratio_norm)

    def test_delay_normalized_by_100(self, disc):
        f = disc.extract_features(differential_delay_ms=50.0, minute=0)
        assert f.delay_ratio_norm == pytest.approx(0.5)

    def test_minute_flags(self, disc):
        f = disc.extract_features(minute=1, power_ratio_db=0)
        assert f.is_440hz_minute is True
        # Minute 1 is in WWVH_ONLY_MINUTES → ground-truth minute
        assert f.is_ground_truth_minute is True

        f15 = disc.extract_features(minute=15, power_ratio_db=0)
        assert f15.is_440hz_minute is False
        assert f15.is_ground_truth_minute is False

    def test_special_tone_flags(self, disc):
        f = disc.extract_features(
            tone_440_wwv_detected=True,
            tone_440_wwvh_detected=False,
            tone_500_600_detected=True,
            minute=2,
        )
        assert f.tone_440_detected_wwv is True
        assert f.tone_440_detected_wwvh is False
        assert f.tone_500_600_detected is True


# =============================================================================
# ProbabilisticDiscriminator — classify
# =============================================================================


class TestClassify:
    def _disc(self, **kwargs):
        return ProbabilisticDiscriminator(auto_train=False, **kwargs)

    def test_default_weights_favor_high_power_ratio_for_wwv(self):
        d = self._disc()
        # Strong positive power ratio + WWV ground-truth indicator → classify WWV
        feats = d.extract_features(power_ratio_db=20.0, minute=8)  # WWV-only
        result = d.classify(feats)
        assert result.station == 'WWV'
        assert result.p_wwv > 0.5

    def test_default_weights_favor_low_power_ratio_for_wwvh(self):
        d = self._disc()
        feats = d.extract_features(power_ratio_db=-20.0, minute=44)  # WWVH-only
        result = d.classify(feats)
        assert result.station == 'WWVH'
        assert result.p_wwv < 0.5

    def test_uncertain_below_confidence_threshold(self):
        d = self._disc(min_confidence_threshold=0.95)
        # Power ratio ~0 → P(WWV) ≈ 0.5 → confidence ≈ 0 → UNCERTAIN
        feats = d.extract_features(power_ratio_db=0.0, minute=15)
        result = d.classify(feats)
        assert result.station == 'UNCERTAIN'

    def test_return_uncertain_false_forces_binary(self):
        d = self._disc(min_confidence_threshold=0.95)
        feats = d.extract_features(power_ratio_db=0.0, minute=15)
        result = d.classify(feats, return_uncertain=False)
        assert result.station in ('WWV', 'WWVH')

    def test_p_wwvh_complements_p_wwv(self):
        d = self._disc()
        feats = d.extract_features(power_ratio_db=3.0, minute=5)
        result = d.classify(feats)
        assert result.p_wwvh == pytest.approx(1.0 - result.p_wwv)

    def test_ground_truth_minute_records_expected_station(self):
        d = self._disc()
        # Minute 29 is WWV-silent → ground truth = WWVH
        feats = d.extract_features(power_ratio_db=-15.0, minute=29)
        result = d.classify(feats)
        assert result.is_ground_truth_minute is True
        assert result.ground_truth_station == 'WWVH'

    def test_predictions_count_increments(self):
        d = self._disc()
        feats = d.extract_features(power_ratio_db=1.0, minute=15)
        d.classify(feats)
        d.classify(feats)
        assert d.predictions_count == 2

    def test_ground_truth_accuracy_tracked(self):
        d = self._disc()
        # Minute 1 is WWVH-only → correct prediction is WWVH
        feats_wwvh = d.extract_features(power_ratio_db=-20.0, minute=1)
        d.classify(feats_wwvh)
        # Minute 8 is WWV-only → correct prediction is WWV
        feats_wwv = d.extract_features(power_ratio_db=+20.0, minute=8)
        d.classify(feats_wwv)
        stats = d.get_statistics()
        assert stats['ground_truth_samples'] == 2
        assert stats['ground_truth_accuracy'] == pytest.approx(1.0)


# =============================================================================
# Auto-train and fit
# =============================================================================


class TestTraining:
    def test_classify_with_auto_train_appends_sample(self):
        d = ProbabilisticDiscriminator(auto_train=True)
        feats = d.extract_features(power_ratio_db=10.0, minute=8)  # WWV-only
        d.classify(feats)
        # Should have queued a training sample with label = 1 (WWV)
        assert len(d.training_buffer) == 1
        sample = d.training_buffer[0]
        assert sample.label == 1
        assert sample.source == "auto_ground_truth"

    def test_auto_train_disabled_does_not_append(self):
        d = ProbabilisticDiscriminator(auto_train=False)
        feats = d.extract_features(power_ratio_db=10.0, minute=8)
        d.classify(feats)
        assert len(d.training_buffer) == 0

    def test_add_training_sample_marks_pending_retrain(self):
        d = ProbabilisticDiscriminator(auto_train=False)
        feats = d.extract_features(power_ratio_db=0.0, minute=15)
        d.add_training_sample(feats, label=1, source='manual')
        assert d._pending_retrain is True
        assert d.training_buffer[-1].source == 'manual'

    def test_fit_returns_false_below_min_samples(self):
        d = ProbabilisticDiscriminator(auto_train=False)
        for _ in range(3):
            d.add_training_sample(
                d.extract_features(power_ratio_db=1.0, minute=15),
                label=1)
        assert d.fit(min_samples=50) is False

    def test_fit_returns_true_above_min_samples(self):
        d = ProbabilisticDiscriminator(auto_train=False)
        # Create enough samples — alternate labels with strong feature signal
        for i in range(60):
            label = 1 if i % 2 == 0 else 0
            f = d.extract_features(power_ratio_db=15.0 if label else -15.0,
                                    minute=15)
            d.add_training_sample(f, label=label)
        assert d.fit(min_samples=50) is True
        assert d.model.is_trained
        assert d._pending_retrain is False


# =============================================================================
# Statistics, weights, importance
# =============================================================================


class TestIntrospection:
    def test_get_statistics_shape(self):
        d = ProbabilisticDiscriminator(auto_train=False)
        stats = d.get_statistics()
        for key in ('predictions_count', 'ground_truth_accuracy',
                    'ground_truth_samples', 'training_buffer_size',
                    'model_trained', 'model_training_samples',
                    'current_weights'):
            assert key in stats
        assert isinstance(stats['current_weights'], dict)
        assert len(stats['current_weights']) == 7

    def test_get_learned_weights_keyed_by_feature_name(self):
        d = ProbabilisticDiscriminator(auto_train=False)
        weights = d.get_learned_weights()
        names = DiscriminationFeatures().feature_names
        assert set(weights) == set(names)

    def test_feature_importance_sums_to_one(self):
        d = ProbabilisticDiscriminator(auto_train=False)
        importance = d.get_feature_importance()
        total = sum(importance.values())
        assert total == pytest.approx(1.0, abs=1e-9)
        # All importances are non-negative
        assert all(v >= 0 for v in importance.values())


# =============================================================================
# Save / load round-trip
# =============================================================================


class TestPersistence:
    def test_save_and_load_round_trip(self, tmp_path):
        path = tmp_path / 'sub' / 'model.json'
        d = ProbabilisticDiscriminator(auto_train=False, model_path=path)
        # Mutate state so we can verify round-trip
        d.predictions_count = 42
        d.correct_on_ground_truth = 30
        d.total_ground_truth = 36
        d.model.weights = np.linspace(-1, 1, 7)
        d.model.bias = 0.25
        d.model.is_trained = True
        d.model.training_samples = 99
        d._save_model()

        # Load into a fresh discriminator
        d2 = ProbabilisticDiscriminator(auto_train=False, model_path=path)
        assert d2.predictions_count == 42
        assert d2.correct_on_ground_truth == 30
        assert d2.total_ground_truth == 36
        assert d2.model.weights.tolist() == d.model.weights.tolist()
        assert d2.model.bias == 0.25
        assert d2.model.is_trained
        assert d2.model.training_samples == 99

    def test_save_with_no_path_is_noop(self, tmp_path):
        # No exception when model_path is None
        d = ProbabilisticDiscriminator(auto_train=False, model_path=None)
        d._save_model()

    def test_load_with_missing_path_is_noop(self, tmp_path):
        # A path that doesn't exist → loader returns silently
        path = tmp_path / 'absent.json'
        d = ProbabilisticDiscriminator(auto_train=False, model_path=path)
        # Should still have default weights
        assert d.model.is_trained is False


# =============================================================================
# Convenience helpers
# =============================================================================


class TestConvenienceFunctions:
    def setup_method(self):
        # Reset module-level singleton before each test
        pd._default_discriminator = None

    def teardown_method(self):
        pd._default_discriminator = None

    def test_get_discriminator_singleton(self):
        a = get_discriminator()
        b = get_discriminator()
        assert a is b

    def test_discriminate_probabilistic_returns_result(self):
        result = discriminate_probabilistic(power_ratio_db=10.0, minute=8)
        assert isinstance(result, ProbabilisticResult)
        assert 0.0 <= result.p_wwv <= 1.0
        assert result.p_wwvh == pytest.approx(1.0 - result.p_wwv)

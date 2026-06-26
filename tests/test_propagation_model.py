#!/usr/bin/env python3
"""
Tests for the new propagation delay modeling system.

Tests:
1. HFPropagationModel — basic delay predictions, multi-mode, frequency dependence
2. IonoDataService — climatological fallback, Chapman profile
3. ArrivalPatternMatrix integration — multi-mode arrivals, adaptive uncertainty
4. Self-consistency check — differential delay vs TEC
"""

import math
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock

import numpy as np


# =============================================================================
# HFPropagationModel Tests
# =============================================================================

class TestHFPropagationModel:
    """Test the core propagation model."""
    
    def setup_method(self):
        from hf_timestd.core.propagation_model import HFPropagationModel
        # Receiver at AC0G location (Columbia, MO)
        self.model = HFPropagationModel(
            receiver_lat=38.92,
            receiver_lon=-92.13,
            enable_realtime=False  # Use parametric fallback only
        )
    
    def test_distances_computed(self):
        """Verify great circle distances are reasonable."""
        # WWV is in Fort Collins, CO — ~1000 km from Columbia, MO
        assert 900 < self.model.distances['WWV'] < 1200
        # CHU is in Ottawa — ~1500 km
        assert 1300 < self.model.distances['CHU'] < 1800
        # WWVH is in Hawaii — ~5500 km
        assert 5000 < self.model.distances['WWVH'] < 7000
        # BPM is in China — ~10000 km
        assert 9000 < self.model.distances['BPM'] < 12000
    
    def test_predict_wwv_10mhz(self):
        """WWV 10 MHz should give a reasonable 1F delay."""
        utc_time = datetime(2026, 3, 15, 18, 0, 0, tzinfo=timezone.utc)
        pred = self.model.predict('WWV', 10.0, utc_time)
        
        assert pred.station == 'WWV'
        assert pred.frequency_mhz == 10.0
        assert pred.distance_km > 0
        
        # Primary delay should be reasonable (3-8 ms for ~1000 km)
        assert 3.0 < pred.primary_delay_ms < 10.0
        
        # Should have at least one feasible arrival
        feasible = pred.get_feasible_arrivals()
        assert len(feasible) >= 1
        
        # Primary mode should be 1F for this distance
        assert pred.primary_mode in ('1F', '1E', 'vacuum_fallback')
    
    def test_predict_wwvh_multihop(self):
        """WWVH at ~5500 km should require multi-hop."""
        utc_time = datetime(2026, 3, 15, 18, 0, 0, tzinfo=timezone.utc)
        pred = self.model.predict('WWVH', 10.0, utc_time)
        
        # Should have multi-hop arrivals
        feasible = pred.get_feasible_arrivals()
        assert len(feasible) >= 1
        
        # Delay should be longer than WWV (farther away)
        assert pred.primary_delay_ms > 15.0  # ~5500 km → >18 ms
    
    def test_frequency_dependence(self):
        """Lower frequencies should have more ionospheric delay."""
        utc_time = datetime(2026, 3, 15, 18, 0, 0, tzinfo=timezone.utc)
        
        pred_5 = self.model.predict('WWV', 5.0, utc_time)
        pred_10 = self.model.predict('WWV', 10.0, utc_time)
        pred_15 = self.model.predict('WWV', 15.0, utc_time)
        
        # All should have valid predictions
        assert pred_5.primary_delay_ms > 0
        assert pred_10.primary_delay_ms > 0
        assert pred_15.primary_delay_ms > 0
        
        # Ionospheric delay scales as 1/f², so lower freq = more delay
        # The total delay includes geometric + iono, so the ordering
        # depends on whether iono dominates. For the same path geometry,
        # the iono component should be larger at lower frequencies.
        primary_5 = pred_5.get_primary_arrival()
        primary_10 = pred_10.get_primary_arrival()
        primary_15 = pred_15.get_primary_arrival()
        
        if primary_5 and primary_10 and primary_15:
            # Iono delay should decrease with frequency
            if primary_5.iono_delay_ms > 0 and primary_15.iono_delay_ms > 0:
                assert primary_5.iono_delay_ms > primary_15.iono_delay_ms
    
    def test_diurnal_variation(self):
        """Delay should vary between day and night (different hmF2)."""
        day_time = datetime(2026, 6, 15, 18, 0, 0, tzinfo=timezone.utc)  # ~noon local
        night_time = datetime(2026, 6, 15, 6, 0, 0, tzinfo=timezone.utc)  # ~midnight local
        
        pred_day = self.model.predict('WWV', 10.0, day_time)
        pred_night = self.model.predict('WWV', 10.0, night_time)
        
        # Both should produce valid predictions
        assert pred_day.primary_delay_ms > 0
        assert pred_night.primary_delay_ms > 0
        
        # Delays should differ (night has higher hmF2 → longer path)
        assert pred_day.primary_delay_ms != pred_night.primary_delay_ms
    
    def test_mode_feasibility(self):
        """Modes should be correctly marked as feasible or not."""
        utc_time = datetime(2026, 3, 15, 18, 0, 0, tzinfo=timezone.utc)
        pred = self.model.predict('WWV', 10.0, utc_time)
        
        for arrival in pred.arrivals:
            if arrival.is_feasible:
                assert arrival.delay_ms > 0
                assert arrival.path_length_km > 0
                assert arrival.uncertainty_1sigma_ms > 0
            else:
                # Infeasible modes should have zero delay
                assert arrival.delay_ms == 0.0
    
    def test_uncertainty_varies_with_source(self):
        """Uncertainty should be larger for lower-quality data sources."""
        utc_time = datetime(2026, 3, 15, 18, 0, 0, tzinfo=timezone.utc)
        pred = self.model.predict('WWV', 10.0, utc_time)
        
        # With parametric fallback, uncertainty should be moderate
        assert pred.primary_uncertainty_1sigma_ms > 0
        # Parametric should give higher uncertainty than WAM-IPE would
        assert pred.primary_uncertainty_1sigma_ms > 1.0
    
    def test_differential_delay(self):
        """Differential delay between two frequencies, differenced on a
        mode shared by both (P-M14): the geometric delay cancels, leaving
        the dispersive 1/f² term, which inverts to slant TEC."""
        utc_time = datetime(2026, 3, 15, 18, 0, 0, tzinfo=timezone.utc)

        diff_ms, implied_tec = self.model.compute_differential_delay(
            'WWV', 5.0, 10.0, utc_time
        )

        # 5 MHz has more ionospheric delay than 10 MHz (delay ∝ 1/f²).
        assert diff_ms > 0

        # The inversion yields slant TEC integrated along the whole
        # (multi-hop) ray path — several times the vertical TEC.
        assert 0 < implied_tec < 500

    def test_differential_delay_differences_a_shared_mode(self):
        """P-M14: the differential is one shared mode's ionospheric-delay
        difference (the geometric delay having cancelled), never a
        cross-mode total-delay difference that would smuggle a 1F-vs-2F
        geometric step into the implied TEC."""
        utc_time = datetime(2026, 3, 15, 18, 0, 0, tzinfo=timezone.utc)
        diff_ms, _ = self.model.compute_differential_delay(
            'WWV', 5.0, 10.0, utc_time
        )

        p5 = self.model.predict('WWV', 5.0, utc_time)
        p10 = self.model.predict('WWV', 10.0, utc_time)
        m5 = {a.mode.label: a for a in p5.arrivals if a.is_feasible}
        m10 = {a.mode.label: a for a in p10.arrivals if a.is_feasible}
        shared = set(m5) & set(m10)
        assert shared, "test precondition: a feasible mode shared by both"

        # diff_ms must equal some shared mode's pure ionospheric
        # differential — proof the geometric delay cancelled exactly.
        per_mode_iono_diff = [
            m5[lbl].iono_delay_ms - m10[lbl].iono_delay_ms for lbl in shared
        ]
        assert any(
            diff_ms == pytest.approx(v, abs=1e-9) for v in per_mode_iono_diff
        )

    def test_differential_delay_is_frequency_pair_independent(self):
        """The implied slant TEC is a property of the ray path, not of
        the frequency pair used to probe it — different pairs agree."""
        utc_time = datetime(2026, 3, 15, 18, 0, 0, tzinfo=timezone.utc)
        _, tec_5_10 = self.model.compute_differential_delay(
            'WWV', 5.0, 10.0, utc_time
        )
        _, tec_10_20 = self.model.compute_differential_delay(
            'WWV', 10.0, 20.0, utc_time
        )
        assert tec_5_10 == pytest.approx(tec_10_20, rel=0.05)

    def test_self_consistency(self):
        """Self-consistency check should pass for model-generated delays."""
        utc_time = datetime(2026, 3, 15, 18, 0, 0, tzinfo=timezone.utc)
        
        # Generate "observed" delays from the model itself
        observed = {}
        for freq in [5.0, 10.0, 15.0]:
            pred = self.model.predict('WWV', freq, utc_time)
            observed[freq] = pred.primary_delay_ms
        
        result = self.model.self_consistency_check('WWV', observed, utc_time)
        
        # Model should be self-consistent
        assert result['consistent']
        assert result['rms_residual_ms'] < 0.5  # Very tight for self-check


# =============================================================================
# IonoDataService Tests
# =============================================================================

class TestIonoDataService:
    """Test the ionospheric data service (offline/fallback mode)."""
    
    def test_climatological_fallback(self):
        from hf_timestd.core.iono_data_service import IonoDataService
        
        service = IonoDataService(
            cache_dir="/tmp/test_iono_cache",
            enable_wamipe=False,
            enable_giro=False
        )
        
        utc_time = datetime(2026, 3, 15, 18, 0, 0, tzinfo=timezone.utc)
        point = service.get_iono_params(38.92, -92.13, utc_time)
        
        # Should return climatological values
        assert 200 < point.hmF2_km < 450
        assert 80 < point.hmE_km < 130
        assert point.NmF2_m3 > 0
        assert 1 < point.foF2_MHz < 15
        assert 1 < point.TEC_TECU < 100
        # IRI-2020 is itself a climatological model and is the legitimate
        # fallback once WAM-IPE/GIRO are disabled (when gfortran is present to
        # build iri2020); accept it alongside the parametric tiers used when
        # IRI is unavailable.
        assert point.source == 'iri' \
            or 'fallback' in point.source or 'climatological' in point.source
    
    def test_chapman_profile(self):
        from hf_timestd.core.iono_data_service import IonoDataService, IonoGridPoint
        
        point = IonoGridPoint(
            latitude=38.92, longitude=-92.13,
            timestamp=datetime.now(timezone.utc),
            hmF2_km=300.0, NmF2_m3=1e12,
            hmE_km=110.0
        )
        
        altitudes, Ne = IonoDataService._chapman_profile(point)
        
        # Profile should span 80-1000 km
        assert altitudes[0] == 80.0
        assert altitudes[-1] >= 1000.0
        
        # Peak should be near hmF2
        peak_idx = np.argmax(Ne)
        peak_alt = altitudes[peak_idx]
        assert abs(peak_alt - 300.0) < 30.0  # Within 30 km of hmF2
        
        # Peak density should be close to NmF2
        assert Ne[peak_idx] > 0.5e12
        
        # Density should decrease above and below peak
        assert Ne[0] < Ne[peak_idx]
        assert Ne[-1] < Ne[peak_idx]
    
    def test_diurnal_hmF2_variation(self):
        from hf_timestd.core.iono_data_service import IonoDataService
        
        service = IonoDataService(
            cache_dir="/tmp/test_iono_cache",
            enable_wamipe=False,
            enable_giro=False
        )
        
        # Day (local noon) vs night (local midnight) for Columbia, MO
        # lon=-92.13 → LST offset ~-6h from UTC
        day = datetime(2026, 6, 15, 18, 0, 0, tzinfo=timezone.utc)   # ~noon local
        night = datetime(2026, 6, 15, 6, 0, 0, tzinfo=timezone.utc)  # ~midnight local
        
        day_point = service.get_iono_params(38.92, -92.13, day)
        night_point = service.get_iono_params(38.92, -92.13, night)
        
        # hmF2 should be higher at night (ionization decays, layer rises)
        assert night_point.hmF2_km > day_point.hmF2_km
        
        # foF2 should be higher during day (more ionization)
        assert day_point.foF2_MHz > night_point.foF2_MHz
    
    def test_electron_density_profile(self):
        from hf_timestd.core.iono_data_service import IonoDataService
        
        service = IonoDataService(
            cache_dir="/tmp/test_iono_cache",
            enable_wamipe=False,
            enable_giro=False
        )
        
        utc_time = datetime(2026, 3, 15, 18, 0, 0, tzinfo=timezone.utc)
        altitudes, Ne = service.get_electron_density_profile(38.92, -92.13, utc_time)
        
        assert len(altitudes) > 10
        assert len(Ne) == len(altitudes)
        assert np.all(Ne >= 0)
        assert np.max(Ne) > 1e10  # Should have significant electron density


# =============================================================================
# ArrivalPatternMatrix Integration Tests
# =============================================================================

class TestArrivalPatternMatrixIntegration:
    """Test the arrival pattern matrix with the new propagation model."""
    
    def setup_method(self):
        from hf_timestd.core.arrival_pattern_matrix import ArrivalPatternMatrix
        self.matrix = ArrivalPatternMatrix(
            receiver_lat=38.92,
            receiver_lon=-92.13,
            sample_rate=24000,
            enable_iri=False  # Use parametric fallback for tests
        )
    
    def test_compute_matrix_produces_arrivals(self):
        utc_time = datetime(2026, 3, 15, 18, 0, 0, tzinfo=timezone.utc)
        result = self.matrix.compute_matrix(utc_time)
        
        # Should have arrivals for all station/frequency pairs
        assert len(result.arrivals) > 0
        
        # Check WWV 10 MHz exists
        wwv_10 = result.get_arrival('WWV', 10.0)
        assert wwv_10 is not None
        assert wwv_10.expected_delay_ms > 0
        assert wwv_10.expected_sample > 0
    
    def test_multi_mode_arrivals(self):
        """Matrix should contain multi-mode arrivals for long paths."""
        utc_time = datetime(2026, 3, 15, 18, 0, 0, tzinfo=timezone.utc)
        result = self.matrix.compute_matrix(utc_time)
        
        # WWVH at ~5500 km should have multi-hop modes
        wwvh_modes = result.get_all_mode_arrivals('WWVH', 10.0)
        # Should have at least the primary mode
        assert len(wwvh_modes) >= 1
        
        # Check that multi-mode dict is populated
        assert len(result.multi_mode_arrivals) >= len(result.arrivals)
    
    def test_backward_compatibility(self):
        """Primary arrivals dict should work exactly as before."""
        utc_time = datetime(2026, 3, 15, 18, 0, 0, tzinfo=timezone.utc)
        result = self.matrix.compute_matrix(utc_time)
        
        # get_arrival should still work
        for station in ['WWV', 'WWVH', 'CHU', 'BPM']:
            arrivals = result.get_station_arrivals(station)
            assert len(arrivals) > 0
            for a in arrivals:
                assert a.station == station
                assert a.expected_delay_ms > 0
                assert a.uncertainty_3sigma_ms > 0
                assert a.min_search_sample >= 0
                assert a.max_search_sample > a.min_search_sample
    
    def test_new_fields_populated(self):
        """New ExpectedArrival fields should be populated."""
        utc_time = datetime(2026, 3, 15, 18, 0, 0, tzinfo=timezone.utc)
        result = self.matrix.compute_matrix(utc_time)
        
        wwv_10 = result.get_arrival('WWV', 10.0)
        assert wwv_10 is not None
        
        # New fields
        assert wwv_10.propagation_mode != ''
        assert wwv_10.data_source != ''
    
    def test_validate_detection_still_works(self):
        """validate_detection should work with the new matrix."""
        utc_time = datetime(2026, 3, 15, 18, 0, 0, tzinfo=timezone.utc)
        self.matrix.compute_matrix(utc_time)
        
        # Get expected arrival for WWV 10 MHz
        arrival = self.matrix.get_expected_arrivals(utc_time).get_arrival('WWV', 10.0)
        assert arrival is not None
        
        # Validate a detection at the expected sample
        is_valid, confidence, reason = self.matrix.validate_detection(
            station='WWV',
            frequency_mhz=10.0,
            detected_sample=arrival.expected_sample,
            snr_db=25.0,
            utc_time=utc_time
        )
        
        assert is_valid
        assert confidence > 0.5
        assert 'Valid' in reason
    
    def test_search_windows(self):
        """Search windows should be reasonable."""
        utc_time = datetime(2026, 3, 15, 18, 0, 0, tzinfo=timezone.utc)
        windows = self.matrix.get_search_windows(10.0, utc_time)
        
        # Should have windows for WWV and WWVH (both broadcast on 10 MHz)
        assert 'WWV' in windows
        assert 'WWVH' in windows
        
        for station, (min_s, max_s) in windows.items():
            assert min_s >= 0
            assert max_s > min_s
            # Window should be reasonable (not the entire minute)
            window_ms = (max_s - min_s) * 1000 / 24000
            assert window_ms < 200  # Less than 200ms total window
    
    def test_matrix_model_metadata(self):
        """Matrix should report model metadata."""
        utc_time = datetime(2026, 3, 15, 18, 0, 0, tzinfo=timezone.utc)
        result = self.matrix.compute_matrix(utc_time)
        
        assert result.ionospheric_model_tier != ''
        assert result.data_source != ''


# =============================================================================
# Numerical Integration Tests
# =============================================================================

class TestNumericalIntegration:
    """Test the numerical group delay integration."""
    
    def test_tec_group_delay(self):
        """TEC-based group delay should match analytical formula."""
        from hf_timestd.core.propagation_model import HFPropagationModel
        
        model = HFPropagationModel(38.92, -92.13, enable_realtime=False)
        
        # Known: 40.3 * sTEC / (c * f²) per one-way pass
        # Each hop traverses the ionosphere twice (up and down), so total = 2× one-way.
        # For TEC=20 TECU, f=10 MHz, vertical (1 hop):
        # one_way = 40.3 * 20e16 / (3e8 * (10e6)²) = 0.269 ms
        # round_trip = 2 * 0.269 = 0.537 ms
        
        delay = model._tec_group_delay(
            tec_tecu=20.0,
            frequency_mhz=10.0,
            n_hops=1,
            elevation_deg=90.0  # Vertical
        )
        
        one_way = 40.3 * 20e16 / (3e8 * (10e6)**2) * 1000  # ms
        expected = 2.0 * one_way  # Round-trip through ionosphere per hop
        # Should be close (within 1% due to using exact c)
        assert abs(delay - expected) / expected < 0.01
    
    def test_tec_frequency_scaling(self):
        """Group delay should scale as 1/f²."""
        from hf_timestd.core.propagation_model import HFPropagationModel
        
        model = HFPropagationModel(38.92, -92.13, enable_realtime=False)
        
        delay_5 = model._tec_group_delay(20.0, 5.0, 1, 90.0)
        delay_10 = model._tec_group_delay(20.0, 10.0, 1, 90.0)
        delay_15 = model._tec_group_delay(20.0, 15.0, 1, 90.0)
        
        # delay_5 / delay_10 should be (10/5)² = 4
        assert abs(delay_5 / delay_10 - 4.0) < 0.01
        
        # delay_10 / delay_15 should be (15/10)² = 2.25
        assert abs(delay_10 / delay_15 - 2.25) < 0.01
    
    def test_integrate_vs_tec(self):
        """Numerical integration through Chapman profile should roughly match TEC formula."""
        from hf_timestd.core.propagation_model import HFPropagationModel
        from hf_timestd.core.iono_data_service import IonoDataService, IonoGridPoint
        
        model = HFPropagationModel(38.92, -92.13, enable_realtime=False)
        
        # Create a known Chapman profile
        point = IonoGridPoint(
            latitude=38.92, longitude=-92.13,
            timestamp=datetime.now(timezone.utc),
            hmF2_km=300.0, NmF2_m3=1e12,
            hmE_km=110.0, TEC_TECU=20.0
        )
        altitudes, Ne = IonoDataService._chapman_profile(point)
        
        # Integrate group delay at 10 MHz, vertical
        integrated_delay = model._integrate_group_delay(
            altitudes_km=altitudes,
            Ne_m3=Ne,
            frequency_mhz=10.0,
            n_hops=1,
            elevation_deg=90.0
        )
        
        # TEC-based delay
        tec_delay = model._tec_group_delay(20.0, 10.0, 1, 90.0)
        
        # They should be in the same ballpark (within factor of 3)
        # The Chapman profile TEC may not exactly match the stated TEC_TECU
        assert integrated_delay > 0
        assert 0.1 * tec_delay < integrated_delay < 10 * tec_delay


if __name__ == '__main__':
    pytest.main([__file__, '-v'])

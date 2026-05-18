import unittest
import logging
import math
import numpy as np
from pathlib import Path
from hf_timestd.core.tec_estimator import TECEstimator, TECResult, K_IONOSPHERE, TECU_SCALE


class TestTECEstimatorDiagnostics(unittest.TestCase):
    def setUp(self):
        self.estimator = TECEstimator()

    def test_flat_data_zero_tec(self):
        """Flat data (same ToA for all frequencies) should give ~0 TEC."""
        measurements = [
            {'frequency_hz': 5e6, 'toa_ms': 1000.0, 'uncertainty_ms': 0.1},
            {'frequency_hz': 10e6, 'toa_ms': 1000.0, 'uncertainty_ms': 0.1},
            {'frequency_hz': 15e6, 'toa_ms': 1000.0, 'uncertainty_ms': 0.1},
        ]
        result = self.estimator.estimate_tec(measurements, "TEST", 0.0)
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result.tec_u, 0.0, delta=0.01)
        self.assertAlmostEqual(result.confidence, 0.0)

    def test_negative_slope_retained(self):
        """Negative slope is RETAINED with zero confidence, not rejected.

        CR-2 (settled 2026-05-17, see DATA_CONTRACT.md): a negative TEC
        estimate is a normal noisy realisation for a noise-dominated signal;
        discarding it censors the estimator and biases aggregates high.
        """
        # 5 MHz: 1000ms, 10 MHz: 1010ms — higher freq arrives later, so the
        # 1/f² slope (hence the TEC estimate) is negative.
        measurements = [
            {'frequency_hz': 5e6, 'toa_ms': 1000.0, 'uncertainty_ms': 0.1},
            {'frequency_hz': 10e6, 'toa_ms': 1010.0, 'uncertainty_ms': 0.1},
        ]
        with self.assertLogs('hf_timestd.core.tec_estimator', level='WARNING'):
            result = self.estimator.estimate_tec(measurements, "TEST", 0.0)
        self.assertIsNotNone(result, "Negative slope must be retained, not discarded")
        self.assertLess(result.tec_u, 0.0, "Negative slope should yield negative tec_u")
        self.assertEqual(result.confidence, 0.0,
                         "Negative-slope result must carry zero confidence")

    def test_n2_confidence_capped(self):
        """With N=2 frequencies, confidence must be capped at 0.3."""
        # Create physically valid data: lower freq has higher delay
        # TEC = 20 TECU → delay(5MHz) = K*TEC*TECU_SCALE / (5e6)^2 in seconds
        tec_el_m2 = 20.0 * TECU_SCALE
        delay_5mhz_s = K_IONOSPHERE * tec_el_m2 / (5e6)**2
        delay_10mhz_s = K_IONOSPHERE * tec_el_m2 / (10e6)**2
        t_vacuum_ms = 5.0

        measurements = [
            {'frequency_hz': 5e6, 'toa_ms': t_vacuum_ms + delay_5mhz_s * 1000, 'uncertainty_ms': 0.1},
            {'frequency_hz': 10e6, 'toa_ms': t_vacuum_ms + delay_10mhz_s * 1000, 'uncertainty_ms': 0.1},
        ]
        result = self.estimator.estimate_tec(measurements, "TEST", 0.0)
        self.assertIsNotNone(result)
        self.assertLessEqual(result.confidence, 0.3,
                             f"N=2 confidence should be capped at 0.3, got {result.confidence}")
        self.assertAlmostEqual(result.tec_u, 20.0, delta=0.5)

    def test_n3_good_fit_high_confidence(self):
        """With N>=3 and good 1/f² fit, confidence should be high."""
        tec_el_m2 = 25.0 * TECU_SCALE
        t_vacuum_ms = 5.0
        freqs = [5e6, 10e6, 15e6, 20e6]

        measurements = []
        for f in freqs:
            delay_s = K_IONOSPHERE * tec_el_m2 / (f ** 2)
            measurements.append({
                'frequency_hz': f,
                'toa_ms': t_vacuum_ms + delay_s * 1000,
                'uncertainty_ms': 0.1,
            })

        result = self.estimator.estimate_tec(measurements, "TEST", 0.0)
        self.assertIsNotNone(result)
        self.assertGreater(result.confidence, 0.9)
        self.assertAlmostEqual(result.tec_u, 25.0, delta=0.5)
        self.assertEqual(result.n_frequencies, 4)

    def test_outlier_rejection(self):
        """One mode-mixed measurement should be rejected as 3σ outlier."""
        tec_el_m2 = 20.0 * TECU_SCALE
        t_vacuum_ms = 5.0
        # Use 8 frequencies so the clean majority dominates the fit and
        # the MAD-based outlier detection can identify the corrupted point.
        freqs = [3.33e6, 5e6, 7.85e6, 10e6, 14.67e6, 15e6, 20e6, 25e6]

        measurements = []
        for f in freqs:
            delay_s = K_IONOSPHERE * tec_el_m2 / (f ** 2)
            toa = t_vacuum_ms + delay_s * 1000
            measurements.append({
                'frequency_hz': f,
                'toa_ms': toa,
                'uncertainty_ms': 0.1,
            })

        # Corrupt the 10 MHz measurement by 2ms (simulate 2F mode mixing).
        # With 8 frequencies, the 7 clean points dominate the fit and the
        # corrupted point's residual (1.7ms) exceeds the MAD-based 3σ threshold.
        measurements[3]['toa_ms'] += 2.0

        result = self.estimator.estimate_tec(measurements, "TEST", 0.0)
        self.assertIsNotNone(result, "Should produce a result after rejecting the outlier")
        self.assertGreater(result.n_rejected, 0, "Should have rejected the outlier")
        self.assertEqual(result.rejection_reason, 'outlier_3sigma')
        # TEC should still be close to 20 TECU after rejection
        self.assertAlmostEqual(result.tec_u, 20.0, delta=3.0)

    def test_propagation_mode_field_exists(self):
        """TECResult should have propagation_mode field."""
        tec_el_m2 = 15.0 * TECU_SCALE
        t_vacuum_ms = 5.0
        measurements = [
            {'frequency_hz': 5e6, 'toa_ms': t_vacuum_ms + K_IONOSPHERE * tec_el_m2 / (5e6)**2 * 1000, 'uncertainty_ms': 0.1},
            {'frequency_hz': 10e6, 'toa_ms': t_vacuum_ms + K_IONOSPHERE * tec_el_m2 / (10e6)**2 * 1000, 'uncertainty_ms': 0.1},
            {'frequency_hz': 15e6, 'toa_ms': t_vacuum_ms + K_IONOSPHERE * tec_el_m2 / (15e6)**2 * 1000, 'uncertainty_ms': 0.1},
        ]
        result = self.estimator.estimate_tec(measurements, "TEST", 0.0)
        self.assertIsNotNone(result)
        self.assertTrue(hasattr(result, 'propagation_mode'))
        self.assertEqual(result.propagation_mode, 'UNKNOWN')

    def test_snr_weighting(self):
        """SNR-based weighting should influence the fit."""
        tec_el_m2 = 20.0 * TECU_SCALE
        t_vacuum_ms = 5.0
        measurements = []
        for f in [5e6, 10e6, 15e6]:
            delay_s = K_IONOSPHERE * tec_el_m2 / (f ** 2)
            measurements.append({
                'frequency_hz': f,
                'toa_ms': t_vacuum_ms + delay_s * 1000,
                'uncertainty_ms': 0.5,
                'snr_db': 30.0,
                'mode_confidence': 0.9,
            })
        result = self.estimator.estimate_tec(measurements, "TEST", 0.0)
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result.tec_u, 20.0, delta=0.5)

    def test_insufficient_frequencies(self):
        """Single frequency should return None."""
        result = self.estimator.estimate_tec(
            [{'frequency_hz': 5e6, 'toa_ms': 1000.0}], "TEST", 0.0
        )
        self.assertIsNone(result)

    def test_no_high_precision_mode_param(self):
        """TECEstimator constructor should not accept high_precision_mode."""
        # The old dead parameter should be removed
        import inspect
        sig = inspect.signature(TECEstimator.__init__)
        self.assertNotIn('high_precision_mode', sig.parameters)


class TestCarrierTEC(unittest.TestCase):
    """Tests for carrier-phase differential TEC module."""

    def test_smooth_diurnal_dtec(self):
        """Smooth sinusoidal phase should produce smooth dTEC."""
        from hf_timestd.core.carrier_tec import CarrierTECEstimator

        estimator = CarrierTECEstimator()

        # Simulate 1 hour of carrier phase at 14.67 MHz
        # Phase changes due to TEC variation: φ = -(2π/c) · (40.3/f) · sTEC
        freq_mhz = 14.67
        freq_hz = freq_mhz * 1e6
        n_points = 3600  # 1 per second
        epochs = np.arange(n_points, dtype=float)

        # Simulate sinusoidal TEC variation: 20 + 5·sin(2π·t/3600) TECU
        tec_tecu = 20.0 + 5.0 * np.sin(2 * np.pi * epochs / 3600)
        tec_el_m2 = tec_tecu * 1e16

        # Phase from TEC: φ = -(2π/c) · (40.3/f) · TEC
        phase_rad = -(2 * np.pi / 299792458.0) * (40.3 / freq_hz) * tec_el_m2

        result = estimator.compute_dtec_from_phase(
            epochs=epochs,
            carrier_phase_rad=phase_rad,
            frequency_mhz=freq_mhz,
            station='CHU',
            channel='CHU_14670',
        )

        self.assertIsNotNone(result)
        self.assertGreater(result.n_points, 100)

        # The integrated dTEC should show the sinusoidal variation
        dtec = np.array(result.dtec_tecu)
        # Check that the range is approximately 10 TECU (peak-to-peak of 5*sin)
        dtec_range = np.ptp(dtec)
        self.assertGreater(dtec_range, 2.0, "dTEC range should reflect TEC variation")

    def test_anchor_to_absolute(self):
        """Anchoring should shift dTEC to match absolute TEC."""
        from hf_timestd.core.carrier_tec import CarrierTECEstimator

        estimator = CarrierTECEstimator()
        epochs = np.arange(100, dtype=float)
        # Constant phase = zero dTEC
        phase_rad = np.ones(100) * 1.5

        result = estimator.compute_dtec_from_phase(
            epochs=epochs,
            carrier_phase_rad=phase_rad,
            frequency_mhz=10.0,
            anchor_tec_tecu=25.0,
            anchor_epoch=50.0,
        )

        self.assertIsNotNone(result)
        self.assertTrue(result.is_anchored)
        # All dTEC values should be near 25.0 (constant phase = zero rate)
        dtec = np.array(result.dtec_tecu)
        self.assertAlmostEqual(np.mean(dtec), 25.0, delta=1.0)


class TestIonoTomography(unittest.TestCase):
    """Tests for multi-layer E/F tomographic separation."""

    def test_e_layer_vanishes_at_night(self):
        """At night, E-layer TEC should be near zero."""
        from hf_timestd.core.iono_tomography import IonoTomography, RayPath

        tomo = IonoTomography()

        # Create paths with different elevations
        paths = [
            RayPath('WWV', 5.0, 15.0, 270.0, 2500.0, '1F', 1, 20.0, 2.0),
            RayPath('WWV', 10.0, 20.0, 270.0, 2500.0, '1F', 1, 18.0, 2.0),
            RayPath('CHU', 7.85, 35.0, 10.0, 1500.0, '1F', 1, 15.0, 1.5),
            RayPath('CHU', 14.67, 40.0, 10.0, 1500.0, '1F', 1, 14.0, 1.5),
        ]

        # Nighttime: solar elevation < 0
        result = tomo.solve(paths, solar_elevation_deg=-20.0)
        self.assertIsNotNone(result)
        self.assertLess(result.tec_e_tecu, 2.0,
                        f"E-layer TEC should be near zero at night, got {result.tec_e_tecu}")
        self.assertFalse(result.is_daytime)

    def test_daytime_e_f_separation(self):
        """Daytime should show both E and F layer contributions."""
        from hf_timestd.core.iono_tomography import IonoTomography, RayPath

        tomo = IonoTomography()

        # Low-elevation paths see more E-layer
        # High-elevation paths see proportionally more F-layer
        paths = [
            RayPath('BPM', 5.0, 8.0, 300.0, 8000.0, '1F', 1, 45.0, 3.0),
            RayPath('BPM', 10.0, 10.0, 300.0, 8000.0, '1F', 1, 40.0, 3.0),
            RayPath('WWV', 10.0, 25.0, 270.0, 2500.0, '1F', 1, 25.0, 2.0),
            RayPath('CHU', 7.85, 35.0, 10.0, 1500.0, '1F', 1, 20.0, 1.5),
            RayPath('CHU', 14.67, 45.0, 10.0, 1500.0, '1F', 1, 18.0, 1.5),
        ]

        result = tomo.solve(paths, solar_elevation_deg=45.0)
        self.assertIsNotNone(result)
        self.assertTrue(result.is_daytime)
        self.assertGreater(result.tec_f_tecu, result.tec_e_tecu,
                          "F-layer should dominate over E-layer")
        self.assertGreater(result.tec_total_tecu, 0)

    def test_insufficient_paths(self):
        """Less than 2 paths should return None."""
        from hf_timestd.core.iono_tomography import IonoTomography, RayPath

        tomo = IonoTomography()
        result = tomo.solve([RayPath('WWV', 10.0, 30.0, 0.0, 2500.0, '1F', 1, 20.0, 2.0)])
        self.assertIsNone(result)


class TestVTECMapper(unittest.TestCase):
    """Tests for VTEC map generation."""

    def test_stec_to_vtec_zenith(self):
        """At zenith (90° elevation), vTEC = sTEC."""
        from hf_timestd.core.vtec_mapper import VTECMapper

        mapper = VTECMapper()
        vtec, mf = mapper.stec_to_vtec(20.0, 90.0)
        self.assertAlmostEqual(vtec, 20.0, delta=0.01)
        self.assertAlmostEqual(mf, 1.0, delta=0.01)

    def test_stec_to_vtec_oblique(self):
        """At low elevation, vTEC < sTEC (mapping factor > 1)."""
        from hf_timestd.core.vtec_mapper import VTECMapper

        mapper = VTECMapper()
        vtec, mf = mapper.stec_to_vtec(20.0, 30.0)
        self.assertLess(vtec, 20.0)
        self.assertGreater(mf, 1.0)

    def test_generate_map_basic(self):
        """Generate a VTEC map from synthetic IPP measurements."""
        from hf_timestd.core.vtec_mapper import VTECMapper, IPPMeasurement

        mapper = VTECMapper(grid_extent_deg=5.0, grid_resolution_deg=2.0)

        # Create synthetic IPP measurements spread around the receiver
        measurements = [
            IPPMeasurement('WWV', 10.0, 37.0, -95.0, 20.0, 18.0, 1.1, 25.0, 2.0),
            IPPMeasurement('WWV', 15.0, 38.0, -93.0, 22.0, 20.0, 1.1, 30.0, 2.0),
            IPPMeasurement('CHU', 7.85, 42.0, -88.0, 18.0, 16.0, 1.1, 35.0, 1.5),
            IPPMeasurement('CHU', 14.67, 41.0, -89.0, 19.0, 17.0, 1.1, 40.0, 1.5),
            IPPMeasurement('BPM', 10.0, 35.0, -98.0, 30.0, 22.0, 1.4, 10.0, 3.0),
        ]

        result = mapper.generate_map(measurements, timestamp=1234567890.0)
        self.assertIsNotNone(result)
        self.assertEqual(result.n_ipps, 5)
        self.assertGreater(result.confidence, 0)
        self.assertGreater(len(result.grid_vtec), 0)
        self.assertGreater(len(result.grid_lats), 0)

    def test_ionex_output(self):
        """IONEX file should be writable."""
        import tempfile
        from hf_timestd.core.vtec_mapper import VTECMapper, IPPMeasurement

        mapper = VTECMapper(grid_extent_deg=3.0, grid_resolution_deg=1.0)
        measurements = [
            IPPMeasurement('WWV', 10.0, 37.0, -95.0, 20.0, 18.0, 1.1, 25.0, 2.0),
            IPPMeasurement('CHU', 7.85, 42.0, -88.0, 18.0, 16.0, 1.1, 35.0, 1.5),
            IPPMeasurement('CHU', 14.67, 41.0, -89.0, 19.0, 17.0, 1.1, 40.0, 1.5),
        ]
        result = mapper.generate_map(measurements, timestamp=1234567890.0)
        self.assertIsNotNone(result)

        with tempfile.NamedTemporaryFile(suffix='.ionex', delete=True) as f:
            success = mapper.write_ionex(result, Path(f.name))
            self.assertTrue(success)

    def test_insufficient_ipps(self):
        """Less than 3 IPPs should return None."""
        from hf_timestd.core.vtec_mapper import VTECMapper, IPPMeasurement

        mapper = VTECMapper()
        result = mapper.generate_map([
            IPPMeasurement('WWV', 10.0, 37.0, -95.0, 20.0, 18.0, 1.1, 25.0, 2.0),
        ])
        self.assertIsNone(result)


if __name__ == '__main__':
    logging.basicConfig(level=logging.DEBUG)
    unittest.main()

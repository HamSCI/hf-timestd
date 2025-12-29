#!/usr/bin/env python3
"""
Tests for signal_templates.py - BCD, AFSK, and BPM modulation pattern templates.
"""

import unittest
import numpy as np
from datetime import datetime
from hf_timestd.core import (
    BCDTemplateGenerator,
    BCDCorrelationResult,
    CHUAFSKTemplateGenerator,
    AFSKCorrelationResult,
    BPMTemplateGenerator,
    BPMCorrelationResult,
    SignalTemplateCorrelator,
    create_bcd_generator,
    create_afsk_generator,
    create_bpm_generator,
    create_correlator,
)


class TestBCDTemplateGenerator(unittest.TestCase):
    """Test BCD 100 Hz template generation for WWV/WWVH"""
    
    def setUp(self):
        self.sample_rate = 20000
        self.generator = create_bcd_generator(self.sample_rate)
    
    def test_second_template_length(self):
        """Each second template should be exactly 1 second"""
        template = self.generator.generate_second_template(bit_value=0)
        self.assertEqual(len(template), self.sample_rate)
    
    def test_marker_pulse_width(self):
        """Marker pulses should be 800ms HIGH"""
        template = self.generator.generate_second_template(
            bit_value=0, is_marker=True, with_carrier=False
        )
        # Count samples above threshold (HIGH level)
        high_amp = 10.0 ** (-6.0 / 20.0)
        high_samples = np.sum(template > high_amp * 0.5)
        expected_high = int(0.8 * self.sample_rate)
        self.assertAlmostEqual(high_samples, expected_high, delta=10)
    
    def test_binary_one_pulse_width(self):
        """Binary 1 pulses should be 500ms HIGH"""
        template = self.generator.generate_second_template(
            bit_value=1, is_marker=False, with_carrier=False
        )
        high_amp = 10.0 ** (-6.0 / 20.0)
        high_samples = np.sum(template > high_amp * 0.5)
        expected_high = int(0.5 * self.sample_rate)
        self.assertAlmostEqual(high_samples, expected_high, delta=10)
    
    def test_binary_zero_pulse_width(self):
        """Binary 0 pulses should be 200ms HIGH"""
        template = self.generator.generate_second_template(
            bit_value=0, is_marker=False, with_carrier=False
        )
        high_amp = 10.0 ** (-6.0 / 20.0)
        high_samples = np.sum(template > high_amp * 0.5)
        expected_high = int(0.2 * self.sample_rate)
        self.assertAlmostEqual(high_samples, expected_high, delta=10)
    
    def test_minute_template_length(self):
        """Full minute template should be 60 seconds"""
        timestamp = datetime(2025, 12, 16, 12, 30, 0).timestamp()
        template = self.generator.generate_minute_template(timestamp)
        self.assertEqual(len(template), 60 * self.sample_rate)
    
    def test_minute_template_normalized(self):
        """Minute template should be normalized to unit energy"""
        timestamp = datetime(2025, 12, 16, 12, 30, 0).timestamp()
        template = self.generator.generate_minute_template(timestamp)
        energy = np.sqrt(np.sum(template**2))
        self.assertAlmostEqual(energy, 1.0, places=5)
    
    def test_window_template(self):
        """Window template should extract correct portion"""
        timestamp = datetime(2025, 12, 16, 12, 30, 0).timestamp()
        window = self.generator.generate_window_template(
            timestamp, start_second=10, duration_seconds=5
        )
        self.assertEqual(len(window), 5 * self.sample_rate)
    
    def test_position_markers(self):
        """Position markers should be at seconds 0,9,19,29,39,49,59"""
        expected_markers = {0, 9, 19, 29, 39, 49, 59}
        self.assertEqual(self.generator.POSITION_MARKERS, expected_markers)
    
    def test_100hz_carrier(self):
        """Template with carrier should have 100 Hz component"""
        template = self.generator.generate_second_template(
            bit_value=1, with_carrier=True
        )
        # FFT to check for 100 Hz
        fft = np.abs(np.fft.rfft(template))
        freqs = np.fft.rfftfreq(len(template), 1/self.sample_rate)
        
        # Find peak near 100 Hz
        idx_100 = np.argmin(np.abs(freqs - 100))
        peak_freq = freqs[np.argmax(fft[idx_100-5:idx_100+5]) + idx_100 - 5]
        self.assertAlmostEqual(peak_freq, 100, delta=5)


class TestCHUAFSKTemplateGenerator(unittest.TestCase):
    """Test CHU AFSK (Bell 103) template generation"""
    
    def setUp(self):
        self.sample_rate = 20000
        self.generator = create_afsk_generator(self.sample_rate)
    
    def test_mark_frequency(self):
        """Mark frequency should be 2225 Hz"""
        self.assertEqual(self.generator.MARK_FREQ, 2225.0)
    
    def test_space_frequency(self):
        """Space frequency should be 2025 Hz"""
        self.assertEqual(self.generator.SPACE_FREQ, 2025.0)
    
    def test_baud_rate(self):
        """Baud rate should be 300 bps"""
        self.assertEqual(self.generator.BAUD_RATE, 300)
    
    def test_fsk_seconds(self):
        """FSK seconds should be 31-39"""
        expected = [31, 32, 33, 34, 35, 36, 37, 38, 39]
        self.assertEqual(self.generator.FSK_SECONDS, expected)
    
    def test_mark_template_frequency(self):
        """Mark template should have 2225 Hz component"""
        template = self.generator.generate_mark_template(duration_ms=100)
        fft = np.abs(np.fft.rfft(template))
        freqs = np.fft.rfftfreq(len(template), 1/self.sample_rate)
        
        peak_idx = np.argmax(fft)
        peak_freq = freqs[peak_idx]
        self.assertAlmostEqual(peak_freq, 2225, delta=50)
    
    def test_space_template_frequency(self):
        """Space template should have 2025 Hz component"""
        template = self.generator.generate_space_template(duration_ms=100)
        fft = np.abs(np.fft.rfft(template))
        freqs = np.fft.rfftfreq(len(template), 1/self.sample_rate)
        
        peak_idx = np.argmax(fft)
        peak_freq = freqs[peak_idx]
        self.assertAlmostEqual(peak_freq, 2025, delta=50)
    
    def test_fsk_second_template_length(self):
        """FSK second template should be 500ms (to timing boundary)"""
        template = self.generator.generate_fsk_second_template(second=31)
        expected_samples = int(500 * self.sample_rate / 1000)
        self.assertEqual(len(template), expected_samples)
    
    def test_quadrature_templates(self):
        """Quadrature templates should be orthogonal"""
        sin_t, cos_t = self.generator.generate_quadrature_templates(
            duration_ms=100, frequency=2225
        )
        # Orthogonality: dot product should be near zero
        dot_product = np.abs(np.dot(sin_t, cos_t))
        self.assertLess(dot_product, 0.1)
    
    def test_quadrature_templates_normalized(self):
        """Quadrature templates should have unit energy"""
        sin_t, cos_t = self.generator.generate_quadrature_templates(
            duration_ms=100, frequency=2225
        )
        sin_energy = np.sqrt(np.sum(sin_t**2))
        cos_energy = np.sqrt(np.sum(cos_t**2))
        self.assertAlmostEqual(sin_energy, 1.0, places=5)
        self.assertAlmostEqual(cos_energy, 1.0, places=5)


class TestBPMTemplateGenerator(unittest.TestCase):
    """Test BPM (China) template generation"""
    
    def setUp(self):
        self.sample_rate = 20000
        self.generator = create_bpm_generator(self.sample_rate)
    
    def test_tick_frequency(self):
        """BPM tick frequency should be 1000 Hz"""
        self.assertEqual(self.generator.TICK_FREQ, 1000.0)
    
    def test_utc_tick_duration(self):
        """UTC tick duration should be 10ms"""
        self.assertEqual(self.generator.UTC_TICK_MS, 10.0)
    
    def test_ut1_tick_duration(self):
        """UT1 tick duration should be 100ms"""
        self.assertEqual(self.generator.UT1_TICK_MS, 100.0)
    
    def test_minute_marker_duration(self):
        """Minute marker should be 300ms"""
        self.assertEqual(self.generator.MINUTE_MARKER_MS, 300.0)
    
    def test_ut1_minutes(self):
        """UT1 minutes should be 25-29 and 55-59"""
        expected = {25, 26, 27, 28, 29, 55, 56, 57, 58, 59}
        self.assertEqual(self.generator.UT1_MINUTES, expected)
    
    def test_utc_minute_detection(self):
        """Should correctly identify UTC minutes"""
        self.assertTrue(self.generator.is_utc_minute(0))
        self.assertTrue(self.generator.is_utc_minute(10))
        self.assertTrue(self.generator.is_utc_minute(30))
        self.assertFalse(self.generator.is_utc_minute(25))
        self.assertFalse(self.generator.is_utc_minute(55))
    
    def test_tick_duration_utc_minute(self):
        """UTC minute should return 10ms tick duration"""
        duration = self.generator.get_tick_duration_ms(minute=10, second=5)
        self.assertEqual(duration, 10.0)
    
    def test_tick_duration_ut1_minute(self):
        """UT1 minute should return 100ms tick duration"""
        duration = self.generator.get_tick_duration_ms(minute=25, second=5)
        self.assertEqual(duration, 100.0)
    
    def test_tick_duration_minute_marker(self):
        """Second 0 should return 300ms minute marker duration"""
        duration = self.generator.get_tick_duration_ms(minute=10, second=0)
        self.assertEqual(duration, 300.0)
    
    def test_tick_template_length_utc(self):
        """UTC tick template should be 10ms"""
        sin_t, cos_t = self.generator.generate_tick_template(minute=10, second=5)
        expected_samples = int(10 * self.sample_rate / 1000)
        self.assertEqual(len(sin_t), expected_samples)
    
    def test_tick_template_length_ut1(self):
        """UT1 tick template should be 100ms"""
        sin_t, cos_t = self.generator.generate_tick_template(minute=25, second=5)
        expected_samples = int(100 * self.sample_rate / 1000)
        self.assertEqual(len(sin_t), expected_samples)
    
    def test_composite_template_length(self):
        """Composite template should span correct duration"""
        sin_t, cos_t, valid = self.generator.generate_composite_template(
            minute=10, start_second=1, num_seconds=5
        )
        self.assertEqual(len(sin_t), 5 * self.sample_rate)
    
    def test_composite_template_skips_second_zero(self):
        """Composite template should skip second 0 (minute marker)"""
        sin_t, cos_t, valid = self.generator.generate_composite_template(
            minute=10, start_second=0, num_seconds=5
        )
        self.assertNotIn(0, valid)
        self.assertIn(1, valid)


class TestSignalTemplateCorrelator(unittest.TestCase):
    """Test unified correlation engine"""
    
    def setUp(self):
        self.sample_rate = 20000
        self.correlator = create_correlator(self.sample_rate)
    
    def test_correlator_has_generators(self):
        """Correlator should have all three generators"""
        self.assertIsInstance(self.correlator.bcd_generator, BCDTemplateGenerator)
        self.assertIsInstance(self.correlator.afsk_generator, CHUAFSKTemplateGenerator)
        self.assertIsInstance(self.correlator.bpm_generator, BPMTemplateGenerator)
    
    def test_extract_100hz_band(self):
        """Should extract 100 Hz band from IQ samples"""
        # Generate test signal with 100 Hz component
        t = np.arange(60 * self.sample_rate) / self.sample_rate
        signal = np.sin(2 * np.pi * 100 * t) + 0.1 * np.random.randn(len(t))
        
        filtered = self.correlator._extract_100hz_band(signal)
        
        # Check that 100 Hz is preserved
        fft = np.abs(np.fft.rfft(filtered))
        freqs = np.fft.rfftfreq(len(filtered), 1/self.sample_rate)
        
        idx_100 = np.argmin(np.abs(freqs - 100))
        # 100 Hz should be dominant
        self.assertGreater(fft[idx_100], np.mean(fft) * 5)
    
    def test_correlate_bcd_returns_results(self):
        """BCD correlation should return results for valid signal"""
        # Generate synthetic BCD-like signal
        timestamp = datetime(2025, 12, 16, 12, 30, 0).timestamp()
        template = self.correlator.bcd_generator.generate_minute_template(timestamp)
        
        # Add noise
        signal = template + 0.1 * np.random.randn(len(template))
        
        results = self.correlator.correlate_bcd(
            signal, timestamp, window_seconds=10, overlap_seconds=5
        )
        
        # Should get some results
        self.assertGreater(len(results), 0)
        self.assertIsInstance(results[0], BCDCorrelationResult)
    
    def test_correlate_afsk_returns_results(self):
        """AFSK correlation should return results"""
        # Generate 60 seconds of noise (placeholder)
        signal = 0.1 * np.random.randn(60 * self.sample_rate)
        
        results = self.correlator.correlate_afsk(signal)
        
        # Should get results for FSK seconds
        self.assertEqual(len(results), 9)  # Seconds 31-39
        self.assertIsInstance(results[0], AFSKCorrelationResult)
    
    def test_correlate_bpm_returns_results(self):
        """BPM correlation should return results"""
        # Generate 60 seconds of noise (placeholder)
        signal = 0.1 * np.random.randn(60 * self.sample_rate)
        
        results = self.correlator.correlate_bpm(
            signal, minute=10, window_seconds=5, overlap_seconds=1
        )
        
        # Should get some results
        self.assertGreater(len(results), 0)
        self.assertIsInstance(results[0], BPMCorrelationResult)
    
    def test_bpm_correlation_timing_mode(self):
        """BPM correlation should report correct timing mode"""
        signal = 0.1 * np.random.randn(60 * self.sample_rate)
        
        # UTC minute
        results_utc = self.correlator.correlate_bpm(signal, minute=10)
        self.assertEqual(results_utc[0].timing_mode, 'UTC')
        self.assertTrue(results_utc[0].is_usable)
        
        # UT1 minute
        results_ut1 = self.correlator.correlate_bpm(signal, minute=25)
        self.assertEqual(results_ut1[0].timing_mode, 'UT1')
        self.assertFalse(results_ut1[0].is_usable)


class TestFactoryFunctions(unittest.TestCase):
    """Test factory function creation"""
    
    def test_create_bcd_generator(self):
        """Factory should create BCD generator"""
        gen = create_bcd_generator(sample_rate=16000)
        self.assertIsInstance(gen, BCDTemplateGenerator)
        self.assertEqual(gen.sample_rate, 16000)
    
    def test_create_afsk_generator(self):
        """Factory should create AFSK generator"""
        gen = create_afsk_generator(sample_rate=16000)
        self.assertIsInstance(gen, CHUAFSKTemplateGenerator)
        self.assertEqual(gen.sample_rate, 16000)
    
    def test_create_bpm_generator(self):
        """Factory should create BPM generator"""
        gen = create_bpm_generator(sample_rate=16000)
        self.assertIsInstance(gen, BPMTemplateGenerator)
        self.assertEqual(gen.sample_rate, 16000)
    
    def test_create_correlator(self):
        """Factory should create correlator"""
        corr = create_correlator(sample_rate=16000)
        self.assertIsInstance(corr, SignalTemplateCorrelator)
        self.assertEqual(corr.sample_rate, 16000)


if __name__ == '__main__':
    unittest.main()

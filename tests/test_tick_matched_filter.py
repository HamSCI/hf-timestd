#!/usr/bin/env python3
"""
Tests for tick_matched_filter.py - Station-specific per-second tick detection.
"""

import unittest
import numpy as np
from hf_timestd.core import (
    TickMatchedFilter,
    TickTemplate,
    TickDetectionResult,
    MinuteTickAnalysis,
    create_tick_filter,
    WWV_TEMPLATE,
    WWVH_TEMPLATE,
    CHU_TEMPLATE,
    BPM_TEMPLATE,
    STATION_TEMPLATES,
)
from hf_timestd.core.tick_matched_filter import StationType


class TestTickTemplates(unittest.TestCase):
    """Test station template configurations"""
    
    def test_wwv_template(self):
        """WWV: 1000 Hz, 5ms ticks, skip 0/29/59"""
        self.assertEqual(WWV_TEMPLATE.frequency_hz, 1000.0)
        self.assertEqual(WWV_TEMPLATE.tick_duration_ms, 5.0)
        self.assertIn(0, WWV_TEMPLATE.skip_seconds)
        self.assertIn(29, WWV_TEMPLATE.skip_seconds)
        self.assertIn(59, WWV_TEMPLATE.skip_seconds)
    
    def test_wwvh_template(self):
        """WWVH: 1200 Hz, 5ms ticks, skip 0/29/59"""
        self.assertEqual(WWVH_TEMPLATE.frequency_hz, 1200.0)
        self.assertEqual(WWVH_TEMPLATE.tick_duration_ms, 5.0)
        self.assertIn(0, WWVH_TEMPLATE.skip_seconds)
        self.assertIn(29, WWVH_TEMPLATE.skip_seconds)
    
    def test_chu_template(self):
        """CHU: 1000 Hz, variable duration, skip 0/29"""
        self.assertEqual(CHU_TEMPLATE.frequency_hz, 1000.0)
        self.assertIn(29, CHU_TEMPLATE.skip_seconds)
        self.assertEqual(CHU_TEMPLATE.fsk_duration_ms, 10.0)
        self.assertEqual(CHU_TEMPLATE.regular_duration_ms, 300.0)
        # FSK seconds 31-39
        self.assertEqual(CHU_TEMPLATE.fsk_seconds, set(range(31, 40)))
        # Voice seconds 50-59
        self.assertEqual(CHU_TEMPLATE.voice_seconds, set(range(50, 60)))
    
    def test_bpm_template(self):
        """BPM: 1000 Hz, 10ms UTC / 100ms UT1"""
        self.assertEqual(BPM_TEMPLATE.frequency_hz, 1000.0)
        self.assertEqual(BPM_TEMPLATE.tick_duration_ms, 10.0)
        self.assertEqual(BPM_TEMPLATE.ut1_tick_duration_ms, 100.0)
        # UT1 minutes: 25-29, 55-59
        self.assertIn(25, BPM_TEMPLATE.ut1_minutes)
        self.assertIn(55, BPM_TEMPLATE.ut1_minutes)
        self.assertNotIn(0, BPM_TEMPLATE.ut1_minutes)


class TestTickMatchedFilterCreation(unittest.TestCase):
    """Test filter creation and initialization"""
    
    def test_create_wwv_filter(self):
        """Create WWV filter with default parameters"""
        f = create_tick_filter('WWV')
        self.assertEqual(f.station, StationType.WWV)
        self.assertEqual(f.sample_rate, 20000)
        self.assertEqual(f.window_seconds, 5)
        self.assertEqual(f.overlap_seconds, 1)
    
    def test_create_wwvh_filter(self):
        """Create WWVH filter"""
        f = create_tick_filter('WWVH', sample_rate=16000)
        self.assertEqual(f.station, StationType.WWVH)
        self.assertEqual(f.sample_rate, 16000)
    
    def test_create_chu_filter(self):
        """Create CHU filter"""
        f = create_tick_filter('CHU')
        self.assertEqual(f.station, StationType.CHU)
        # CHU should have multiple template durations
        self.assertGreater(len(f._templates), 1)
    
    def test_create_bpm_filter(self):
        """Create BPM filter"""
        f = create_tick_filter('BPM', window_seconds=3, overlap_seconds=1)
        self.assertEqual(f.station, StationType.BPM)
        self.assertEqual(f.window_seconds, 3)
    
    def test_templates_built(self):
        """Verify templates are pre-built on initialization"""
        f = create_tick_filter('WWV')
        self.assertIn(5.0, f._templates)
        template_sin, template_cos = f._templates[5.0]
        self.assertEqual(len(template_sin), len(template_cos))
        # 5ms at 20kHz = 100 samples
        self.assertEqual(len(template_sin), 100)


class TestTickDurationSelection(unittest.TestCase):
    """Test station-specific tick duration selection"""
    
    def test_wwv_constant_duration(self):
        """WWV always returns 5ms"""
        f = create_tick_filter('WWV')
        for sec in range(1, 60):
            if sec not in WWV_TEMPLATE.skip_seconds:
                self.assertEqual(f._get_tick_duration_ms(sec), 5.0)
    
    def test_chu_variable_duration(self):
        """CHU returns different durations by second"""
        f = create_tick_filter('CHU')
        # Regular second
        self.assertEqual(f._get_tick_duration_ms(5), 300.0)
        # FSK second (31-39)
        self.assertEqual(f._get_tick_duration_ms(35), 10.0)
        # Voice second (50-59)
        self.assertEqual(f._get_tick_duration_ms(55), 10.0)
    
    def test_bpm_minute_dependent(self):
        """BPM returns different durations by minute"""
        f = create_tick_filter('BPM')
        # UTC minute
        self.assertEqual(f._get_tick_duration_ms(5, minute=0), 10.0)
        self.assertEqual(f._get_tick_duration_ms(5, minute=10), 10.0)
        # UT1 minute
        self.assertEqual(f._get_tick_duration_ms(5, minute=25), 100.0)
        self.assertEqual(f._get_tick_duration_ms(5, minute=55), 100.0)


class TestCompositeTemplate(unittest.TestCase):
    """Test composite template generation for multi-tick windows"""
    
    def test_composite_template_length(self):
        """Composite template has correct length"""
        f = create_tick_filter('WWV', sample_rate=20000)
        template_sin, template_cos, valid_secs = f._build_composite_template(1, 6)
        # 5 seconds at 20kHz = 100000 samples
        self.assertEqual(len(template_sin), 5 * 20000)
        self.assertEqual(len(template_cos), 5 * 20000)
    
    def test_composite_skips_invalid_seconds(self):
        """Composite template skips seconds in skip_seconds"""
        f = create_tick_filter('WWV')
        # Window including second 29 (silent)
        template_sin, template_cos, valid_secs = f._build_composite_template(27, 32)
        self.assertNotIn(29, valid_secs)
        self.assertIn(27, valid_secs)
        self.assertIn(28, valid_secs)
        self.assertIn(30, valid_secs)
        self.assertIn(31, valid_secs)
    
    def test_composite_valid_seconds_count(self):
        """Composite returns correct valid seconds"""
        f = create_tick_filter('WWV')
        # Window 1-6: all valid (skip_seconds = {0, 29, 59})
        _, _, valid_secs = f._build_composite_template(1, 6)
        self.assertEqual(len(valid_secs), 5)
        self.assertEqual(valid_secs, [1, 2, 3, 4, 5])


class TestSyntheticSignalDetection(unittest.TestCase):
    """Test detection with synthetic signals"""
    
    def setUp(self):
        self.sample_rate = 20000
        self.filter = create_tick_filter('WWV', sample_rate=self.sample_rate)
    
    def _generate_tick(self, freq_hz: float, duration_ms: float, 
                       offset_ms: float = 0.0, snr_db: float = 20.0) -> np.ndarray:
        """Generate a single tick tone with noise"""
        duration_sec = duration_ms / 1000.0
        n_samples = int(duration_sec * self.sample_rate)
        t = np.arange(n_samples) / self.sample_rate
        
        # Pure tone
        tone = np.sin(2 * np.pi * freq_hz * t)
        
        # Add noise
        signal_power = np.mean(tone**2)
        noise_power = signal_power / (10 ** (snr_db / 10))
        noise = np.sqrt(noise_power) * np.random.randn(n_samples)
        
        return tone + noise
    
    def _generate_minute_with_ticks(
        self, 
        freq_hz: float = 1000.0,
        tick_duration_ms: float = 5.0,
        snr_db: float = 20.0,
        timing_offset_ms: float = 0.0
    ) -> np.ndarray:
        """Generate 60 seconds of IQ with ticks at each second"""
        n_samples = 60 * self.sample_rate
        # Start with noise
        noise_level = 0.1
        signal = noise_level * np.random.randn(n_samples) + 1j * noise_level * np.random.randn(n_samples)
        
        tick_samples = int(tick_duration_ms * self.sample_rate / 1000.0)
        offset_samples = int(timing_offset_ms * self.sample_rate / 1000.0)
        
        # Add ticks at each second (except skip seconds)
        for sec in range(60):
            if sec in {0, 29, 59}:
                continue
            
            tick_start = sec * self.sample_rate + offset_samples
            if tick_start < 0 or tick_start + tick_samples > n_samples:
                continue
            
            t = np.arange(tick_samples) / self.sample_rate
            tick = np.sin(2 * np.pi * freq_hz * t)
            
            # Scale tick for desired SNR
            tick_power = np.mean(tick**2)
            noise_power = tick_power / (10 ** (snr_db / 10))
            tick_scaled = tick * np.sqrt(1.0 / tick_power)
            
            # Add to signal (as AM modulation on carrier)
            signal[tick_start:tick_start + tick_samples] += tick_scaled
        
        return signal
    
    def test_detect_clean_signal(self):
        """Detect ticks in clean synthetic signal"""
        # Generate minute with WWV ticks (1000 Hz, 5ms)
        iq = self._generate_minute_with_ticks(
            freq_hz=1000.0,
            tick_duration_ms=5.0,
            snr_db=30.0,
            timing_offset_ms=0.0
        )
        
        result = self.filter.process_minute(iq, minute_number=0)
        
        self.assertIsInstance(result, MinuteTickAnalysis)
        self.assertEqual(result.station, StationType.WWV)
        self.assertGreater(result.valid_windows, 0)
        # With clean signal, timing offset should be near zero
        self.assertLess(abs(result.mean_timing_offset_ms), 5.0)
    
    def test_detect_with_offset(self):
        """Detect ticks with known timing offset"""
        offset_ms = 2.5  # 2.5ms offset
        
        iq = self._generate_minute_with_ticks(
            freq_hz=1000.0,
            tick_duration_ms=5.0,
            snr_db=25.0,
            timing_offset_ms=offset_ms
        )
        
        result = self.filter.process_minute(iq, minute_number=0)
        
        # Should detect the offset (within tolerance)
        self.assertGreater(result.valid_windows, 0)
        # Allow 1ms tolerance
        self.assertLess(abs(result.mean_timing_offset_ms - offset_ms), 1.0)
    
    def test_low_snr_detection(self):
        """Detection degrades gracefully with low SNR"""
        iq = self._generate_minute_with_ticks(
            freq_hz=1000.0,
            tick_duration_ms=5.0,
            snr_db=6.0,  # Low SNR
            timing_offset_ms=0.0
        )
        
        result = self.filter.process_minute(iq, minute_number=0, min_snr_db=3.0)
        
        # Should still get some detections, but fewer and with higher uncertainty
        self.assertIsInstance(result, MinuteTickAnalysis)
        # Confidence should be lower than clean signal
        self.assertLess(result.overall_confidence, 0.9)


class TestOverlappingWindows(unittest.TestCase):
    """Test overlapping window behavior"""
    
    def test_window_count(self):
        """Verify correct number of overlapping windows"""
        f = create_tick_filter('WWV', window_seconds=5, overlap_seconds=1)
        
        # Generate dummy signal
        iq = np.random.randn(60 * 20000) + 1j * np.random.randn(60 * 20000)
        result = f.process_minute(iq, minute_number=0, min_snr_db=-100)
        
        # With 5-second windows, 1-second overlap, starting at second 1:
        # Windows: 1-5, 2-6, 3-7, ..., 55-59
        # That's 55 windows
        self.assertEqual(result.total_windows, 55)
    
    def test_window_coverage(self):
        """Verify windows cover expected seconds"""
        f = create_tick_filter('WWV', window_seconds=5, overlap_seconds=1)
        
        iq = np.random.randn(60 * 20000) + 1j * np.random.randn(60 * 20000)
        result = f.process_minute(iq, minute_number=0, min_snr_db=-100)
        
        # Check first and last windows
        if result.window_results:
            first = result.window_results[0]
            self.assertEqual(first.window_start_second, 1)
            self.assertEqual(first.window_end_second, 6)


class TestBPMUT1Handling(unittest.TestCase):
    """Test BPM UT1/UTC minute handling"""
    
    def test_utc_minute_tick_duration(self):
        """BPM UTC minutes use 10ms ticks"""
        f = create_tick_filter('BPM')
        # Minute 0 is UTC
        duration = f._get_tick_duration_ms(5, minute=0)
        self.assertEqual(duration, 10.0)
    
    def test_ut1_minute_tick_duration(self):
        """BPM UT1 minutes use 100ms ticks"""
        f = create_tick_filter('BPM')
        # Minute 25 is UT1
        duration = f._get_tick_duration_ms(5, minute=25)
        self.assertEqual(duration, 100.0)


if __name__ == '__main__':
    unittest.main()

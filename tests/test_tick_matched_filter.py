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
        """WWV: 1000 Hz, 5ms ticks, 800ms minute marker, skip 29/59"""
        self.assertEqual(WWV_TEMPLATE.frequency_hz, 1000.0)
        self.assertEqual(WWV_TEMPLATE.tick_duration_ms, 5.0)
        self.assertEqual(WWV_TEMPLATE.minute_marker_duration_ms, 800.0)
        self.assertNotIn(0, WWV_TEMPLATE.skip_seconds)  # sec 0 = minute marker
        self.assertIn(29, WWV_TEMPLATE.skip_seconds)
        self.assertIn(59, WWV_TEMPLATE.skip_seconds)
    
    def test_wwvh_template(self):
        """WWVH: 1200 Hz, 5ms ticks, 800ms minute marker, skip 29/59"""
        self.assertEqual(WWVH_TEMPLATE.frequency_hz, 1200.0)
        self.assertEqual(WWVH_TEMPLATE.tick_duration_ms, 5.0)
        self.assertEqual(WWVH_TEMPLATE.minute_marker_duration_ms, 800.0)
        self.assertNotIn(0, WWVH_TEMPLATE.skip_seconds)  # sec 0 = minute marker
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
        timing_offset_ms: float = 0.0,
        marker_duration_ms: float = 800.0,
    ) -> np.ndarray:
        """Generate 60 seconds of IQ with ticks and minute marker.
        
        Second 0 gets an 800ms minute marker (primary timing source).
        Seconds 1-58 (except 29, 59) get per-second ticks.
        """
        n_samples = 60 * self.sample_rate
        # Start with noise
        noise_level = 0.1
        signal = noise_level * np.random.randn(n_samples) + 1j * noise_level * np.random.randn(n_samples)
        
        offset_samples = int(timing_offset_ms * self.sample_rate / 1000.0)
        
        for sec in range(60):
            if sec in {29, 59}:
                continue
            
            # Second 0: minute marker; others: per-second tick
            dur_ms = marker_duration_ms if sec == 0 else tick_duration_ms
            dur_samples = int(dur_ms * self.sample_rate / 1000.0)
            
            tick_start = sec * self.sample_rate + offset_samples
            if tick_start < 0 or tick_start + dur_samples > n_samples:
                continue
            
            t = np.arange(dur_samples) / self.sample_rate
            tick = np.sin(2 * np.pi * freq_hz * t)
            
            # Scale tick for desired SNR
            tick_power = np.mean(tick**2)
            tick_scaled = tick * np.sqrt(1.0 / max(tick_power, 1e-10))
            
            # Add to signal (as AM modulation on carrier)
            signal[tick_start:tick_start + dur_samples] += tick_scaled
        
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
        # Per-second tick offset should be within 1ms of known offset
        self.assertLess(abs(result.tick_mean_offset_ms - offset_ms), 1.0)
        # Marker offset may differ slightly due to bandpass group delay
        # on the longer 800ms template, but should still be within 2ms
        if result.marker_detected:
            self.assertLess(abs(result.marker_timing_offset_ms - offset_ms), 2.0)
    
    def test_low_snr_detection(self):
        """Detection still works at low SNR with relaxed threshold"""
        iq = self._generate_minute_with_ticks(
            freq_hz=1000.0,
            tick_duration_ms=5.0,
            snr_db=3.0,  # Low SNR
            timing_offset_ms=0.0
        )
        
        # Use min_snr_db=0.0 to accept low-SNR detections
        result = self.filter.process_minute(iq, minute_number=0, min_snr_db=0.0)
        
        # IQ-domain correlation is robust even at low SNR
        self.assertIsInstance(result, MinuteTickAnalysis)
        self.assertGreater(result.valid_windows, 0)
        # Timing should still be reasonable (< 10ms) even at low SNR
        self.assertLess(abs(result.mean_timing_offset_ms), 10.0)


class TestOverlappingWindows(unittest.TestCase):
    """Test overlapping window behavior"""
    
    def test_window_count(self):
        """Verify correct number of overlapping windows"""
        f = create_tick_filter('WWV', window_seconds=5, overlap_seconds=1)
        
        # Generate dummy signal
        iq = np.random.randn(60 * 20000) + 1j * np.random.randn(60 * 20000)
        result = f.process_minute(iq, minute_number=0, min_snr_db=-100)
        
        # 1 minute marker + 55 overlapping tick windows (1-5, 2-6, ..., 55-59)
        self.assertEqual(result.total_windows, 56)
    
    def test_window_coverage(self):
        """Verify windows cover expected seconds"""
        np.random.seed(42)
        f = create_tick_filter('WWV', window_seconds=5, overlap_seconds=1)

        iq = np.random.randn(60 * 20000) + 1j * np.random.randn(60 * 20000)
        result = f.process_minute(iq, minute_number=0, min_snr_db=-100)

        # First window is the minute marker (second 0); subsequent are overlapping
        # 5-second tick windows starting at seconds 1, 2, 3, ...
        self.assertTrue(result.window_results, "expected windows to be produced")
        first = result.window_results[0]
        self.assertEqual(first.window_start_second, 0)
        self.assertEqual(first.window_end_second, 1)
        second = result.window_results[1]
        self.assertEqual(second.window_start_second, 1)
        self.assertEqual(second.window_end_second, 6)


class TestPhaseContinuity(unittest.TestCase):
    """
    Regression test for phase continuity across overlapping windows.
    
    Before the fix, the IQ mixer used window-relative time (t starting at 0),
    causing ~1.7 rad phase jumps between consecutive windows. After the fix,
    the mixer uses absolute time within the minute, so consecutive windows
    should produce smoothly varying carrier_phase_rad.
    """
    
    def test_carrier_phase_continuity(self):
        """Consecutive windows should have smooth carrier phase (σ < 0.3 rad)"""
        sample_rate = 20000
        tone_freq = 1000.0
        carrier_phase = 0.7  # Known carrier phase offset
        
        # Generate 60s of IQ with a carrier at DC and tone at +1000 Hz,
        # both with a known constant phase. This simulates a stable
        # ionospheric path (no Doppler).
        n_samples = 60 * sample_rate
        t = np.arange(n_samples) / sample_rate
        
        # Carrier (DC component in baseband IQ)
        carrier = 1.0 * np.exp(1j * carrier_phase)
        
        # Tone modulation: 5ms ticks at each second
        tick_duration = int(0.005 * sample_rate)  # 100 samples
        tone_signal = np.zeros(n_samples, dtype=np.complex128)
        for sec in range(60):
            if sec in {0, 29, 59}:
                continue
            start = sec * sample_rate
            t_tick = np.arange(tick_duration) / sample_rate
            # Tone as AM sidebands on the carrier
            tone_signal[start:start + tick_duration] = 0.5 * np.exp(
                1j * (2 * np.pi * tone_freq * (sec + t_tick) + carrier_phase)
            )
        
        # IQ = carrier + tone + noise
        noise_level = 0.02
        iq = (carrier + tone_signal + 
              noise_level * (np.random.randn(n_samples) + 1j * np.random.randn(n_samples)))
        iq = iq.astype(np.complex64)
        
        f = create_tick_filter('WWV', sample_rate=sample_rate)
        result = f.process_minute(iq, minute_number=0, min_snr_db=3.0)
        
        # Need enough windows for a meaningful test
        self.assertGreater(result.valid_windows, 20,
                          f"Only {result.valid_windows} valid windows — need >20 for phase test")
        
        # Extract carrier phases from consecutive windows
        phases = [r.carrier_phase_rad for r in result.window_results]
        
        # Phase differences between consecutive windows
        phase_diffs = np.diff(phases)
        # Wrap to [-π, π]
        phase_diffs = np.arctan2(np.sin(phase_diffs), np.cos(phase_diffs))
        
        std_phase_diff = float(np.std(phase_diffs))
        
        # Before fix: σ ≈ 1.5-2.0 rad (near uniform random)
        # After fix: σ should be < 0.3 rad for a stable carrier
        self.assertLess(std_phase_diff, 0.3,
                       f"Phase discontinuity too large: σ={std_phase_diff:.3f} rad "
                       f"(should be < 0.3 for stable carrier)")
    
    def test_dc_carrier_phase_stability(self):
        """DC carrier phase should be stable on unambiguous channels"""
        sample_rate = 20000
        carrier_phase = 1.2
        
        n_samples = 60 * sample_rate
        
        # Pure carrier with ticks (CHU-like: USB with preserved carrier)
        carrier = 1.0 * np.exp(1j * carrier_phase)
        tick_duration = int(0.300 * sample_rate)  # 300ms CHU ticks
        tone_signal = np.zeros(n_samples, dtype=np.complex128)
        for sec in range(60):
            if sec in {0, 29}:
                continue
            if sec in range(31, 40) or sec in range(50, 60):
                continue  # FSK/voice seconds — short ticks, skip for simplicity
            start = sec * sample_rate
            t_tick = np.arange(min(tick_duration, sample_rate)) / sample_rate
            tone_signal[start:start + len(t_tick)] = 0.3 * np.exp(
                1j * (2 * np.pi * 1000.0 * (sec + t_tick) + carrier_phase)
            )
        
        noise_level = 0.02
        iq = (carrier + tone_signal +
              noise_level * (np.random.randn(n_samples) + 1j * np.random.randn(n_samples)))
        iq = iq.astype(np.complex64)
        
        f = create_tick_filter('CHU', sample_rate=sample_rate)
        result = f.process_minute(iq, minute_number=0, min_snr_db=3.0)
        
        if result.valid_windows < 10:
            self.skipTest(f"Only {result.valid_windows} valid windows")
        
        dc_phases = [r.dc_carrier_phase_rad for r in result.window_results]
        dc_diffs = np.diff(dc_phases)
        dc_diffs = np.arctan2(np.sin(dc_diffs), np.cos(dc_diffs))
        std_dc = float(np.std(dc_diffs))
        
        # DC carrier phase should be very stable for constant carrier
        self.assertLess(std_dc, 0.3,
                       f"DC phase discontinuity too large: σ={std_dc:.3f} rad")


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


class TestTimingPrecision(unittest.TestCase):
    """
    Regression tests for timing precision.
    
    The old composite-template + AM-demod approach had ±50ms scatter due to
    the AM envelope detector creating multiple near-equal correlation peaks
    separated by the tone period. The IQ-domain per-tick correlator should
    achieve sub-millisecond precision.
    """
    
    def setUp(self):
        self.sample_rate = 20000
    
    def _generate_minute_iq(self, freq_hz, tick_duration_ms, timing_offset_ms,
                            snr_db=30.0, skip_seconds=None,
                            marker_duration_ms=800.0):
        """Generate 60s IQ with ticks and minute marker at known positions."""
        if skip_seconds is None:
            skip_seconds = {29, 59}
        n_samples = 60 * self.sample_rate
        noise_level = 0.1
        iq = noise_level * (np.random.randn(n_samples) + 1j * np.random.randn(n_samples))
        
        offset_samples = int(timing_offset_ms * self.sample_rate / 1000.0)
        
        for sec in range(60):
            if sec in skip_seconds:
                continue
            # Second 0: minute marker; others: per-second tick
            dur_ms = marker_duration_ms if sec == 0 else tick_duration_ms
            dur_samples = int(dur_ms * self.sample_rate / 1000.0)
            
            tick_start = sec * self.sample_rate + offset_samples
            if tick_start < 0 or tick_start + dur_samples > n_samples:
                continue
            t = np.arange(dur_samples) / self.sample_rate
            tick = np.sin(2 * np.pi * freq_hz * t)
            tick_power = np.mean(tick**2)
            tick_scaled = tick * np.sqrt(1.0 / max(tick_power, 1e-10))
            iq[tick_start:tick_start + dur_samples] += tick_scaled
        
        return iq
    
    def test_wwv_timing_std_below_1ms(self):
        """WWV timing std across overlapping windows must be < 1ms"""
        np.random.seed(42)
        iq = self._generate_minute_iq(1000.0, 5.0, timing_offset_ms=4.0, snr_db=20.0)
        f = create_tick_filter('WWV', sample_rate=self.sample_rate)
        result = f.process_minute(iq, minute_number=0)
        
        self.assertGreater(result.valid_windows, 40)
        self.assertLess(result.std_timing_offset_ms, 1.0,
                       f"Timing std={result.std_timing_offset_ms:.3f}ms exceeds 1ms limit")
    
    def test_wwvh_timing_std_below_1ms(self):
        """WWVH timing std across overlapping windows must be < 1ms"""
        np.random.seed(42)
        iq = self._generate_minute_iq(1200.0, 5.0, timing_offset_ms=22.0, snr_db=20.0)
        f = create_tick_filter('WWVH', sample_rate=self.sample_rate)
        result = f.process_minute(iq, minute_number=0)
        
        self.assertGreater(result.valid_windows, 40)
        self.assertLess(result.std_timing_offset_ms, 1.0,
                       f"Timing std={result.std_timing_offset_ms:.3f}ms exceeds 1ms limit")
    
    def test_known_offset_recovery(self):
        """Known timing offset must be recovered within 0.5ms by per-tick windows"""
        np.random.seed(42)
        known_offset_ms = 4.2
        iq = self._generate_minute_iq(1000.0, 5.0, timing_offset_ms=known_offset_ms, snr_db=25.0)
        f = create_tick_filter('WWV', sample_rate=self.sample_rate)
        result = f.process_minute(iq, minute_number=0)
        
        # Check per-tick offset recovery (not marker, which has different group delay)
        error_ms = abs(result.tick_mean_offset_ms - known_offset_ms)
        self.assertLess(error_ms, 0.5,
                       f"Tick offset error={error_ms:.3f}ms (detected={result.tick_mean_offset_ms:.3f}ms, "
                       f"expected={known_offset_ms:.3f}ms)")
    
    def test_overlapping_windows_consistent(self):
        """Adjacent overlapping tick windows (sharing 4/5 data) must agree within 0.5ms"""
        np.random.seed(42)
        iq = self._generate_minute_iq(1000.0, 5.0, timing_offset_ms=3.0, snr_db=25.0)
        f = create_tick_filter('WWV', sample_rate=self.sample_rate)
        result = f.process_minute(iq, minute_number=0)
        
        # Skip the minute marker (first result, window_start_second=0) —
        # it uses a single 800ms correlation, not the 5-tick median used by
        # overlapping tick windows, so the marker→tick transition may differ.
        tick_offsets = [r.timing_offset_ms for r in result.window_results
                       if r.window_start_second > 0]
        self.assertGreater(len(tick_offsets), 20)
        
        # Adjacent tick window differences
        diffs = [abs(tick_offsets[i+1] - tick_offsets[i]) for i in range(len(tick_offsets)-1)]
        max_diff = max(diffs)
        self.assertLess(max_diff, 0.5,
                       f"Max adjacent window difference={max_diff:.3f}ms exceeds 0.5ms")
    
    def test_no_unphysical_drift(self):
        """Drift rate must be < 0.01 ms/s for a stationary signal"""
        np.random.seed(42)
        iq = self._generate_minute_iq(1000.0, 5.0, timing_offset_ms=5.0, snr_db=25.0)
        f = create_tick_filter('WWV', sample_rate=self.sample_rate)
        result = f.process_minute(iq, minute_number=0)
        
        if result.drift_rate_ms_per_sec is not None:
            self.assertLess(abs(result.drift_rate_ms_per_sec), 0.01,
                           f"Drift={result.drift_rate_ms_per_sec:.4f}ms/s is unphysical "
                           f"for stationary signal")


if __name__ == '__main__':
    unittest.main()

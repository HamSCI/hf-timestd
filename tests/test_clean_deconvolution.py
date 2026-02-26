"""
Test CLEAN deconvolution on synthetic multipath signals.

Verifies that the TickEdgeDetector's _clean_deconvolve method can resolve
two arrivals separated by a known delay (simulating 1F2 + 2F2 multipath).
"""
import numpy as np
import pytest
from hf_timestd.core.tick_edge_detector import (
    TickEdgeDetector,
    CleanComponent,
    STATION_TICK_FREQ,
    STATION_TICK_CYCLES,
    STATION_TICK_DURATION_MS,
)


SAMPLE_RATE = 24000


def _make_tick_signal(freq_hz, n_cycles, sample_rate, delay_samples, amplitude=1.0):
    """Create a single tick at the given delay."""
    duration_sec = n_cycles / freq_hz
    n_template = int(duration_sec * sample_rate)
    # Buffer long enough for tick + margin
    buf = np.zeros(int(sample_rate * 0.050))  # 50ms buffer
    t = np.arange(n_template) / sample_rate
    tick = amplitude * np.sin(2 * np.pi * freq_hz * t)
    start = int(delay_samples)
    if start + n_template <= len(buf):
        buf[start:start + n_template] += tick
    return buf


def _make_two_arrival_signal(freq_hz, n_cycles, sample_rate, delay1_ms, delay2_ms,
                              amp1=1.0, amp2=0.5):
    """Create a signal with two tick arrivals at different delays."""
    duration_sec = n_cycles / freq_hz
    n_template = int(duration_sec * sample_rate)
    buf_len = int(sample_rate * 0.050)  # 50ms
    buf = np.zeros(buf_len)
    
    d1_samples = int(delay1_ms * sample_rate / 1000.0)
    d2_samples = int(delay2_ms * sample_rate / 1000.0)
    
    t = np.arange(n_template) / sample_rate
    tick1 = amp1 * np.sin(2 * np.pi * freq_hz * t)
    tick2 = amp2 * np.sin(2 * np.pi * freq_hz * t)
    
    if d1_samples + n_template <= len(buf):
        buf[d1_samples:d1_samples + n_template] += tick1
    if d2_samples + n_template <= len(buf):
        buf[d2_samples:d2_samples + n_template] += tick2
    
    return buf


class TestCleanDeconvolution:
    """Test the CLEAN algorithm resolves multipath arrivals."""
    
    def setup_method(self):
        self.detector = TickEdgeDetector(sample_rate=SAMPLE_RATE)
    
    def test_psf_computed_for_short_ticks(self):
        """PSF should be pre-computed for WWV (5ms) and BPM (10ms), not CHU (300ms)."""
        assert 'WWV' in self.detector._psf
        assert 'WWVH' in self.detector._psf
        assert 'BPM' in self.detector._psf
        assert 'CHU' not in self.detector._psf
    
    def test_psf_shape_and_normalization(self):
        """PSF should be normalized with peak = 1.0."""
        for station in ('WWV', 'WWVH', 'BPM'):
            psf = self.detector._psf[station]
            assert psf is not None
            assert len(psf) > 0
            assert abs(np.max(psf) - 1.0) < 1e-6, f"{station} PSF peak != 1.0"
            assert np.all(psf >= 0), f"{station} PSF has negative values"
    
    def test_single_arrival_returns_one_component(self):
        """A clean single-arrival signal should yield exactly 1 CLEAN component."""
        freq_hz = STATION_TICK_FREQ['WWV']  # 1000 Hz
        n_cycles = STATION_TICK_CYCLES['WWV']  # 5 cycles = 5ms
        n_template = int(n_cycles / freq_hz * SAMPLE_RATE)
        
        # Single tick at 15ms into buffer
        buf = _make_tick_signal(freq_hz, n_cycles, SAMPLE_RATE,
                                delay_samples=int(0.015 * SAMPLE_RATE), amplitude=1.0)
        
        # Add very small noise so primary is well above CLEAN_MIN_SNR_DB
        buf += np.random.normal(0, 0.001, len(buf))
        
        # Correlate
        from scipy.signal import correlate
        template_sin, template_cos = self.detector._templates['WWV']
        corr_sin = correlate(buf, template_sin, mode='valid')
        corr_cos = correlate(buf, template_cos, mode='valid')
        corr_env = np.sqrt(corr_sin**2 + corr_cos**2)
        
        peak_idx = int(np.argmax(corr_env))
        peak_val = corr_env[peak_idx]
        noise_floor = float(np.median(corr_env)) + 1e-10
        
        components = self.detector._clean_deconvolve(
            corr_env=corr_env,
            station='WWV',
            primary_peak_idx=peak_idx,
            primary_peak_val=peak_val,
            noise_floor=noise_floor,
            region_start=0,
            half_template=n_template // 2,
            expected_sample=int(0.015 * SAMPLE_RATE),
            iq_samples=None,
            tick_freq=freq_hz,
        )
        
        # Should have exactly 1 component (the primary)
        assert len(components) == 1
        assert components[0].peak_rank == 0
        assert components[0].relative_amplitude == 1.0
    
    def test_two_arrivals_resolved(self):
        """Two arrivals separated by 4ms should be resolved by CLEAN."""
        freq_hz = STATION_TICK_FREQ['WWV']  # 1000 Hz
        n_cycles = STATION_TICK_CYCLES['WWV']  # 5 cycles = 5ms
        n_template = int(n_cycles / freq_hz * SAMPLE_RATE)
        
        # Two ticks: primary at 15ms, secondary at 19ms (4ms separation)
        delay1_ms = 15.0
        delay2_ms = 19.0
        buf = _make_two_arrival_signal(freq_hz, n_cycles, SAMPLE_RATE,
                                        delay1_ms, delay2_ms,
                                        amp1=1.0, amp2=0.6)
        
        # Add very small noise so both arrivals are well above CLEAN_MIN_SNR_DB
        buf += np.random.normal(0, 0.001, len(buf))
        
        # Correlate
        from scipy.signal import correlate
        template_sin, template_cos = self.detector._templates['WWV']
        corr_sin = correlate(buf, template_sin, mode='valid')
        corr_cos = correlate(buf, template_cos, mode='valid')
        corr_env = np.sqrt(corr_sin**2 + corr_cos**2)
        
        peak_idx = int(np.argmax(corr_env))
        peak_val = corr_env[peak_idx]
        noise_floor = float(np.median(corr_env)) + 1e-10
        
        components = self.detector._clean_deconvolve(
            corr_env=corr_env,
            station='WWV',
            primary_peak_idx=peak_idx,
            primary_peak_val=peak_val,
            noise_floor=noise_floor,
            region_start=0,
            half_template=n_template // 2,
            expected_sample=int(delay1_ms * SAMPLE_RATE / 1000.0),
            iq_samples=None,
            tick_freq=freq_hz,
        )
        
        # Should have at least 2 components
        assert len(components) >= 2, (
            f"Expected ≥2 CLEAN components, got {len(components)}"
        )
        
        # Primary should be rank 0
        assert components[0].peak_rank == 0
        
        # Secondary should have positive delay offset (~4ms)
        secondary = [c for c in components if c.peak_rank > 0]
        assert len(secondary) >= 1
        
        # The delay offset should be approximately 4ms
        delay_offsets = [c.delay_offset_ms for c in secondary]
        closest_to_4ms = min(delay_offsets, key=lambda d: abs(abs(d) - 4.0))
        assert abs(abs(closest_to_4ms) - 4.0) < 1.5, (
            f"Expected secondary ~4ms from primary, got {closest_to_4ms:.2f}ms"
        )
    
    def test_clean_arrivals_stored_in_tick_detection(self):
        """Verify TickDetection dataclass accepts clean_arrivals field."""
        from hf_timestd.core.tick_edge_detector import TickDetection
        
        comp = CleanComponent(
            peak_rank=1,
            timing_error_ms=4.0,
            corr_snr_db=8.0,
            relative_amplitude=0.5,
            delay_offset_ms=4.0,
            carrier_phase_rad=1.2,
        )
        
        td = TickDetection(
            utc_second=1000000,
            sec_in_minute=5,
            expected_sample=360,
            peak_sample=365.0,
            front_edge_sample=360.0,
            corr_snr_db=15.0,
            timing_error_ms=0.2,
            detected=True,
            is_clean_minute=True,
            is_doubled_tick=False,
            carrier_phase_rad=0.5,
            clean_arrivals=[comp],
        )
        
        assert len(td.clean_arrivals) == 1
        assert td.clean_arrivals[0].peak_rank == 1
        assert td.clean_arrivals[0].delay_offset_ms == 4.0

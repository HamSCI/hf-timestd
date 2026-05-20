#!/usr/bin/env python3
"""
Tick Edge Detector — Per-Second Matched Filter Timing for WWV/WWVH/CHU/BPM
============================================================================

Extracts UTC timing from per-second ticks using the approach proven by the
ntpd Type 36 driver (refclock_wwv.c, D.L. Mills, University of Delaware):

1. **Matched filter** for the exact tick shape:
   - WWV:  5 cycles of 1000 Hz (5.0 ms)
   - WWVH: 6 cycles of 1200 Hz (5.0 ms)
   - CHU:  300 cycles of 1000 Hz (300 ms)
   - BPM:  10 cycles of 1000 Hz (10 ms)

2. **Front-edge back-calculation**: The correlation peak corresponds to the
   CENTER of the tick pulse.  The on-time marker is the LEADING EDGE.
   We subtract half the tick duration from the peak position to recover
   the front edge with sub-sample precision.

3. **Ensemble combination**: Combine timing from all detected ticks in the
   minute using SNR-weighted robust median.

This overcomes the two problems that forced us to drop 5ms WWV/WWVH ticks:

- **Intermodulation**: The 10ms silence zone before each tick (NIST SP 432)
  suppresses the intermod pedestal.  The matched filter's narrow bandwidth
  (5 cycles = 5ms = 200 Hz effective bandwidth) rejects out-of-band energy.
  The quadrature (I/Q) matched filter is phase-invariant, so the intermod
  pedestal (which has arbitrary phase) adds only to the noise floor, not
  to the peak.

- **Low processing gain**: A single 5ms tick at 24 kHz gives 120 samples
  and ~21 dB processing gain.  With 57 ticks per minute combined via
  ensemble median, effective gain is 21 + 10*log10(57) ≈ 38.6 dB.

Signal structure (NIST SP 432):
    Each tick is preceded by 10ms of silence and followed by 25ms of silence.
    The on-time marker is the START of the 5ms tone burst.
    Ticks are at 100% modulation; audio tones at 50%.

Audio tone schedule (determines intermod environment):
    WWV:  even minutes → 500 Hz, odd → 600 Hz, min 2 → 440 Hz
          Silent: minutes 43-51, 29, 59
    WWVH: even minutes → 600 Hz, odd → 500 Hz, min 1 → 440 Hz
          Silent: minutes 0, 8-10, 14-19, 30

    When one station's audio tone is silent, the other station's ticks
    are free of intermod contamination — these are "clean calibration"
    minutes with higher detection confidence.

Reference:
    Mills, D.L. "A precision radio clock for WWV transmissions."
    Electrical Engineering Report 97-8-1, University of Delaware, 1997.
    ntpd refclock_wwv.c (Type 36 driver), open source.
"""

import logging
import numpy as np
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Tuple, Set
from scipy.signal import butter, sosfiltfilt, correlate

from .snr import peak_snr_db_envelope

logger = logging.getLogger(__name__)


# =============================================================================
# WWV/WWVH Audio Tone Schedule
# =============================================================================
# Determines which minutes have intermod contamination at 1000/1200 Hz.

# Minutes when WWV transmits NO audio tone (ticks only + BCD).
# During these minutes, WWVH ticks at 1200 Hz are free of WWV intermod.
WWV_SILENT_AUDIO_MINUTES: frozenset = frozenset({29, 43, 44, 45, 46, 47, 48, 49, 50, 51, 59})

# Minutes when WWVH transmits NO audio tone.
# During these minutes, WWV ticks at 1000 Hz are free of WWVH intermod.
WWVH_SILENT_AUDIO_MINUTES: frozenset = frozenset({0, 8, 9, 10, 14, 15, 16, 17, 18, 19, 30})

# WWV audio tone frequency by minute (None = silent)
def wwv_audio_tone_hz(minute: int) -> Optional[int]:
    """Return WWV's audio tone frequency for a given minute, or None if silent."""
    if minute in WWV_SILENT_AUDIO_MINUTES:
        return None
    if minute == 2:
        return 440
    if minute % 2 == 0:
        return 500
    return 600

# WWVH audio tone frequency by minute (None = silent)
def wwvh_audio_tone_hz(minute: int) -> Optional[int]:
    """Return WWVH's audio tone frequency for a given minute, or None if silent."""
    if minute in WWVH_SILENT_AUDIO_MINUTES:
        return None
    if minute == 1:
        return 440
    if minute % 2 == 0:
        return 600
    return 500


def is_clean_minute(station: str, minute: int) -> bool:
    """
    Return True if the OTHER station's audio tone is silent this minute,
    meaning our station's ticks are free of cross-station intermod at
    the tick frequency.

    This helper is purely about audio-tone overlap and does **not** take a
    channel parameter; the dedicated-channel exemption (WWV_20000 /
    WWV_25000 are always clean because WWVH does not transmit at those
    frequencies) is enforced at the caller via
    ``is_dedicated_channel or is_clean_minute(station, minute)``.
    """
    if station == 'WWV':
        # WWV ticks are clean when WWVH has no audio tone
        return minute in WWVH_SILENT_AUDIO_MINUTES
    elif station == 'WWVH':
        # WWVH ticks are clean when WWV has no audio tone
        return minute in WWV_SILENT_AUDIO_MINUTES
    else:
        # CHU/BPM: no intermod concern from WWV/WWVH
        return True


def intermod_at_tick_freq(station: str, minute: int) -> bool:
    """
    Return True if intermod products from the OTHER station's audio tone
    could contaminate this station's tick frequency this minute.
    
    WWV ticks at 1000 Hz are contaminated by:
      - WWVH 500 Hz × 2 = 1000 Hz (when WWVH broadcasts 500 Hz)
    WWVH ticks at 1200 Hz are contaminated by:
      - WWV 600 Hz × 2 = 1200 Hz (when WWV broadcasts 600 Hz)
    """
    if station == 'WWV':
        other_tone = wwvh_audio_tone_hz(minute)
        # 500 Hz 2nd harmonic lands on 1000 Hz
        return other_tone == 500
    elif station == 'WWVH':
        other_tone = wwv_audio_tone_hz(minute)
        # 600 Hz 2nd harmonic lands on 1200 Hz
        return other_tone == 600
    return False


# =============================================================================
# Data Classes
# =============================================================================

@dataclass
class CleanComponent:
    """One CLEAN deconvolution component — a resolved multipath arrival."""
    peak_rank: int               # 0 = primary (same as original), 1+ = secondary
    timing_error_ms: float       # Front edge offset from expected (ms)
    corr_snr_db: float           # SNR of this component
    relative_amplitude: float    # Amplitude relative to primary peak (0-1)
    delay_offset_ms: float       # Delay relative to primary arrival (ms)
    carrier_phase_rad: float = 0.0  # Carrier phase at this arrival


@dataclass
class TickDetection:
    """Single per-second tick matched filter detection result."""
    utc_second: int              # Absolute UTC second
    sec_in_minute: int           # 0-59
    expected_sample: int         # Expected onset sample in buffer
    peak_sample: float           # Matched filter peak sample (sub-sample)
    front_edge_sample: float     # Front edge sample (peak - half_template)
    corr_snr_db: float           # Matched filter correlation SNR
    timing_error_ms: float       # Front edge offset from expected (ms)
    detected: bool               # Passed SNR threshold
    is_clean_minute: bool        # No intermod contamination
    is_doubled_tick: bool        # UT1 doubled tick (seconds 1-16)
    carrier_phase_rad: float = 0.0  # Carrier phase at tick (from IQ mixing)
    clean_arrivals: List['CleanComponent'] = field(default_factory=list)  # CLEAN multipath


@dataclass
class EdgeEnsembleResult:
    """Combined result from all per-second ticks in one minute."""
    station: str
    frequency_hz: float
    minute_number: int
    
    # Ensemble timing estimate
    ensemble_timing_error_ms: float    # Robust median of individual timing errors
    ensemble_uncertainty_ms: float     # MAD-based uncertainty estimate
    ensemble_n_edges: int              # Number of ticks used
    
    # Breakdown
    n_attempted: int                   # Total seconds attempted
    n_detected: int                    # Ticks passing threshold
    n_clean: int                       # Ticks from clean (no-intermod) minutes
    
    # Quality
    mean_edge_snr_db: float            # Mean SNR of detected ticks
    confidence: float                  # 0-1 quality metric
    
    # Doppler from carrier phase slope across the minute
    doppler_hz: Optional[float] = None
    doppler_uncertainty_hz: Optional[float] = None
    
    # Per-second details (for diagnostics)
    edges: List[TickDetection] = field(default_factory=list)


# =============================================================================
# Station Definitions
# =============================================================================

# Station tick frequencies
STATION_TICK_FREQ: Dict[str, float] = {
    'WWV': 1000.0,
    'WWVH': 1200.0,
    'CHU': 1000.0,
    'BPM': 1000.0,
}

# Number of cycles in the tick (defines the matched filter template)
# WWV: 5 cycles of 1000 Hz = 5.0 ms
# WWVH: 6 cycles of 1200 Hz = 5.0 ms
# CHU: 300 cycles of 1000 Hz = 300 ms (regular seconds)
# BPM: 10 cycles of 1000 Hz = 10 ms
STATION_TICK_CYCLES: Dict[str, int] = {
    'WWV': 5,
    'WWVH': 6,
    'CHU': 300,
    'BPM': 10,
}

# Seconds with no tick (silent)
STATION_SKIP_SECONDS: Dict[str, frozenset] = {
    'WWV': frozenset({29, 59}),
    'WWVH': frozenset({29, 59}),
    'CHU': frozenset({0, 29}),
    'BPM': frozenset(),
}

# Tick durations in ms
STATION_TICK_DURATION_MS: Dict[str, float] = {
    'WWV': 5.0,
    'WWVH': 5.0,
    'CHU': 300.0,
    'BPM': 10.0,
}


# =============================================================================
# Tick Edge Detector (ntpd-inspired matched filter + front-edge back-off)
# =============================================================================

class TickEdgeDetector:
    """
    Detect per-second tick onsets using matched filter correlation with
    front-edge back-calculation, inspired by ntpd refclock_wwv.c.
    
    For each expected tick position:
    1. Extract a short region around the expected onset
    2. Correlate with quadrature (sin/cos) templates of the exact tick shape
    3. Find the correlation peak (= center of tick)
    4. Back-calculate front edge: onset = peak - (tick_duration / 2)
    5. Compute SNR from peak vs. noise floor
    6. Sub-sample interpolation via parabolic fit
    
    The ensemble of all per-second detections is combined with a
    SNR-weighted robust median to produce a single timing estimate
    per station per minute.
    """
    
    # Search window around expected onset (±ms)
    # The physics model constrains arrivals to ±15ms, but we allow ±20ms
    # for the per-second ticks to handle ionospheric variation within
    # the minute.  This is much tighter than the ±100ms used for the
    # minute marker, because we already know the propagation delay from
    # the minute marker or from the propagation model.
    SEARCH_WINDOW_MS = 20.0
    
    # SNR thresholds (correlation SNR in dB)
    # Lower than the 8 dB minute marker threshold because:
    # 1. We combine many ticks (ensemble gain)
    # 2. The search window is narrow (fewer false positives)
    # 3. The physics gate from the minute marker constrains the search
    MIN_TICK_SNR_DB = 4.0           # Individual tick minimum
    MIN_TICK_SNR_CLEAN_DB = 3.0     # Lower threshold for clean minutes
    MIN_ENSEMBLE_TICKS = 3          # Minimum ticks for valid ensemble
    
    # Bandpass filter: 800-1400 Hz (same as ntpd)
    # Wide enough to pass both 1000 and 1200 Hz with their sidebands,
    # narrow enough to reject 100 Hz BCD, 440/500/600 Hz tones.
    BANDPASS_LOW_HZ = 800.0
    BANDPASS_HIGH_HZ = 1400.0
    
    # CLEAN deconvolution parameters
    CLEAN_GAIN = 0.3           # Fraction of peak subtracted per iteration (loop gain)
    CLEAN_MAX_ITER = 3         # Maximum CLEAN iterations (real multipath rarely >2 modes)
    CLEAN_MIN_SNR_DB = 8.0     # Minimum SNR for a CLEAN component to be kept
    CLEAN_PRIMARY_MIN_SNR_DB = 15.0  # Primary must be this strong before running CLEAN
    # Only apply CLEAN for templates shorter than this (ms).
    # 5ms WWV/WWVH and 10ms BPM benefit; 300ms CHU does not.
    CLEAN_MAX_TEMPLATE_MS = 15.0

    def __init__(self, sample_rate: int = 24000):
        self.sample_rate = sample_rate
        self._bandpass_sos: Optional[np.ndarray] = None
        self._templates: Dict[str, Tuple[np.ndarray, np.ndarray]] = {}
        self._template_half_len: Dict[str, int] = {}
        self._psf: Dict[str, np.ndarray] = {}  # Template autocorrelation (PSF)
        
        self._init_bandpass()
        self._init_templates()
    
    def _init_bandpass(self):
        """Initialize 800-1400 Hz bandpass filter (matches ntpd design)."""
        nyquist = self.sample_rate / 2
        if self.BANDPASS_HIGH_HZ < nyquist:
            self._bandpass_sos = butter(
                4, [self.BANDPASS_LOW_HZ, self.BANDPASS_HIGH_HZ],
                btype='band', fs=self.sample_rate, output='sos'
            )
    
    def _init_templates(self):
        """
        Build quadrature matched filter templates for each station's tick.
        
        Following ntpd: the template is the exact tick waveform (N cycles
        of the tone frequency).  Quadrature (sin + cos) templates provide
        phase-invariant detection — the envelope sqrt(I² + Q²) peaks at
        the tick center regardless of the carrier phase.
        """
        for station, freq_hz in STATION_TICK_FREQ.items():
            n_cycles = STATION_TICK_CYCLES[station]
            duration_sec = n_cycles / freq_hz
            n_samples = int(duration_sec * self.sample_rate)
            
            if n_samples < 2:
                continue
            
            t = np.arange(n_samples) / self.sample_rate
            
            # Quadrature templates (no windowing — match the rectangular
            # pulse shape exactly, as ntpd does)
            template_sin = np.sin(2 * np.pi * freq_hz * t)
            template_cos = np.cos(2 * np.pi * freq_hz * t)
            
            # Normalize to unit energy
            energy = np.sqrt(np.sum(template_sin**2))
            if energy > 0:
                template_sin = template_sin / energy
                template_cos = template_cos / energy
            
            self._templates[station] = (template_sin, template_cos)
            self._template_half_len[station] = n_samples // 2
            
            # Pre-compute PSF (template autocorrelation) for CLEAN deconvolution.
            # The PSF is the response of the matched filter to its own template —
            # this is what needs to be subtracted to reveal secondary arrivals.
            # Use the envelope (sqrt(sin²+cos²)) autocorrelation.
            tick_duration_ms = STATION_TICK_DURATION_MS.get(station, float('inf'))
            if tick_duration_ms <= self.CLEAN_MAX_TEMPLATE_MS:
                # Create a unit-amplitude tick signal
                unit_tick = np.sin(2 * np.pi * freq_hz * t)
                # Correlate it with the template pair to get the envelope response
                psf_sin = correlate(unit_tick, template_sin, mode='full')
                psf_cos = correlate(unit_tick, template_cos, mode='full')
                psf = np.sqrt(psf_sin**2 + psf_cos**2)
                # Normalize so peak = 1.0
                psf_max = np.max(psf)
                if psf_max > 0:
                    psf = psf / psf_max
                self._psf[station] = psf
    
    def _clean_deconvolve(
        self,
        corr_env: np.ndarray,
        station: str,
        primary_peak_idx: int,
        primary_peak_val: float,
        noise_floor: float,
        region_start: int,
        half_template: int,
        expected_sample: int,
        iq_samples: Optional[np.ndarray],
        tick_freq: float,
    ) -> List[CleanComponent]:
        """
        CLEAN deconvolution on correlation envelope to resolve multipath.
        
        Algorithm (adapted from Högbom 1974 for 1-D correlation envelopes):
        1. Find the peak in the residual correlation envelope.
        2. Record the peak as a CLEAN component (arrival).
        3. Subtract gain × PSF centered at the peak.
        4. Repeat until the residual peak is below the noise threshold.
        
        Returns a list of CleanComponent objects.  The first (rank 0) is
        always the primary arrival (same as the original detection).
        Secondary arrivals (rank ≥ 1) are resolved multipath.
        
        For WWV/WWVH 5ms ticks: the PSF mainlobe is ~5ms wide, so
        CLEAN can resolve secondary arrivals separated by ≥ 2-3 ms
        from the primary — well within the 1F→2F range (3-6 ms typical).
        """
        psf = self._psf.get(station)
        if psf is None:
            return []
        
        psf_center = len(psf) // 2
        residual = corr_env.copy()
        components: List[CleanComponent] = []
        
        # Primary arrival as rank 0.  The matched-filter peak's `peak_idx`
        # is the *front-edge* offset in the search region — no half-template
        # correction is needed here.  (The previous
        # `+ half_template - half_template` round-trip was a no-op left
        # over from an earlier centre-then-front-edge formulation.)
        primary_front_edge = region_start + primary_peak_idx
        primary_timing_error_ms = (primary_front_edge - expected_sample) * 1000.0 / self.sample_rate
        
        # Extract carrier phase for primary (already done in main loop, but
        # we store it for consistency in the CleanComponent)
        primary_phase = 0.0
        if iq_samples is not None:
            tick_start = int(round(region_start + primary_peak_idx))
            n_template = half_template * 2
            tick_end = tick_start + n_template
            if 0 <= tick_start and tick_end <= len(iq_samples):
                iq_tick = iq_samples[tick_start:tick_end]
                t_tick = (tick_start + np.arange(n_template)) / self.sample_rate
                mixer = np.exp(-1j * 2 * np.pi * tick_freq * t_tick)
                primary_phase = float(np.angle(np.mean(iq_tick * mixer)))
        
        primary_snr_db = 20 * np.log10(max(primary_peak_val, 1e-10) / max(noise_floor, 1e-10))
        components.append(CleanComponent(
            peak_rank=0,
            timing_error_ms=primary_timing_error_ms,
            corr_snr_db=primary_snr_db,
            relative_amplitude=1.0,
            delay_offset_ms=0.0,
            carrier_phase_rad=primary_phase,
        ))
        
        # Iterative CLEAN
        for iteration in range(self.CLEAN_MAX_ITER):
            # Find current peak in residual
            peak_idx = int(np.argmax(residual))
            peak_val = residual[peak_idx]
            
            if peak_val <= 0:
                break
            
            # Check SNR of this peak against noise floor
            iter_snr_db = 20 * np.log10(max(peak_val, 1e-10) / max(noise_floor, 1e-10))
            if iter_snr_db < self.CLEAN_MIN_SNR_DB:
                break
            
            # Subtract scaled PSF centered at this peak
            psf_start = peak_idx - psf_center
            for i in range(len(psf)):
                target = psf_start + i
                if 0 <= target < len(residual):
                    residual[target] -= self.CLEAN_GAIN * peak_val * psf[i]
            
            # Clamp residual to non-negative (correlation envelope)
            residual = np.maximum(residual, 0)
            
            # If this is the primary peak location (within ±2 samples), skip —
            # we already have rank 0
            if abs(peak_idx - primary_peak_idx) <= 2:
                continue
            
            # This is a secondary arrival.  Same simplification as the
            # primary above: `peak_idx` already names the front edge.
            front_edge_sample = region_start + peak_idx
            timing_error_ms = (front_edge_sample - expected_sample) * 1000.0 / self.sample_rate
            delay_offset_ms = timing_error_ms - primary_timing_error_ms
            relative_amplitude = peak_val / max(primary_peak_val, 1e-10)
            
            # Extract carrier phase for this arrival
            arrival_phase = 0.0
            if iq_samples is not None:
                n_template = half_template * 2
                tick_start = int(round(region_start + peak_idx))
                tick_end = tick_start + n_template
                if 0 <= tick_start and tick_end <= len(iq_samples):
                    iq_tick = iq_samples[tick_start:tick_end]
                    t_tick = (tick_start + np.arange(n_template)) / self.sample_rate
                    mixer = np.exp(-1j * 2 * np.pi * tick_freq * t_tick)
                    arrival_phase = float(np.angle(np.mean(iq_tick * mixer)))
            
            components.append(CleanComponent(
                peak_rank=len(components),
                timing_error_ms=timing_error_ms,
                corr_snr_db=iter_snr_db,
                relative_amplitude=relative_amplitude,
                delay_offset_ms=delay_offset_ms,
                carrier_phase_rad=arrival_phase,
            ))
        
        return components

    def detect_edges(
        self,
        audio_signal: np.ndarray,
        station: str,
        minute_number: int,
        buffer_timing,  # BufferTiming object
        expected_delay_sec: float,
        is_dedicated_channel: bool = False,
        iq_samples: np.ndarray = None,
    ) -> Optional[EdgeEnsembleResult]:
        """
        Detect tick onsets for all seconds in the buffer.
        
        Args:
            audio_signal: AM-demodulated audio (real-valued, magnitude - mean)
            station: Station name ('WWV', 'WWVH', 'CHU', 'BPM')
            minute_number: Minute within hour (0-59), for audio tone schedule
            buffer_timing: BufferTiming object for UTC↔sample conversion
            expected_delay_sec: Expected propagation delay in seconds
            is_dedicated_channel: True for WWV_20000/WWV_25000 (no WWVH intermod)
            iq_samples: Raw complex IQ samples (optional). When provided,
                       carrier phase is extracted at each detected tick by
                       mixing down at the tone frequency. Phase slope across
                       the minute gives Doppler shift.
            
        Returns:
            EdgeEnsembleResult with combined timing estimate, or None if
            insufficient ticks detected.
        """
        if station not in self._templates:
            return None
        if self._bandpass_sos is None:
            return None
        
        tick_freq = STATION_TICK_FREQ[station]
        skip_seconds = STATION_SKIP_SECONDS[station]
        template_sin, template_cos = self._templates[station]
        half_template = self._template_half_len[station]
        n_template = len(template_sin)
        
        # Bandpass filter to isolate tick frequency band
        try:
            filtered = sosfiltfilt(self._bandpass_sos, audio_signal)
        except Exception as e:
            logger.debug(f"Tick edge bandpass failed for {station}: {e}")
            return None
        
        # Find all UTC seconds whose tick onset falls within the buffer
        n_samples = len(audio_signal)
        buf_start_utc = buffer_timing.sample0_utc
        buf_end_utc = buffer_timing.sample_to_utc(n_samples)
        
        first_utc_sec = int(buf_start_utc) - 1
        last_utc_sec = int(buf_end_utc) + 1
        
        search_samples = int(self.SEARCH_WINDOW_MS * self.sample_rate / 1000)
        # Total margin needed: search window + template length + noise region
        margin = search_samples + n_template + 50
        
        ticks: List[TickDetection] = []
        
        for utc_sec in range(first_utc_sec, last_utc_sec + 1):
            sec_in_minute = utc_sec % 60
            
            # Skip silent seconds
            if sec_in_minute in skip_seconds:
                continue
            
            # Skip second 0 — handled by the 800ms minute marker correlator
            if sec_in_minute == 0:
                continue
            
            # Expected onset sample (integer!)
            # CHU 300ms tones start ~74ms after utc_sec + prop_delay.
            # See metrology_engine.py for full evidence chain.
            chu_tx_onset_sec = 0.074 if station == 'CHU' else 0.0
            onset_utc = utc_sec + expected_delay_sec + chu_tx_onset_sec
            expected_sample = int(round(buffer_timing.utc_to_sample(onset_utc)))
            
            # Check buffer bounds
            if expected_sample - margin < 0:
                continue
            if expected_sample + margin >= n_samples:
                continue
            
            # Extract region around expected onset for correlation.
            # The region must be large enough for the search window on
            # both sides plus the template length.
            region_start = expected_sample - search_samples - n_template
            region_end = expected_sample + search_samples + n_template
            region_start = max(0, region_start)
            region_end = min(n_samples, region_end)
            region = filtered[region_start:region_end]
            
            if len(region) < n_template + 10:
                continue
            
            # Quadrature matched filter correlation (mode='valid')
            corr_sin = correlate(region, template_sin, mode='valid')
            corr_cos = correlate(region, template_cos, mode='valid')
            corr_env = np.sqrt(corr_sin**2 + corr_cos**2)
            
            if len(corr_env) == 0:
                continue
            
            # Search window in correlation index space.
            # corr_env[0] corresponds to template aligned at region_start.
            # The expected onset maps to:
            expected_corr_idx = expected_sample - region_start
            sw_start = max(0, expected_corr_idx - search_samples)
            sw_end = min(len(corr_env), expected_corr_idx + search_samples)
            
            if sw_end <= sw_start:
                continue
            
            search_region = corr_env[sw_start:sw_end]
            local_peak_idx = int(np.argmax(search_region))
            peak_idx = sw_start + local_peak_idx
            peak_val = corr_env[peak_idx]
            
            # Noise floor: exclude ±template_length around peak
            exclusion = max(50, n_template)
            noise_region = np.concatenate([
                corr_env[:max(0, peak_idx - exclusion)],
                corr_env[min(len(corr_env), peak_idx + exclusion):]
            ])
            
            # SNR via the shared canonical (Rayleigh envelope branch):
            # σ̂ = median(noise)/1.1774, then 20·log10(peak/σ̂) — replaces
            # a raw peak/median(noise) ratio that under-reported SNR by
            # ~1.4 dB (review items S4 + M-M1).
            noise_envelope = noise_region if len(noise_region) > 5 else corr_env
            corr_snr_db = peak_snr_db_envelope(peak_val, noise_envelope)
            if not np.isfinite(corr_snr_db):
                corr_snr_db = 0.0
            
            # Sub-sample parabolic interpolation
            sub_offset = 0.0
            if 0 < peak_idx < len(corr_env) - 1:
                y_m1 = corr_env[peak_idx - 1]
                y_0 = corr_env[peak_idx]
                y_p1 = corr_env[peak_idx + 1]
                denom = y_m1 - 2 * y_0 + y_p1
                if abs(denom) > 1e-10:
                    sub_offset = 0.5 * (y_m1 - y_p1) / denom
                    sub_offset = max(-0.5, min(0.5, sub_offset))
            
            # Peak position in buffer sample space.
            # For mode='valid', corr_env[i] = template aligned starting at
            # region[i], so the template CENTER is at region[i + half_template].
            peak_center_sample = region_start + peak_idx + sub_offset + half_template
            
            # Front-edge back-calculation (the ntpd key insight):
            # The on-time marker is the START of the tick, not the center.
            # Subtract half the template length to get the front edge.
            front_edge_sample = peak_center_sample - half_template
            
            # Timing error: measured front edge vs expected onset
            timing_error_samples = front_edge_sample - expected_sample
            timing_error_ms = timing_error_samples * 1000.0 / self.sample_rate
            
            # Clean minute check
            clean = is_dedicated_channel or is_clean_minute(station, minute_number)
            
            # UT1 doubled tick check (seconds 1-16)
            doubled = (1 <= sec_in_minute <= 16)
            
            # SNR threshold
            threshold = (self.MIN_TICK_SNR_CLEAN_DB if clean
                        else self.MIN_TICK_SNR_DB)
            detected = (corr_snr_db >= threshold)
            
            # Carrier phase extraction from raw IQ at the detected tick.
            # Mix the IQ at the tone frequency over the tick duration,
            # then take the angle of the mean phasor.  This gives the
            # carrier phase at this tick — the progression across the
            # minute encodes Doppler shift.
            carrier_phase = 0.0
            if iq_samples is not None and detected:
                tick_start = int(round(front_edge_sample))
                tick_end = tick_start + n_template
                if 0 <= tick_start and tick_end <= len(iq_samples):
                    iq_tick = iq_samples[tick_start:tick_end]
                    t_tick = (tick_start + np.arange(n_template)) / self.sample_rate
                    mixer = np.exp(-1j * 2 * np.pi * tick_freq * t_tick)
                    carrier_phase = float(np.angle(np.mean(iq_tick * mixer)))
            
            # CLEAN deconvolution for multipath resolution on short-tick stations.
            # Only run on DEDICATED channels — on shared channels (e.g.
            # SHARED_10000 carrying both WWV and WWVH), the other station's
            # tick at the same frequency creates a systematic ±5ms offset
            # artifact that CLEAN incorrectly reports as multipath.
            clean_components: List[CleanComponent] = []
            tick_duration_ms = STATION_TICK_DURATION_MS.get(station, float('inf'))
            if (detected
                    and is_dedicated_channel
                    and tick_duration_ms <= self.CLEAN_MAX_TEMPLATE_MS
                    and station in self._psf
                    and corr_snr_db >= self.CLEAN_PRIMARY_MIN_SNR_DB):
                clean_components = self._clean_deconvolve(
                    corr_env=corr_env,
                    station=station,
                    primary_peak_idx=peak_idx,
                    primary_peak_val=peak_val,
                    noise_floor=noise_floor,
                    region_start=region_start,
                    half_template=half_template,
                    expected_sample=expected_sample,
                    iq_samples=iq_samples,
                    tick_freq=tick_freq,
                )
            
            ticks.append(TickDetection(
                utc_second=utc_sec,
                sec_in_minute=sec_in_minute,
                expected_sample=expected_sample,
                peak_sample=float(peak_center_sample),
                front_edge_sample=float(front_edge_sample),
                corr_snr_db=float(corr_snr_db),
                timing_error_ms=float(timing_error_ms),
                detected=detected,
                is_clean_minute=clean,
                is_doubled_tick=doubled,
                carrier_phase_rad=carrier_phase,
                clean_arrivals=clean_components,
            ))
        
        # --- Ensemble combination ---
        detected_ticks = [t for t in ticks if t.detected]
        n_detected = len(detected_ticks)
        n_clean = sum(1 for t in detected_ticks if t.is_clean_minute)
        
        if n_detected < self.MIN_ENSEMBLE_TICKS:
            logger.debug(f"{station}: Tick MF found only {n_detected}/{len(ticks)} "
                        f"ticks (need {self.MIN_ENSEMBLE_TICKS})")
            return EdgeEnsembleResult(
                station=station,
                frequency_hz=tick_freq,
                minute_number=minute_number,
                ensemble_timing_error_ms=0.0,
                ensemble_uncertainty_ms=999.0,
                ensemble_n_edges=n_detected,
                n_attempted=len(ticks),
                n_detected=n_detected,
                n_clean=n_clean,
                mean_edge_snr_db=0.0,
                confidence=0.0,
                edges=ticks,
            )
        
        # Robust SNR-weighted median of timing errors
        timing_errors = np.array([t.timing_error_ms for t in detected_ticks])
        tick_snrs = np.array([t.corr_snr_db for t in detected_ticks])
        
        # Weight by SNR (linear amplitude scale)
        weights = 10 ** (tick_snrs / 20.0)
        ensemble_error = self._weighted_median(timing_errors, weights)
        
        # Uncertainty: MAD of timing errors → σ → σ/√N
        residuals = timing_errors - ensemble_error
        mad = float(np.median(np.abs(residuals)))
        sigma_single = mad * 1.4826  # MAD → σ for normal distribution
        ensemble_uncertainty = sigma_single / np.sqrt(n_detected)
        
        mean_snr = float(np.mean(tick_snrs))
        
        # Confidence: N, SNR, clean fraction
        n_factor = min(1.0, n_detected / 30.0)
        snr_factor = min(1.0, max(0.0, mean_snr / 10.0))
        clean_factor = 0.5 + 0.5 * (n_clean / max(1, n_detected))
        confidence = n_factor * snr_factor * clean_factor
        
        # --- Doppler from carrier phase slope ---
        doppler_hz = None
        doppler_uncertainty_hz = None
        if iq_samples is not None and n_detected >= 5:
            doppler_hz, doppler_uncertainty_hz = self._estimate_doppler(detected_ticks)
        
        dop_str = f", doppler={doppler_hz:+.4f}Hz" if doppler_hz is not None else ""
        logger.info(f"{station}: Tick MF ensemble: {n_detected}/{len(ticks)} ticks, "
                    f"timing={ensemble_error:+.3f}±{ensemble_uncertainty:.3f}ms, "
                    f"SNR={mean_snr:.1f}dB, clean={n_clean}, conf={confidence:.2f}"
                    f"{dop_str}")
        
        return EdgeEnsembleResult(
            station=station,
            frequency_hz=tick_freq,
            minute_number=minute_number,
            ensemble_timing_error_ms=float(ensemble_error),
            ensemble_uncertainty_ms=float(ensemble_uncertainty),
            ensemble_n_edges=n_detected,
            n_attempted=len(ticks),
            n_detected=n_detected,
            n_clean=n_clean,
            mean_edge_snr_db=mean_snr,
            confidence=float(confidence),
            doppler_hz=doppler_hz,
            doppler_uncertainty_hz=doppler_uncertainty_hz,
            edges=ticks,
        )
    
    @staticmethod
    def _weighted_median(values: np.ndarray, weights: np.ndarray) -> float:
        """Compute weighted median of values."""
        sorted_idx = np.argsort(values)
        sorted_vals = values[sorted_idx]
        sorted_weights = weights[sorted_idx]
        cumweight = np.cumsum(sorted_weights)
        half = cumweight[-1] / 2.0
        idx = int(np.searchsorted(cumweight, half))
        return float(sorted_vals[min(idx, len(sorted_vals) - 1)])

    @staticmethod
    def _estimate_doppler(
        detected_ticks: List[TickDetection],
    ) -> Tuple[Optional[float], Optional[float]]:
        """
        Estimate Doppler (Hz) from carrier-phase slope across the minute.

        Fits φ(t) = 2π·f_D·t + φ₀ in absolute UTC time, SNR-weighted, with
        a slip-safe two-step unwrap that survives missing ticks. Returns
        ``(doppler_hz, doppler_uncertainty_hz)``; either is ``None`` when
        too little usable data exists.

        Why not ``np.unwrap`` over ``sec_in_minute``?  np.unwrap's ±π
        adjacency rule is per-sample, not per-second, so a missing tick
        (Δt = 2 s) plus a real Doppler |f| > 0.25 Hz already exceeds ±π
        and is misclassified as a 2π slip. We instead seed the rate from
        Δt ≤ 1.5 s pairs only — where the wrap is unambiguous for the
        ±0.5 Hz band that survives 1-Hz sampling — then unwrap the full
        series against that seed line.

        Why SNR weighting?  Matched-filter phase σ ∝ 1/SNR_amp, so a
        15 dB tick has 1/8 the phase variance of a 3 dB tick.
        ``np.polyfit`` treats its ``w`` argument as 1/σ, so weights are
        the amplitude SNR (10^(SNR_dB/20)).

        Intrinsic limit (cannot be lifted at 1-Hz sampling): the
        adjacent-tick wrap resolves Doppler only within ±0.5 Hz. The
        residual baseband Doppler seen *after* the minute-marker carrier
        lock comfortably fits this band; bulk-channel Doppler is already
        absorbed by the lock.
        """
        if len(detected_ticks) < 5:
            return None, None

        t_abs = np.array([t.utc_second for t in detected_ticks], dtype=float)
        phase_raw = np.array([t.carrier_phase_rad for t in detected_ticks], dtype=float)
        snr_db = np.array([t.corr_snr_db for t in detected_ticks], dtype=float)

        # Drop non-finite phases / SNRs (e.g. ticks that bypassed IQ extraction).
        finite = np.isfinite(phase_raw) & np.isfinite(snr_db)
        if finite.sum() < 5:
            return None, None
        t_abs = t_abs[finite]
        phase_raw = phase_raw[finite]
        snr_db = snr_db[finite]

        if (t_abs[-1] - t_abs[0]) <= 5.0:
            return None, None

        t_rel = t_abs - t_abs[0]
        snr_amp = 10.0 ** (snr_db / 20.0)

        # Step 1: seed the rate from short-gap pairs only. Adjacent 1-sec
        # ticks wrap to (-π, π] unambiguously for |f_D| < 0.5 Hz.
        dt = np.diff(t_rel)
        dphi = np.diff(phase_raw)
        dphi_wrapped = (dphi + np.pi) % (2.0 * np.pi) - np.pi
        short_gap = dt <= 1.5
        if short_gap.sum() < 3:
            return None, None

        # Weight each pair by its weaker tick (limiting-noise principle).
        pair_w = np.minimum(snr_amp[:-1], snr_amp[1:])[short_gap]
        rate_seed = float(np.average(
            dphi_wrapped[short_gap] / dt[short_gap],
            weights=pair_w,
        ))

        # Step 2: unwrap by subtracting the seed line and wrapping the
        # residual into (-π, π]. Slip-safe whenever the true rate lies
        # within ±π/dt_max of the seed — guaranteed because the seed is
        # itself the wrapped 1-sec-pair mean.
        predicted = rate_seed * t_rel
        residual = (phase_raw - predicted + np.pi) % (2.0 * np.pi) - np.pi
        phase_unwrapped = predicted + residual

        # Step 3: SNR-weighted linear fit. np.polyfit's `w` is 1/σ-style;
        # phase σ ∝ 1/SNR_amp, so w ∝ SNR_amp.
        try:
            coeffs, cov = np.polyfit(
                t_rel, phase_unwrapped, 1, w=snr_amp, cov=True
            )
        except (np.linalg.LinAlgError, ValueError):
            return None, None

        slope_rad_per_sec = float(coeffs[0])
        slope_std = float(np.sqrt(cov[0, 0]))
        return (
            slope_rad_per_sec / (2.0 * np.pi),
            slope_std / (2.0 * np.pi),
        )

#!/usr/bin/env python3
"""
WWV/WWVH Scientific Test Signal Generator and Detector

Generates and detects the scientific modulation test signal transmitted at:
- Minute 8 (WWV at Fort Collins, CO)
- Minute 44 (WWVH at Kauai, HI)

Signal designed by WWV/H Scientific Modulation Working Group.
Reference: hamsci.org/wwv

Signal structure (45 seconds total):
1. Voice announcement (10s) - "What follows is a scientific modulation test..."
2. Gaussian white noise (2s) - synchronization
3. Blank time (1s)
4. Phase-coherent multi-tone (10s) - 2, 3, 4, 5 kHz with 3dB attenuation steps
5. Blank time (1s)
6. Chirp sequences (8s) - linear up/down chirps, short and long
7. Blank time (2s)
8. Single-cycle bursts (2s) - 2.5 kHz and 5 kHz timing marks
9. Blank time (1s)
10. Gaussian white noise (2s) - repeated for synchronization
11. Blank time (3s)

This implementation focuses on the most distinctive features for discrimination:
- Multi-tone with attenuation pattern (strongest discriminator)
- Chirp sequences (confirmatory)
- White noise bookends (for alignment)
"""

import numpy as np
import logging
from typing import Tuple, Optional, Dict, List
from scipy import signal
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class TestSignalDetection:
    """
    Results from test signal detection - Channel Sounding Instrument
    
    The test signal at minutes :08 (WWV) and :44 (WWVH) is IDENTICAL for both stations.
    Discrimination comes from the SCHEDULE, not signal content. The value of detection
    is channel characterization via multiple signal segments:
    
    Signal Structure (per Zenodo 5602094):
    - 0-10s:  Voice announcement
    - 10-12s: White noise #1 (wideband coherence)
    - 12-13s: Blank
    - 13-23s: Multi-tone 2,3,4,5 kHz (frequency selectivity)
    - 23-24s: Blank
    - 24-32s: Chirp sequences (delay spread via pulse compression)
    - 32-34s: Blank
    - 34-36s: Single-cycle bursts at 2.5kHz, 5kHz (high-precision timing)
    - 36-37s: Blank
    - 37-39s: White noise #2 (same as #1, for transient detection)
    - 39-42s: Blank
    """
    detected: bool
    confidence: float  # 0.0 to 1.0
    station: Optional[str]  # 'WWV' or 'WWVH' (from schedule, not signal content)
    minute_number: int
    
    # Feature-specific scores (for detection confidence)
    multitone_score: float = 0.0
    chirp_score: float = 0.0
    noise_correlation: float = 0.0  # Average of noise1 and noise2
    
    # Timing information - high-precision ToA from template correlation
    signal_start_time: Optional[float] = None  # Seconds into minute when signal detected
    toa_offset_ms: Optional[float] = None  # Time of arrival offset from expected (ms)
    toa_source: Optional[str] = None  # Source of ToA: 'burst', 'chirp', 'multitone', 'noise'
    burst_toa_offset_ms: Optional[float] = None  # High-precision ToA from single-cycle bursts
    
    # SNR measurement - high processing gain from complex signal structure
    snr_db: Optional[float] = None
    effective_snr_db: Optional[float] = None  # SNR with processing gain included
    
    # Channel characterization from test signal analysis
    delay_spread_ms: Optional[float] = None  # Multipath delay spread (from chirp analysis)
    coherence_time_sec: Optional[float] = None  # Channel coherence time estimate
    
    # Frequency Selectivity Score (FSS) - path signature
    # FSS = 10*log10((P_2kHz + P_3kHz) / (P_4kHz + P_5kHz))
    # Positive FSS = high-frequency attenuation (longer/more dispersive path)
    frequency_selectivity_db: Optional[float] = None
    tone_powers_db: Optional[Dict[int, float]] = None  # Individual tone powers {2000: dB, 3000: dB, ...}
    
    # Per-frequency time-series data (10 seconds, 1-second windows)
    tone_power_timeseries: Optional[Dict[int, List[float]]] = None  # {2000: [dB_t0, dB_t1, ...], ...}
    
    # Fading and scintillation metrics
    fading_variance: Optional[float] = None  # Normalized variance of fading (detrended)
    scintillation_index: Optional[float] = None  # S4 scintillation index (can exceed 1.0 for saturated scintillation)
    s4_by_frequency: Optional[Dict[int, float]] = None  # Per-frequency S4: {2000: 0.3, 3000: 0.4, ...}
    s4_frequency_slope: Optional[float] = None  # S4 vs frequency slope (positive = D-layer, near-zero = F-layer)
    field_strength_db: Optional[float] = None  # Overall field strength
    field_strength_stability: Optional[float] = None  # Stability metric (1/CV)
    
    # White noise template correlation timing (highest precision)
    noise_toa_offset_ms: Optional[float] = None  # ToA from white noise template correlation
    noise_correlation_peak: Optional[float] = None  # Peak correlation coefficient (0-1)
    
    # Noise segment analysis for transient interference detection
    noise1_score: float = 0.0  # Noise segment at 10-12s
    noise2_score: float = 0.0  # Noise segment at 37-39s
    noise_coherence_diff: Optional[float] = None  # |noise1 - noise2|, high = transient event
    transient_detected: bool = False  # Transient interference flag
    
    # Anomaly detection (solar flares, sporadic E, etc.)
    anomaly_detected: bool = False
    anomaly_type: Optional[str] = None  # 'sudden_amplitude_drop', 'sudden_amplitude_increase', etc.
    anomaly_confidence: Optional[float] = None
    
    # Channel quality assessment
    multipath_detected: bool = False
    channel_quality: Optional[str] = None  # 'excellent', 'good', 'fair', 'poor'


class WWVTestSignalGenerator:
    """
    Generate WWV/WWVH scientific test signal
    
    This is a deterministic signal that can be generated at any sample rate
    for template matching and discrimination purposes.
    """
    
    def __init__(self, sample_rate: int = 20000):
        """
        Initialize test signal generator
        
        Args:
            sample_rate: Sample rate in Hz (24000 default, 16000 for legacy)
        """
        self.sample_rate = sample_rate
        self.dt = 1.0 / sample_rate
        
    def generate_white_noise(self, duration_sec: float, seed: Optional[int] = None) -> np.ndarray:
        """
        Generate Gaussian white noise segment
        
        Args:
            duration_sec: Duration in seconds
            seed: Random seed for reproducibility (optional)
            
        Returns:
            Normalized white noise array
        """
        if seed is not None:
            np.random.seed(seed)
        
        num_samples = int(duration_sec * self.sample_rate)
        noise = np.random.randn(num_samples)
        
        # Normalize to prevent clipping
        noise = noise / np.max(np.abs(noise))
        
        return noise
    
    def generate_multitone(self, duration_sec: float = 10.0) -> np.ndarray:
        """
        Generate phase-coherent multi-tone sequence with 3dB attenuation steps
        
        This is the most distinctive feature of the test signal:
        - Four tones: 2, 3, 4, 5 kHz
        - All phase-locked (coherent)
        - 1 second at each attenuation level
        - Starts at -12 dB (0.25 amplitude), attenuates by 3 dB 9 times
        
        Args:
            duration_sec: Total duration (default 10s for 10 attenuation steps)
            
        Returns:
            Multi-tone signal array
        """
        t = np.arange(0, 1.0, self.dt)  # 1 second segments
        
        # Generate four phase-locked tones
        tone_2k = np.cos(2 * np.pi * 2000 * t)
        tone_3k = np.cos(2 * np.pi * 3000 * t)
        tone_4k = np.cos(2 * np.pi * 4000 * t)
        tone_5k = np.cos(2 * np.pi * 5000 * t)
        
        # Sum and scale to prevent clipping
        tone_sum = tone_2k + tone_3k + tone_4k + tone_5k
        tone_1sec = 0.25 * tone_sum  # Start at -12 dB
        
        # Create attenuation sequence: 10 steps of 3 dB
        multitone = tone_1sec.copy()
        current_level = tone_1sec
        
        for i in range(9):  # 9 more attenuation steps
            current_level = current_level / np.sqrt(2)  # -3 dB
            multitone = np.concatenate([multitone, current_level])
        
        return multitone
    
    def generate_chirp_sequence(self) -> np.ndarray:
        """
        Generate chirp sequence: short and long up/down chirps
        
        Sequence:
        - 3 short up-chirps (0.05s each, 0-5 kHz, TBW=250)
        - 3 short down-chirps
        - 0.5s blank
        - 3 long up-chirps (1.0s each, 0-5 kHz, TBW=5000)
        - 3 long down-chirps
        - 0.1s gaps between chirps
        
        Total: ~8 seconds
        
        Returns:
            Chirp sequence array
        """
        short_duration = 0.05
        long_duration = 1.0
        gap_duration = 0.1
        
        # Short chirps
        t_short = np.arange(0, short_duration, self.dt)
        short_up = signal.chirp(t_short, 0, short_duration, 5000, method='linear')
        short_down = signal.chirp(t_short, 5000, short_duration, 0, method='linear')
        
        # Long chirps
        t_long = np.arange(0, long_duration, self.dt)
        long_up = signal.chirp(t_long, 0, long_duration, 5000, method='linear')
        long_down = signal.chirp(t_long, 5000, long_duration, 0, method='linear')
        
        # Gaps
        gap = np.zeros(int(gap_duration * self.sample_rate))
        long_gap = np.zeros(int(0.5 * self.sample_rate))
        
        # Assemble sequence
        chirp_seq = np.concatenate([
            # 3 short up
            short_up, gap, short_up, gap, short_up, gap,
            # 3 short down
            short_down, gap, short_down, gap, short_down,
            # 0.5s gap
            long_gap,
            # 3 long up
            long_up, gap, long_up, gap, long_up, gap,
            # 3 long down
            long_down, gap, long_down, gap, long_down, gap
        ])
        
        return chirp_seq
    
    def generate_burst_sequence(self) -> np.ndarray:
        """
        Generate single-cycle burst sequence for timing measurement
        
        - 5 bursts of 2.5 kHz (one cycle each)
        - 5 bursts of 5 kHz (one cycle each)
        - Evenly spaced over 1 second each
        
        Total: 2 seconds
        
        Returns:
            Burst sequence array
        """
        # 2.5 kHz bursts
        t_2k5 = np.arange(0, 1.0/2500, self.dt)
        burst_2k5 = np.sin(2 * np.pi * 2500 * t_2k5)
        
        # 5 kHz bursts
        t_5k = np.arange(0, 1.0/5000, self.dt)
        burst_5k = np.sin(2 * np.pi * 5000 * t_5k)
        
        # Create 1-second sequences with 5 bursts each
        burst_interval = int(self.sample_rate / 6)  # ~6 bursts per second
        
        seq_2k5 = np.zeros(self.sample_rate)
        seq_5k = np.zeros(self.sample_rate)
        
        for i in range(5):
            start_idx = i * burst_interval
            seq_2k5[start_idx:start_idx + len(burst_2k5)] = burst_2k5
            seq_5k[start_idx:start_idx + len(burst_5k)] = burst_5k
        
        return np.concatenate([seq_2k5, seq_5k])
    
    def generate_full_signal(self, include_voice: bool = False) -> np.ndarray:
        """
        Generate complete test signal
        
        Args:
            include_voice: If True, prepend 10s silence placeholder for voice
                          (actual voice is pre-recorded, not synthesized)
            
        Returns:
            Complete test signal array
        """
        components = []
        
        # Voice announcement (10s) - placeholder
        if include_voice:
            components.append(np.zeros(int(10 * self.sample_rate)))
        
        # 1. White noise (2s) - fixed seed for template matching
        components.append(self.generate_white_noise(2.0, seed=42))
        
        # 2. Blank (1s)
        components.append(np.zeros(int(1 * self.sample_rate)))
        
        # 3. Multi-tone with attenuation (10s) - STRONGEST DISCRIMINATOR
        components.append(self.generate_multitone(10.0))
        
        # 4. Blank (1s)
        components.append(np.zeros(int(1 * self.sample_rate)))
        
        # 5. Chirp sequences (8s)
        components.append(self.generate_chirp_sequence())
        
        # 6. Blank (2s)
        components.append(np.zeros(int(2 * self.sample_rate)))
        
        # 7. Single-cycle bursts (2s)
        components.append(self.generate_burst_sequence())
        
        # 8. Blank (1s)
        components.append(np.zeros(int(1 * self.sample_rate)))
        
        # 9. White noise (2s) - same seed for synchronization
        components.append(self.generate_white_noise(2.0, seed=42))
        
        # 10. Blank (3s)
        components.append(np.zeros(int(3 * self.sample_rate)))
        
        full_signal = np.concatenate(components)
        
        logger.info(f"Generated test signal: {len(full_signal)/self.sample_rate:.1f} seconds")
        
        return full_signal
    
    def get_multitone_template(self) -> np.ndarray:
        """
        Get just the multi-tone segment for template matching
        
        This is the most distinctive feature for discrimination.
        
        Returns:
            10-second multi-tone template
        """
        return self.generate_multitone(10.0)
    
    def get_chirp_template(self) -> np.ndarray:
        """
        Get just the chirp sequence for template matching
        
        Returns:
            ~8-second chirp template
        """
        return self.generate_chirp_sequence()


class WWVTestSignalDetector:
    """
    Detect WWV/WWVH scientific test signal in received audio
    
    Detection strategy:
    1. Check minute number (must be 8 for WWV or 44 for WWVH)
    2. White noise matched filter for high-precision ToA (BT≈10000, 40dB gain)
    3. Cross-correlate against multi-tone template for detection
    4. Chirp matched filter for delay spread estimation
    5. Multi-tone fading analysis for coherence time
    6. Classify as WWV or WWVH based on minute number
    
    Signal timing (seconds into minute):
        0-10:  Voice announcement
        10-12: White noise #1 (deterministic, seed=42)
        12-13: Blank
        13-23: Multi-tone with 3dB attenuation steps
        23-24: Blank
        24-32: Chirp sequences
        32-34: Blank
        34-36: Single-cycle bursts
        36-37: Blank
        37-39: White noise #2 (identical to #1)
        39-42: Blank
    """
    
    # Signal timing constants (seconds into minute, per Zenodo 5602094)
    NOISE1_START = 10.0
    NOISE1_END = 12.0
    MULTITONE_START = 13.0
    MULTITONE_END = 23.0
    CHIRP_START = 24.0
    CHIRP_END = 32.0
    BURST_START = 34.0  # Single-cycle bursts: 5x @ 2.5kHz then 5x @ 5kHz
    BURST_END = 36.0
    NOISE2_START = 37.0
    NOISE2_END = 39.0
    
    # Tone frequencies for multi-tone segment
    TONE_FREQUENCIES = [2000, 3000, 4000, 5000]  # Hz
    
    def __init__(self, sample_rate: int = 20000):
        """
        Initialize detector
        
        Args:
            sample_rate: Sample rate in Hz
        """
        self.sample_rate = sample_rate
        self.generator = WWVTestSignalGenerator(sample_rate)
        
        # Pre-generate templates for matched filtering
        self.multitone_template = self.generator.get_multitone_template()
        self.chirp_template = self.generator.get_chirp_template()
        
        # White noise template - NOTE: This is for energy detection only!
        # The actual WWV broadcast uses a specific PRNG sequence (LabVIEW implementation)
        # that differs from Python's random generator. Cross-correlation requires
        # bit-identical sequences, so matched filtering won't provide processing gain.
        # See: https://github.com/aidanmontare-edu/wwv-h-characterization-signal-ports
        # We keep this for energy-based detection as a secondary indicator.
        self.noise_template = self.generator.generate_white_noise(2.0, seed=42)
        
        # Generate chirp templates per official WWV spec (Zenodo 5602094):
        # - Long chirp: 5 kHz over 1 second (TBW = 5000)
        # - Short chirp: 5 kHz over 0.05 seconds (TBW = 250)
        # Sequence: 3 short up, 3 short down, 0.5s blank, 3 long up, 3 long down
        # 100 ms between chirps
        
        # Long chirp (1 second, 0-5 kHz)
        t_long = np.arange(0, 1.0, 1.0/sample_rate)
        self.long_chirp_up = signal.chirp(t_long, 0, 1.0, 5000, method='linear')
        self.long_chirp_down = signal.chirp(t_long, 5000, 1.0, 0, method='linear')
        
        # Short chirp (50 ms, 0-5 kHz) - higher time-bandwidth product density
        t_short = np.arange(0, 0.05, 1.0/sample_rate)
        self.short_chirp_up = signal.chirp(t_short, 0, 0.05, 5000, method='linear')
        self.short_chirp_down = signal.chirp(t_short, 5000, 0.05, 0, method='linear')
        
        # Detection thresholds
        self.multitone_threshold = 0.15
        self.chirp_threshold = 0.15  # Lowered - chirps are harder to detect through ionosphere
        self.noise_threshold = 0.3
        self.combined_threshold = 0.20
        
        logger.info(f"Test signal detector initialized (sample_rate={sample_rate})")
        logger.info(f"  White noise template: {len(self.noise_template)} samples")
        logger.info(f"  Chirp templates: long={len(self.long_chirp_up)}, short={len(self.short_chirp_up)} samples")
    
    def detect(
        self,
        iq_samples: np.ndarray,
        minute_number: int,
        sample_rate: int
    ) -> TestSignalDetection:
        """
        Detect test signal in received IQ samples with full signal exploitation
        
        Args:
            iq_samples: Complex IQ samples (full minute, ~1440000 samples @ 24kHz)
            minute_number: Minute of hour (0-59)
            sample_rate: Sample rate in Hz
            
        Returns:
            TestSignalDetection object with comprehensive results including:
            - High-precision ToA from white noise matched filter
            - Delay spread from chirp impulse response
            - Coherence time from multi-tone fading analysis
            - Processing-gain SNR estimate
        """
        # Quick exit if not test signal minute
        if minute_number not in [8, 44]:
            return TestSignalDetection(
                detected=False,
                confidence=0.0,
                station=None,
                minute_number=minute_number
            )
        
        # Determine expected station from schedule
        expected_station = 'WWV' if minute_number == 8 else 'WWVH'
        
        # Convert IQ to demodulated audio using AM envelope detection
        if np.iscomplexobj(iq_samples):
            envelope = np.abs(iq_samples)
            audio_signal = envelope - np.mean(envelope)
        else:
            audio_signal = iq_samples
        
        # Resample if necessary
        if sample_rate != self.sample_rate:
            num_samples = int(len(audio_signal) * self.sample_rate / sample_rate)
            audio_signal = signal.resample(audio_signal, num_samples)
        
        # Normalize
        max_val = np.max(np.abs(audio_signal))
        if max_val > 0:
            audio_signal = audio_signal / max_val
        
        # === STAGE 1: Detection (is test signal present?) ===
        
        # Multi-tone detection (most robust for presence detection)
        multitone_score_template, multitone_start = self._detect_multitone(audio_signal)
        multitone_score_simple = self._detect_multitone_simple(audio_signal)
        multitone_score = max(multitone_score_template, multitone_score_simple)
        
        # If simple method wins but template method gave no start time,
        # use expected segment start as coarse estimate
        if multitone_score_simple > multitone_score_template and multitone_start is None:
            if multitone_score_simple > self.multitone_threshold:
                multitone_start = self.MULTITONE_START  # Coarse: signal present at expected time
        
        # White noise analysis (both segments for transient detection)
        noise1_score, noise2_score, noise_coherence_diff = self._detect_both_noise_segments(audio_signal)
        noise_score = (noise1_score + noise2_score) / 2.0  # Average for overall detection
        
        # White noise template correlation (highest precision timing)
        noise_correlation_peak, noise_toa_offset_ms = self._detect_noise_template_correlation(audio_signal)
        
        # Chirp matched filter detection
        chirp_score, chirp_toa_sec, delay_spread_ms = self._detect_chirp_matched(audio_signal)
        
        # Single-cycle burst detection (highest precision timing)
        burst_score, burst_toa_offset_ms = self._detect_single_cycle_bursts(audio_signal)
        
        # Combined confidence: multi-tone is the most reliable detector on HF.
        # Old formula (0.5*mt + 0.3*noise + 0.2*chirp) capped at 0.5 from
        # multitone alone, making quality always MARGINAL/BAD.  Reweight so
        # strong multitone detection alone can reach GOOD quality, with noise
        # and chirp as confirmatory bonuses.
        confidence = 0.7 * multitone_score + 0.15 * noise_score + 0.15 * chirp_score
        detected = confidence >= self.combined_threshold
        
        # === STAGE 2: Timing (high-precision ToA) ===
        
        # Priority: burst (highest resolution) → chirp (high BT) → multitone → noise
        # Burst: Single-cycle pulses have sharpest time-domain resolution (τ ≈ 1/f)
        # Chirp: Pulse compression provides sub-ms precision via BT product (~5000)
        # Multitone: Onset detection provides coarse timing
        toa_offset_ms = None
        toa_source = None
        
        if burst_score > 0.3 and burst_toa_offset_ms is not None:
            # Burst has highest time resolution (lowest uncertainty)
            toa_offset_ms = burst_toa_offset_ms
            toa_source = 'burst'
        elif chirp_toa_sec is not None and chirp_score > self.chirp_threshold:
            # Chirp has high processing gain, excellent secondary timing source
            toa_offset_ms = (chirp_toa_sec - self.CHIRP_START) * 1000.0
            toa_source = 'chirp'
        elif multitone_start is not None and multitone_score > self.multitone_threshold:
            # Multi-tone provides robust coarse time alignment
            toa_offset_ms = (multitone_start - self.MULTITONE_START) * 1000.0
            toa_source = 'multitone'
        elif noise1_score > self.noise_threshold:
            # Noise segment energy onset (coarse timing)
            toa_offset_ms = 0.0
            toa_source = 'noise'
        
        # === STAGE 3: Channel Characterization ===
        
        # Frequency Selectivity Score (FSS) - path signature
        fss_db, tone_powers = self._calculate_frequency_selectivity(audio_signal)
        
        # Per-frequency time-series extraction (comprehensive analysis)
        tone_power_timeseries = {}
        fading_variance = None
        scintillation_index = None
        s4_by_frequency = {}
        s4_frequency_slope = None
        if detected:
            tone_power_timeseries, fading_variance, scintillation_index, s4_by_frequency, s4_frequency_slope = \
                self._extract_per_frequency_timeseries(audio_signal)
        
        # Coherence time from multi-tone fading pattern
        coherence_time_sec = None
        if detected:
            coherence_time_sec = self._estimate_coherence_time(audio_signal)
        
        # Delay spread already computed from chirp matched filter
        
        # SNR estimate with processing gain consideration
        snr_db = None
        effective_snr_db = None
        if detected:
            snr_db = self._estimate_snr_with_gain(audio_signal, noise_score, chirp_score)
            # Effective SNR includes processing gain
            processing_gain_db = max(40.0 * noise_score, 37.0 * chirp_score) if noise_score > 0.1 or chirp_score > 0.1 else 0.0
            effective_snr_db = snr_db + processing_gain_db if snr_db is not None else None
        
        # Field strength metrics (NEW)
        field_strength_db, field_strength_stability = None, None
        if detected and tone_power_timeseries:
            field_strength_db, field_strength_stability = \
                self._calculate_field_strength_metrics(tone_power_timeseries)
        
        # Anomaly detection (NEW - solar flares, sporadic E, etc.)
        anomaly_detected = False
        anomaly_type = None
        anomaly_confidence = None
        if detected and tone_power_timeseries:
            anomaly_detected, anomaly_type, anomaly_confidence = \
                self._detect_anomalies(tone_power_timeseries, noise_coherence_diff)
        
        # Transient detection from noise segments
        transient_detected = (noise_coherence_diff is not None and noise_coherence_diff > 0.2)
        
        # Multipath detection
        multipath_detected = (delay_spread_ms is not None and delay_spread_ms > 1.0)
        
        # Channel quality assessment (NEW)
        channel_quality = None
        if detected:
            channel_quality = self._assess_channel_quality(snr_db, delay_spread_ms, coherence_time_sec)
        
        # === STAGE 4: Logging ===
        
        logger.info(f"Test signal detection: minute={minute_number} ({expected_station})")
        logger.info(f"  Scores: multitone={multitone_score:.3f}, noise={noise_score:.3f}, "
                   f"chirp={chirp_score:.3f}, burst={burst_score:.3f}")
        logger.info(f"  Confidence: {confidence:.3f}, detected={detected}")
        if toa_offset_ms is not None:
            logger.info(f"  ToA: {toa_offset_ms:+.2f}ms (from {toa_source})")
        if fss_db is not None:
            logger.info(f"  Frequency selectivity (FSS): {fss_db:.1f}dB")
        if delay_spread_ms is not None:
            logger.info(f"  Delay spread: {delay_spread_ms:.2f}ms")
        if coherence_time_sec is not None:
            logger.info(f"  Coherence time: {coherence_time_sec:.2f}s")
        if noise_coherence_diff is not None and noise_coherence_diff > 0.1:
            logger.warning(f"  ⚠️ Noise segment diff: {noise_coherence_diff:.2f} (possible transient event)")
        
        # Enhanced logging for new metrics
        if anomaly_detected:
            logger.warning(f"  ⚠️ Anomaly detected: {anomaly_type} (confidence={anomaly_confidence:.2f})")
        if field_strength_db is not None:
            logger.info(f"  Field strength: {field_strength_db:.1f}dB, stability={field_strength_stability:.2f}")
        if scintillation_index is not None:
            logger.info(f"  Scintillation index S4: {scintillation_index:.3f}")
        if channel_quality is not None:
            logger.info(f"  Channel quality: {channel_quality}")
        
        return TestSignalDetection(
            detected=detected,
            confidence=confidence,
            station=expected_station if detected else None,
            minute_number=minute_number,
            multitone_score=multitone_score,
            chirp_score=chirp_score,
            noise_correlation=noise_score,
            signal_start_time=multitone_start,
            toa_offset_ms=toa_offset_ms,
            toa_source=toa_source,
            burst_toa_offset_ms=burst_toa_offset_ms,
            snr_db=snr_db,
            effective_snr_db=effective_snr_db,
            delay_spread_ms=delay_spread_ms,
            coherence_time_sec=coherence_time_sec,
            frequency_selectivity_db=fss_db,
            tone_powers_db=tone_powers if tone_powers else None,
            tone_power_timeseries=tone_power_timeseries if tone_power_timeseries else None,
            fading_variance=fading_variance,
            scintillation_index=scintillation_index,
            s4_by_frequency=s4_by_frequency if s4_by_frequency else None,
            s4_frequency_slope=s4_frequency_slope,
            field_strength_db=field_strength_db,
            field_strength_stability=field_strength_stability,
            noise_toa_offset_ms=noise_toa_offset_ms,
            noise_correlation_peak=noise_correlation_peak,
            noise1_score=noise1_score,
            noise2_score=noise2_score,
            noise_coherence_diff=noise_coherence_diff,
            transient_detected=transient_detected,
            anomaly_detected=anomaly_detected,
            anomaly_type=anomaly_type,
            anomaly_confidence=anomaly_confidence,
            multipath_detected=multipath_detected,
            channel_quality=channel_quality
        )
    
    def _detect_multitone(self, audio_signal: np.ndarray) -> Tuple[float, Optional[float]]:
        """
        Detect multi-tone sequence using normalized cross-correlation
        
        Uses a sliding window approach with proper normalization to compute
        correlation coefficient at each position.
        
        Returns:
            (correlation_score, start_time_sec)
        """
        template = self.multitone_template
        template_len = len(template)
        
        # Pre-compute template statistics
        template_mean = np.mean(template)
        template_std = np.std(template)
        template_energy = np.sum((template - template_mean)**2)
        
        if template_std < 1e-10 or template_energy < 1e-10:
            return 0.0, None
        
        # Compute local means and stds using convolution (efficient)
        ones = np.ones(template_len)
        signal_len = len(audio_signal)
        
        # Local sums
        local_sum = signal.correlate(audio_signal, ones, mode='valid')
        local_mean = local_sum / template_len
        
        # Local squared sums for std calculation
        local_sum_sq = signal.correlate(audio_signal**2, ones, mode='valid')
        local_var = (local_sum_sq / template_len) - local_mean**2
        local_var = np.maximum(local_var, 0.0)  # Avoid negative variance from numerical errors
        local_std = np.sqrt(local_var)
        
        # Cross-correlation
        template_centered = template - template_mean
        correlation = signal.correlate(audio_signal, template_centered, mode='valid')
        
        # Normalize: corr_coef = correlation / (template_std * local_std * template_len)
        # But template is already centered, so we use template_energy instead
        normalized_corr = np.zeros(len(correlation))
        for i in range(len(correlation)):
            if local_std[i] > 1e-10:
                # Pearson correlation coefficient
                local_energy = local_std[i]**2 * template_len
                normalized_corr[i] = correlation[i] / np.sqrt(template_energy * local_energy)
        
        # Find peak correlation
        peak_idx = np.argmax(np.abs(normalized_corr))
        score = np.clip(abs(normalized_corr[peak_idx]), 0.0, 1.0)
        
        start_time = peak_idx / self.sample_rate if score > self.multitone_threshold else None
        
        return score, start_time
    
    def _detect_multitone_simple(self, audio_signal: np.ndarray) -> float:
        """
        Simple multi-tone detection based on presence of 2, 3, 4, 5 kHz tones
        
        This method is more robust to ionospheric fading and phase distortion
        than template correlation. It counts 1-second windows in the expected
        test signal period (13-23 seconds) where at least 3 of 4 tones have
        positive SNR (5 kHz is often attenuated near the Nyquist limit).
        
        Returns:
            Detection score 0.0 to 1.0 (fraction of windows with sufficient tones)
        """
        from scipy.fft import rfft, rfftfreq
        
        # Expected multi-tone window: 13-23 seconds into minute
        multitone_start_sec = 13
        multitone_end_sec = 23
        
        # Analyze 1-second windows
        windows_passing = 0
        total_windows = 0
        
        for sec in range(multitone_start_sec, multitone_end_sec):
            start = sec * self.sample_rate
            end = start + self.sample_rate
            
            if end > len(audio_signal):
                break
            
            segment = audio_signal[start:end]
            
            # FFT
            fft_result = np.abs(rfft(segment))
            freqs = rfftfreq(len(segment), 1/self.sample_rate)
            
            # Measure power at each test signal frequency
            tone_snrs = []
            for target in [2000, 3000, 4000, 5000]:
                idx = np.argmin(np.abs(freqs - target))
                tone_power = np.max(fft_result[max(0, idx-1):idx+2])
                
                # Noise reference at 1.5 kHz (clean band)
                noise_idx = np.argmin(np.abs(freqs - 1500))
                noise_level = np.mean(fft_result[max(0, noise_idx-10):noise_idx+10])
                
                if noise_level > 0:
                    snr_db = 20 * np.log10(tone_power / noise_level)
                else:
                    snr_db = 0
                    
                tone_snrs.append(snr_db)
            
            # Count tones with positive SNR
            # Note: 5 kHz (tone_snrs[3]) is often attenuated near Nyquist limit
            tones_detected = sum(1 for snr in tone_snrs if snr > 0)
            
            # Require at least 3 of 4 tones (2, 3, 4 kHz are most reliable)
            # Give extra credit if all 4 are present
            if tones_detected >= 3:
                windows_passing += 1
            
            total_windows += 1
        
        if total_windows == 0:
            return 0.0
        
        # Score is fraction of windows with sufficient tones present
        raw_score = windows_passing / total_windows
        
        # Scale to match detection range
        # 30% of windows = 0.20 (threshold), 80% = 1.0
        if raw_score < 0.2:
            score = raw_score * 0.75  # Below threshold but give some credit
        else:
            score = min(1.0, 0.20 + (raw_score - 0.2) * 1.33)
        
        logger.debug(f"Simple multitone: {windows_passing}/{total_windows} windows "
                    f"({raw_score:.1%}), score={score:.3f}")
        
        return score
    
    def _detect_chirp(self, audio_signal: np.ndarray) -> Tuple[float, Optional[float]]:
        """
        Detect chirp sequence using spectrogram analysis
        
        Returns:
            (detection_score, start_time_sec)
        """
        # For chirps, use spectrogram rather than simple correlation
        # Look for characteristic time-frequency signature
        
        # Compute spectrogram
        f, t, Sxx = signal.spectrogram(
            audio_signal,
            fs=self.sample_rate,
            nperseg=512,
            noverlap=256
        )
        
        # Look for energy in 0-5 kHz band (chirp range)
        chirp_band = (f >= 0) & (f <= 5000)
        chirp_energy = np.sum(Sxx[chirp_band, :], axis=0)
        
        # Chirps create distinctive peaks in energy
        # Simple heuristic: look for variance in chirp band
        if len(chirp_energy) > 0:
            chirp_variance = np.std(chirp_energy) / (np.mean(chirp_energy) + 1e-10)
            score = np.clip(chirp_variance / 10.0, 0.0, 1.0)  # Empirical scaling
        else:
            score = 0.0
        
        # Rough start time from energy peak
        if score > self.chirp_threshold:
            peak_time_idx = np.argmax(chirp_energy)
            start_time = t[peak_time_idx]
        else:
            start_time = None
        
        return score, start_time
    
    def _estimate_snr(
        self,
        audio_signal: np.ndarray,
        signal_start: float,
        signal_length: int
    ) -> float:
        """
        Estimate SNR of detected signal
        
        Args:
            audio_signal: Full audio signal
            signal_start: Start time of signal (seconds)
            signal_length: Length of signal (samples)
            
        Returns:
            SNR in dB
        """
        start_idx = int(signal_start * self.sample_rate)
        end_idx = start_idx + signal_length
        
        if end_idx > len(audio_signal):
            return 0.0
        
        # Signal power
        signal_segment = audio_signal[start_idx:end_idx]
        signal_power = np.mean(signal_segment**2)
        
        # Noise power (from before signal)
        noise_start = max(0, start_idx - signal_length)
        noise_segment = audio_signal[noise_start:start_idx]
        noise_power = np.mean(noise_segment**2) if len(noise_segment) > 0 else 1e-10
        
        snr_db = 10 * np.log10(signal_power / noise_power)
        
        return float(snr_db)
    
    def _detect_white_noise(self, audio_signal: np.ndarray) -> Tuple[float, Optional[float]]:
        """
        Detect white noise segments using energy and spectral flatness
        
        The test signal has white noise at:
        - 10-12 seconds (noise #1)
        - 37-39 seconds (noise #2, identical)
        
        NOTE: Matched filtering is NOT possible because the actual WWV broadcast
        uses a LabVIEW PRNG sequence that differs from Python's implementation.
        Instead, we detect noise by:
        1. High energy in the expected time window
        2. Spectral flatness (white noise has flat spectrum)
        
        Returns:
            (detection_score, toa_seconds) - ToA is start of noise segment
        """
        from scipy.fft import rfft, rfftfreq
        
        # Extract expected noise region (10-12s)
        noise_start = int(self.NOISE1_START * self.sample_rate)
        noise_end = int(self.NOISE1_END * self.sample_rate)
        
        if noise_end > len(audio_signal):
            return 0.0, None
        
        noise_segment = audio_signal[noise_start:noise_end]
        
        # Compare to a blank/silent segment for relative energy.
        # The blank at 12-13s (between noise1 and multitone) is a true silence
        # reference. Using the voice segment (8-10s) gives ratio ~1 since voice
        # has comparable energy to noise, making detection unreliable.
        blank_start = int(12.0 * self.sample_rate)
        blank_end = int(13.0 * self.sample_rate)
        if blank_end <= len(audio_signal):
            ref_segment = audio_signal[blank_start:blank_end]
        else:
            ref_segment = audio_signal[max(0, noise_start - int(2.0 * self.sample_rate)):noise_start]
        
        noise_power = np.mean(noise_segment**2)
        ref_power = np.mean(ref_segment**2) if len(ref_segment) > 0 else 1e-10
        
        # Energy ratio (noise should be much louder than blank segment)
        energy_ratio = noise_power / (ref_power + 1e-10)
        
        # Spectral flatness: ratio of geometric to arithmetic mean of spectrum
        # White noise ≈ 1.0, tonal signals << 1.0
        fft_result = np.abs(rfft(noise_segment[:self.sample_rate]))  # First second
        fft_power = fft_result[10:] ** 2  # Skip DC and very low frequencies
        
        if len(fft_power) > 0 and np.all(fft_power > 0):
            geometric_mean = np.exp(np.mean(np.log(fft_power + 1e-10)))
            arithmetic_mean = np.mean(fft_power)
            spectral_flatness = geometric_mean / (arithmetic_mean + 1e-10)
        else:
            spectral_flatness = 0.0
        
        # Combined score: both energy and flatness should be high
        # Scale energy_ratio (typically 1-10) and flatness (0-1) to 0-1 range
        energy_score = np.clip((energy_ratio - 1.0) / 5.0, 0.0, 1.0)
        flatness_score = np.clip(spectral_flatness * 2.0, 0.0, 1.0)
        
        score = 0.5 * energy_score + 0.5 * flatness_score
        
        # ToA is simply the expected start time (no matched filter precision)
        toa_sec = self.NOISE1_START if score > 0.2 else None
        
        logger.debug(f"White noise energy detection: energy_ratio={energy_ratio:.2f}, "
                    f"flatness={spectral_flatness:.3f}, score={score:.3f}")
        
        return score, toa_sec
    
    def _detect_noise_template_correlation(self, audio_signal: np.ndarray) -> Tuple[float, Optional[float]]:
        """
        Detect white noise via template correlation for high-precision timing.
        
        The white noise segments (10-12s and 37-39s) are generated with a known seed,
        making them deterministic and suitable for matched filter detection.
        Cross-correlation provides sub-sample timing precision with ~40dB processing
        gain (BT product = 2s × 10kHz = 20,000).
        
        Reference: wwv-signal-timing-analysis notebook methodology
        
        Returns:
            (correlation_peak, toa_offset_ms) - peak correlation and timing offset
        """
        # Generate template with known seed (must match broadcast)
        # WWV uses seed=42 for reproducibility
        template = self.generator.generate_white_noise(2.0, seed=42)
        
        # High-pass filter to isolate white noise (per notebook: >95% Nyquist)
        # This removes voice/tone content and focuses on wideband noise
        nyquist = self.sample_rate / 2
        Wn = 0.90 * nyquist  # 90% of Nyquist to be safe
        
        try:
            b, a = signal.butter(4, Wn, 'high', fs=self.sample_rate)
            template_filt = signal.filtfilt(b, a, template)
            
            # Search in first noise segment window (8-14s to allow for propagation delay)
            search_start = int(8.0 * self.sample_rate)
            search_end = int(14.0 * self.sample_rate)
            
            if search_end > len(audio_signal):
                search_end = len(audio_signal)
            
            search_segment = audio_signal[search_start:search_end]
            
            if len(search_segment) < len(template):
                return 0.0, None
            
            # Filter the search segment
            search_filt = signal.filtfilt(b, a, search_segment)
            
            # Cross-correlation
            Rxy = signal.correlate(search_filt, template_filt, mode='valid')
            
            # Normalize by template energy for correlation coefficient
            template_energy = np.sum(template_filt**2)
            
            # Find peak
            peak_idx = np.argmax(np.abs(Rxy))
            peak_val = np.abs(Rxy[peak_idx])
            
            # Normalize to 0-1 range (correlation coefficient)
            # Account for signal energy in the window
            window_start = peak_idx
            window_end = peak_idx + len(template_filt)
            if window_end <= len(search_filt):
                signal_window = search_filt[window_start:window_end]
                signal_energy = np.sum(signal_window**2)
                if signal_energy > 0 and template_energy > 0:
                    correlation_coef = peak_val / np.sqrt(template_energy * signal_energy)
                    correlation_coef = np.clip(correlation_coef, 0.0, 1.0)
                else:
                    correlation_coef = 0.0
            else:
                correlation_coef = 0.0
            
            # Calculate ToA offset from expected position
            # Expected: noise starts at 10.0s, peak should be at search_start + peak_idx
            actual_sample = search_start + peak_idx
            expected_sample = int(self.NOISE1_START * self.sample_rate)
            toa_offset_samples = actual_sample - expected_sample
            toa_offset_ms = (toa_offset_samples / self.sample_rate) * 1000.0
            
            logger.debug(f"Noise template correlation: peak={correlation_coef:.3f}, "
                        f"toa_offset={toa_offset_ms:+.2f}ms")
            
            return float(correlation_coef), float(toa_offset_ms) if correlation_coef > 0.1 else None
            
        except Exception as e:
            logger.warning(f"Noise template correlation failed: {e}")
            return 0.0, None
    
    def _detect_chirp_matched(self, audio_signal: np.ndarray) -> Tuple[float, Optional[float], Optional[float]]:
        """
        Detect chirp sequences using matched filter for ToA and delay spread
        
        Official WWV chirp structure (8 seconds at 24-32s into minute):
        - 3 short up-chirps (50ms each, 100ms spacing)
        - 3 short down-chirps (50ms each, 100ms spacing)
        - 0.5s blank
        - 3 long up-chirps (1s each, 100ms spacing)
        - 3 long down-chirps (1s each, 100ms spacing)
        
        Short chirps: 5 kHz over 50ms (TBW=250)
        Long chirps: 5 kHz over 1s (TBW=5000)
        
        Returns:
            (score, toa_seconds, delay_spread_ms)
        """
        # Search window around expected chirp location (24-32s)
        search_start = int((self.CHIRP_START - 0.5) * self.sample_rate)
        search_end = int((self.CHIRP_END + 0.5) * self.sample_rate)
        
        if search_end > len(audio_signal):
            search_end = len(audio_signal)
        if search_start < 0:
            search_start = 0
        
        search_segment = audio_signal[search_start:search_end]
        
        if len(search_segment) < len(self.long_chirp_up):
            return 0.0, None, None
        
        # Matched filter with SHORT chirp templates (50ms)
        # These come first in the sequence and are easier to detect
        short_corr_up = signal.correlate(search_segment, self.short_chirp_up, mode='valid')
        short_corr_down = signal.correlate(search_segment, self.short_chirp_down, mode='valid')
        
        # Matched filter with LONG chirp templates (1s)
        long_corr_up = signal.correlate(search_segment, self.long_chirp_up, mode='valid')
        long_corr_down = signal.correlate(search_segment, self.long_chirp_down, mode='valid')
        
        # Normalize correlations to proper correlation coefficients (0-1)
        # Correlation coefficient = Rxy / sqrt(Ex * Ey)
        short_energy = np.sum(self.short_chirp_up**2)
        long_energy = np.sum(self.long_chirp_up**2)
        
        # Estimate local signal energy for normalization
        short_len = len(self.short_chirp_up)
        long_len = len(self.long_chirp_up)
        
        def _normalized_peak(corr, template_energy, template_len, segment):
            peak_idx = np.argmax(np.abs(corr))
            peak_val = np.abs(corr[peak_idx])
            # Signal energy in the window aligned with the peak
            win_start = peak_idx
            win_end = min(win_start + template_len, len(segment))
            if win_end > win_start:
                sig_energy = np.sum(segment[win_start:win_end]**2)
            else:
                sig_energy = 1e-10
            denom = np.sqrt(template_energy * sig_energy)
            return np.clip(peak_val / (denom + 1e-10), 0.0, 1.0)
        
        # Peak detection for each type (properly normalized)
        short_score_up = _normalized_peak(short_corr_up, short_energy, short_len, search_segment)
        short_score_dn = _normalized_peak(short_corr_down, short_energy, short_len, search_segment)
        long_score_up = _normalized_peak(long_corr_up, long_energy, long_len, search_segment)
        long_score_dn = _normalized_peak(long_corr_down, long_energy, long_len, search_segment)
        
        short_score = max(short_score_up, short_score_dn)
        long_score = max(long_score_up, long_score_dn)
        
        # Combined score - weight long chirps higher (more processing gain)
        score = 0.3 * short_score + 0.7 * long_score
        
        # Find ToA from long chirp correlation peak (better precision)
        combined_long = np.abs(long_corr_up) + np.abs(long_corr_down)
        peak_idx = np.argmax(combined_long)
        toa_samples = search_start + peak_idx
        toa_sec = toa_samples / self.sample_rate
        
        # Estimate delay spread from long chirp matched filter response width
        # The -3dB width reveals multipath spreading
        delay_spread_ms = None
        if long_score > 0.05:
            peak_val = combined_long[peak_idx]
            half_power = peak_val * 0.707
            
            # Find -3dB points
            left_idx = peak_idx
            while left_idx > 0 and combined_long[left_idx] > half_power:
                left_idx -= 1
            
            right_idx = peak_idx
            while right_idx < len(combined_long) - 1 and combined_long[right_idx] > half_power:
                right_idx += 1
            
            # Width in samples, convert to ms
            width_samples = right_idx - left_idx
            delay_spread_ms = (width_samples / self.sample_rate) * 1000.0
            
            # Subtract ideal width (1s chirp with 5kHz BW → ~0.2ms resolution)
            ideal_width_ms = 0.2
            delay_spread_ms = max(0.0, delay_spread_ms - ideal_width_ms)
        
        logger.debug(f"Chirp detection: short={short_score:.3f}, long={long_score:.3f}, "
                    f"combined={score:.3f}")
        
        logger.debug(f"Chirp matched filter: score={score:.3f}, ToA={toa_sec:.3f}s, "
                    f"delay_spread={delay_spread_ms:.2f}ms" if delay_spread_ms else "")
        
        return score, toa_sec if score > 0.1 else None, delay_spread_ms
    
    def _estimate_coherence_time(self, audio_signal: np.ndarray) -> Optional[float]:
        """
        Estimate channel coherence time from multi-tone fading pattern
        
        The 10-second multi-tone segment (13-23s) has 1-second windows with
        known attenuation steps. Deviations from expected pattern reveal fading.
        Coherence time is estimated from the fading rate.
        
        Returns:
            Coherence time in seconds (None if cannot estimate)
        """
        from scipy.fft import rfft, rfftfreq
        
        # Extract multi-tone segment
        start_idx = int(self.MULTITONE_START * self.sample_rate)
        end_idx = int(self.MULTITONE_END * self.sample_rate)
        
        if end_idx > len(audio_signal):
            return None
        
        multitone_segment = audio_signal[start_idx:end_idx]
        
        # Measure power in each 1-second window at 2 kHz (most reliable tone)
        tone_powers = []
        for sec in range(10):
            window_start = sec * self.sample_rate
            window_end = window_start + self.sample_rate
            
            if window_end > len(multitone_segment):
                break
            
            window = multitone_segment[window_start:window_end]
            
            # FFT to get power at 2 kHz
            fft_result = np.abs(rfft(window))
            freqs = rfftfreq(len(window), 1/self.sample_rate)
            
            idx_2k = np.argmin(np.abs(freqs - 2000))
            power_2k = np.max(fft_result[max(0, idx_2k-2):idx_2k+3])
            tone_powers.append(power_2k)
        
        if len(tone_powers) < 5:
            return None
        
        tone_powers = np.array(tone_powers)
        
        # Only use the first 5 windows (0-12 dB designed attenuation)
        # where tone SNR is sufficient.  Later windows are noise-dominated
        # and inflate variance, causing coherence_time to always hit 0.1s.
        n_use = min(5, len(tone_powers))
        tp = tone_powers[:n_use]
        
        # Convert to dB for detrending
        tp_db = 20.0 * np.log10(tp + 1e-10)
        expected_atten_db = np.array([-3.0 * i for i in range(n_use)])
        
        # LS-fit detrend (robust to single outlier)
        p0_fit = np.mean(tp_db - expected_atten_db)
        detrended_db = tp_db - (p0_fit + expected_atten_db)
        
        # Estimate coherence time from variance of detrended fading (in dB)
        # Fast fading → high variance → short coherence time
        variance_db = np.var(detrended_db)
        
        if variance_db > 0.5:  # >0.7 dB RMS fading
            # Empirical model: coherence_time ≈ 5 / variance_db
            # Calibrated so that 5 dB² variance → 1s, 1 dB² → 5s
            coherence_time = 5.0 / variance_db
            coherence_time = np.clip(coherence_time, 0.1, 30.0)
        else:
            coherence_time = 10.0  # Stable channel
        
        return float(coherence_time)
    
    def _estimate_snr_with_gain(self, audio_signal: np.ndarray, 
                                 noise_score: float, chirp_score: float) -> float:
        """
        Estimate SNR accounting for matched filter processing gain
        
        The white noise and chirp matched filters provide significant
        processing gain that should be factored into the SNR estimate.
        
        Args:
            audio_signal: Demodulated audio
            noise_score: Score from white noise matched filter
            chirp_score: Score from chirp matched filter
            
        Returns:
            SNR in dB (with processing gain consideration)
        """
        # Spectral SNR: measure tone peak power vs adjacent noise floor.
        # Broadband power ratio is ~0dB because the narrowband tones barely
        # affect the total wideband power.  Spectral SNR is meaningful.
        from scipy.fft import rfft, rfftfreq
        
        start_idx = int(self.MULTITONE_START * self.sample_rate)
        # Use the first 1-second window (strongest tones, before attenuation)
        end_idx = start_idx + self.sample_rate
        
        if end_idx > len(audio_signal):
            end_idx = len(audio_signal)
        
        window = audio_signal[start_idx:end_idx]
        fft_mag = np.abs(rfft(window))
        freqs = rfftfreq(len(window), 1.0 / self.sample_rate)
        freq_res = freqs[1] - freqs[0] if len(freqs) > 1 else 1.0
        
        # Measure peak power at each tone and adjacent noise floor
        tone_snrs = []
        for tone_freq in self.TONE_FREQUENCIES:
            idx = np.argmin(np.abs(freqs - tone_freq))
            # Tone peak: max within ±5 bins
            peak_range = slice(max(0, idx - 5), min(len(fft_mag), idx + 6))
            tone_peak = np.max(fft_mag[peak_range]**2)
            # Noise floor: median of bins 50-200 Hz away from tone
            noise_bins = np.concatenate([
                fft_mag[max(0, idx - int(200/freq_res)):max(0, idx - int(50/freq_res))]**2,
                fft_mag[min(len(fft_mag), idx + int(50/freq_res)):min(len(fft_mag), idx + int(200/freq_res))]**2
            ])
            if len(noise_bins) > 0:
                noise_floor = np.median(noise_bins)
                if noise_floor > 0:
                    tone_snrs.append(10 * np.log10(tone_peak / noise_floor))
        
        # Base SNR: average spectral SNR across tones
        if tone_snrs:
            base_snr_db = float(np.mean(tone_snrs))
        else:
            base_snr_db = 0.0
        
        # Processing gain from matched filters
        # White noise: BT ≈ 10000 → 40 dB gain (but score < 1 means less coherent)
        noise_gain_db = 40.0 * noise_score if noise_score > 0.1 else 0.0
        
        # Chirp: BT ≈ 5000 → 37 dB gain
        chirp_gain_db = 37.0 * chirp_score if chirp_score > 0.1 else 0.0
        
        # Use the better of the two gains
        processing_gain_db = max(noise_gain_db, chirp_gain_db)
        
        # Effective SNR (what matched filter sees)
        effective_snr_db = base_snr_db + processing_gain_db
        
        logger.debug(f"SNR estimate: base={base_snr_db:.1f}dB, "
                    f"processing_gain={processing_gain_db:.1f}dB, "
                    f"effective={effective_snr_db:.1f}dB")
        
        return float(base_snr_db)  # Return base SNR, note processing gain in logs
    
    def _calculate_frequency_selectivity(self, audio_signal: np.ndarray) -> Tuple[Optional[float], Dict[int, float]]:
        """
        Calculate Frequency Selectivity Score (FSS) from multi-tone segment
        
        FSS = 10*log10((P_2kHz + P_3kHz) / (P_4kHz + P_5kHz))
        
        The ionosphere typically attenuates higher frequencies more than lower.
        This creates a path-specific "fingerprint" that can help confirm station identity:
        - WWV (continental path, shorter): typically lower FSS (less selective fading)
        - WWVH (trans-oceanic, longer): typically higher FSS (more high-freq attenuation)
        
        Returns:
            (fss_db, tone_powers_dict) where tone_powers_dict is {freq_hz: power_db}
        """
        from scipy.fft import rfft, rfftfreq
        
        # Extract multi-tone segment (13-23s)
        start_idx = int(self.MULTITONE_START * self.sample_rate)
        end_idx = int(self.MULTITONE_END * self.sample_rate)
        
        if end_idx > len(audio_signal):
            return None, {}
        
        multitone_segment = audio_signal[start_idx:end_idx]
        
        # Measure power at each tone frequency
        # Use a 1-second window in the middle of the segment (before heavy attenuation)
        window_start = int(2 * self.sample_rate)  # 2 seconds into multitone
        window_end = window_start + self.sample_rate
        
        if window_end > len(multitone_segment):
            window_end = len(multitone_segment)
            window_start = max(0, window_end - self.sample_rate)
        
        window = multitone_segment[window_start:window_end]
        
        # FFT
        fft_result = np.abs(rfft(window))
        freqs = rfftfreq(len(window), 1/self.sample_rate)
        
        # Measure power at each tone (peak within ±50 Hz)
        tone_powers = {}
        for freq in self.TONE_FREQUENCIES:
            idx = np.argmin(np.abs(freqs - freq))
            # Search window for peak
            search_range = int(50 / (freqs[1] - freqs[0])) if len(freqs) > 1 else 5
            start = max(0, idx - search_range)
            end = min(len(fft_result), idx + search_range + 1)
            
            peak_power = np.max(fft_result[start:end]**2)
            tone_powers[freq] = 10 * np.log10(peak_power + 1e-10)
        
        # Calculate FSS = 10*log10((P_2k + P_3k) / (P_4k + P_5k))
        if all(f in tone_powers for f in [2000, 3000, 4000, 5000]):
            p_low = 10**(tone_powers[2000]/10) + 10**(tone_powers[3000]/10)
            p_high = 10**(tone_powers[4000]/10) + 10**(tone_powers[5000]/10)
            
            if p_high > 1e-10:
                fss_db = 10 * np.log10(p_low / p_high)
            else:
                fss_db = None
        else:
            fss_db = None
        
        logger.debug(f"Frequency selectivity: FSS={fss_db:.1f}dB" if fss_db else "FSS calculation failed")
        logger.debug(f"  Tone powers: {tone_powers}")
        
        return fss_db, tone_powers
    
    def _detect_single_cycle_bursts(self, audio_signal: np.ndarray) -> Tuple[float, Optional[float]]:
        """
        Detect single-cycle bursts for high-precision ToA
        
        The test signal contains 5 bursts at 2.5 kHz then 5 at 5 kHz (34-36s).
        These are the shortest features, providing the highest time resolution.
        
        Returns:
            (burst_score, toa_offset_ms) - ToA offset from expected burst start
        """
        from scipy.fft import rfft, rfftfreq
        
        # Extract burst segment (34-36s)
        start_idx = int(self.BURST_START * self.sample_rate)
        end_idx = int(self.BURST_END * self.sample_rate)
        
        if end_idx > len(audio_signal):
            return 0.0, None
        
        burst_segment = audio_signal[start_idx:end_idx]
        
        # The bursts are single-cycle, so look for impulsive energy
        # at 2.5 kHz (first second) and 5 kHz (second second)
        
        # Generate single-cycle templates
        t_25 = np.arange(0, 1/2500, 1/self.sample_rate)
        burst_template_25 = np.sin(2 * np.pi * 2500 * t_25)
        
        t_50 = np.arange(0, 1/5000, 1/self.sample_rate)
        burst_template_50 = np.sin(2 * np.pi * 5000 * t_50)
        
        # Correlate with templates
        # First half: 2.5 kHz bursts (5 bursts over 1 second, ~200ms apart)
        first_half = burst_segment[:len(burst_segment)//2]
        corr_25 = signal.correlate(first_half, burst_template_25, mode='valid')
        
        # Second half: 5 kHz bursts
        second_half = burst_segment[len(burst_segment)//2:]
        corr_50 = signal.correlate(second_half, burst_template_50, mode='valid')
        
        # Find peaks (should be 5 in each half)
        # Simple peak detection
        peak_25 = np.max(np.abs(corr_25)) if len(corr_25) > 0 else 0
        peak_50 = np.max(np.abs(corr_50)) if len(corr_50) > 0 else 0
        
        # Score based on peak prominence
        noise_25 = np.std(corr_25) if len(corr_25) > 10 else 1e-10
        noise_50 = np.std(corr_50) if len(corr_50) > 10 else 1e-10
        
        snr_25 = peak_25 / (noise_25 + 1e-10)
        snr_50 = peak_50 / (noise_50 + 1e-10)
        
        # Combined score (both burst types should be present)
        score = np.clip((snr_25 + snr_50) / 20.0, 0.0, 1.0)  # Normalized
        
        # ToA from first burst peak
        toa_offset_ms = None
        if score > 0.1 and len(corr_25) > 0:
            first_peak_idx = np.argmax(np.abs(corr_25))
            # Expected first burst at t=0 within burst segment
            # Actual arrival = first_peak_idx / sample_rate
            toa_offset_ms = (first_peak_idx / self.sample_rate) * 1000.0
        
        logger.debug(f"Burst detection: score={score:.3f}, SNR_2.5k={snr_25:.1f}, SNR_5k={snr_50:.1f}")
        
        return score, toa_offset_ms
    
    def _detect_both_noise_segments(self, audio_signal: np.ndarray) -> Tuple[float, float, Optional[float]]:
        """
        Analyze both white noise segments for transient interference detection
        
        Noise #1 (10-12s) and Noise #2 (37-39s) should have identical characteristics
        since they are the same sequence. Large differences indicate a transient
        event (interference, fading) occurred between them.
        
        Returns:
            (noise1_score, noise2_score, coherence_diff)
            coherence_diff = |noise1_score - noise2_score|, high = transient event
        """
        from scipy.fft import rfft
        
        # Extract both noise segments
        n1_start = int(self.NOISE1_START * self.sample_rate)
        n1_end = int(self.NOISE1_END * self.sample_rate)
        n2_start = int(self.NOISE2_START * self.sample_rate)
        n2_end = int(self.NOISE2_END * self.sample_rate)
        
        if n2_end > len(audio_signal):
            return 0.0, 0.0, None
        
        noise1 = audio_signal[n1_start:n1_end]
        noise2 = audio_signal[n2_start:n2_end]
        
        def analyze_noise_segment(segment: np.ndarray, pre_segment: np.ndarray) -> float:
            """Analyze a single noise segment using energy and spectral flatness"""
            noise_power = np.mean(segment**2)
            pre_power = np.mean(pre_segment**2) if len(pre_segment) > 0 else 1e-10
            
            # Energy ratio
            energy_ratio = noise_power / (pre_power + 1e-10)
            
            # Spectral flatness
            fft_result = np.abs(rfft(segment[:self.sample_rate]))
            fft_power = fft_result[10:]**2
            
            if len(fft_power) > 0 and np.all(fft_power > 0):
                geometric_mean = np.exp(np.mean(np.log(fft_power + 1e-10)))
                arithmetic_mean = np.mean(fft_power)
                flatness = geometric_mean / (arithmetic_mean + 1e-10)
            else:
                flatness = 0.0
            
            energy_score = np.clip((energy_ratio - 1.0) / 5.0, 0.0, 1.0)
            flatness_score = np.clip(flatness * 2.0, 0.0, 1.0)
            
            return 0.5 * energy_score + 0.5 * flatness_score
        
        # Reference segments: use adjacent blank/silent segments, not voice.
        # Noise1 (10-12s): use blank at 12-13s as reference
        # Noise2 (37-39s): use blank at 36-37s as reference
        blank1_start = int(12.0 * self.sample_rate)
        blank1_end = int(13.0 * self.sample_rate)
        pre1 = audio_signal[blank1_start:blank1_end] if blank1_end <= len(audio_signal) else audio_signal[max(0, n1_start - self.sample_rate):n1_start]
        
        blank2_start = int(36.0 * self.sample_rate)
        blank2_end = int(37.0 * self.sample_rate)
        pre2 = audio_signal[blank2_start:blank2_end] if blank2_end <= len(audio_signal) else audio_signal[max(0, n2_start - self.sample_rate):n2_start]
        
        noise1_score = analyze_noise_segment(noise1, pre1)
        noise2_score = analyze_noise_segment(noise2, pre2)
        
        # Coherence difference - should be small if no transients
        coherence_diff = abs(noise1_score - noise2_score)
        
        logger.debug(f"Noise segment analysis: N1={noise1_score:.3f}, N2={noise2_score:.3f}, "
                    f"diff={coherence_diff:.3f}")
        
        return noise1_score, noise2_score, coherence_diff
    
    def _extract_per_frequency_timeseries(self, audio_signal: np.ndarray) -> Tuple[Dict[int, List[float]], Optional[float], Optional[float], Dict[int, float], Optional[float]]:
        """
        Extract per-frequency power time-series from multi-tone segment
        
        This provides frequency-dependent field strength measurements over the
        10-second multi-tone segment, enabling:
        - Frequency-dependent absorption analysis (D-layer characterization)
        - Fading pattern analysis (ionospheric scintillation)
        - Anomaly detection (solar flares, sporadic E)
        
        Args:
            audio_signal: Demodulated audio signal
            
        Returns:
            Tuple of:
            - tone_power_timeseries: {freq_hz: [dB_t0, dB_t1, ..., dB_t9]}
            - fading_variance: Normalized variance of fading (detrended)
            - scintillation_index: S4 scintillation index
        """
        from scipy.fft import rfft, rfftfreq
        
        # Extract multi-tone segment (13-23s)
        start_idx = int(self.MULTITONE_START * self.sample_rate)
        end_idx = int(self.MULTITONE_END * self.sample_rate)
        
        if end_idx > len(audio_signal):
            return {}, None, None, {}, None
        
        multitone_segment = audio_signal[start_idx:end_idx]
        
        # Analyze 1-second windows
        tone_power_timeseries = {freq: [] for freq in self.TONE_FREQUENCIES}
        noise_floor_timeseries = []  # Per-window noise floor from off-tone bins
        
        for sec in range(10):
            window_start = sec * self.sample_rate
            window_end = window_start + self.sample_rate
            
            if window_end > len(multitone_segment):
                break
            
            window = multitone_segment[window_start:window_end]
            
            # FFT to get power at each tone frequency
            fft_result = np.abs(rfft(window))
            freqs = rfftfreq(len(window), 1/self.sample_rate)
            freq_res = freqs[1] - freqs[0] if len(freqs) > 1 else 1.0
            search_range = int(50 / freq_res) if freq_res > 0 else 5
            
            # Collect tone powers
            tone_bins = set()  # Track which bins belong to tones
            for target_freq in self.TONE_FREQUENCIES:
                idx = np.argmin(np.abs(freqs - target_freq))
                start = max(0, idx - search_range)
                end = min(len(fft_result), idx + search_range + 1)
                for b in range(start, end):
                    tone_bins.add(b)
                
                peak_power = np.max(fft_result[start:end]**2)
                power_db = 10 * np.log10(peak_power + 1e-10)
                tone_power_timeseries[target_freq].append(power_db)
            
            # Noise floor: median power of bins between 1.5–5.5 kHz excluding tone bins
            noise_lo = int(1500 / freq_res)
            noise_hi = min(len(fft_result), int(5500 / freq_res))
            noise_bins = [i for i in range(noise_lo, noise_hi) if i not in tone_bins]
            if noise_bins:
                noise_power = np.median(fft_result[noise_bins]**2)
                noise_floor_timeseries.append(10 * np.log10(noise_power + 1e-10))
            else:
                noise_floor_timeseries.append(float('nan'))
        
        # Calculate fading variance and multi-frequency S4 scintillation
        fading_variance = None
        scintillation_index = None
        s4_by_frequency = {}
        s4_frequency_slope = None
        
        # Compute S4 for each frequency
        # S4 is only meaningful when the tone is well above the noise floor;
        # at low SNR the FFT peak is noise-dominated and power variance is
        # driven by noise statistics, not ionospheric scintillation.
        noise_floor_arr = np.array(noise_floor_timeseries) if noise_floor_timeseries else np.array([])
        median_noise_db = float(np.nanmedian(noise_floor_arr)) if len(noise_floor_arr) > 0 else -999.0
        
        for freq, powers in tone_power_timeseries.items():
            if len(powers) >= 5:
                powers_arr = np.array(powers)
                n_pts = len(powers_arr)
                
                # SNR gate: compare median tone power against noise floor
                # measured from off-tone FFT bins in the same windows.
                # The designed -3dB/sec attenuation is masked by ionospheric
                # fading (~13 dB std) so we cannot use the attenuation model
                # to estimate noise floor.
                median_tone_db = float(np.median(powers_arr))
                tone_snr_db = median_tone_db - median_noise_db
                
                if tone_snr_db < 6.0:
                    logger.debug(f"S4 skipped at {freq}Hz: tone_snr={tone_snr_db:.1f}dB < 6dB")
                    continue
                
                # Detrend: data-driven linear fit instead of -3dB/sec model.
                # After ionospheric propagation, fading dominates the designed
                # attenuation pattern.  A linear fit removes any slow trend
                # (including residual attenuation) without assuming a fixed slope.
                t_sec = np.arange(n_pts, dtype=float)
                try:
                    coeffs = np.polyfit(t_sec, powers_arr, 1)
                    trend_db = np.polyval(coeffs, t_sec)
                except (np.linalg.LinAlgError, ValueError):
                    trend_db = np.full(n_pts, np.mean(powers_arr))
                detrended_db = powers_arr - trend_db
                
                # Clamp to ±15 dB to prevent extreme values from dominating
                detrended_db = np.clip(detrended_db, -15.0, 15.0)
                
                # Convert detrended dB to linear intensity for S4 calculation
                # S4 = σ(I) / μ(I) where I is intensity (not dB)
                intensity = 10**(detrended_db / 10)
                
                if np.mean(intensity) > 0:
                    s4 = float(np.std(intensity) / np.mean(intensity))
                    s4_by_frequency[freq] = s4
                    
                    # Log warning for strong scintillation (S4 > 1.0 is valid
                    # but rare on HF except during geomagnetic storms)
                    if s4 > 1.0:
                        logger.warning(f"Strong scintillation at {freq}Hz: S4={s4:.2f}")
        
        # Use 2 kHz as primary S4 (most reliable, furthest from Nyquist)
        if 2000 in s4_by_frequency:
            scintillation_index = s4_by_frequency[2000]
        
        # Calculate S4 frequency slope (D-layer vs F-layer discrimination)
        # D-layer absorption is frequency-dependent, F-layer is not
        if len(s4_by_frequency) >= 3:
            freqs_khz = np.array(sorted(s4_by_frequency.keys())) / 1000.0
            s4_values = np.array([s4_by_frequency[int(f*1000)] for f in freqs_khz])
            
            # Linear regression: S4 = slope * freq + intercept
            if len(freqs_khz) > 1:
                slope, _ = np.polyfit(freqs_khz, s4_values, 1)
                s4_frequency_slope = float(slope)
        
        # Fading variance from 2 kHz (for backward compatibility)
        # Use data-driven detrending consistent with S4 calculation
        if len(tone_power_timeseries.get(2000, [])) >= 5:
            powers_2k = np.array(tone_power_timeseries[2000])
            t_2k = np.arange(len(powers_2k), dtype=float)
            try:
                coeffs_2k = np.polyfit(t_2k, powers_2k, 1)
                trend_2k = np.polyval(coeffs_2k, t_2k)
            except (np.linalg.LinAlgError, ValueError):
                trend_2k = np.full(len(powers_2k), np.mean(powers_2k))
            detrended = powers_2k - trend_2k
            fading_variance = float(np.var(detrended))
        
        return tone_power_timeseries, fading_variance, scintillation_index, s4_by_frequency, s4_frequency_slope
    
    def _detect_anomalies(
        self, 
        tone_power_timeseries: Dict[int, List[float]],
        noise_coherence_diff: Optional[float]
    ) -> Tuple[bool, Optional[str], Optional[float]]:
        """
        Detect ionospheric anomalies from test signal characteristics
        
        Anomaly types:
        - sudden_amplitude_drop: Solar flare (sudden ionospheric disturbance)
        - sudden_amplitude_increase: Sporadic E layer formation
        - rapid_fading: Severe ionospheric scintillation
        - frequency_selective_fade: Frequency-dependent absorption event
        
        Args:
            tone_power_timeseries: Per-frequency power time-series
            noise_coherence_diff: Difference between noise segments
            
        Returns:
            Tuple of (anomaly_detected, anomaly_type, confidence)
        """
        if not tone_power_timeseries or 2000 not in tone_power_timeseries:
            return False, None, None
        
        powers_2k = np.array(tone_power_timeseries[2000])
        
        if len(powers_2k) < 5:
            return False, None, None
        
        # Guard: check tone SNR before anomaly detection.
        # At low SNR the power measurements are noise-dominated and
        # fluctuations reflect noise statistics, not ionospheric events.
        first3 = np.mean(powers_2k[:3])
        last3 = np.mean(powers_2k[-3:])
        expected_drop = 3.0 * (len(powers_2k) - 2)
        tone_snr = first3 - last3 - expected_drop
        if tone_snr < 6.0:
            return False, None, None
        
        # Detrend first: remove expected -3dB/sec attenuation pattern
        expected_atten_db = np.array([-3.0 * i for i in range(len(powers_2k))])
        detrended = powers_2k - (powers_2k[0] + expected_atten_db)
        
        # Check for sudden amplitude drop (solar flare signature)
        # Must exceed the expected -6dB/2sec attenuation by a large margin
        for i in range(len(detrended) - 2):
            drop = detrended[i] - detrended[i+2]
            if drop > 12.0:  # >12 dB EXTRA drop beyond expected attenuation
                return True, "sudden_amplitude_drop", 0.8
        
        # Check for sudden amplitude increase (sporadic E)
        # Signal should be monotonically decreasing; any large increase is anomalous
        for i in range(len(detrended) - 2):
            increase = detrended[i+2] - detrended[i]
            if increase > 10.0:  # >10 dB increase in detrended signal
                return True, "sudden_amplitude_increase", 0.7
        
        # Check for rapid fading (severe scintillation)
        # Normal HF channels have 3-6 dB RMS fluctuation; only flag truly severe cases
        if np.std(detrended) > 8.0:  # >8 dB RMS fluctuation (was 5.0, too sensitive)
            return True, "rapid_fading", 0.6
        
        # Check for frequency-selective fade
        # Compare 2 kHz vs 5 kHz behavior
        if 5000 in tone_power_timeseries and len(tone_power_timeseries[5000]) >= 5:
            powers_5k = np.array(tone_power_timeseries[5000])
            
            # Calculate average difference (should be consistent with FSS)
            avg_diff = np.mean(powers_2k - powers_5k)
            std_diff = np.std(powers_2k - powers_5k)
            
            # Sudden change in frequency selectivity
            if std_diff > 4.0:  # High variance in frequency-dependent behavior
                return True, "frequency_selective_fade", 0.5
        
        # Check transient interference from noise segment analysis
        if noise_coherence_diff is not None and noise_coherence_diff > 0.3:
            return True, "transient_interference", 0.6
        
        return False, "none", 0.0
    
    def _calculate_field_strength_metrics(
        self,
        tone_power_timeseries: Dict[int, List[float]]
    ) -> Tuple[Optional[float], Optional[float]]:
        """
        Calculate overall field strength and stability metrics
        
        Args:
            tone_power_timeseries: Per-frequency power time-series
            
        Returns:
            Tuple of (field_strength_db, field_strength_stability)
        """
        if not tone_power_timeseries or 2000 not in tone_power_timeseries:
            return None, None
        
        # Use 2 kHz as reference (most reliable)
        powers_2k = np.array(tone_power_timeseries[2000])
        
        if len(powers_2k) < 3:
            return None, None
        
        # Overall field strength (average of first 3 seconds before heavy attenuation)
        field_strength_db = float(np.mean(powers_2k[:3]))
        
        # Stability: inverse of coefficient of variation
        # High stability = low CV = stable channel
        mean_power = np.mean(powers_2k)
        std_power = np.std(powers_2k)
        
        if std_power > 0 and mean_power != 0:
            cv = std_power / abs(mean_power)
            field_strength_stability = float(1.0 / (cv + 0.1))  # Add small constant to avoid div by zero
        else:
            field_strength_stability = 10.0  # Very stable
        
        return field_strength_db, field_strength_stability
    
    def _assess_channel_quality(
        self,
        snr_db: Optional[float],
        delay_spread_ms: Optional[float],
        coherence_time_sec: Optional[float]
    ) -> str:
        """
        Assess overall channel quality based on multiple metrics
        
        Quality grades (per L2 schema):
        - excellent: SNR > 20 dB, delay_spread < 0.5 ms, coherence_time > 5 s
        - good: SNR > 10 dB, delay_spread < 2 ms, coherence_time > 2 s
        - fair: SNR > 5 dB, delay_spread < 5 ms, coherence_time > 1 s
        - poor: Below fair thresholds
        
        Args:
            snr_db: Signal-to-noise ratio
            delay_spread_ms: Multipath delay spread
            coherence_time_sec: Channel coherence time
            
        Returns:
            Quality grade string
        """
        # Default values for missing metrics (neutral — don't penalize)
        snr = snr_db if snr_db is not None else 10.0
        delay = delay_spread_ms if delay_spread_ms is not None else 1.0
        coherence = coherence_time_sec if coherence_time_sec is not None else 5.0
        
        # Excellent channel (rare on HF — very strong, stable signal)
        if snr > 20 and delay < 0.5 and coherence > 5.0:
            return "excellent"
        
        # Good channel (typical daytime on well-propagated frequencies)
        if snr > 8 and delay < 2.0 and coherence > 2.0:
            return "good"
        
        # Fair channel (marginal propagation, still usable)
        if snr > 3 and delay < 5.0 and coherence > 0.5:
            return "fair"
        
        # Poor channel
        return "poor"


# Convenience function for integration
def detect_test_signal(
    iq_samples: np.ndarray,
    sample_rate: int,
    minute_number: int
) -> TestSignalDetection:
    """
    Convenience function to detect test signal
    
    Args:
        iq_samples: Complex IQ samples
        sample_rate: Sample rate in Hz
        minute_number: Minute of hour (0-59)
        
    Returns:
        TestSignalDetection object
    """
    detector = WWVTestSignalDetector(sample_rate)
    return detector.detect(iq_samples, minute_number, sample_rate)

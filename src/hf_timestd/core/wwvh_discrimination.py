#!/usr/bin/env python3
"""
WWV/WWVH Station Discrimination — Active Services

Provides three services used at runtime by MetrologyEngine/MetrologyService:

1. BCD correlation discrimination (100 Hz cross-correlation for WWV/WWVH amplitude)
   detect_bcd_discrimination() → bcd_correlation_discrimination() → _generate_bcd_template()

2. Legacy Doppler estimation from per-tick phase progression
   estimate_doppler_shift_from_ticks() → extract_per_tick_phases()

3. WWVTestSignalDetector sub-object (minutes 8/44 test signal detection)
   Instantiated in __init__(), accessed via self.test_signal_detector

The weighted voting pipeline (compute_discrimination, finalize_discrimination,
_cross_validate_methods, etc.) was removed 2026-03-06 — superseded by the
TickEdgeDetector which runs station-specific matched-filter templates in parallel.
Full original preserved in core/legacy/wwvh_discrimination_archive.py.

BROADCAST FEATURE REFERENCE (for context — canonical source is wwv_constants.py):
┌────────────────────┬──────────────────────┬──────────────────────┐
│ Characteristic     │ WWV (Colorado)       │ WWVH (Hawaii)        │
├────────────────────┼──────────────────────┼──────────────────────┤
│ Timing Tone        │ 1000 Hz, 0.8s        │ 1200 Hz, 0.8s        │
│ 440 Hz Tone        │ Minute 2             │ Minute 1             │
│ 500/600 Hz Tone    │ Minutes 1,16,17,19   │ Minutes 2,43-51      │
│ Test Signal        │ Minute 8             │ Minute 44            │
│ BCD Phase          │ Leading edge         │ Lagging edge         │
└────────────────────┴──────────────────────┴──────────────────────┘

REFERENCE: NIST Special Publication 250-67 (2009)

REVISION HISTORY:
2026-03-06: Removed dead voting pipeline, archived to core/legacy/
2025-12-07: Added comprehensive theoretical documentation
2025-12-01: Added dual-station time recovery for UTC cross-validation
2025-11-20: Added test signal analysis for minutes 8/44
2025-11-15: Added 500/600 Hz ground truth detection
2025-10-20: Initial implementation with tone power ratio and BCD correlation
"""

import logging
import numpy as np
from typing import Optional, Dict, List, Tuple
from scipy import signal as scipy_signal
from scipy.fft import rfft, rfftfreq
from scipy.signal import iirnotch, filtfilt

from .wwv_bcd_encoder import WWVBCDEncoder
from .wwv_geographic_predictor import WWVGeographicPredictor
from .timing_discrimination import TimingDiscriminator
from .wwv_test_signal import WWVTestSignalDetector

logger = logging.getLogger(__name__)


class WWVHDiscriminator:
    """
    Provides BCD correlation, Doppler estimation, and test signal detection
    for WWV/WWVH shared-frequency channels.
    
    Station separation is handled upstream by TickEdgeDetector running
    station-specific matched-filter templates in parallel. This class
    provides supplementary channel characterization services.
    """
    
    def __init__(
        self,
        channel_name: str,
        receiver_grid: Optional[str] = None,
        history_dir: Optional[str] = None,
        sample_rate: int = 24000,
        timing_discriminator: Optional[TimingDiscriminator] = None
    ):
        """
        Initialize discriminator
        
        Args:
            channel_name: Channel name for logging
            receiver_grid: Maidenhead grid square (e.g., "EM38ww") for geographic ToA prediction
            history_dir: Directory for persisting ToA history (optional)
            sample_rate: Sample rate in Hz (24000 default, 16000 for legacy)
            timing_discriminator: Optional timing-based discriminator for validation
        """
        self.channel_name = channel_name
        self.sample_rate = sample_rate
        
        # Initialize BCD encoder for template generation (WWV/WWVH only)
        # CHU doesn't use BCD encoding
        self.is_chu_channel = 'CHU' in channel_name.upper()
        if not self.is_chu_channel:
            self.bcd_encoder = WWVBCDEncoder(sample_rate=sample_rate)
        else:
            self.bcd_encoder = None
        
        # Initialize test signal detector for minute 8/44 discrimination (WWV/WWVH only)
        # CHU doesn't broadcast test signals
        if not self.is_chu_channel:
            self.test_signal_detector = WWVTestSignalDetector(sample_rate=sample_rate)
            logger.info(f"{channel_name}: Test signal detector initialized for minutes 8/44 @ {sample_rate} Hz")
        else:
            self.test_signal_detector = None
        
        # Initialize geographic predictor if grid square provided
        self.geo_predictor: Optional[WWVGeographicPredictor] = None
        if receiver_grid:
            from pathlib import Path
            history_file = None
            if history_dir:
                history_file = Path(history_dir) / f"toa_history_{channel_name.replace(' ', '_')}.json"
            
            self.geo_predictor = WWVGeographicPredictor(
                receiver_grid=receiver_grid,
                history_file=history_file,
                max_history=1000
            )
            logger.info(f"{channel_name}: Geographic ToA prediction enabled for {receiver_grid}")
        else:
            logger.info(f"{channel_name}: Geographic ToA prediction disabled (no grid square configured)")
        
        # Timing-based discriminator (optional, for SHARED frequencies)
        self.timing_discriminator = timing_discriminator
        if timing_discriminator:
            logger.info(f"{channel_name}: Timing-based discrimination enabled (phase: {timing_discriminator.phase.value})")
        
        logger.info(f"{channel_name}: WWVHDiscriminator initialized")
    
    def extract_per_tick_phases(
        self,
        iq_samples: np.ndarray,
        sample_rate: int,
        snr_threshold_db: float = 0.0  # Lowered from 10 dB - uses narrow noise band reference
    ) -> Dict:
        """
        Extract per-second tick phases for Doppler estimation.
        
        This method extracts the complex amplitude (phasor) of the 1000 Hz (WWV) and
        1200 Hz (WWVH) tones from each 5ms tick, providing 58 phase measurements per
        minute (seconds 1-58, skipping second 0 which has the 800ms marker tone).
        
        Args:
            iq_samples: Full minute of complex IQ samples (16 kHz, 60 seconds)
            sample_rate: Sample rate in Hz (typically 16000)
            snr_threshold_db: Minimum SNR for reliable phase measurement (default 0 dB)
            
        Returns:
            Dictionary with per-tick phase measurements for WWV and WWVH
        """
        # AM demodulation
        magnitude = np.abs(iq_samples)
        audio_signal = magnitude - np.mean(magnitude)  # AC coupling
        
        # Remove harmonic-generating tones (440/500/600 Hz)
        b_440, a_440 = iirnotch(440, 20, sample_rate)
        audio_signal = filtfilt(b_440, a_440, audio_signal)
        b_500, a_500 = iirnotch(500, 20, sample_rate)
        audio_signal = filtfilt(b_500, a_500, audio_signal)
        b_600, a_600 = iirnotch(600, 20, sample_rate)
        audio_signal = filtfilt(b_600, a_600, audio_signal)
        
        samples_per_second = sample_rate
        tick_duration_samples = int(0.005 * sample_rate)  # 5ms tick
        
        # FFT parameters for fine frequency resolution
        # Zero-pad to 1 second for 1 Hz resolution
        padded_length = sample_rate
        
        wwv_phases = []
        wwvh_phases = []
        carrier_phases = []
        wwv_complex_amps = []
        wwvh_complex_amps = []
        carrier_complex_amps = []
        noise_estimates = []
        
        # Process seconds 1-58 (skip second 0 with 800ms marker, and 59 for safety margin)
        for second in range(1, 59):
            start_sample = second * samples_per_second
            end_sample = start_sample + tick_duration_samples
            
            if end_sample > len(audio_signal):
                break
            
            # Extract 5ms tick and apply Hann window
            tick_samples = audio_signal[start_sample:end_sample]
            windowed_tick = tick_samples * np.hanning(len(tick_samples))
            
            # Zero-pad to 1 second for 1 Hz FFT resolution
            padded_tick = np.pad(windowed_tick, (0, padded_length - len(windowed_tick)), mode='constant')
            
            # FFT to extract complex amplitudes
            fft_result = rfft(padded_tick)
            freqs = rfftfreq(padded_length, 1/sample_rate)
            
            # Extract complex values at WWV (1000 Hz) and WWVH (1200 Hz)
            wwv_freq_idx = np.argmin(np.abs(freqs - 1000.0))
            wwvh_freq_idx = np.argmin(np.abs(freqs - 1200.0))
            
            wwv_complex = fft_result[wwv_freq_idx]
            wwvh_complex = fft_result[wwvh_freq_idx]
            
            # Measure noise in 825-875 Hz guard band
            noise_low_idx = np.argmin(np.abs(freqs - 825.0))
            noise_high_idx = np.argmin(np.abs(freqs - 875.0))
            noise_bins = fft_result[noise_low_idx:noise_high_idx]
            
            if len(noise_bins) > 0:
                noise_power = np.mean(np.abs(noise_bins)**2)
                noise_estimates.append(noise_power)
            else:
                noise_power = 1e-12
            
            # Calculate SNR for each tone
            wwv_power = np.abs(wwv_complex)**2
            wwvh_power = np.abs(wwvh_complex)**2
            
            # Carrier Phase Extraction
            # ------------------------
            # Logic:
            # 1. "Safe Bands": WWV 20/25 MHz and CHU (all). Use raw IQ for true RF Carrier Phase.
            # 2. "Shared Bands": WWV/H 2.5, 5, 10, 15 MHz. Use AM Envelope DC (0 Hz).
            
            frequency_mhz = self.frequency_mhz if hasattr(self, 'frequency_mhz') and self.frequency_mhz else 0.0
            is_safe_carrier_band = frequency_mhz in [3.33, 7.85, 14.67, 20.0, 25.0]
            
            if is_safe_carrier_band:
                # Extract Carrier from raw IQ (RF Phase)
                tick_start_idx = int(start_sample)
                tick_end_idx = int(end_sample)
                if tick_end_idx <= len(iq_samples):
                    iq_slice = iq_samples[tick_start_idx:tick_end_idx]
                    # Mean of complex samples = DC component = Carrier Phasor
                    carrier_complex = np.mean(iq_slice)
                    
                    # Re-calculate Envelope Carrier for SNR consistency
                    env_carrier_complex = fft_result[0]
                    carrier_power = np.abs(env_carrier_complex)**2
                    
                    # Use RF Carrier Phase
                    carrier_phase = np.angle(carrier_complex)
                else:
                    carrier_complex = fft_result[0]
                    carrier_power = np.abs(carrier_complex)**2
                    carrier_phase = np.angle(carrier_complex)
            else:
                # Use AM Envelope DC (Phase is meaningless/zero)
                carrier_complex = fft_result[0]
                carrier_power = np.abs(carrier_complex)**2
                carrier_phase = np.angle(carrier_complex)

            wwv_snr_db = 10 * np.log10(wwv_power / noise_power) if noise_power > 0 else -100
            wwvh_snr_db = 10 * np.log10(wwvh_power / noise_power) if noise_power > 0 else -100
            carrier_snr_db = 10 * np.log10(carrier_power / noise_power) if noise_power > 0 else -100
            
            # Debug: Print first tick's SNR values to stderr
            if second == 1:
                import sys
                print(f"DEBUG {self.channel_name}: Phase SNR: Carrier={carrier_snr_db:.1f}dB, WWV={wwv_snr_db:.1f}dB, WWVH={wwvh_snr_db:.1f}dB (threshold={snr_threshold_db}dB)", file=sys.stderr)
            
            # Extract phase (only if SNR is sufficient for reliable measurement)
            wwv_phase = np.angle(wwv_complex)
            wwvh_phase = np.angle(wwvh_complex)
            carrier_phase = np.angle(carrier_complex)
            
            # Store results with SNR qualification
            # Always store carrier if SNR > threshold (same as tones)
            # Note: Carrier SNR usually much higher than tone SNR.
            if carrier_snr_db >= snr_threshold_db:
                 carrier_phases.append((second, float(carrier_phase), float(carrier_snr_db)))
                 carrier_complex_amps.append((second, complex(carrier_complex)))
            
            if wwv_snr_db >= snr_threshold_db:
                wwv_phases.append((second, float(wwv_phase), float(wwv_snr_db)))
                wwv_complex_amps.append((second, complex(wwv_complex)))
            
            if wwvh_snr_db >= snr_threshold_db:
                wwvh_phases.append((second, float(wwvh_phase), float(wwvh_snr_db)))
                wwvh_complex_amps.append((second, complex(wwvh_complex)))
        
        # Calculate noise floor
        noise_floor_db = 10 * np.log10(np.mean(noise_estimates)) if noise_estimates else -100
        
        # Log at INFO level if one station has significantly fewer phases
        if len(wwv_phases) < 10 or len(wwvh_phases) < 10:
            # Get average SNR for debugging
            avg_wwv_snr = np.mean([p[2] for p in wwv_phases]) if wwv_phases else -100
            avg_wwvh_snr = np.mean([p[2] for p in wwvh_phases]) if wwvh_phases else -100
            logger.info(f"{self.channel_name}: Phase extraction: WWV={len(wwv_phases)} (avg SNR {avg_wwv_snr:.1f}dB), "
                       f"WWVH={len(wwvh_phases)} (avg SNR {avg_wwvh_snr:.1f}dB), "
                       f"threshold={snr_threshold_db} dB, noise_floor={noise_floor_db:.1f} dB")
        else:
            logger.debug(f"{self.channel_name}: Extracted {len(wwv_phases)} WWV and {len(wwvh_phases)} WWVH "
                        f"tick phases (SNR threshold={snr_threshold_db} dB, noise floor={noise_floor_db:.1f} dB)")
        
        return {
            'wwv_phases': wwv_phases,
            'wwvh_phases': wwvh_phases,
            'carrier_phases': carrier_phases,
            'wwv_complex': wwv_complex_amps,
            'wwvh_complex': wwvh_complex_amps,
            'carrier_complex': carrier_complex_amps,
            'valid_tick_count': max(len(wwv_phases), len(wwvh_phases), len(carrier_phases)),
            'noise_floor_db': float(noise_floor_db)
        }
    
    def estimate_doppler_shift_from_ticks(
        self,
        iq_samples: np.ndarray,
        sample_rate: int,
        snr_threshold_db: float = 0.0  # Lowered from 10 dB - per-tick SNR uses different noise ref
    ) -> Optional[Dict[str, float]]:
        """
        Estimate instantaneous Doppler shift from per-tick phase progression.
        
        Uses the adjacent pulse phase difference method:
            Δf_D,k = Δφ_k / (2π × 1s)
        
        where Δφ_k is the unwrapped phase difference between tick k and tick k-1.
        
        This provides ~57 instantaneous Doppler measurements per minute, enabling
        accurate determination of the maximum coherent integration window:
            T_max ≈ 1 / (4 × |Δf_D|)
        
        Args:
            iq_samples: Full minute of complex IQ samples
            sample_rate: Sample rate in Hz
            snr_threshold_db: Minimum SNR for reliable phase tracking
            
        Returns:
            Dictionary with:
                - wwv_doppler_hz: Mean Doppler shift for WWV (Hz)
                - wwvh_doppler_hz: Mean Doppler shift for WWVH (Hz)
                - wwv_doppler_std_hz: Doppler variability (Hz)
                - wwvh_doppler_std_hz: Doppler variability (Hz)
                - max_coherent_window_sec: Maximum window for π/4 phase error
                - doppler_quality: Confidence metric (0-1)
                - phase_variance_rad: RMS phase deviation from linear fit
                - instantaneous_doppler: List of per-second Doppler measurements
            Returns None if insufficient high-SNR ticks available
        """
        # Extract per-tick phases
        tick_data = self.extract_per_tick_phases(iq_samples, sample_rate, snr_threshold_db)
        
        wwv_phases = tick_data['wwv_phases']
        wwvh_phases = tick_data['wwvh_phases']
        carrier_phases = tick_data.get('carrier_phases', [])
        
        if len(wwv_phases) < 10 and len(wwvh_phases) < 10 and len(carrier_phases) < 10:
            logger.debug(f"{self.channel_name}: Insufficient high-SNR ticks for Doppler estimation "
                        f"(WWV: {len(wwv_phases)}, WWVH: {len(wwvh_phases)}, Carrier: {len(carrier_phases)})")
            return None
        
        def compute_doppler_from_phases(phases_list):
            """Compute Doppler shift from list of (second, phase, snr) tuples."""
            if len(phases_list) < 10:
                return None, None, None, []
            
            # Extract times and phases
            times = np.array([p[0] for p in phases_list])
            phases = np.array([p[1] for p in phases_list])
            
            # Unwrap phases to handle 2π discontinuities
            phases_unwrapped = np.unwrap(phases)
            
            # Method 1: Linear regression for mean Doppler
            # φ(t) = 2π·Δf_D·t + φ₀
            coeffs = np.polyfit(times, phases_unwrapped, deg=1)
            mean_doppler_hz = coeffs[0] / (2 * np.pi)
            
            # Method 2: Adjacent difference for instantaneous Doppler
            # Δf_D,k = (φ_k - φ_{k-1}) / (2π × Δt)
            instantaneous_doppler = []
            for i in range(1, len(phases_list)):
                dt = times[i] - times[i-1]
                if dt > 0:
                    dphi = phases_unwrapped[i] - phases_unwrapped[i-1]
                    inst_doppler = dphi / (2 * np.pi * dt)
                    instantaneous_doppler.append({
                        'second': int(times[i]),
                        'doppler_hz': float(inst_doppler),
                        'snr_db': float(phases_list[i][2])
                    })
            
            # Doppler variability (standard deviation)
            if instantaneous_doppler:
                doppler_values = [d['doppler_hz'] for d in instantaneous_doppler]
                doppler_std = np.std(doppler_values)
            else:
                doppler_std = 0.0
            
            # Phase fit quality
            fit_phases = np.polyval(coeffs, times)
            residuals = phases_unwrapped - fit_phases
            phase_variance = np.var(residuals)
            
            return mean_doppler_hz, doppler_std, phase_variance, instantaneous_doppler
        
        # Compute for WWV
        wwv_doppler, wwv_std, wwv_var, wwv_inst = compute_doppler_from_phases(wwv_phases)
        
        # Compute for WWVH
        wwvh_doppler, wwvh_std, wwvh_var, wwvh_inst = compute_doppler_from_phases(wwvh_phases)

        # Compute for Carrier
        carrier_doppler, carrier_std, carrier_var, carrier_inst = compute_doppler_from_phases(carrier_phases)
        
        # Use whichever station has more valid measurements
        if wwv_doppler is None and wwvh_doppler is None and carrier_doppler is None:
            return None
        
        # Default to 0 if missing
        wwv_doppler = wwv_doppler or 0.0
        wwvh_doppler = wwvh_doppler or 0.0
        carrier_doppler = carrier_doppler or 0.0
        wwv_std = wwv_std or 0.0
        wwvh_std = wwvh_std or 0.0
        carrier_std = carrier_std or 0.0
        wwv_var = wwv_var or 0.0
        wwvh_var = wwvh_var or 0.0
        carrier_var = carrier_var or 0.0
        
        # Calculate maximum coherent integration window
        # Limit phase error to π/4 (45°) for <3 dB coherent loss
        # T_max = π/4 / (2π × |Δf_D|) = 1 / (8 × |Δf_D|)
        max_doppler = max(abs(wwv_doppler), abs(wwvh_doppler), abs(carrier_doppler))
        if max_doppler > 0.001:  # Avoid division by zero
            max_coherent_window = 1.0 / (8.0 * max_doppler)
        else:
            max_coherent_window = 60.0  # Stable channel, no Doppler limit
        
        # Clamp to reasonable range
        max_coherent_window = min(max_coherent_window, 60.0)
        
        # Quality metric from phase fit residuals
        # Quality: 1.0 = perfect fit, 0.0 = random phase (variance = π²/3)
        phase_variance = max(wwv_var, wwvh_var) # Use modulation variance for generic quality? Or include carrier?
        # Include carrier? Usually carrier is cleaner.
        # But if carrier is messy (beats), we might not want to report high quality based on it?
        # Let's keep quality based on Tones for now as legacy metric, or upgrade it.
        # Upgrading it to include carrier helps single-station locking.
        if carrier_doppler != 0.0:
             phase_variance = min(phase_variance, carrier_var) if phase_variance > 0 else carrier_var
        
        doppler_quality = max(0.0, min(1.0, 1.0 - (phase_variance / (np.pi**2 / 3))))
        
        logger.info(f"{self.channel_name}: Doppler estimate (per-tick): "
                   f"Carrier={carrier_doppler:+.4f}±{carrier_std:.4f} Hz, "
                   f"WWV={wwv_doppler:+.4f}±{wwv_std:.4f} Hz, "
                   f"WWVH={wwvh_doppler:+.4f}±{wwvh_std:.4f} Hz, "
                   f"T_max={max_coherent_window:.1f}s, quality={doppler_quality:.2f}")
        
        return {
            'wwv_doppler_hz': float(wwv_doppler),
            'wwvh_doppler_hz': float(wwvh_doppler),
            'carrier_doppler_hz': float(carrier_doppler),
            'wwv_doppler_std_hz': float(wwv_std),
            'wwvh_doppler_std_hz': float(wwvh_std),
            'wwvh_doppler_std_hz': float(wwvh_std),
            'max_coherent_window_sec': float(max_coherent_window),
            'doppler_quality': float(doppler_quality),
            'phase_variance_rad': float(np.sqrt(phase_variance)),
            'wwv_instantaneous_doppler': wwv_inst,
            'wwvh_instantaneous_doppler': wwvh_inst
        }
    
    def bcd_correlation_discrimination(
        self,
        iq_samples: np.ndarray,
        sample_rate: int,
        minute_timestamp: float,
        frequency_mhz: Optional[float] = None,
        window_seconds: float = 10,
        step_seconds: float = 1,
        adaptive: bool = False,
        enable_single_station_detection: bool = True,
        timing_power_ratio_db: Optional[float] = None,  # WWV-WWVH power from 1000/1200 Hz (positive=WWV stronger)
        ground_truth_station: Optional[str] = None,  # From 500/600 Hz exclusive minutes ('WWV' or 'WWVH')
        wwv_tick_snr_db: Optional[float] = None,  # SNR of 1000 Hz tick
        wwvh_tick_snr_db: Optional[float] = None,  # SNR of 1200 Hz tick
        downsample_factor: int = 4  # Downsample for CPU efficiency (4x = 5kHz, safe for 100Hz BCD)
    ) -> Tuple[Optional[float], Optional[float], Optional[float], Optional[float], List[Dict[str, float]]]:
        """
        Discriminate WWV/WWVH using 100 Hz BCD cross-correlation with sliding windows
        
        Both WWV and WWVH transmit the IDENTICAL 100 Hz BCD time code simultaneously.
        By cross-correlating the received 100 Hz signal against the expected template,
        we get two peaks separated by the ionospheric differential delay (~10-20ms).
        
        COHERENCE-LIMITED WINDOWING (2025-11-26 fix):
        Default uses 10-second windows with 1-second sliding steps. This keeps T_int
        within typical HF ionospheric coherence time (Tc ~10-20s), preventing Doppler-
        induced phase rotation from destroying correlation quality.
        
        WHY 10 SECONDS:
        - Within typical HF coherence time (Tc ~10-20s for quiet ionosphere)
        - Provides √10 = 3.2x SNR improvement over 1-second (+5 dB)
        - 1-second sliding step produces ~50 windows/minute for time-series tracking
        - Captures propagation dynamics (fading, multipath variations)
        
        WHY NOT 60 SECONDS:
        - Exceeds Tc under typical conditions, causing phase rotation
        - Doppler shift of ±0.1 Hz causes 37.7 radians rotation over 60s
        - Averages over multiple fading periods, destroying amplitude information
        
        This method completely avoids the 1000/1200 Hz time marker tone separation problem!
        The 100 Hz BCD signal is the actual carrier.
        
        Args:
            iq_samples: Full minute of complex IQ samples
            sample_rate: Sample rate in Hz (typically 16000)
            minute_timestamp: UTC timestamp of minute boundary
            frequency_mhz: Operating frequency for geographic ToA prediction (optional)
            window_seconds: Integration window length (default 10s, within typical Tc)
            step_seconds: Sliding step size (default 1s for high-resolution time-series)
            adaptive: Enable adaptive window recommendations (default False, use Doppler-adaptive wrapper)
            enable_single_station_detection: Use geographic predictor for single peaks (default True)
            
        Returns:
            Tuple of (wwv_amp_mean, wwvh_amp_mean, delay_mean, quality_mean, windows_list)
            Scalar values are means across all windows; windows_list contains time-series data
            Returns (None, None, None, None, None) if correlation fails
        """
        try:
            # Step 1: Extract 100 Hz BCD tone from the combined IQ signal
            # BCD is amplitude modulation of a 100 Hz subcarrier, independent of 1000/1200 Hz ID tones
            # Both WWV and WWVH transmit the same BCD pattern on 100 Hz
            
            # Bandpass filter around 100 Hz to isolate BCD subcarrier
            nyquist = sample_rate / 2
            bcd_low_norm = 50 / nyquist   # 50-150 Hz captures 100 Hz BCD
            bcd_high_norm = 150 / nyquist
            sos_bcd = scipy_signal.butter(4, [bcd_low_norm, bcd_high_norm], 'bandpass', output='sos')
            bcd_100hz = scipy_signal.sosfilt(sos_bcd, iq_samples)
            
            # Step 2: Use the bandpass-filtered 100 Hz signal directly for correlation
            # The 100 Hz carrier IS the BCD signal - correlate directly with template
            # For complex IQ, take real part since template is real
            if np.iscomplexobj(bcd_100hz):
                bcd_signal = np.real(bcd_100hz)
            else:
                bcd_signal = bcd_100hz
            
            # Normalize signal for correlation
            bcd_signal = bcd_signal - np.mean(bcd_signal)
            
            # Step 2b: Downsample for CPU efficiency (BCD is 100 Hz, 5 kHz is 50x oversampling)
            # This reduces correlation CPU by ~75% with negligible accuracy loss
            # Timing resolution at 5 kHz = 0.2 ms, well within ionospheric uncertainty (~3-10 ms)
            effective_sample_rate = sample_rate
            if downsample_factor > 1:
                # Anti-alias filter before decimation (already bandpass filtered to 50-150 Hz)
                # scipy.signal.decimate applies proper anti-aliasing
                bcd_signal = scipy_signal.decimate(bcd_signal, downsample_factor, ftype='fir', zero_phase=True)
                effective_sample_rate = sample_rate // downsample_factor
                logger.debug(f"{self.channel_name}: BCD downsampled {sample_rate}→{effective_sample_rate} Hz ({downsample_factor}x)")
            
            # Step 3: Generate expected BCD template for this minute (full 60 seconds)
            # Template includes 100 Hz carrier modulated by BCD pattern
            # Generate at effective (downsampled) rate
            bcd_template_full = self._generate_bcd_template(minute_timestamp, effective_sample_rate, envelope_only=False)
            
            if bcd_template_full is None:
                logger.warning(f"{self.channel_name}: Failed to generate BCD template")
                return None, None, None, None, None
            
            # Step 5: Sliding window correlation to find delay AND amplitudes
            # The 100 Hz BCD signal IS the carrier - both stations transmit on 100 Hz
            # Correlation peak heights give us the individual station amplitudes
            window_samples = int(window_seconds * effective_sample_rate)
            step_samples = int(step_seconds * effective_sample_rate)
            
            # Calculate number of windows - CRITICAL: limit by BOTH signal AND template length
            # Template is exactly 60 seconds; signal may be longer
            total_samples = len(bcd_signal)
            template_samples = len(bcd_template_full)
            max_start_sample = min(total_samples, template_samples) - window_samples
            
            if max_start_sample <= 0:
                logger.warning(f"{self.channel_name}: BCD signal ({total_samples}) or template ({template_samples}) "
                              f"too short for {window_seconds}s window ({window_samples} samples)")
                return None, None, None, None, None
            
            num_windows = max_start_sample // step_samples + 1
            
            windows_data = []
            
            for i in range(num_windows):
                start_sample = int(i * step_samples)
                end_sample = int(start_sample + window_samples)
                
                # Safety check - skip if we'd exceed template bounds
                if end_sample > template_samples:
                    break
                    
                window_start_time = start_sample / effective_sample_rate  # Seconds into the minute
                
                # Extract BCD signal window and template
                signal_window = bcd_signal[start_sample:end_sample]
                template_window = bcd_template_full[start_sample:end_sample]
                
                # Cross-correlate to find two peaks (WWV and WWVH arrivals)
                correlation = scipy_signal.correlate(signal_window, template_window, mode='full', method='fft')
                correlation = np.abs(correlation)
                
                # Zero-lag is at index len(template_window) - 1
                zero_lag_idx = len(template_window) - 1
                
                # Use geographic predictor for targeted peak search if available
                # With improved timing, we know where to look for each station's peak
                if self.geo_predictor and frequency_mhz:
                    expected = self.geo_predictor.calculate_expected_delays(frequency_mhz)
                    wwv_expected_ms = expected['wwv_delay_ms']
                    wwvh_expected_ms = expected['wwvh_delay_ms']
                    
                    # Search ±15ms around each expected delay (tight window with good timing)
                    search_window_ms = 15.0
                    search_window_samples = int(search_window_ms * effective_sample_rate / 1000)
                    
                    # WWV search window
                    wwv_center_idx = zero_lag_idx + int(wwv_expected_ms * effective_sample_rate / 1000)
                    wwv_start = max(0, wwv_center_idx - search_window_samples)
                    wwv_end = min(len(correlation), wwv_center_idx + search_window_samples)
                    
                    # WWVH search window
                    wwvh_center_idx = zero_lag_idx + int(wwvh_expected_ms * effective_sample_rate / 1000)
                    wwvh_start = max(0, wwvh_center_idx - search_window_samples)
                    wwvh_end = min(len(correlation), wwvh_center_idx + search_window_samples)
                    
                    # Find best peak in each window
                    wwv_region = correlation[wwv_start:wwv_end]
                    wwvh_region = correlation[wwvh_start:wwvh_end]
                    
                    wwv_peak_local = np.argmax(wwv_region)
                    wwvh_peak_local = np.argmax(wwvh_region)
                    
                    wwv_peak_idx = wwv_start + wwv_peak_local
                    wwvh_peak_idx = wwvh_start + wwvh_peak_local
                    
                    wwv_peak_height = float(wwv_region[wwv_peak_local])
                    wwvh_peak_height = float(wwvh_region[wwvh_peak_local])
                    
                    # Noise floor for quality calculation
                    noise_floor = np.median(correlation)
                    
                    # Build peaks array in order (early, late)
                    if wwv_peak_idx < wwvh_peak_idx:
                        peaks = np.array([wwv_peak_idx, wwvh_peak_idx])
                        properties = {'peak_heights': np.array([wwv_peak_height, wwvh_peak_height])}
                    else:
                        peaks = np.array([wwvh_peak_idx, wwv_peak_idx])
                        properties = {'peak_heights': np.array([wwvh_peak_height, wwv_peak_height])}
                    
                    # Threshold check - both peaks should be above noise
                    mean_corr = np.mean(correlation)
                    std_corr = np.std(correlation)
                    threshold = mean_corr + 0.5 * std_corr
                    
                    if wwv_peak_height < threshold or wwvh_peak_height < threshold:
                        # Weak signal - fall back to single peak detection
                        if wwv_peak_height >= wwvh_peak_height and wwv_peak_height >= threshold:
                            peaks = np.array([wwv_peak_idx])
                            properties = {'peak_heights': np.array([wwv_peak_height])}
                        elif wwvh_peak_height >= threshold:
                            peaks = np.array([wwvh_peak_idx])
                            properties = {'peak_heights': np.array([wwvh_peak_height])}
                        else:
                            peaks = np.array([])
                            properties = {'peak_heights': np.array([])}
                else:
                    # Fallback: broad search ±150ms (no geographic predictor)
                    search_radius_samples = int(0.150 * effective_sample_rate)
                    search_start = max(0, zero_lag_idx - search_radius_samples)
                    search_end = min(len(correlation), zero_lag_idx + search_radius_samples)
                    
                    search_region = correlation[search_start:search_end]
                    
                    mean_corr = np.mean(search_region)
                    std_corr = np.std(search_region)
                    threshold = mean_corr + 0.5 * std_corr
                    
                    min_peak_distance = int(0.003 * effective_sample_rate)  # 3ms minimum
                    
                    peaks_local, properties = scipy_signal.find_peaks(
                        search_region,
                        height=threshold,
                        distance=min_peak_distance,
                        prominence=std_corr * 0.2
                    )
                    
                    peaks = peaks_local + search_start
                
                # Handle both dual-peak (both stations) and single-peak (one station) scenarios
                if len(peaks) >= 2:
                    # DUAL PEAK: Both WWV and WWVH detected
                    peak_heights = properties['peak_heights']
                    sorted_indices = np.argsort(peak_heights)[-2:]
                    sorted_indices = np.sort(sorted_indices)
                    
                    peak1_idx = sorted_indices[0]
                    peak2_idx = sorted_indices[1]
                    
                    # Peak times relative to zero-lag (positive = signal delayed from template)
                    peak1_time = (peaks[peak1_idx] - zero_lag_idx) / effective_sample_rate
                    peak2_time = (peaks[peak2_idx] - zero_lag_idx) / effective_sample_rate
                    
                    delay_ms = (peak2_time - peak1_time) * 1000
                    
                    # Log rejection reasons for debugging sparse detections
                    if delay_ms < 3 or delay_ms > 35:
                        logger.debug(f"{self.channel_name}: BCD window {window_start_time:.0f}s: delay {delay_ms:.1f}ms outside 3-35ms range")
                    
                    if 3 <= delay_ms <= 35:  # Relaxed from 5-30ms to 3-35ms
                        # Joint Least Squares Estimation to overcome temporal leakage
                        # At each peak, we measure: C(τ) = A_early*R(τ-τ_early) + A_late*R(τ-τ_late)
                        # This forms a 2x2 linear system we solve for A_early and A_late
                        
                        # Get correlation values at both peaks (peak1=early, peak2=late)
                        c_peak_early = float(peak_heights[peak1_idx])
                        c_peak_late = float(peak_heights[peak2_idx])
                        
                        # Peak times relative to zero-lag (for geographic classification)
                        peak_early_delay_ms = peak1_time * 1000
                        peak_late_delay_ms = peak2_time * 1000
                        
                        # Compute template autocorrelation at delay Δτ
                        delay_samples = int(delay_ms * effective_sample_rate / 1000)
                        
                        # R(0) = template autocorrelation at zero lag (template energy)
                        R_0 = float(np.sum(template_window**2))
                        
                        # R(Δτ) = template autocorrelation at the measured delay
                        # Shift template and compute overlap
                        if delay_samples < len(template_window):
                            R_delta = float(np.sum(template_window[:-delay_samples] * 
                                                  template_window[delay_samples:]))
                        else:
                            R_delta = 0.0
                        
                        # Set up the 2x2 system: [R(0) R(Δτ)] [A_early] = [C(τ_early)]
                        #                        [R(Δτ) R(0) ] [A_late ]   [C(τ_late) ]
                        # Note: R(-Δτ) = R(Δτ) due to autocorrelation symmetry
                        
                        if R_0 > 0:
                            # Solve the linear system
                            A_matrix = np.array([[R_0, R_delta],
                                               [R_delta, R_0]])
                            b_vector = np.array([c_peak_early, c_peak_late])
                            
                            try:
                                amplitudes = np.linalg.solve(A_matrix, b_vector)
                                early_amp = float(amplitudes[0])
                                late_amp = float(amplitudes[1])
                                
                                # Normalize by sqrt(template energy) for physical units
                                early_amp = early_amp / np.sqrt(R_0)
                                late_amp = late_amp / np.sqrt(R_0)
                                
                                # Amplitudes must be non-negative (use absolute value)
                                early_amp = abs(early_amp)
                                late_amp = abs(late_amp)
                            except np.linalg.LinAlgError:
                                # Matrix is singular, fall back to naive method
                                early_amp = abs(c_peak_early / np.sqrt(R_0))
                                late_amp = abs(c_peak_late / np.sqrt(R_0))
                        else:
                            early_amp = 0.0
                            late_amp = 0.0
                        
                        # GEOGRAPHIC PEAK ASSIGNMENT: Use ToA prediction to assign WWV/WWVH
                        # The geographic predictor uses receiver location to determine which
                        # station should arrive first based on propagation delay
                        if self.geo_predictor and frequency_mhz:
                            early_station, late_station = self.geo_predictor.classify_dual_peaks(
                                peak_early_delay_ms, peak_late_delay_ms,
                                early_amp, late_amp,
                                frequency_mhz
                            )
                            if early_station == 'WWV':
                                wwv_amp = early_amp
                                wwvh_amp = late_amp
                            else:
                                wwv_amp = late_amp
                                wwvh_amp = early_amp
                        else:
                            # Fallback: Assume WWV arrives first (common for US receivers)
                            # This is a heuristic that works for most continental US locations
                            early_station = 'WWV'
                            late_station = 'WWVH'
                            wwv_amp = early_amp
                            wwvh_amp = late_amp
                            logger.debug(f"{self.channel_name}: No geo predictor, assuming early=WWV")
                        
                        # Safety check for NaN/Inf values (breaks JSON)
                        if not np.isfinite(wwv_amp):
                            wwv_amp = 0.0
                        if not np.isfinite(wwvh_amp):
                            wwvh_amp = 0.0
                        
                        # Quality from correlation SNR
                        noise_floor = np.median(correlation)
                        quality = (c_peak_early + c_peak_late) / (2 * noise_floor) if noise_floor > 0 else 0.0
                        
                        if not np.isfinite(quality):
                            quality = 0.0
                        
                        # Measure delay spread (τD) from correlation peak widths (FWHM)
                        # This quantifies channel multipath time spreading
                        def measure_peak_width(correlation, peak_idx, sample_rate):
                            """Measure FWHM of correlation peak in milliseconds"""
                            peak_val = correlation[peak_idx]
                            half_max = peak_val / 2.0
                            
                            # Find left edge
                            left_idx = peak_idx
                            while left_idx > 0 and correlation[left_idx] > half_max:
                                left_idx -= 1
                            
                            # Find right edge
                            right_idx = peak_idx
                            while right_idx < len(correlation) - 1 and correlation[right_idx] > half_max:
                                right_idx += 1
                            
                            # Width in samples → milliseconds
                            width_samples = right_idx - left_idx
                            width_ms = (width_samples / sample_rate) * 1000.0
                            return width_ms
                        
                        wwv_delay_spread_ms = measure_peak_width(correlation, peaks[peak1_idx], effective_sample_rate)
                        wwvh_delay_spread_ms = measure_peak_width(correlation, peaks[peak2_idx], effective_sample_rate)
                        
                        # === DUAL-STATION TIME RECOVERY ===
                        # Both stations transmit at the same UTC second boundary.
                        # By subtracting expected propagation delay from measured ToA,
                        # we back-calculate the emission time. Both should agree.
                        time_recovery_data = {}
                        if self.geo_predictor and frequency_mhz:
                            expected = self.geo_predictor.calculate_expected_delays(frequency_mhz)
                            
                            # Determine which peak is WWV vs WWVH based on geographic assignment
                            if early_station == 'WWV':
                                wwv_toa_ms = peak_early_delay_ms
                                wwvh_toa_ms = peak_late_delay_ms
                            else:
                                wwv_toa_ms = peak_late_delay_ms
                                wwvh_toa_ms = peak_early_delay_ms
                            
                            wwv_expected_ms = expected['wwv_delay_ms']
                            wwvh_expected_ms = expected['wwvh_delay_ms']
                            
                            # Back-calculate emission time offset from minute boundary
                            # If everything is perfect, this should be ~0 for both
                            t_emission_wwv = wwv_toa_ms - wwv_expected_ms
                            t_emission_wwvh = wwvh_toa_ms - wwvh_expected_ms
                            
                            # Cross-validation: both should give the same result
                            cross_error = abs(t_emission_wwv - t_emission_wwvh)
                            
                            if cross_error < 1.0:
                                confidence = 'excellent'
                            elif cross_error < 2.0:
                                confidence = 'good'
                            elif cross_error < 5.0:
                                confidence = 'fair'
                            else:
                                confidence = 'investigate'
                            
                            time_recovery_data = {
                                'wwv_toa_ms': float(wwv_toa_ms),
                                'wwvh_toa_ms': float(wwvh_toa_ms),
                                'wwv_expected_delay_ms': float(wwv_expected_ms),
                                'wwvh_expected_delay_ms': float(wwvh_expected_ms),
                                't_emission_from_wwv_ms': float(t_emission_wwv),
                                't_emission_from_wwvh_ms': float(t_emission_wwvh),
                                'cross_validation_error_ms': float(cross_error),
                                'dual_station_confidence': confidence
                            }
                        
                        windows_data.append({
                            'window_start_sec': float(window_start_time),
                            'wwv_amplitude': wwv_amp,
                            'wwvh_amplitude': wwvh_amp,
                            'differential_delay_ms': float(delay_ms),
                            'correlation_quality': float(quality),
                            'detection_type': 'dual_peak',
                            # Channel characterization: delay spread from peak width
                            'wwv_delay_spread_ms': float(wwv_delay_spread_ms),
                            'wwvh_delay_spread_ms': float(wwvh_delay_spread_ms),
                            # Dual-station time recovery
                            **time_recovery_data
                        })
                        
                        # Update geographic predictor history if available
                        if self.geo_predictor and frequency_mhz:
                            # Convert peak times to absolute delays from correlation zero
                            peak1_delay_ms = peak1_time * 1000
                            peak2_delay_ms = peak2_time * 1000
                            self.geo_predictor.update_dual_peak_history(
                                frequency_mhz,
                                peak1_delay_ms, peak2_delay_ms,
                                wwv_amp, wwvh_amp
                            )
                
                elif len(peaks) == 1 and enable_single_station_detection:
                    # SINGLE PEAK: One station detected - use multi-evidence classification
                    peak_idx = 0
                    peak_time = (peaks[peak_idx] - zero_lag_idx) / effective_sample_rate
                    peak_delay_ms = peak_time * 1000
                    peak_height = float(properties['peak_heights'][peak_idx])
                    
                    # Normalize amplitude
                    R_0 = float(np.sum(template_window**2))
                    if R_0 > 0:
                        peak_amplitude = abs(peak_height / np.sqrt(R_0))
                    else:
                        continue
                    
                    noise_floor = np.median(correlation)
                    quality = peak_height / noise_floor if noise_floor > 0 else 0.0
                    
                    # === MULTI-EVIDENCE CLASSIFICATION ===
                    # Collect votes from multiple sources with exclusion logic
                    wwv_votes = 0.0
                    wwvh_votes = 0.0
                    exclusion_wwv = False  # If True, cannot be WWV
                    exclusion_wwvh = False  # If True, cannot be WWVH
                    evidence_sources = []
                    
                    # EVIDENCE 1: 500/600 Hz Ground Truth (DEFINITIVE - can exclude)
                    if ground_truth_station == 'WWV':
                        wwv_votes += 10.0
                        exclusion_wwvh = True
                        evidence_sources.append('gt_wwv')
                    elif ground_truth_station == 'WWVH':
                        wwvh_votes += 10.0
                        exclusion_wwv = True
                        evidence_sources.append('gt_wwvh')
                    
                    # EVIDENCE 2: Geographic ToA prediction
                    geo_station = None
                    if self.geo_predictor and frequency_mhz:
                        geo_station = self.geo_predictor.classify_single_peak(
                            peak_delay_ms, peak_amplitude, frequency_mhz, quality
                        )
                        if geo_station == 'WWV':
                            wwv_votes += 3.0
                            evidence_sources.append('geo_wwv')
                        elif geo_station == 'WWVH':
                            wwvh_votes += 3.0
                            evidence_sources.append('geo_wwvh')
                    
                    # EVIDENCE 3: Timing tone power ratio (1000/1200 Hz)
                    if timing_power_ratio_db is not None:
                        if abs(timing_power_ratio_db) > 3.0:
                            # Strong difference - high confidence
                            if timing_power_ratio_db > 0:
                                wwv_votes += 5.0
                                evidence_sources.append('pwr_wwv_strong')
                            else:
                                wwvh_votes += 5.0
                                evidence_sources.append('pwr_wwvh_strong')
                        elif abs(timing_power_ratio_db) > 1.0:
                            # Moderate difference
                            if timing_power_ratio_db > 0:
                                wwv_votes += 2.0
                                evidence_sources.append('pwr_wwv_mod')
                            else:
                                wwvh_votes += 2.0
                                evidence_sources.append('pwr_wwvh_mod')
                        else:
                            # Marginal - still counts but less
                            if timing_power_ratio_db > 0:
                                wwv_votes += 0.5
                            else:
                                wwvh_votes += 0.5
                    
                    # EVIDENCE 4: Tick SNR comparison
                    if wwv_tick_snr_db is not None and wwvh_tick_snr_db is not None:
                        snr_diff = wwv_tick_snr_db - wwvh_tick_snr_db
                        if snr_diff > 3.0:
                            wwv_votes += 2.0
                            evidence_sources.append('snr_wwv')
                        elif snr_diff < -3.0:
                            wwvh_votes += 2.0
                            evidence_sources.append('snr_wwvh')
                    
                    # === APPLY EXCLUSIONS ===
                    if exclusion_wwv:
                        wwv_votes = 0.0  # Cannot be WWV
                    if exclusion_wwvh:
                        wwvh_votes = 0.0  # Cannot be WWVH
                    
                    # === MAKE DECISION ===
                    total_votes = wwv_votes + wwvh_votes
                    
                    if total_votes > 0:
                        wwv_confidence = wwv_votes / total_votes
                        wwvh_confidence = wwvh_votes / total_votes
                        
                        if wwv_confidence > 0.6:
                            detection_type = 'single_peak_wwv_multi'
                            if ground_truth_station == 'WWV':
                                detection_type = 'single_peak_wwv_gt'
                            quality_adj = quality * min(1.0, wwv_confidence + 0.2)
                            windows_data.append({
                                'window_start_sec': float(window_start_time),
                                'wwv_amplitude': peak_amplitude,
                                'wwvh_amplitude': 0.0,
                                'differential_delay_ms': None,
                                'correlation_quality': float(quality_adj),
                                'detection_type': detection_type,
                                'peak_delay_ms': float(peak_delay_ms),
                                'evidence': evidence_sources
                            })
                        elif wwvh_confidence > 0.6:
                            detection_type = 'single_peak_wwvh_multi'
                            if ground_truth_station == 'WWVH':
                                detection_type = 'single_peak_wwvh_gt'
                            quality_adj = quality * min(1.0, wwvh_confidence + 0.2)
                            windows_data.append({
                                'window_start_sec': float(window_start_time),
                                'wwv_amplitude': 0.0,
                                'wwvh_amplitude': peak_amplitude,
                                'differential_delay_ms': None,
                                'correlation_quality': float(quality_adj),
                                'detection_type': detection_type,
                                'peak_delay_ms': float(peak_delay_ms),
                                'evidence': evidence_sources
                            })
                        else:
                            # Ambiguous - lean toward stronger evidence
                            if wwv_votes > wwvh_votes:
                                windows_data.append({
                                    'window_start_sec': float(window_start_time),
                                    'wwv_amplitude': peak_amplitude,
                                    'wwvh_amplitude': 0.0,
                                    'differential_delay_ms': None,
                                    'correlation_quality': float(quality * 0.6),
                                    'detection_type': 'single_peak_wwv_ambig',
                                    'peak_delay_ms': float(peak_delay_ms)
                                })
                            else:
                                windows_data.append({
                                    'window_start_sec': float(window_start_time),
                                    'wwv_amplitude': 0.0,
                                    'wwvh_amplitude': peak_amplitude,
                                    'differential_delay_ms': None,
                                    'correlation_quality': float(quality * 0.6),
                                    'detection_type': 'single_peak_wwvh_ambig',
                                    'peak_delay_ms': float(peak_delay_ms)
                                })
                    else:
                        # No evidence available - unclassified
                        windows_data.append({
                            'window_start_sec': float(window_start_time),
                            'wwv_amplitude': 0.0,
                            'wwvh_amplitude': 0.0,
                            'differential_delay_ms': None,
                            'correlation_quality': float(quality * 0.3),
                            'detection_type': 'single_peak_unclassified',
                            'peak_delay_ms': float(peak_delay_ms)
                        })
            
            # Step 5: Compute summary statistics from all valid windows
            if not windows_data:
                logger.info(f"{self.channel_name}: No valid BCD correlation windows detected (threshold={threshold:.1f}, mean={mean_corr:.1f}, std={std_corr:.1f})")
                return None, None, None, None, []
            
            wwv_amps = [w['wwv_amplitude'] for w in windows_data]
            wwvh_amps = [w['wwvh_amplitude'] for w in windows_data]
            delays = [w['differential_delay_ms'] for w in windows_data if w['differential_delay_ms'] is not None]
            qualities = [w['correlation_quality'] for w in windows_data]
            
            wwv_amp_mean = float(np.mean(wwv_amps))
            wwvh_amp_mean = float(np.mean(wwvh_amps))
            delay_mean = float(np.mean(delays)) if delays else None
            quality_mean = float(np.mean(qualities))
            
            # Adaptive windowing: Adjust window size based on signal conditions
            window_adjustment = None
            if adaptive:
                # Calculate amplitude ratio (dB)
                amp_ratio_db = 20 * np.log10(max(wwv_amp_mean, 1e-10) / max(wwvh_amp_mean, 1e-10))
                
                # Determine if one station is dominant or both are similar
                if abs(amp_ratio_db) > 10:
                    # One station is 10+ dB stronger (dominant or alone)
                    # → Tighten window for better temporal resolution
                    if window_seconds > 5:
                        window_adjustment = "tighten"
                        logger.info(f"{self.channel_name}: One station dominant ({amp_ratio_db:+.1f}dB) "
                                   f"- consider 5-second windows for better resolution")
                
                elif abs(amp_ratio_db) < 3:
                    # Stations within 3 dB (similar strength, hard to discriminate)
                    # → Expand window for better SNR discrimination
                    if window_seconds < 15:
                        window_adjustment = "expand"
                        logger.info(f"{self.channel_name}: Similar amplitudes ({amp_ratio_db:+.1f}dB) "
                                   f"- consider 15-second windows for better discrimination")
                
                # Check overall signal strength (quality)
                if quality_mean < 3.0 and window_seconds < 20:
                    # Weak signals (poor SNR)
                    # → Expand window regardless of amplitude ratio
                    window_adjustment = "expand_weak"
                    logger.info(f"{self.channel_name}: Weak signals (quality={quality_mean:.1f}) "
                               f"- consider 15-20 second windows for better SNR")
            
            # Format delay info (may be None if all single-peak detections)
            if delay_mean is not None and delays:
                delay_str = f"delay={delay_mean:.2f}±{np.std(delays):.2f}ms"
            else:
                delay_str = "delay=N/A (single-peak only)"
            
            logger.info(f"{self.channel_name}: BCD correlation ({len(windows_data)} windows, {window_seconds}s) - "
                       f"WWV amp={wwv_amp_mean:.4f}±{np.std(wwv_amps):.4f}, "
                       f"WWVH amp={wwvh_amp_mean:.4f}±{np.std(wwvh_amps):.4f}, "
                       f"ratio={20*np.log10(max(wwv_amp_mean,1e-10)/max(wwvh_amp_mean,1e-10)):+.1f}dB, "
                       f"{delay_str}, "
                       f"quality={quality_mean:.1f}")
            
            return wwv_amp_mean, wwvh_amp_mean, delay_mean, quality_mean, windows_data
            
        except Exception as e:
            logger.error(f"{self.channel_name}: BCD discrimination failed: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return None, None, None, None, None
    
    def detect_bcd_discrimination(
        self,
        iq_samples: np.ndarray,
        sample_rate: int,
        minute_timestamp: float,
        frequency_mhz: Optional[float] = None,
        doppler_info: Optional[Dict[str, float]] = None,
        timing_power_ratio_db: Optional[float] = None,  # WWV-WWVH power for single-peak classification
        ground_truth_station: Optional[str] = None,  # From 500/600 Hz exclusive minutes
        wwv_tick_snr_db: Optional[float] = None,  # SNR of 1000 Hz tick
        wwvh_tick_snr_db: Optional[float] = None  # SNR of 1200 Hz tick
    ) -> Tuple[Optional[float], Optional[float], Optional[float], Optional[float], List[Dict[str, float]]]:
        """
        Wrapper method for BCD discrimination with adaptive window sizing.
        
        Calls bcd_correlation_discrimination with Doppler-adaptive window selection.
        Uses ionospheric Doppler shift to determine maximum coherent integration
        window, preventing phase rotation from degrading correlation quality.
        
        CRITICAL FIX (2025-11-26): Changed default from 60s non-overlapping to 10s
        sliding windows. The 60s window exceeded typical HF coherence time (Tc ~10-20s),
        causing Doppler-induced phase rotation to destroy correlation. Now defaults to
        10s windows with 1s steps, producing ~50 measurements/minute for time-series
        tracking of propagation dynamics.
        
        Args:
            iq_samples: Full minute of complex IQ samples
            sample_rate: Sample rate in Hz
            minute_timestamp: UTC timestamp of minute boundary
            frequency_mhz: Operating frequency for geographic ToA prediction
            doppler_info: Optional Doppler estimation from tick phase tracking
            
        Returns:
            Tuple of (wwv_amp_mean, wwvh_amp_mean, delay_mean, quality_mean, windows_list)
        """
        # 1. Determine Window Size (T_int)
        # Default to 10 seconds - within typical HF coherence time (Tc ~10-20s)
        # This prevents Doppler-induced phase rotation from destroying correlation
        window_seconds = 10.0  # Safe default within typical Tc
        
        if doppler_info and 'max_coherent_window_sec' in doppler_info:
            # Use Doppler-derived coherence limit, clamped to [10s, 20s]
            # Even with stable channel, >20s risks averaging over fading periods
            doppler_limit = doppler_info['max_coherent_window_sec']
            window_seconds = max(10.0, min(doppler_limit, 20.0))
            
            logger.info(f"{self.channel_name}: Doppler-limited BCD window to {window_seconds:.1f}s "
                       f"(Δf_D={doppler_info.get('wwv_doppler_hz', 0):+.3f} Hz, "
                       f"quality={doppler_info.get('doppler_quality', 0):.2f})")
        
        # 2. Determine Slide Step (T_slide)
        # Use 1-second sliding step for high-resolution time-series tracking
        # This produces ~50 windows/minute, capturing propagation dynamics
        step_seconds = 1.0
        
        logger.debug(f"{self.channel_name}: BCD correlation: T_int={window_seconds:.1f}s, T_slide={step_seconds:.1f}s")
        
        return self.bcd_correlation_discrimination(
            iq_samples=iq_samples,
            sample_rate=sample_rate,
            minute_timestamp=minute_timestamp,
            frequency_mhz=frequency_mhz,
            window_seconds=window_seconds,  # Now 10-20s (within Tc)
            step_seconds=step_seconds,      # 1s sliding for time-series
            adaptive=False,  # Doppler adaptation handles window sizing
            enable_single_station_detection=True,
            timing_power_ratio_db=timing_power_ratio_db,  # For single-peak cross-validation
            ground_truth_station=ground_truth_station,  # From 500/600 Hz exclusive minutes
            wwv_tick_snr_db=wwv_tick_snr_db,  # SNR evidence
            wwvh_tick_snr_db=wwvh_tick_snr_db
        )
    
    def _generate_bcd_template(
        self,
        minute_timestamp: float,
        sample_rate: int,
        envelope_only: bool = False
    ) -> Optional[np.ndarray]:
        """
        Generate expected 100 Hz BCD template for a given UTC minute
        
        Uses the WWVBCDEncoder to generate an accurate template based on
        Phil Karn's wwvsim.c implementation.
        
        Args:
            minute_timestamp: UTC timestamp of minute boundary
            sample_rate: Sample rate in Hz
            envelope_only: If True, return envelope without 100 Hz carrier
                          (for correlation with demodulated signals)
            
        Returns:
            60-second BCD template as numpy array, or None if generation fails
        """
        try:
            # BCD encoder not available for CHU channels
            if self.bcd_encoder is None:
                return None
            
            # Use the encoder instance that was created during __init__
            template = self.bcd_encoder.encode_minute(minute_timestamp, envelope_only=envelope_only)
            return template
            
        except Exception as e:
            logger.error(f"{self.channel_name}: Failed to generate BCD template: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return None

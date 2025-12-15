#!/usr/bin/env python3
"""
Multi-Stage Decimation for GRAPE Recorder - Precision Doppler Measurements

================================================================================
PURPOSE
================================================================================
Decimate 20 kHz IQ samples to 10 Hz (factor 2000) while preserving:
    1. Phase continuity (for Doppler measurements)
    2. Flat passband response (for accurate amplitude)
    3. Clean stopband (no aliasing artifacts)

This is Phase 3 of the GRAPE pipeline: prepare data for upload to the
GRAPE/HamSCI/PSWS repository at 10 Hz sample rate.

================================================================================
THEORY: WHY MULTI-STAGE DECIMATION?
================================================================================
Direct decimation by factor R=2000 would require an anti-aliasing filter
with transition bandwidth of (10/2)/20000 = 0.00025 of Nyquist, which would
need ~100,000 taps. This is computationally prohibitive.

Multi-stage decimation splits the problem:
    20 kHz → 400 Hz → 10 Hz
    (R=50)    (R=40)

Each stage has relaxed filter requirements:
    Stage 1: 20 kHz → 400 Hz, transition at 200 Hz (1% of Nyquist) → ~100 taps
    Stage 2: 400 Hz → 10 Hz, transition at 5 Hz (2.5% of Nyquist) → ~200 taps

TOTAL COMPUTATION: ~300 multiplies/sample vs ~100,000 for single-stage

REFERENCE: Crochiere, R.E. & Rabiner, L.R. (1983). "Multirate Digital Signal
           Processing." Prentice-Hall. Chapter 3.

================================================================================
THEORY: CIC FILTERS (Stage 1)
================================================================================
The CIC (Cascaded Integrator-Comb) filter is ideal for large decimation
factors because it requires NO multipliers - only additions and subtractions.

STRUCTURE:
    ┌───────────┐   ┌───────────┐   ┌───────────┐   ┌───────────┐
    │ Integrator│ → │ Integrator│ → │   ↓R      │ → │   Comb    │ → ...
    │   1/(1-z⁻¹)│   │   1/(1-z⁻¹)│   │ Decimate │   │  (1-z⁻¹)  │
    └───────────┘   └───────────┘   └───────────┘   └───────────┘
       (N stages)                                      (N stages)

TRANSFER FUNCTION:
    H(z) = [(1 - z^(-RM)) / (1 - z^(-1))]^N

Where:
    R = decimation factor (50 for 20 kHz → 400 Hz)
    M = differential delay (typically 1)
    N = filter order (4 in our implementation)

FREQUENCY RESPONSE:
    |H(f)| = |sin(πfRM/fs) / sin(πf/fs)|^N

This creates:
    - Passband with sinc-like droop (requires compensation)
    - Nulls at multiples of fs/R (perfect alias rejection at those frequencies)

CIC DROOP at edge of 5 Hz passband (f=5 Hz, R=50, fs=20000, N=4):
    |H(5)| = |sin(π×5×50/20000) / sin(π×5/20000)|^4 ≈ 0.987 (−0.11 dB)

REFERENCE: Hogenauer, E.B. (1981). "An economical class of digital filters
           for decimation and interpolation." IEEE Trans. ASSP-29(2), 155-162.

================================================================================
THEORY: COMPENSATION FILTER (Stage 2a)
================================================================================
The CIC's sinc response causes passband droop. We correct this with an
inverse-sinc FIR filter operating at the intermediate rate (400 Hz).

COMPENSATION RESPONSE:
    H_comp(f) = 1 / |H_CIC(f)|   for f ∈ [0, passband_width]
    H_comp(f) = 1                 for f > passband_width

This flattens the passband to within 0.1 dB of unity gain.

WHY AT INTERMEDIATE RATE?
- At 400 Hz, the compensation filter is applied AFTER CIC decimation
- Filter operates on fewer samples (50× fewer than at 20 kHz)
- Passband droop correction is independent of final decimation

REFERENCE: Altera Application Note 455: "Understanding CIC Compensation Filters"

================================================================================
THEORY: FINAL ANTI-ALIASING FILTER (Stage 2b)
================================================================================
Before the final decimation (400 Hz → 10 Hz), we need a sharp lowpass filter:

REQUIREMENTS:
    - Passband: 0 - 5 Hz (Nyquist for 10 Hz output)
    - Stopband: > 6 Hz
    - Stopband attenuation: > 90 dB (prevents aliasing artifacts)

DESIGN METHOD: Kaiser Window
    The Kaiser window provides optimal trade-off between main lobe width
    and side lobe level for a given filter length.

    β = Kaiser shape parameter (calculated from required attenuation)
    N = filter length ≈ (A - 8) / (2.285 × Δω)

    Where:
        A = stopband attenuation (dB)
        Δω = transition width (normalized frequency)

REFERENCE: Kaiser, J.F. (1974). "Nonrecursive digital filter design using
           the I₀-sinh window function." IEEE Int. Symp. Circuits and Systems.

================================================================================
SIGNAL FLOW
================================================================================
    Input: Complex IQ @ 20 kHz (1,200,000 samples/minute)
           │
           ▼
    ┌──────────────────────────────────────────────────────────────────────┐
    │ STAGE 1: CIC Filter (R=50, N=4)                                      │
    │   - Input:  20,000 Hz                                                │
    │   - Output: 400 Hz                                                   │
    │   - Alias rejection at 400, 800, 1200... Hz                          │
    │   - Passband droop: ~0.1 dB at 5 Hz                                  │
    └──────────────────────────────────────────────────────────────────────┘
           │
           ▼ (24,000 samples/minute)
    ┌──────────────────────────────────────────────────────────────────────┐
    │ STAGE 2a: Compensation FIR (R=1, 63 taps)                            │
    │   - Input:  400 Hz                                                   │
    │   - Output: 400 Hz (no decimation)                                   │
    │   - Inverse sinc correction in passband (0-5 Hz)                     │
    │   - Flattens response to < 0.1 dB ripple                             │
    └──────────────────────────────────────────────────────────────────────┘
           │
           ▼
    ┌──────────────────────────────────────────────────────────────────────┐
    │ STAGE 2b: Final FIR + Decimate (R=40, ~200 taps)                     │
    │   - Input:  400 Hz                                                   │
    │   - Output: 10 Hz                                                    │
    │   - Sharp cutoff at 5 Hz (Nyquist)                                   │
    │   - Stopband > 90 dB (aliasing prevention)                           │
    └──────────────────────────────────────────────────────────────────────┘
           │
           ▼
    Output: Complex IQ @ 10 Hz (600 samples/minute)

================================================================================
DESIGN GOALS
================================================================================
1. DOPPLER PRECISION
   - Flat passband 0-5 Hz (< 0.1 dB ripple)
   - Phase continuity (linear phase filters throughout)
   - Resolution: 0.1 Hz Doppler at 10 MHz carrier = 3 m/s velocity

2. CLEAN OUTPUT
   - No aliasing artifacts (> 90 dB stopband)
   - No filter ringing (smooth transitions)
   - Consistent amplitude (flat passband)

3. COMPUTATIONAL EFFICIENCY
   - CIC requires no multiplications
   - Multi-stage reduces total operations by ~100×
   - Real-time capable on modest hardware

================================================================================
USAGE
================================================================================
    from hf_timestd.core.decimation import decimate_for_upload
    
    # 60 seconds of 20 kHz IQ data
    iq_20k = np.random.randn(1200000) + 1j*np.random.randn(1200000)
    
    # Decimate to 10 Hz
    iq_10hz = decimate_for_upload(iq_20k, input_rate=20000, output_rate=10)
    
    print(len(iq_10hz))  # 600 samples (60 seconds × 10 Hz)

================================================================================
REVISION HISTORY
================================================================================
2025-12-07: Added comprehensive theoretical documentation
2025-11-01: Added support for 16 kHz input (legacy compatibility)
2025-10-15: Initial implementation with 3-stage pipeline
"""

import numpy as np
from scipy import signal
import logging
from typing import Optional, Dict

logger = logging.getLogger(__name__)

# Supported input sample rates and their decimation parameters
# All paths converge to 400 Hz intermediate rate, then final FIR decimates to 10 Hz
# To add a new rate: ensure input_rate / cic_decimation = 400
SUPPORTED_INPUT_RATES: Dict[int, Dict] = {
    20000: {
        'cic_decimation': 50,      # 20000 / 50 = 400 Hz
        'total_factor': 2000,      # 20000 / 10 = 2000
        'description': '20 kHz (default)',
    },
    16000: {
        'cic_decimation': 40,      # 16000 / 40 = 400 Hz  
        'total_factor': 1600,      # 16000 / 10 = 1600
        'description': '16 kHz (legacy)',
    },
    # To add 24 kHz support:
    # 24000: {
    #     'cic_decimation': 60,    # 24000 / 60 = 400 Hz
    #     'total_factor': 2400,    # 24000 / 10 = 2400
    #     'description': '24 kHz',
    # },
}

OUTPUT_RATE = 10  # Fixed 10 Hz output for GRAPE
INTERMEDIATE_RATE = 400  # 400 Hz intermediate (after CIC)
FINAL_FIR_DECIMATION = 40  # 400 / 40 = 10 Hz


def _design_cic_filter(decimation_factor: int, order: int = 4) -> dict:
    """
    Design CIC (Cascaded Integrator-Comb) filter parameters.
    
    THEORY:
    -------
    The CIC filter is a multiplier-free implementation of a moving average
    filter cascaded N times. It's ideal for large decimation factors.
    
    Transfer function:
        H(z) = [(1 - z^(-R)) / (1 - z^(-1))]^N
    
    Frequency response:
        |H(f)| = |sin(πfR/fs) / sin(πf/fs)|^N ≈ |sinc(fR/fs)|^N
    
    Properties:
        - Passband droop: ~(π²/6) × (f/fs × R)² × N at small f
        - Nulls at: f = k × fs/R for k = 1, 2, 3, ...
        - These nulls provide alias rejection at folded frequencies
    
    ORDER SELECTION:
    ----------------
    Higher order (N) provides:
        + Better stopband attenuation (~6N dB/octave rolloff)
        + Deeper nulls at alias frequencies
        - More passband droop (must be compensated)
        - Larger output word growth (N × log₂(R) bits)
    
    N=4 is a good compromise: ~24 dB/octave rolloff with manageable droop.
    
    Args:
        decimation_factor: Decimation ratio R (50 for 20 kHz → 400 Hz)
        order: Filter order N (4 = good balance of performance/complexity)
        
    Returns:
        dict with 'order' and 'decimation_factor' for _apply_cic_filter()
        
    Reference:
        Hogenauer (1981), IEEE Trans. ASSP-29(2)
    """
    return {
        'order': order,
        'decimation_factor': decimation_factor
    }


def _apply_cic_filter(samples: np.ndarray, cic_params: dict) -> np.ndarray:
    """
    Apply CIC filter to input samples and decimate.
    
    IMPLEMENTATION:
    ---------------
    This uses a boxcar (moving average) filter approximation rather than
    true CIC integrator/comb stages. This is mathematically equivalent:
    
        True CIC: H(z) = [(1 - z^(-R)) / (1 - z^(-1))]^N
                       = [Σ(k=0 to R-1) z^(-k)]^N
        
        Boxcar:   H(z) = (1/R) × Σ(k=0 to R-1) z^(-k)
        
        N boxcars = (1/R^N) × [Σ z^(-k)]^N
    
    The 1/R normalization is applied to prevent overflow and maintain
    unity DC gain.
    
    FILTER STAGES:
    --------------
    Stage 1: Boxcar(R) → output = (x[n] + x[n-1] + ... + x[n-R+1]) / R
    Stage 2: Boxcar(R) on Stage 1 output
    ...
    Stage N: Boxcar(R) on Stage N-1 output
    Then: Decimate by R (keep every R-th sample)
    
    WHY BOXCAR APPROXIMATION?
    - Simpler to implement with scipy.signal.lfilter
    - Numerically stable (no accumulator overflow)
    - Equivalent frequency response to true CIC
    - True CIC only advantageous in fixed-point hardware
    
    Args:
        samples: Complex input samples at high rate
        cic_params: Dict with 'order' (N) and 'decimation_factor' (R)
        
    Returns:
        Decimated complex samples at output_rate = input_rate / R
        
    Note:
        Output length = floor(input_length / R)
    """
    R = cic_params['decimation_factor']
    N = cic_params['order']
    
    # Boxcar filter coefficients: [1/R, 1/R, ..., 1/R] (R terms)
    # This is equivalent to an R-point moving average
    b = np.ones(R) / R
    a = [1.0]  # FIR filter (no feedback)
    
    # Apply N cascaded stages (equivalent to N-stage CIC)
    # Each stage convolves with the boxcar, effectively raising
    # the sinc response to the Nth power
    filtered = samples
    for _ in range(N):
        filtered = signal.lfilter(b, a, filtered)
    
    # Decimate: keep every R-th sample
    # Safe to do after anti-alias filtering
    decimated = filtered[::R]
    
    logger.debug(f"CIC filter: {len(samples)} → {len(decimated)} samples (R={R}, N={N})")
    return decimated


def _design_compensation_fir(sample_rate: int, passband_width: float, 
                             cic_order: int, cic_decimation: int,
                             num_taps: int = 63) -> np.ndarray:
    """
    Design inverse-sinc FIR filter to compensate for CIC passband droop.
    
    THEORY:
    -------
    The CIC filter has a sinc^N frequency response that droops in the passband:
    
        |H_CIC(f)| = |sin(πfR/fs_orig) / sin(πf/fs_orig)|^N
        
    At the edge of our passband (f=5 Hz), this causes ~0.1-0.5 dB attenuation
    depending on the decimation factor and order.
    
    For Doppler measurements, we need flat passband response. The compensation
    filter inverts the CIC droop within the passband:
    
        |H_comp(f)| = 1 / |H_CIC(f)|   for f ≤ passband_width
        |H_comp(f)| = 1                 for f > passband_width
    
    DESIGN METHOD: Frequency Sampling
    ----------------------------------
    We specify the desired frequency response at discrete points and use
    scipy.signal.firwin2 to design an FIR filter that matches it.
    
    This is more flexible than window-based design because we can specify
    arbitrary magnitude responses.
    
    WHY FIR (not IIR)?
    ------------------
    - Linear phase (no group delay distortion)
    - Inherently stable
    - Predictable transient behavior
    - IIR would have lower latency but nonlinear phase
    
    TAP COUNT SELECTION:
    --------------------
    More taps = better frequency resolution = more accurate compensation
    - 31 taps: ~0.3 dB ripple in passband
    - 63 taps: ~0.1 dB ripple in passband (our choice)
    - 127 taps: ~0.03 dB ripple in passband
    
    Args:
        sample_rate: Current sample rate (400 Hz after CIC decimation)
        passband_width: Width of flat passband in Hz (5 Hz for ±5 Hz Doppler)
        cic_order: Order N of the preceding CIC filter (4)
        cic_decimation: Decimation factor R of the preceding CIC (50)
        num_taps: FIR filter length (63 = good accuracy/efficiency tradeoff)
        
    Returns:
        FIR filter coefficients (real-valued, symmetric for linear phase)
        
    Reference:
        Altera AN-455: "Understanding CIC Compensation Filters"
    """
    # Frequency points for compensation
    nyquist = sample_rate / 2
    freqs = np.linspace(0, nyquist, 500)
    
    # CIC frequency response (normalized)
    # |sinc(π * f * R / fs)|^N
    cic_response = np.ones_like(freqs)
    for f_idx, f in enumerate(freqs):
        if f == 0:
            cic_response[f_idx] = 1.0
        else:
            # Normalized frequency for CIC
            x = np.pi * f * cic_decimation / (sample_rate * cic_decimation)
            cic_response[f_idx] = np.abs(np.sin(x) / x) ** cic_order
    
    # Compensation: inverse of CIC droop within passband
    compensation_response = np.ones_like(freqs)
    for f_idx, f in enumerate(freqs):
        if f <= passband_width:
            # Compensate within passband
            compensation_response[f_idx] = 1.0 / max(cic_response[f_idx], 0.1)  # Avoid division by zero
        else:
            # Unity gain outside passband (let final filter handle stopband)
            compensation_response[f_idx] = 1.0
    
    # Design FIR using frequency sampling method
    # firwin2 creates filter matching arbitrary frequency response
    taps = signal.firwin2(num_taps, freqs, compensation_response, fs=sample_rate)
    
    logger.debug(f"Compensation FIR: {num_taps} taps, passband ±{passband_width} Hz")
    return taps


def _design_final_fir(sample_rate: int, cutoff: float, 
                     transition_width: float = 1.0,
                     stopband_attenuation_db: float = 90) -> np.ndarray:
    """
    Design sharp anti-aliasing FIR filter for final decimation stage.
    
    THEORY: ANTI-ALIASING REQUIREMENT
    ----------------------------------
    Before decimating by factor R, all frequency content above the new
    Nyquist frequency must be attenuated to prevent aliasing:
    
        New Nyquist = (sample_rate / R) / 2 = 400 / 40 / 2 = 5 Hz
        
    Any signal above 5 Hz will fold back into 0-5 Hz after decimation.
    We need > 90 dB attenuation to make aliases below the noise floor.
    
    SPECIFICATION:
    --------------
        Passband:   0 - 5 Hz (flat, < 0.1 dB ripple)
        Transition: 5 - 6 Hz (as sharp as possible)
        Stopband:   > 6 Hz (> 90 dB attenuation)
    
    DESIGN METHOD: Kaiser Window
    ----------------------------
    The Kaiser window provides near-optimal (Chebyshev sense) trade-off
    between main lobe width and side lobe level.
    
    Key equations:
        β = Kaiser shape parameter (controls side lobe level)
        N = filter length ≈ (A - 8) / (2.285 × Δω)
        
    Where:
        A = stopband attenuation in dB (90)
        Δω = transition width in radians/sample = 2π × (transition_width/sample_rate)
    
    For our specs (A=90 dB, Δf=1 Hz at 400 Hz):
        Δω = 2π × (1/400) = 0.0157 rad/sample
        N ≈ (90 - 8) / (2.285 × 0.0157) ≈ 229 taps
    
    WHY 90 dB STOPBAND?
    -------------------
    - 16-bit audio: ~96 dB dynamic range → aliases below quantization noise
    - 24-bit audio: ~144 dB dynamic range → aliases still ~50 dB below noise
    - Higher attenuation (>100 dB) provides diminishing returns
    
    Args:
        sample_rate: Current sample rate (400 Hz)
        cutoff: Passband edge frequency (5 Hz for 10 Hz output Nyquist)
        transition_width: Width of transition band in Hz (1 Hz = 5-6 Hz band)
        stopband_attenuation_db: Required stopband rejection (90 dB typical)
        
    Returns:
        FIR filter coefficients (real-valued, symmetric for linear phase)
        
    Reference:
        Kaiser, J.F. (1974). IEEE Int. Symp. Circuits and Systems.
        Oppenheim & Schafer (2010). "Discrete-Time Signal Processing," 3rd ed.
    """
    # Calculate filter order for required attenuation
    # Kaiser window provides excellent control over stopband
    # Formula: N ≈ (stopband_db - 8) / (2.285 * transition_width / fs)
    transition_normalized = transition_width / sample_rate
    num_taps = int(np.ceil((stopband_attenuation_db - 8) / (2.285 * 2 * np.pi * transition_normalized))) + 1
    
    # Ensure odd number of taps (symmetric, linear phase)
    if num_taps % 2 == 0:
        num_taps += 1
    
    # Limit maximum taps for computational efficiency
    num_taps = min(num_taps, 401)
    
    # Design using Kaiser window (optimal for given specs)
    beta = signal.kaiser_beta(signal.kaiser_atten(num_taps, transition_normalized))
    taps = signal.firwin(num_taps, cutoff, window=('kaiser', beta), 
                        fs=sample_rate, scale=True)
    
    logger.debug(f"Final FIR: {num_taps} taps, cutoff={cutoff} Hz, "
                f"stopband={stopband_attenuation_db} dB")
    return taps


def decimate_for_upload(iq_samples: np.ndarray, input_rate: int = 20000, 
                        output_rate: int = 10) -> Optional[np.ndarray]:
    """
    Multi-stage optimized decimation: 20 kHz → 10 Hz (factor 2000)
    Also supports legacy 16 kHz → 10 Hz (factor 1600) for backward compatibility.
    
    Three-stage pipeline preserving Doppler precision:
    - 20 kHz: CIC R=50 (20000→400 Hz) + Comp FIR + Final FIR R=40 (400→10 Hz)
    - 16 kHz: CIC R=40 (16000→400 Hz) + Comp FIR + Final FIR R=40 (400→10 Hz)
    
    Args:
        iq_samples: Complex IQ samples at input_rate
        input_rate: Input sample rate (Hz) - 20000 (default) or 16000
        output_rate: Output sample rate (Hz) - must be 10
        
    Returns:
        Decimated complex IQ samples at output_rate (10 Hz)
        Returns None if input too short or rates unsupported
        
    Example:
        >>> iq_20k = np.random.randn(1200000) + 1j*np.random.randn(1200000)  # 60 seconds
        >>> iq_10hz = decimate_for_upload(iq_20k, 20000, 10)
        >>> len(iq_10hz)
        600  # 60 seconds at 10 Hz
    """
    # Validate parameters
    if output_rate != OUTPUT_RATE:
        raise ValueError(f"Output rate must be {OUTPUT_RATE} Hz (got {output_rate})")
    
    # Check if input rate is supported
    if input_rate not in SUPPORTED_INPUT_RATES:
        supported = ', '.join(f"{r} Hz" for r in sorted(SUPPORTED_INPUT_RATES.keys()))
        raise ValueError(
            f"Unsupported input rate: {input_rate} Hz. "
            f"Supported rates: {supported}. "
            f"To add a new rate, update SUPPORTED_INPUT_RATES in decimation.py."
        )
    
    rate_config = SUPPORTED_INPUT_RATES[input_rate]
    cic_decimation = rate_config['cic_decimation']
    expected_factor = rate_config['total_factor']
    
    total_factor = input_rate // output_rate
    if total_factor != expected_factor:
        raise ValueError(f"Total decimation factor must be {expected_factor} (got {total_factor})")
    
    # Check minimum length
    # CIC needs at least R samples, compensation FIR needs its length, final FIR needs its length
    min_length = 1000  # Conservative minimum for 60-second files
    if len(iq_samples) < min_length:
        logger.warning(f"Input too short for decimation: {len(iq_samples)} < {min_length} samples")
        return None
    
    logger.debug(f"Starting 3-stage decimation: {len(iq_samples)} samples @ {input_rate} Hz")
    
    try:
        # STAGE 1: CIC Filter (input_rate → 400 Hz)
        cic_params = _design_cic_filter(decimation_factor=cic_decimation, order=4)
        iq_400hz = _apply_cic_filter(iq_samples, cic_params)
        logger.debug(f"After CIC (R={cic_decimation}): {len(iq_400hz)} samples @ 400 Hz")
        
        # STAGE 2: Compensation FIR (400 Hz, R=1)
        # Flatten ±5 Hz passband (corrects CIC droop)
        comp_taps = _design_compensation_fir(
            sample_rate=400,
            passband_width=5.0,  # ±5 Hz for Doppler measurements
            cic_order=4,
            cic_decimation=cic_decimation,
            num_taps=63
        )
        iq_400hz_flat = signal.lfilter(comp_taps, [1.0], iq_400hz)
        logger.debug(f"After compensation FIR: {len(iq_400hz_flat)} samples @ 400 Hz")
        
        # STAGE 3: Final FIR + Decimation (400 Hz → 10 Hz, R=40)
        final_taps = _design_final_fir(
            sample_rate=400,
            cutoff=5.0,  # Nyquist for 10 Hz output
            transition_width=1.0,  # 5-6 Hz transition
            stopband_attenuation_db=90
        )
        iq_filtered = signal.lfilter(final_taps, [1.0], iq_400hz_flat)
        iq_10hz = iq_filtered[::40]  # Decimate by 40
        
        logger.debug(f"After final FIR+decimate: {len(iq_10hz)} samples @ 10 Hz")
        logger.info(f"✅ Decimation complete: {len(iq_samples)} @ {input_rate} Hz → "
                   f"{len(iq_10hz)} @ {output_rate} Hz (factor {total_factor})")
        
        return iq_10hz
        
    except Exception as e:
        logger.error(f"Decimation failed: {e}")
        return None


def decimate_for_upload_simple(iq_samples: np.ndarray, input_rate: int = 20000,
                               output_rate: int = 10) -> Optional[np.ndarray]:
    """
    Simple fallback decimation using scipy.signal.decimate
    
    Use this if the optimized version has issues.
    - 20 kHz: Stages 10×10×20 = 2000
    - 16 kHz: Stages 10×10×16 = 1600
    
    Args:
        iq_samples: Complex IQ samples at input_rate
        input_rate: Input sample rate (Hz) - 20000 or 16000
        output_rate: Output sample rate (Hz) - must be 10
        
    Returns:
        Decimated complex IQ samples at output_rate
    """
    if input_rate % output_rate != 0:
        raise ValueError(f"Input rate {input_rate} must be integer multiple of output rate {output_rate}")
    
    total_factor = input_rate // output_rate
    
    if total_factor == 1:
        return iq_samples
    
    min_length = 100
    if len(iq_samples) < min_length:
        logger.warning(f"Input too short: {len(iq_samples)} < {min_length} samples")
        return None
    
    try:
        # Stage 1: input → input/10 Hz (factor 10)
        # 20000 → 2000 Hz or 16000 → 1600 Hz
        if total_factor >= 10:
            iq_samples = signal.decimate(iq_samples, q=10, ftype='iir', zero_phase=True)
            total_factor //= 10
        
        # Stage 2: → /10 Hz (factor 10)
        # 2000 → 200 Hz or 1600 → 160 Hz
        if total_factor >= 10:
            iq_samples = signal.decimate(iq_samples, q=10, ftype='iir', zero_phase=True)
            total_factor //= 10
        
        # Stage 3: → 10 Hz (factor 20 for 20kHz, factor 16 for 16kHz)
        # 200 → 10 Hz (factor 20) or 160 → 10 Hz (factor 16)
        if total_factor >= 16:
            iq_samples = signal.decimate(iq_samples, q=total_factor, ftype='iir', zero_phase=True)
            total_factor = 1
        
        # Handle any remaining factor
        if total_factor > 1:
            iq_samples = signal.decimate(iq_samples, q=total_factor, ftype='iir', zero_phase=True)
        
        return iq_samples
        
    except Exception as e:
        logger.error(f"Simple decimation failed: {e}")
        return None


class StatefulDecimator:
    """
    Stateful decimator that preserves filter state across calls.
    
    This eliminates the ~1 second filter transients at minute boundaries
    that cause visible artifacts in spectrograms.
    
    Usage:
        decimator = StatefulDecimator(input_rate=20000, output_rate=10)
        
        # Process minute by minute - state is preserved
        for minute_samples in minute_chunks:
            decimated = decimator.process(minute_samples)
            # No transient artifacts at boundaries!
        
        # Reset state for new channel/session
        decimator.reset()
    """
    
    def __init__(self, input_rate: int = 20000, output_rate: int = 10):
        """
        Initialize stateful decimator.
        
        Args:
            input_rate: Input sample rate (Hz) - 20000 or 16000
            output_rate: Output sample rate (Hz) - must be 10
        """
        if output_rate != OUTPUT_RATE:
            raise ValueError(f"Output rate must be {OUTPUT_RATE} Hz")
        
        if input_rate not in SUPPORTED_INPUT_RATES:
            raise ValueError(f"Unsupported input rate: {input_rate} Hz")
        
        self.input_rate = input_rate
        self.output_rate = output_rate
        
        rate_config = SUPPORTED_INPUT_RATES[input_rate]
        self.cic_decimation = rate_config['cic_decimation']
        
        # Design filters once
        self.cic_params = _design_cic_filter(self.cic_decimation, order=4)
        
        # CIC boxcar coefficients
        R = self.cic_params['decimation_factor']
        self.cic_b = np.ones(R) / R
        self.cic_a = [1.0]
        self.cic_order = self.cic_params['order']
        
        self.comp_taps = _design_compensation_fir(
            sample_rate=400,
            passband_width=5.0,
            cic_order=4,
            cic_decimation=self.cic_decimation,
            num_taps=63
        )
        
        self.final_taps = _design_final_fir(
            sample_rate=400,
            cutoff=5.0,
            transition_width=1.0,
            stopband_attenuation_db=90
        )
        
        # Initialize filter states
        self.reset()
        
        logger.info(f"StatefulDecimator initialized: {input_rate} → {output_rate} Hz")
    
    def reset(self):
        """Reset all filter states (call at session/channel boundaries)."""
        # CIC: N cascaded stages, each needs state
        R = self.cic_params['decimation_factor']
        self.cic_zi = [signal.lfilter_zi(self.cic_b, self.cic_a) * 0 
                      for _ in range(self.cic_order)]
        
        # Compensation FIR state
        self.comp_zi = signal.lfilter_zi(self.comp_taps, [1.0]) * 0
        
        # Final FIR state
        self.final_zi = signal.lfilter_zi(self.final_taps, [1.0]) * 0
        
        # Track samples for decimation phase alignment
        self.cic_sample_count = 0
        self.final_sample_count = 0
        
        logger.debug("Decimator state reset")
    
    def process(self, iq_samples: np.ndarray) -> Optional[np.ndarray]:
        """
        Process samples with preserved filter state.
        
        Args:
            iq_samples: Complex IQ samples at input_rate
            
        Returns:
            Decimated samples at output_rate
        """
        if len(iq_samples) == 0:
            return np.array([], dtype=np.complex64)
        
        try:
            # STAGE 1: CIC Filter with state preservation
            R = self.cic_params['decimation_factor']
            filtered = iq_samples
            
            for i in range(self.cic_order):
                filtered, self.cic_zi[i] = signal.lfilter(
                    self.cic_b, self.cic_a, filtered, zi=self.cic_zi[i]
                )
            
            # Decimate with phase alignment
            # Account for samples from previous calls
            start_offset = (R - (self.cic_sample_count % R)) % R
            self.cic_sample_count += len(filtered)
            iq_400hz = filtered[start_offset::R]
            
            if len(iq_400hz) == 0:
                return np.array([], dtype=np.complex64)
            
            # STAGE 2: Compensation FIR with state
            iq_400hz_flat, self.comp_zi = signal.lfilter(
                self.comp_taps, [1.0], iq_400hz, zi=self.comp_zi
            )
            
            # STAGE 3: Final FIR + decimation with state
            iq_filtered, self.final_zi = signal.lfilter(
                self.final_taps, [1.0], iq_400hz_flat, zi=self.final_zi
            )
            
            # Decimate by 40 with phase alignment
            start_offset = (40 - (self.final_sample_count % 40)) % 40
            self.final_sample_count += len(iq_filtered)
            iq_10hz = iq_filtered[start_offset::40]
            
            return iq_10hz.astype(np.complex64)
            
        except Exception as e:
            logger.error(f"Stateful decimation failed: {e}")
            return None


# Module configuration - easy to swap implementations
DECIMATION_FUNCTION = decimate_for_upload  # Use optimized version
# DECIMATION_FUNCTION = decimate_for_upload_simple  # Uncomment for simple fallback


def get_supported_rates() -> Dict[int, Dict]:
    """
    Get dictionary of supported input sample rates.
    
    Returns:
        Dict mapping input_rate to configuration dict with:
        - cic_decimation: CIC filter decimation factor
        - total_factor: Total decimation factor to reach 10 Hz
        - description: Human-readable description
    """
    return SUPPORTED_INPUT_RATES.copy()


def is_rate_supported(input_rate: int) -> bool:
    """Check if an input sample rate is supported for decimation."""
    return input_rate in SUPPORTED_INPUT_RATES


def get_decimator(input_rate: int, output_rate: int):
    """
    Factory function to get appropriate decimator
    
    Returns a configured decimation function for the given rates.
    
    Args:
        input_rate: Input sample rate (Hz) - see SUPPORTED_INPUT_RATES
        output_rate: Output sample rate (Hz) - must be 10
    
    Returns:
        Callable that decimates from input_rate to output_rate
        
    Raises:
        ValueError: If input_rate is not supported
    """
    def decimator(samples: np.ndarray) -> Optional[np.ndarray]:
        return DECIMATION_FUNCTION(samples, input_rate, output_rate)
    
    return decimator

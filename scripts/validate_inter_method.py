#!/usr/bin/env python3
"""
Inter-Method Validation for WWV/WWVH Discrimination

Cross-validates timing information from independent sources:
1. RTP timestamps → absolute time anchor
2. BCD 100 Hz → encoded minute/hour/day
3. Per-second ticks → sample-accurate timing
4. Test signal ToA → expected at 13.0s
5. 440 Hz detection → minute-specific timing

The goal is to assess agreement between methods and detect anomalies.
"""

import sys
import numpy as np
from pathlib import Path
from datetime import datetime, timezone
from scipy.fft import rfft, rfftfreq
from scipy import signal
import argparse

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


def load_archive(npz_path: Path) -> dict:
    """Load NPZ archive with all metadata"""
    data = np.load(npz_path)
    return {k: data[k] for k in data.keys()}


def extract_tick_timing(iq_samples: np.ndarray, sample_rate: int) -> dict:
    """
    Extract per-second tick positions from IQ samples.
    
    Returns sample indices where 1000 Hz (WWV) and 1200 Hz (WWVH) ticks are detected.
    """
    # AM demodulate
    envelope = np.abs(iq_samples)
    audio = envelope - np.mean(envelope)
    
    # Bandpass filter around tick frequencies
    # WWV: 1000 Hz, WWVH: 1200 Hz, bandwidth ~100 Hz
    nyq = sample_rate / 2
    
    wwv_ticks = []
    wwvh_ticks = []
    
    # Analyze each second
    for sec in range(60):
        # Skip second 0 (marker tone, not tick)
        if sec == 0:
            continue
            
        # Expected tick at start of each second, duration 5ms
        expected_sample = sec * sample_rate
        window_start = max(0, expected_sample - int(0.01 * sample_rate))  # 10ms before
        window_end = min(len(audio), expected_sample + int(0.02 * sample_rate))  # 20ms after
        
        if window_end > len(audio):
            break
            
        segment = audio[window_start:window_end]
        
        # FFT to find tick power
        fft_result = np.abs(rfft(segment))
        freqs = rfftfreq(len(segment), 1/sample_rate)
        
        # WWV 1000 Hz
        wwv_idx = np.argmin(np.abs(freqs - 1000))
        wwv_power = np.max(fft_result[max(0, wwv_idx-2):wwv_idx+3])
        
        # WWVH 1200 Hz
        wwvh_idx = np.argmin(np.abs(freqs - 1200))
        wwvh_power = np.max(fft_result[max(0, wwvh_idx-2):wwvh_idx+3])
        
        # Find actual peak position within window
        # Use correlation with 5ms tone template
        tick_duration_samples = int(0.005 * sample_rate)  # 5ms
        
        wwv_ticks.append({
            'second': sec,
            'expected_sample': expected_sample,
            'power_db': 20 * np.log10(wwv_power + 1e-12),
        })
        
        wwvh_ticks.append({
            'second': sec,
            'expected_sample': expected_sample,
            'power_db': 20 * np.log10(wwvh_power + 1e-12),
        })
    
    return {
        'wwv_ticks': wwv_ticks,
        'wwvh_ticks': wwvh_ticks,
    }


def decode_bcd_minute(iq_samples: np.ndarray, sample_rate: int) -> dict:
    """
    Attempt to decode the BCD minute from 100 Hz subcarrier.
    
    BCD time code structure (simplified):
    - Bits are transmitted at 1-second intervals
    - '1' bit: 500ms of 100 Hz tone
    - '0' bit: 200ms of 100 Hz tone
    - Position marker: 800ms of 100 Hz tone
    
    Minute encoding (BCD format):
    - Seconds 1-4: Units of minutes (1, 2, 4, 8)
    - Seconds 5-8: Tens of minutes (10, 20, 40, -)
    """
    # AM demodulate
    envelope = np.abs(iq_samples)
    audio = envelope - np.mean(envelope)
    
    # Bandpass filter around 100 Hz
    nyq = sample_rate / 2
    b, a = signal.butter(4, [80/nyq, 120/nyq], btype='band')
    bcd_signal = signal.filtfilt(b, a, audio)
    
    # Rectify and smooth to get envelope of 100 Hz
    bcd_envelope = np.abs(bcd_signal)
    smooth_samples = int(0.05 * sample_rate)  # 50ms smoothing
    bcd_smooth = np.convolve(bcd_envelope, np.ones(smooth_samples)/smooth_samples, mode='same')
    
    # Detect bit durations at each second
    bits = []
    for sec in range(1, 10):  # Focus on seconds 1-9 for minute encoding
        start = int((sec - 0.1) * sample_rate)
        end = int((sec + 0.9) * sample_rate)
        
        if end > len(bcd_smooth):
            break
        
        segment = bcd_smooth[start:end]
        
        # Find duration of elevated signal
        threshold = np.median(segment) + np.std(segment)
        above_threshold = segment > threshold
        
        # Count samples above threshold
        duration_samples = np.sum(above_threshold)
        duration_ms = (duration_samples / sample_rate) * 1000
        
        # Classify bit
        if duration_ms > 600:
            bit_type = 'P'  # Position marker
        elif duration_ms > 350:
            bit_type = '1'
        else:
            bit_type = '0'
        
        bits.append({
            'second': sec,
            'duration_ms': duration_ms,
            'bit_type': bit_type
        })
    
    # Decode minute from bits 1-4 (units) and 5-8 (tens)
    # Bit weights: sec1=1, sec2=2, sec3=4, sec4=8, sec5=10, sec6=20, sec7=40
    decoded_minute = None
    if len(bits) >= 8:
        try:
            units = 0
            tens = 0
            
            for b in bits[:4]:
                if b['bit_type'] == '1':
                    if b['second'] == 1: units += 1
                    elif b['second'] == 2: units += 2
                    elif b['second'] == 3: units += 4
                    elif b['second'] == 4: units += 8
            
            for b in bits[4:8]:
                if b['bit_type'] == '1':
                    if b['second'] == 5: tens += 10
                    elif b['second'] == 6: tens += 20
                    elif b['second'] == 7: tens += 40
            
            decoded_minute = tens + units
        except:
            pass
    
    return {
        'bits': bits,
        'decoded_minute': decoded_minute,
    }


def measure_test_signal_toa(iq_samples: np.ndarray, sample_rate: int) -> dict:
    """
    Measure test signal Time-of-Arrival offset from expected 13.0s start.
    """
    from grape_recorder.wwv_test_signal import WWVTestSignalDetector
    
    detector = WWVTestSignalDetector(sample_rate=sample_rate)
    
    # Get detection with timing
    result = detector.detect(iq_samples, 8, sample_rate)  # Use minute 8 as reference
    
    return {
        'detected': result.detected,
        'signal_start_time': result.signal_start_time,
        'toa_offset_ms': result.toa_offset_ms,
        'confidence': result.confidence,
    }


def station_agreement_check(archive: dict, channel_name: str = 'WWV_10_MHz') -> dict:
    """
    Check if POWER and TIMING methods agree on station identity.
    
    Two independent discrimination approaches:
    1. POWER-BASED: Which station's audio is stronger (FFT power ratio)
    2. TIMING-BASED: Which station arrives first (differential delay)
    
    For Kansas (EM38ww):
    - WWV (Colorado) is ~2400 km away
    - WWVH (Hawaii) is ~5600 km away
    - Expected: WWV arrives ~8-30 ms before WWVH
    
    When both methods agree, we have high confidence.
    When they disagree, investigate (fading, multipath, etc.)
    """
    from grape_recorder.grape.wwvh_discrimination import WWVHDiscriminator
    
    iq = archive['iq']
    sr = int(archive['sample_rate'])
    unix_ts = float(archive['unix_timestamp'])
    
    # Extract frequency from channel name
    import re
    freq_match = re.search(r'(\d+(?:\.\d+)?)[_\s]*MHz', channel_name, re.IGNORECASE)
    freq_mhz = float(freq_match.group(1)) if freq_match else 10.0
    
    disc = WWVHDiscriminator(channel_name, receiver_grid='EM38ww')
    result = disc.analyze_minute_with_440hz(iq, sr, unix_ts, frequency_mhz=freq_mhz)
    
    if result is None:
        return {'error': 'Discrimination failed'}
    
    methods = {}
    
    # ===== POWER-BASED METHODS =====
    
    # Method 1: Timing tone power (FFT-based)
    if result.power_ratio_db is not None:
        if result.power_ratio_db > 3:
            methods['power_fft'] = {'station': 'WWV', 'value': f'{result.power_ratio_db:+.1f} dB'}
        elif result.power_ratio_db < -3:
            methods['power_fft'] = {'station': 'WWVH', 'value': f'{result.power_ratio_db:+.1f} dB'}
        else:
            methods['power_fft'] = {'station': 'BALANCED', 'value': f'{result.power_ratio_db:+.1f} dB'}
    
    # Method 2: Tick window power (absolute power ratio)
    if result.tick_windows_10sec:
        tw = result.tick_windows_10sec[0]
        power_ratio = tw.get('power_ratio_db', 0)
        if power_ratio > 3:
            methods['power_ticks'] = {'station': 'WWV', 'value': f'{power_ratio:+.1f} dB'}
        elif power_ratio < -3:
            methods['power_ticks'] = {'station': 'WWVH', 'value': f'{power_ratio:+.1f} dB'}
        else:
            methods['power_ticks'] = {'station': 'BALANCED', 'value': f'{power_ratio:+.1f} dB'}
    
    # ===== TIMING-BASED METHODS =====
    
    # Method 3: BCD differential delay
    # Positive delay = WWVH arrives AFTER WWV (correct for Kansas)
    if result.bcd_differential_delay_ms is not None:
        delay = result.bcd_differential_delay_ms
        if delay > 5:
            methods['timing_bcd'] = {'station': 'WWV_FIRST', 'value': f'{delay:+.1f} ms'}
        elif delay < -5:
            methods['timing_bcd'] = {'station': 'WWVH_FIRST', 'value': f'{delay:+.1f} ms'}
        else:
            methods['timing_bcd'] = {'station': 'UNCLEAR', 'value': f'{delay:+.1f} ms'}
    
    # ===== GROUND TRUTH METHODS =====
    
    # Method 4: 440 Hz station ID (ground truth)
    minute = datetime.fromtimestamp(unix_ts, tz=timezone.utc).minute
    if minute == 1 and result.tone_440hz_wwvh_detected:
        methods['440hz_id'] = {'station': 'WWVH', 'value': 'Minute 1 detection'}
    elif minute == 2 and result.tone_440hz_wwv_detected:
        methods['440hz_id'] = {'station': 'WWV', 'value': 'Minute 2 detection'}
    
    # Method 5: Test signal (ground truth)
    if result.test_signal_detected:
        methods['test_signal'] = {'station': result.test_signal_station, 
                                   'value': f'Minute {minute} detection'}
    
    # ===== CROSS-VALIDATION =====
    
    # Check power vs timing agreement
    power_station = methods.get('power_fft', {}).get('station', 'UNKNOWN')
    timing_result = methods.get('timing_bcd', {}).get('station', 'UNKNOWN')
    
    # Convert timing result to station identity
    # If WWV arrives first and we hear WWVH louder, they AGREE
    # (WWVH is the distant station, arriving later, but heard louder due to propagation)
    if timing_result == 'WWV_FIRST':
        timing_confirms = 'WWVH_DISTANT'  # The louder station should be WWVH
    elif timing_result == 'WWVH_FIRST':
        timing_confirms = 'WWV_DISTANT'  # The louder station should be WWV
    else:
        timing_confirms = 'UNCLEAR'
    
    # Agreement check
    if power_station == 'WWVH' and timing_confirms == 'WWVH_DISTANT':
        agreement = 'AGREE_WWVH'
        confidence_boost = True
    elif power_station == 'WWV' and timing_confirms == 'WWV_DISTANT':
        agreement = 'AGREE_WWV'
        confidence_boost = True
    elif power_station == 'BALANCED':
        agreement = 'POWER_BALANCED'
        confidence_boost = False
    elif timing_confirms == 'UNCLEAR':
        agreement = 'TIMING_UNCLEAR'
        confidence_boost = False
    else:
        agreement = 'DISAGREE'
        confidence_boost = False
    
    return {
        'methods': methods,
        'power_station': power_station,
        'timing_result': timing_result,
        'agreement': agreement,
        'confidence_boost': confidence_boost,
        'final': result.dominant_station,
        'original_confidence': result.confidence,
    }


def validate_inter_method(archive_path: Path) -> dict:
    """
    Run inter-method validation on an archive.
    
    Cross-validates:
    1. Station agreement across all methods
    2. Timing consistency (RTP, ticks, test signal ToA)
    3. Minute consistency (schedule vs detection)
    """
    archive = load_archive(archive_path)
    
    iq = archive['iq']
    sr = int(archive['sample_rate'])
    
    # Expected minute from filename/timestamp
    unix_ts = float(archive['unix_timestamp'])
    expected_dt = datetime.fromtimestamp(unix_ts, tz=timezone.utc)
    expected_minute = expected_dt.minute
    
    # RTP timing info
    rtp_timestamp = int(archive['rtp_timestamp'])
    time_snap_rtp = int(archive.get('time_snap_rtp', 0))
    time_snap_utc = float(archive.get('time_snap_utc', 0))
    
    print(f"{'='*70}")
    print(f"INTER-METHOD VALIDATION: {archive_path.name}")
    print(f"{'='*70}")
    print()
    
    # 1. Expected timing from archive metadata
    print("📍 TIMING REFERENCE (Archive Metadata)")
    print(f"   Expected time: {expected_dt.strftime('%Y-%m-%d %H:%M:%S')} UTC")
    print(f"   Expected minute: {expected_minute}")
    print(f"   RTP timestamp: {rtp_timestamp}")
    print(f"   Time snap RTP: {time_snap_rtp}")
    print(f"   Time snap UTC: {datetime.fromtimestamp(time_snap_utc, tz=timezone.utc).strftime('%H:%M:%S') if time_snap_utc else 'N/A'}")
    print()
    
    # 2. BCD decoded minute
    print("📍 BCD TIME CODE DECODING (100 Hz subcarrier)")
    bcd_result = decode_bcd_minute(iq, sr)
    print(f"   Decoded minute: {bcd_result['decoded_minute']}")
    print(f"   Bit sequence (s1-s9):", end=" ")
    for b in bcd_result['bits'][:9]:
        print(f"{b['bit_type']}", end="")
    print()
    
    bcd_match = bcd_result['decoded_minute'] == expected_minute
    print(f"   Match with expected: {'✅ YES' if bcd_match else '❌ NO'}")
    print()
    
    # 3. Per-second tick consistency
    print("📍 TICK TIMING CONSISTENCY (per-second)")
    tick_result = extract_tick_timing(iq, sr)
    
    wwv_powers = [t['power_db'] for t in tick_result['wwv_ticks']]
    wwvh_powers = [t['power_db'] for t in tick_result['wwvh_ticks']]
    
    print(f"   WWV 1000 Hz ticks:  mean={np.mean(wwv_powers):.1f} dB, std={np.std(wwv_powers):.1f} dB")
    print(f"   WWVH 1200 Hz ticks: mean={np.mean(wwvh_powers):.1f} dB, std={np.std(wwvh_powers):.1f} dB")
    
    # Check for consistent tick power (low std = consistent)
    tick_consistency = np.std(wwv_powers) < 5.0  # Less than 5 dB variation
    print(f"   Tick consistency: {'✅ GOOD' if tick_consistency else '⚠️ VARIABLE'}")
    print()
    
    # 4. Test signal ToA (only for minute 8 or 44)
    print("📍 TEST SIGNAL TIMING (expected at 13.0s)")
    if expected_minute in [8, 44]:
        ts_result = measure_test_signal_toa(iq, sr)
        print(f"   Detected: {ts_result['detected']}")
        if ts_result['detected'] and ts_result['toa_offset_ms'] is not None:
            print(f"   ToA offset: {ts_result['toa_offset_ms']:+.2f} ms from expected 13.0s")
            toa_match = abs(ts_result['toa_offset_ms']) < 50  # Within 50ms
            print(f"   Timing accuracy: {'✅ GOOD' if toa_match else '⚠️ OFF'}")
        else:
            print(f"   ToA offset: N/A (not detected)")
            toa_match = None
    else:
        print(f"   (Minute {expected_minute} - no test signal expected)")
        ts_result = None
        toa_match = None
    print()
    
    # 5. Station agreement: POWER vs TIMING
    print("📍 POWER vs TIMING CROSS-VALIDATION")
    
    # Get channel name from archive path
    channel_name = archive_path.parent.name if archive_path else 'WWV_10_MHz'
    station_check = station_agreement_check(archive, channel_name)
    
    if 'error' not in station_check:
        print(f"   POWER methods:")
        for method, info in station_check['methods'].items():
            if method.startswith('power'):
                print(f"      {method}: {info['station']} ({info['value']})")
        
        print(f"   TIMING methods:")
        for method, info in station_check['methods'].items():
            if method.startswith('timing'):
                print(f"      {method}: {info['station']} ({info['value']})")
        
        print(f"   GROUND TRUTH:")
        for method, info in station_check['methods'].items():
            if method in ['440hz_id', 'test_signal']:
                print(f"      {method}: {info['station']} ({info['value']})")
        
        print()
        print(f"   Power says: {station_check['power_station']}")
        print(f"   Timing says: {station_check['timing_result']}")
        print(f"   Agreement: {station_check['agreement']}")
        
        if station_check['confidence_boost']:
            print(f"   ✅ Power + Timing AGREE → High confidence")
        else:
            print(f"   ⚠️ Methods don't fully agree → Investigate")
    print()
    
    # 6. Cross-method agreement summary
    print("📍 INTER-METHOD AGREEMENT SUMMARY")
    
    agreements = []
    disagreements = []
    
    # Station agreement
    if 'error' not in station_check:
        if station_check['confidence_boost']:
            agreements.append(f"Power + Timing agree: {station_check['final']}")
        elif station_check['agreement'] in ['POWER_BALANCED', 'TIMING_UNCLEAR']:
            agreements.append(f"Inconclusive: {station_check['agreement']}")
        else:
            disagreements.append(f"Power vs Timing disagree")
    
    # Tick consistency
    if tick_consistency:
        agreements.append("Tick power consistent across seconds")
    else:
        disagreements.append("Tick power varies significantly (fading?)")
    
    # Test signal schedule match
    if expected_minute in [8, 44] and ts_result and ts_result['detected']:
        expected_station = 'WWV' if expected_minute == 8 else 'WWVH'
        agreements.append(f"Test signal at minute {expected_minute} matches {expected_station} schedule")
    
    print(f"   ✅ Agreements ({len(agreements)}):")
    for a in agreements:
        print(f"      - {a}")
    
    if disagreements:
        print(f"   ❌ Disagreements ({len(disagreements)}):")
        for d in disagreements:
            print(f"      - {d}")
    
    print()
    
    # Overall confidence score
    total_checks = len(agreements) + len(disagreements)
    if total_checks > 0:
        confidence = len(agreements) / total_checks
        print(f"   Overall inter-method confidence: {confidence:.0%}")
    
    return {
        'expected_minute': expected_minute,
        'station_check': station_check,
        'tick_consistency': tick_consistency,
        'test_signal_toa': ts_result,
        'agreements': agreements,
        'disagreements': disagreements,
    }


def main():
    parser = argparse.ArgumentParser(description='Inter-method validation for WWV/WWVH')
    parser.add_argument('archive', nargs='?', help='Path to NPZ archive')
    parser.add_argument('--channel', default='WWV_2.5_MHz', help='Channel name')
    parser.add_argument('--minute', type=int, help='Specific minute to test')
    args = parser.parse_args()
    
    if args.archive:
        archive_path = Path(args.archive)
    else:
        # Find most recent archive
        archive_dir = Path(f"/tmp/timestd-test/archives/{args.channel}")
        archives = sorted(archive_dir.glob("*.npz"))
        
        if args.minute is not None:
            # Filter to specific minute
            for f in reversed(archives):
                ts_str = f.stem.split('_')[0]
                minute = int(ts_str[11:13])
                if minute == args.minute:
                    archive_path = f
                    break
            else:
                print(f"No archive found for minute {args.minute}")
                return
        else:
            archive_path = archives[-1]  # Most recent
    
    validate_inter_method(archive_path)


if __name__ == '__main__':
    main()

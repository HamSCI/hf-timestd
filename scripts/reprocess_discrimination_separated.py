#!/usr/bin/env python3
"""
Reprocess WWV/WWVH discrimination with SEPARATED CSV outputs per method.

Clean architecture:
- Each method writes to its own CSV in dedicated directory
- Standardized naming: {channel}_{method}_YYYYMMDD.csv
- No time range suffixes or ad-hoc names
- Independent, reprocessable outputs

Usage:
    python3 reprocess_discrimination_separated.py --date 20251119 --channel "WWV 5 MHz"
"""

import argparse
import logging
from pathlib import Path
from datetime import datetime, timezone
import sys

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))

from grape_recorder.grape.wwvh_discrimination import WWVHDiscriminator
from grape_recorder.grape.discrimination_csv_writers import DiscriminationCSVWriters
from grape_recorder.paths import load_paths_from_config
import numpy as np

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def reprocess_day(date_str: str, channel_name: str, data_root: str):
    """
    Reprocess full day of discrimination data with separated outputs.
    
    Creates:
    - tone_detections/{channel}_tones_{date}.csv
    - tick_windows/{channel}_ticks_{date}.csv  
    - station_id_440hz/{channel}_440hz_{date}.csv
    - bcd_discrimination/{channel}_bcd_{date}.csv
    - discrimination/{channel}_discrimination_{date}.csv (weighted voting)
    """
    
    # Setup paths using GRAPEPaths API
    from grape_recorder.paths import GRAPEPaths
    paths = GRAPEPaths(data_root)
    archive_dir = paths.get_archive_dir(channel_name)
    analytics_dir = paths.get_analytics_dir(channel_name)
    
    logger.info(f"Processing {channel_name} on {date_str}")
    logger.info(f"Archive dir: {archive_dir}")
    logger.info(f"Analytics dir: {analytics_dir}")
    
    # Initialize CSV writers for separated outputs
    csv_writers = DiscriminationCSVWriters(
        data_root=data_root,
        channel_name=channel_name
    )
    
    # Initialize discriminator
    discriminator = WWVHDiscriminator(channel_name=channel_name)
    
    # Find all NPZ files for this date
    pattern = f"{date_str}T*.npz"
    npz_files = sorted(archive_dir.glob(pattern))
    
    if not npz_files:
        logger.error(f"No NPZ files found matching {pattern}")
        return
    
    logger.info(f"Found {len(npz_files)} files to process")
    
    # Process each minute
    results = []
    tone_records = []
    tick_records = []
    hz440_records = []
    bcd_records = []
    
    for i, npz_file in enumerate(npz_files):
        try:
            # Load NPZ
            data = np.load(npz_file)
            iq_samples = data['iq']
            sample_rate = int(data['sample_rate'])
            unix_timestamp = float(data['unix_timestamp'])
            
            # Skip incomplete files
            if len(iq_samples) < 100000:
                continue
            
            timestamp_utc = datetime.fromtimestamp(unix_timestamp, tz=timezone.utc)
            
            # METHOD 1: Timing Tones (1000/1200 Hz)
            wwv_power, wwvh_power, diff_delay, tone_detections = discriminator.detect_timing_tones(
                iq_samples, sample_rate, unix_timestamp
            )
            
            if tone_detections:
                for det in tone_detections:
                    tone_records.append({
                        'timestamp_utc': datetime.fromtimestamp(det.timestamp_utc, tz=timezone.utc).isoformat(),
                        'station': det.station.value if hasattr(det.station, 'value') else str(det.station),
                        'frequency_hz': det.frequency_hz,
                        'duration_sec': det.duration_sec,
                        'timing_error_ms': det.timing_error_ms,
                        'snr_db': det.snr_db,
                        'tone_power_db': det.tone_power_db if det.tone_power_db is not None else (wwv_power if det.frequency_hz == 1000 else wwvh_power),
                        'confidence': det.confidence
                    })
            
            # METHOD 2: Tick Windows (5ms ticks, 10-sec windows)
            tick_windows = discriminator.detect_tick_windows(iq_samples, sample_rate)
            
            if tick_windows:
                for window in tick_windows:
                    tick_records.append({
                        'timestamp_utc': timestamp_utc.isoformat(),
                        'window_second': window['second'],
                        'coherent_wwv_snr_db': window.get('coherent_wwv_snr_db'),
                        'coherent_wwvh_snr_db': window.get('coherent_wwvh_snr_db'),
                        'incoherent_wwv_snr_db': window.get('incoherent_wwv_snr_db'),
                        'incoherent_wwvh_snr_db': window.get('incoherent_wwvh_snr_db'),
                        'coherence_quality_wwv': window.get('coherence_quality_wwv'),
                        'coherence_quality_wwvh': window.get('coherence_quality_wwvh'),
                        'integration_method': window.get('integration_method'),
                        'wwv_snr_db': window.get('wwv_snr_db'),
                        'wwvh_snr_db': window.get('wwvh_snr_db'),
                        'ratio_db': window.get('ratio_db')
                    })
            
            # METHOD 3: 440 Hz Station ID
            minute_number = timestamp_utc.minute
            
            # 440 Hz detection returns (detected, power_db)
            # Minute 1 = WWVH, Minute 2 = WWV
            detected_440, power_440 = discriminator.detect_440hz_tone(
                iq_samples, sample_rate, minute_number
            )
            
            if detected_440:
                wwv_detected = minute_number == 2
                wwvh_detected = minute_number == 1
                
                hz440_records.append({
                    'timestamp_utc': timestamp_utc.isoformat(),
                    'minute_number': minute_number,
                    'wwv_detected': wwv_detected,
                    'wwvh_detected': wwvh_detected,
                    'wwv_power_db': power_440 if wwv_detected else None,
                    'wwvh_power_db': power_440 if wwvh_detected else None
                })
            
            # METHOD 4: BCD Discrimination (100 Hz subcarrier)
            wwv_amp, wwvh_amp, bcd_delay, bcd_quality, bcd_windows = discriminator.detect_bcd_discrimination(
                iq_samples, sample_rate, unix_timestamp
            )
            
            if bcd_windows:
                for window in bcd_windows:
                    bcd_records.append({
                        'timestamp_utc': timestamp_utc.isoformat(),
                        'window_start_sec': window.get('window_start', window.get('window_start_sec', 0)),
                        'wwv_amplitude': window.get('wwv_amplitude'),
                        'wwvh_amplitude': window.get('wwvh_amplitude'),
                        'differential_delay_ms': window.get('differential_delay_ms', window.get('differential_delay', None)),
                        'correlation_quality': window.get('correlation_quality'),
                        'amplitude_ratio_db': 20 * np.log10(window.get('wwv_amplitude') / window.get('wwvh_amplitude')) 
                                             if window.get('wwv_amplitude') and window.get('wwvh_amplitude') and window.get('wwvh_amplitude') > 0 
                                             else None
                    })
            
            # METHOD 5: Weighted Voting (final discrimination)
            result = discriminator.analyze_minute_with_440hz(
                iq_samples=iq_samples,
                sample_rate=sample_rate,
                minute_timestamp=unix_timestamp
            )
            
            if result:
                results.append(result)
            
            if (i + 1) % 100 == 0:
                logger.info(f"Processed {i + 1}/{len(npz_files)} files...")
                
        except Exception as e:
            logger.warning(f"Failed to process {npz_file.name}: {e}")
    
    logger.info(f"Processed {len(results)} minutes successfully")
    
    # Write separated CSV outputs
    date_obj = datetime.strptime(date_str, '%Y%m%d').date()
    
    logger.info(f"Writing tone detections: {len(tone_records)} records")
    csv_writers.append_tone_detections(tone_records, date_obj)
    
    logger.info(f"Writing tick windows: {len(tick_records)} records")
    csv_writers.append_tick_windows(tick_records, date_obj)
    
    logger.info(f"Writing 440 Hz detections: {len(hz440_records)} records")
    csv_writers.append_440hz_detections(hz440_records, date_obj)
    
    logger.info(f"Writing BCD windows: {len(bcd_records)} records")
    csv_writers.append_bcd_windows(bcd_records, date_obj)
    
    logger.info(f"Writing weighted voting results: {len(results)} records")
    csv_writers.append_discrimination_results(results, date_obj)
    
    logger.info("✅ All methods written to separated CSV files")
    logger.info(f"   Tone detections: analytics/{channel_dir}/tone_detections/")
    logger.info(f"   Tick windows: analytics/{channel_dir}/tick_windows/")
    logger.info(f"   440 Hz ID: analytics/{channel_dir}/station_id_440hz/")
    logger.info(f"   BCD windows: analytics/{channel_dir}/bcd_discrimination/")
    logger.info(f"   Final discrimination: analytics/{channel_dir}/discrimination/")


def main():
    parser = argparse.ArgumentParser(description='Reprocess discrimination with separated CSV outputs')
    parser.add_argument('--date', required=True, help='Date in YYYYMMDD format')
    parser.add_argument('--channel', required=True, help='Channel name (e.g., "WWV 5 MHz")')
    parser.add_argument('--data-root', default=None, help='Data root directory (default: from config based on mode)')
    parser.add_argument('--config', default=None, help='Path to timestd-config.toml (default: ../config/timestd-config.toml)')
    
    args = parser.parse_args()
    
    # Get data_root from config if not provided
    if args.data_root:
        data_root = args.data_root
    else:
        # Load from config based on test/production mode
        config_path = args.config or str(Path(__file__).parent.parent / 'config' / 'timestd-config.toml')
        paths = load_paths_from_config(config_path)
        data_root = str(paths.data_root)
        logger.info(f"Using data_root from config: {data_root}")
    
    reprocess_day(args.date, args.channel, data_root)


if __name__ == '__main__':
    main()

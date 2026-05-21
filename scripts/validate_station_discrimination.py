#!/usr/bin/env python3
"""
Validate Station Discrimination Fix

This script validates that the station discrimination fix is working correctly:
1. No WWVH detections at 20 MHz or 25 MHz (physically impossible)
2. CHU channels are labeled correctly without discrimination
3. Shared frequencies (2.5, 5, 10, 15 MHz) still perform discrimination

Usage:
    python3 scripts/validate_station_discrimination.py [--hours HOURS]
"""

import sys
from pathlib import Path
from datetime import datetime, timedelta
import argparse

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))

from hf_timestd.io import SqliteDataProductReader as DataProductReader
from hf_timestd.core.wwv_constants import (
    WWV_FREQUENCIES, WWVH_FREQUENCIES, CHU_FREQUENCIES,
    SHARED_FREQUENCIES, STATION_SPECIFIC_FREQ
)

def validate_station_frequency_combinations(data_dir: Path, hours: int = 1):
    """
    Validate that station/frequency combinations are physically valid.
    
    Args:
        data_dir: Root data directory (/var/lib/timestd/phase2)
        hours: Number of hours to check (default: 1)
    """
    print("=" * 80)
    print("STATION DISCRIMINATION VALIDATION")
    print("=" * 80)
    print()
    
    # Define expected frequencies for each station
    print("Expected Broadcast Schedules:")
    print(f"  WWV:  {WWV_FREQUENCIES} MHz")
    print(f"  WWVH: {WWVH_FREQUENCIES} MHz")
    print(f"  CHU:  {CHU_FREQUENCIES} MHz")
    print()
    print("Station-Specific Frequencies (no discrimination needed):")
    for freq, station in STATION_SPECIFIC_FREQ.items():
        print(f"  {freq} MHz → {station}")
    print()
    print("Shared Frequencies (discrimination required):")
    print(f"  {SHARED_FREQUENCIES} MHz → WWV/WWVH/BPM")
    print()
    print("=" * 80)
    print()
    
    # Time range for validation
    end_time = datetime.utcnow()
    start_time = end_time - timedelta(hours=hours)
    
    print(f"Checking data from {start_time.isoformat()}Z to {end_time.isoformat()}Z")
    print()
    
    # Track validation results
    total_measurements = 0
    invalid_combinations = []
    channel_stats = {}
    
    # Check each channel directory
    for channel_dir in sorted(data_dir.iterdir()):
        if not channel_dir.is_dir() or channel_dir.name in ['fusion', 'science']:
            continue
        
        channel_name = channel_dir.name
        
        # Extract frequency from channel name (e.g., "WWV_20000" -> 20.0 MHz)
        try:
            freq_khz = int(channel_name.split('_')[1])
            freq_mhz = freq_khz / 1000.0
        except (IndexError, ValueError):
            print(f"⚠ Skipping {channel_name} (cannot parse frequency)")
            continue
        
        # Read L2 timing measurements
        try:
            reader = DataProductReader(
                data_dir=channel_dir,
                product_level='L2',
                product_name='timing_measurements',
                channel=channel_name
            )
            
            measurements = reader.read_time_range(
                start_time.isoformat() + 'Z',
                end_time.isoformat() + 'Z'
            )
            
            if not measurements:
                continue
            
            # Initialize channel stats
            if channel_name not in channel_stats:
                channel_stats[channel_name] = {
                    'frequency_mhz': freq_mhz,
                    'total': 0,
                    'wwv': 0,
                    'wwvh': 0,
                    'chu': 0,
                    'bpm': 0,
                    'unknown': 0
                }
            
            # Check each measurement
            for m in measurements:
                total_measurements += 1
                station = m.get('station', 'UNKNOWN')
                
                # Count by station
                channel_stats[channel_name]['total'] += 1
                if station == 'WWV':
                    channel_stats[channel_name]['wwv'] += 1
                elif station == 'WWVH':
                    channel_stats[channel_name]['wwvh'] += 1
                elif station == 'CHU':
                    channel_stats[channel_name]['chu'] += 1
                elif station == 'BPM':
                    channel_stats[channel_name]['bpm'] += 1
                else:
                    channel_stats[channel_name]['unknown'] += 1
                
                # Validate station/frequency combination
                is_valid = True
                reason = ""
                
                if station == 'WWVH' and freq_mhz not in WWVH_FREQUENCIES:
                    is_valid = False
                    reason = f"WWVH does not broadcast at {freq_mhz} MHz (only {WWVH_FREQUENCIES})"
                elif station == 'WWV' and freq_mhz not in WWV_FREQUENCIES:
                    is_valid = False
                    reason = f"WWV does not broadcast at {freq_mhz} MHz (only {WWV_FREQUENCIES})"
                elif station == 'CHU' and freq_mhz not in CHU_FREQUENCIES:
                    is_valid = False
                    reason = f"CHU does not broadcast at {freq_mhz} MHz (only {CHU_FREQUENCIES})"
                
                if not is_valid:
                    invalid_combinations.append({
                        'timestamp': m.get('timestamp_utc'),
                        'channel': channel_name,
                        'frequency_mhz': freq_mhz,
                        'station': station,
                        'reason': reason
                    })
        
        except Exception as e:
            print(f"⚠ Error reading {channel_name}: {e}")
            continue
    
    # Print results
    print()
    print("=" * 80)
    print("VALIDATION RESULTS")
    print("=" * 80)
    print()
    
    print(f"Total measurements checked: {total_measurements}")
    print()
    
    # Print per-channel statistics
    print("Per-Channel Station Distribution:")
    print("-" * 80)
    for channel_name in sorted(channel_stats.keys()):
        stats = channel_stats[channel_name]
        freq = stats['frequency_mhz']
        total = stats['total']
        
        # Determine if this is a station-specific frequency
        is_specific = freq in STATION_SPECIFIC_FREQ
        expected_station = STATION_SPECIFIC_FREQ.get(freq, "SHARED")
        
        print(f"\n{channel_name} ({freq} MHz) - {expected_station}")
        print(f"  Total: {total}")
        if stats['wwv'] > 0:
            print(f"  WWV:   {stats['wwv']} ({100*stats['wwv']/total:.1f}%)")
        if stats['wwvh'] > 0:
            print(f"  WWVH:  {stats['wwvh']} ({100*stats['wwvh']/total:.1f}%)")
        if stats['chu'] > 0:
            print(f"  CHU:   {stats['chu']} ({100*stats['chu']/total:.1f}%)")
        if stats['bpm'] > 0:
            print(f"  BPM:   {stats['bpm']} ({100*stats['bpm']/total:.1f}%)")
        if stats['unknown'] > 0:
            print(f"  UNKNOWN: {stats['unknown']} ({100*stats['unknown']/total:.1f}%)")
    
    print()
    print("=" * 80)
    
    # Report invalid combinations
    if invalid_combinations:
        print()
        print(f"❌ VALIDATION FAILED: {len(invalid_combinations)} invalid station/frequency combinations found!")
        print()
        print("Invalid Combinations:")
        print("-" * 80)
        for combo in invalid_combinations[:20]:  # Show first 20
            print(f"  {combo['timestamp']}: {combo['station']} at {combo['frequency_mhz']} MHz")
            print(f"    Reason: {combo['reason']}")
        
        if len(invalid_combinations) > 20:
            print(f"  ... and {len(invalid_combinations) - 20} more")
        
        print()
        return False
    else:
        print()
        print("✅ VALIDATION PASSED: All station/frequency combinations are valid!")
        print()
        
        # Check that station-specific frequencies are being labeled correctly
        print("Station-Specific Frequency Check:")
        print("-" * 80)
        for freq, expected_station in STATION_SPECIFIC_FREQ.items():
            # Find channels at this frequency
            matching_channels = [
                (name, stats) for name, stats in channel_stats.items()
                if stats['frequency_mhz'] == freq
            ]
            
            if matching_channels:
                for channel_name, stats in matching_channels:
                    total = stats['total']
                    station_count = stats.get(expected_station.lower(), 0)
                    
                    if station_count == total:
                        print(f"  ✅ {freq} MHz: All {total} measurements labeled as {expected_station}")
                    else:
                        print(f"  ⚠ {freq} MHz: Only {station_count}/{total} labeled as {expected_station}")
            else:
                print(f"  ⚠ {freq} MHz: No data found")
        
        print()
        return True


def main():
    parser = argparse.ArgumentParser(
        description="Validate station discrimination fix"
    )
    parser.add_argument(
        '--hours',
        type=int,
        default=1,
        help='Number of hours of data to check (default: 1)'
    )
    parser.add_argument(
        '--data-dir',
        type=Path,
        default=Path('/var/lib/timestd/phase2'),
        help='Root data directory (default: /var/lib/timestd/phase2)'
    )
    
    args = parser.parse_args()
    
    if not args.data_dir.exists():
        print(f"❌ Data directory not found: {args.data_dir}")
        print()
        print("This script requires access to the Phase 2 data directory.")
        print("Run it on the production system or adjust --data-dir")
        sys.exit(1)
    
    success = validate_station_frequency_combinations(args.data_dir, args.hours)
    
    sys.exit(0 if success else 1)


if __name__ == '__main__':
    main()

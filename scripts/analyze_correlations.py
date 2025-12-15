#!/usr/bin/env python3
"""
Analyze correlations in GRAPE signal data to identify scientifically interesting patterns.

Correlations analyzed:
1. SNR vs carrier frequency deviation (propagation multipath effects)
2. Time of day vs tone onset timing differences (ionospheric stability)
3. Gaps vs SNR (receiver saturation or weak signal dropout)
4. Gaps vs time of day (diurnal propagation patterns)
5. WWV/WWVH discrimination confidence vs SNR (detection reliability)

Usage:
    python3 scripts/analyze_correlations.py --date 20251116 --channel "WWV 10 MHz" --data-root /tmp/timestd-test
    python3 scripts/analyze_correlations.py --date 20251116 --channel "WWV 10 MHz" --data-root /tmp/timestd-test --export correlations.json
"""

import argparse
import json
import numpy as np
import csv
from pathlib import Path
from datetime import datetime, timezone
from collections import defaultdict
import sys

sys.path.insert(0, str(Path(__file__).parent.parent))
from src.grape_recorder.paths import get_paths


def load_npz_data(paths, channel_name: str, date_str: str):
    """Load NPZ files for gap and timing analysis."""
    npz_dir = paths.get_archive_dir(channel_name)
    pattern = f"{date_str}T*Z_*_iq.npz"
    npz_files = sorted(npz_dir.glob(pattern))
    
    data = []
    for npz_path in npz_files:
        try:
            npz = np.load(npz_path)
            # Extract hour from filename: 20251115T023400Z
            hour = int(npz_path.name[9:11])
            minute = int(npz_path.name[11:13])
            
            info = {
                'timestamp': f"{date_str}T{npz_path.name[9:15]}Z",
                'hour': hour,
                'minute': minute,
                'gaps_count': int(npz.get('gaps_count', 0)),
                'gaps_filled': int(npz.get('gaps_filled', 0)),
                'packets_received': int(npz.get('packets_received', 0)),
                'packets_expected': int(npz.get('packets_expected', 0)),
            }
            
            # Calculate completeness percentage
            if info['packets_expected'] > 0:
                info['completeness_pct'] = (info['packets_received'] / info['packets_expected']) * 100
            else:
                info['completeness_pct'] = 100.0 if info['gaps_count'] == 0 else 0.0
                
            data.append(info)
        except Exception as e:
            continue
    
    return data


def load_discrimination_data(paths, channel_name: str, date_str: str):
    """Load discrimination CSV for SNR and timing data."""
    # Convert date format: 20251116 -> WWV_10_MHz_discrimination_20251116.csv
    csv_dir = paths.get_discrimination_dir(channel_name)
    csv_pattern = f"*_discrimination_{date_str}.csv"
    csv_files = list(csv_dir.glob(csv_pattern))
    
    if not csv_files:
        return []
    
    data = []
    with open(csv_files[0], 'r') as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                # Parse timestamp to get hour
                ts = datetime.fromisoformat(row['timestamp_utc'].replace('Z', '+00:00'))
                
                info = {
                    'timestamp': row['timestamp_utc'],
                    'hour': ts.hour,
                    'minute': ts.minute,
                    'wwv_detected': int(row['wwv_detected']),
                    'wwvh_detected': int(row['wwvh_detected']),
                    'wwv_snr_db': float(row['wwv_snr_db']) if row['wwv_snr_db'] else None,
                    'wwvh_snr_db': float(row['wwvh_snr_db']) if row['wwvh_snr_db'] else None,
                    'power_ratio_db': float(row['power_ratio_db']) if row['power_ratio_db'] else None,
                    'dominant_station': row['dominant_station'],
                    'confidence': row['confidence']
                }
                
                # Calculate max SNR (best signal detected)
                snrs = [s for s in [info['wwv_snr_db'], info['wwvh_snr_db']] if s is not None and s > 0]
                info['max_snr_db'] = max(snrs) if snrs else None
                
                data.append(info)
            except Exception as e:
                continue
    
    return data


def analyze_snr_vs_gaps(npz_data, discrimination_data):
    """Correlation 1: Gaps vs SNR - Does weak signal cause more gaps?"""
    print("\n" + "="*70)
    print("CORRELATION: SNR vs Data Completeness")
    print("="*70)
    print("\nHypothesis: Weak signals (low SNR) may correlate with packet loss/gaps")
    
    # Match discrimination timestamps to NPZ data by minute
    matched_data = []
    
    for disc in discrimination_data:
        if disc['max_snr_db'] is None:
            continue
            
        # Find matching NPZ by hour:minute
        matching_npz = [n for n in npz_data 
                       if n['hour'] == disc['hour'] and n['minute'] == disc['minute']]
        
        if matching_npz:
            matched_data.append({
                'snr_db': disc['max_snr_db'],
                'completeness_pct': matching_npz[0]['completeness_pct'],
                'gaps_count': matching_npz[0]['gaps_count']
            })
    
    if len(matched_data) < 10:
        print("⚠️  Insufficient data for correlation (need ≥10 points)")
        return None
    
    # Bin SNR into ranges
    snr_bins = {
        'Very Strong (>30 dB)': [],
        'Strong (20-30 dB)': [],
        'Moderate (10-20 dB)': [],
        'Weak (<10 dB)': []
    }
    
    for item in matched_data:
        snr = item['snr_db']
        comp = item['completeness_pct']
        
        if snr > 30:
            snr_bins['Very Strong (>30 dB)'].append(comp)
        elif snr > 20:
            snr_bins['Strong (20-30 dB)'].append(comp)
        elif snr > 10:
            snr_bins['Moderate (10-20 dB)'].append(comp)
        else:
            snr_bins['Weak (<10 dB)'].append(comp)
    
    print(f"\n📊 Analysis of {len(matched_data)} matched minutes:\n")
    print(f"   SNR Range          │  N  │ Avg Completeness │ Interpretation")
    print(f"   ───────────────────┼─────┼──────────────────┼────────────────")
    
    results = {}
    for bin_name, completeness_values in snr_bins.items():
        if completeness_values:
            avg_comp = np.mean(completeness_values)
            count = len(completeness_values)
            
            if avg_comp >= 95:
                interp = "✅ Good"
            elif avg_comp >= 90:
                interp = "⚠️  Fair"
            else:
                interp = "❌ Poor"
            
            print(f"   {bin_name:18s} │ {count:3d} │ {avg_comp:6.1f}%         │ {interp}")
            results[bin_name] = {
                'count': count,
                'avg_completeness': avg_comp,
                'values': completeness_values
            }
    
    # Calculate correlation coefficient
    snrs = [d['snr_db'] for d in matched_data]
    comps = [d['completeness_pct'] for d in matched_data]
    
    if len(snrs) > 2:
        corr_coef = np.corrcoef(snrs, comps)[0, 1]
        print(f"\n   Correlation coefficient: {corr_coef:+.3f}")
        
        if abs(corr_coef) > 0.5:
            print(f"   → {'Strong' if abs(corr_coef) > 0.7 else 'Moderate'} "
                  f"{'positive' if corr_coef > 0 else 'negative'} correlation")
        else:
            print(f"   → Weak correlation (SNR doesn't strongly predict completeness)")
    
    return results


def analyze_time_of_day_patterns(npz_data, discrimination_data):
    """Correlation 2 & 4: Time of day vs gaps and SNR - Diurnal patterns."""
    print("\n" + "="*70)
    print("CORRELATION: Time of Day vs Signal Quality")
    print("="*70)
    print("\nHypothesis: Ionospheric conditions vary by time of day (diurnal pattern)")
    
    # Group by hour
    hourly_gaps = defaultdict(list)
    hourly_snr = defaultdict(list)
    
    for npz in npz_data:
        hourly_gaps[npz['hour']].append(npz['completeness_pct'])
    
    for disc in discrimination_data:
        if disc['max_snr_db'] is not None and disc['max_snr_db'] > 0:
            hourly_snr[disc['hour']].append(disc['max_snr_db'])
    
    print(f"\n📊 Hourly Patterns:\n")
    print(f"   Hour │ Completeness │  SNR (dB)  │ Pattern")
    print(f"   ─────┼──────────────┼────────────┼────────────────")
    
    results = {}
    for hour in range(24):
        gap_data = hourly_gaps.get(hour, [])
        snr_data = hourly_snr.get(hour, [])
        
        if gap_data:
            avg_comp = np.mean(gap_data)
            comp_str = f"{avg_comp:5.1f}%"
            
            # Visual bar
            bar_len = int(avg_comp / 5)
            if avg_comp >= 95:
                bar = "▓" * min(bar_len, 20)
            elif avg_comp >= 90:
                bar = "▒" * min(bar_len, 20)
            else:
                bar = "░" * min(bar_len, 20)
        else:
            comp_str = "  ---  "
            bar = "(no data)"
        
        if snr_data:
            avg_snr = np.mean(snr_data)
            snr_str = f"{avg_snr:5.1f} dB"
        else:
            avg_snr = None
            snr_str = "  ---   "
        
        print(f"   {hour:02d}:xx│ {comp_str:12s} │ {snr_str:10s} │ {bar}")
        
        results[hour] = {
            'avg_completeness': np.mean(gap_data) if gap_data else None,
            'avg_snr': avg_snr,
            'completeness_samples': len(gap_data),
            'snr_samples': len(snr_data)
        }
    
    # Identify patterns
    print(f"\n   ▓ >95%   ▒ 90-95%   ░ <90%")
    
    # Find best/worst hours
    valid_hours = [(h, d['avg_completeness']) for h, d in results.items() 
                   if d['avg_completeness'] is not None]
    
    if valid_hours:
        best_hours = sorted(valid_hours, key=lambda x: x[1], reverse=True)[:3]
        worst_hours = sorted(valid_hours, key=lambda x: x[1])[:3]
        
        print(f"\n🌟 Best hours (completeness):")
        for hour, comp in best_hours:
            print(f"   {hour:02d}:00 UTC → {comp:.1f}%")
        
        print(f"\n⚠️  Worst hours (completeness):")
        for hour, comp in worst_hours:
            print(f"   {hour:02d}:00 UTC → {comp:.1f}%")
    
    return results


def analyze_confidence_vs_snr(discrimination_data):
    """Correlation 5: Discrimination confidence vs SNR."""
    print("\n" + "="*70)
    print("CORRELATION: Discrimination Confidence vs SNR")
    print("="*70)
    print("\nHypothesis: Higher SNR should correlate with higher confidence ratings")
    
    confidence_snr = defaultdict(list)
    
    for disc in discrimination_data:
        if disc['max_snr_db'] is not None and disc['max_snr_db'] > 0:
            confidence_snr[disc['confidence']].append(disc['max_snr_db'])
    
    print(f"\n📊 Confidence vs SNR Distribution:\n")
    print(f"   Confidence │  N  │ Avg SNR │ Min SNR │ Max SNR │ Assessment")
    print(f"   ───────────┼─────┼─────────┼─────────┼─────────┼────────────")
    
    results = {}
    for conf_level in ['high', 'medium', 'low']:
        snr_values = confidence_snr.get(conf_level, [])
        
        if snr_values:
            count = len(snr_values)
            avg_snr = np.mean(snr_values)
            min_snr = np.min(snr_values)
            max_snr = np.max(snr_values)
            
            # Assess if SNR matches confidence expectation
            if conf_level == 'high' and avg_snr > 20:
                assessment = "✅ Expected"
            elif conf_level == 'medium' and 10 < avg_snr < 25:
                assessment = "✅ Expected"
            elif conf_level == 'low' and avg_snr < 15:
                assessment = "✅ Expected"
            else:
                assessment = "⚠️  Review"
            
            print(f"   {conf_level:10s} │ {count:3d} │ {avg_snr:5.1f}   │ {min_snr:5.1f}   │ {max_snr:5.1f}   │ {assessment}")
            
            results[conf_level] = {
                'count': count,
                'avg_snr': avg_snr,
                'min_snr': min_snr,
                'max_snr': max_snr,
                'values': snr_values
            }
        else:
            print(f"   {conf_level:10s} │   0 │   ---   │   ---   │   ---   │ No data")
            results[conf_level] = None
    
    return results


def main():
    parser = argparse.ArgumentParser(description='Analyze correlations in GRAPE signal data')
    parser.add_argument('--date', type=str, required=True,
                        help='Date to analyze (YYYYMMDD)')
    parser.add_argument('--data-root', type=Path, required=True,
                        help='Data root directory')
    parser.add_argument('--channel', type=str, required=True,
                        help='Channel name (e.g., "WWV 10 MHz")')
    parser.add_argument('--export', type=Path,
                        help='Export results to JSON file')
    
    args = parser.parse_args()
    
    paths = get_paths(args.data_root)
    
    print("="*70)
    print("GRAPE CORRELATION ANALYSIS")
    print("="*70)
    print(f"Channel: {args.channel}")
    print(f"Date: {args.date}")
    print(f"Data root: {paths.data_root}")
    
    # Load data
    print("\n🔍 Loading data...")
    npz_data = load_npz_data(paths, args.channel, args.date)
    discrimination_data = load_discrimination_data(paths, args.channel, args.date)
    
    print(f"   NPZ files: {len(npz_data)}")
    print(f"   Discrimination records: {len(discrimination_data)}")
    
    if len(npz_data) == 0:
        print("\n❌ No NPZ data found for analysis")
        sys.exit(1)
    
    # Run correlation analyses
    results = {}
    
    if len(discrimination_data) > 0:
        results['snr_vs_gaps'] = analyze_snr_vs_gaps(npz_data, discrimination_data)
        results['time_of_day'] = analyze_time_of_day_patterns(npz_data, discrimination_data)
        results['confidence_vs_snr'] = analyze_confidence_vs_snr(discrimination_data)
    else:
        print("\n⚠️  No discrimination data available - limited correlation analysis")
        results['time_of_day'] = analyze_time_of_day_patterns(npz_data, [])
    
    # Export if requested
    if args.export and results:
        export_data = {
            'channel': args.channel,
            'date': args.date,
            'correlations': results,
            'data_summary': {
                'npz_records': len(npz_data),
                'discrimination_records': len(discrimination_data)
            }
        }
        
        with open(args.export, 'w') as f:
            json.dump(export_data, f, indent=2, default=str)
        
        print(f"\n💾 Exported correlation data to: {args.export}")
    
    print("\n" + "="*70)
    print("✅ Correlation analysis complete")
    print("="*70)
    
    print("\n💡 INTERPRETATION GUIDE:")
    print("   • SNR vs Gaps: Should be weakly correlated (propagation ≠ packet loss)")
    print("   • Time of Day: Strong diurnal patterns = ionospheric effects")
    print("   • Confidence vs SNR: Should be strongly correlated (algorithm validation)")
    print("   • Unexpected correlations: May reveal system issues or interference")


if __name__ == '__main__':
    main()

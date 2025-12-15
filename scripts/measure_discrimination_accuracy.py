#!/usr/bin/env python3
"""
Measure WWV/WWVH Discrimination Accuracy Against Ground Truth

This script evaluates discrimination performance by comparing
system results against known ground truth periods.

Usage:
    python3 scripts/measure_discrimination_accuracy.py \
        --date 20251119 \
        --channel "WWV 10 MHz" \
        --ground-truth data/ground_truth_periods.csv \
        --discrimination-csv /tmp/timestd-test/analytics/WWV_10_MHz/discrimination/ \
        --output baseline_performance.json
"""

import argparse
import json
import csv
import sys
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from collections import defaultdict
import numpy as np

def load_ground_truth(csv_path: str) -> Dict[float, str]:
    """
    Load ground truth data from CSV
    
    CSV Format:
        timestamp, dominant_station, confidence_level, source, notes
    
    Returns:
        Dict mapping minute_timestamp -> dominant_station ('WWV', 'WWVH', 'BALANCED')
    """
    ground_truth = {}
    
    with open(csv_path, 'r') as f:
        reader = csv.DictReader(f)
        for row in reader:
            timestamp = float(row['timestamp'])
            station = row['dominant_station'].strip().upper()
            
            if station not in ['WWV', 'WWVH', 'BALANCED']:
                continue
                
            ground_truth[timestamp] = station
    
    print(f"Loaded {len(ground_truth)} ground truth entries")
    return ground_truth

def load_discrimination_results(csv_path: str, date_str: str) -> Dict[float, Dict]:
    """
    Load discrimination results from CSV file(s)
    
    Returns:
        Dict mapping minute_timestamp -> result dict
    """
    results = {}
    
    # Find discrimination CSV file(s) for the date
    csv_dir = Path(csv_path)
    pattern = f"*{date_str}*.csv"
    
    csv_files = list(csv_dir.glob(pattern))
    
    if not csv_files:
        print(f"ERROR: No discrimination CSV files found matching {pattern} in {csv_dir}")
        return results
    
    print(f"Found {len(csv_files)} discrimination CSV file(s)")
    
    for csv_file in csv_files:
        print(f"Loading: {csv_file}")
        
        with open(csv_file, 'r') as f:
            reader = csv.DictReader(f)
            for row in reader:
                timestamp = float(row['timestamp'])
                
                results[timestamp] = {
                    'dominant_station': row.get('dominant_station', '').strip().upper(),
                    'confidence': row.get('confidence', 'low').strip().lower(),
                    'wwv_detected': row.get('wwv_detected', 'False') == 'True',
                    'wwvh_detected': row.get('wwvh_detected', 'False') == 'True',
                    'power_ratio_db': float(row['power_ratio_db']) if row.get('power_ratio_db') and row['power_ratio_db'] != '' else None,
                    'bcd_wwv_amplitude': float(row['bcd_wwv_amplitude']) if row.get('bcd_wwv_amplitude') and row['bcd_wwv_amplitude'] != '' else None,
                    'bcd_wwvh_amplitude': float(row['bcd_wwvh_amplitude']) if row.get('bcd_wwvh_amplitude') and row['bcd_wwvh_amplitude'] != '' else None,
                    'minute_number': datetime.utcfromtimestamp(timestamp).minute
                }
    
    print(f"Loaded {len(results)} discrimination results")
    return results

def calculate_accuracy(
    ground_truth: Dict[float, str],
    discrimination: Dict[float, Dict]
) -> Dict:
    """
    Calculate accuracy metrics
    
    Returns:
        Dict with detailed accuracy metrics
    """
    # Find overlapping timestamps
    common_timestamps = set(ground_truth.keys()) & set(discrimination.keys())
    
    if not common_timestamps:
        print("ERROR: No overlapping timestamps between ground truth and discrimination results")
        return {}
    
    print(f"Comparing {len(common_timestamps)} common timestamps")
    
    # Overall accuracy
    correct = 0
    total = len(common_timestamps)
    
    # By minute type
    minute_1_2 = {'correct': 0, 'total': 0}  # 440 Hz minutes
    bcd_minutes = {'correct': 0, 'total': 0}  # 0, 8-10, 29-30
    normal_minutes = {'correct': 0, 'total': 0}  # All other
    
    # By confidence level
    by_confidence = {
        'high': {'correct': 0, 'total': 0},
        'medium': {'correct': 0, 'total': 0},
        'low': {'correct': 0, 'total': 0}
    }
    
    # By SNR level (based on power ratio)
    by_snr = {
        'strong': {'correct': 0, 'total': 0},  # |ratio| > 15 dB
        'medium': {'correct': 0, 'total': 0},  # 6-15 dB
        'weak': {'correct': 0, 'total': 0}     # < 6 dB
    }
    
    # Temporal consistency
    prev_timestamp = None
    prev_result = None
    prev_gt = None
    agreements = 0
    comparisons = 0
    
    # Confusion matrix
    confusion = defaultdict(lambda: defaultdict(int))
    
    for timestamp in sorted(common_timestamps):
        gt = ground_truth[timestamp]
        result = discrimination[timestamp]['dominant_station']
        confidence = discrimination[timestamp]['confidence']
        minute_num = discrimination[timestamp]['minute_number']
        power_ratio = discrimination[timestamp]['power_ratio_db']
        
        # Overall accuracy
        if result == gt:
            correct += 1
        
        # Confusion matrix
        confusion[gt][result] += 1
        
        # By minute type
        if minute_num in [1, 2]:
            minute_1_2['total'] += 1
            if result == gt:
                minute_1_2['correct'] += 1
        elif minute_num in [0, 8, 9, 10, 29, 30]:
            bcd_minutes['total'] += 1
            if result == gt:
                bcd_minutes['correct'] += 1
        else:
            normal_minutes['total'] += 1
            if result == gt:
                normal_minutes['correct'] += 1
        
        # By confidence
        if confidence in by_confidence:
            by_confidence[confidence]['total'] += 1
            if result == gt:
                by_confidence[confidence]['correct'] += 1
        
        # By SNR (using power ratio as proxy)
        if power_ratio is not None:
            abs_ratio = abs(power_ratio)
            if abs_ratio > 15:
                by_snr['strong']['total'] += 1
                if result == gt:
                    by_snr['strong']['correct'] += 1
            elif abs_ratio > 6:
                by_snr['medium']['total'] += 1
                if result == gt:
                    by_snr['medium']['correct'] += 1
            else:
                by_snr['weak']['total'] += 1
                if result == gt:
                    by_snr['weak']['correct'] += 1
        
        # Temporal consistency (adjacent minutes)
        if prev_timestamp is not None and timestamp - prev_timestamp == 60:
            comparisons += 1
            if result == prev_result and gt == prev_gt:
                agreements += 1
        
        prev_timestamp = timestamp
        prev_result = result
        prev_gt = gt
    
    # Calculate percentages
    overall_accuracy = (correct / total * 100) if total > 0 else 0
    
    minute_1_2_accuracy = (minute_1_2['correct'] / minute_1_2['total'] * 100) if minute_1_2['total'] > 0 else 0
    bcd_accuracy = (bcd_minutes['correct'] / bcd_minutes['total'] * 100) if bcd_minutes['total'] > 0 else 0
    normal_accuracy = (normal_minutes['correct'] / normal_minutes['total'] * 100) if normal_minutes['total'] > 0 else 0
    
    high_conf_accuracy = (by_confidence['high']['correct'] / by_confidence['high']['total'] * 100) if by_confidence['high']['total'] > 0 else 0
    med_conf_accuracy = (by_confidence['medium']['correct'] / by_confidence['medium']['total'] * 100) if by_confidence['medium']['total'] > 0 else 0
    low_conf_accuracy = (by_confidence['low']['correct'] / by_confidence['low']['total'] * 100) if by_confidence['low']['total'] > 0 else 0
    
    strong_snr_accuracy = (by_snr['strong']['correct'] / by_snr['strong']['total'] * 100) if by_snr['strong']['total'] > 0 else 0
    medium_snr_accuracy = (by_snr['medium']['correct'] / by_snr['medium']['total'] * 100) if by_snr['medium']['total'] > 0 else 0
    weak_snr_accuracy = (by_snr['weak']['correct'] / by_snr['weak']['total'] * 100) if by_snr['weak']['total'] > 0 else 0
    
    temporal_consistency = (agreements / comparisons * 100) if comparisons > 0 else 0
    
    return {
        'summary': {
            'total_minutes': total,
            'correct': correct,
            'overall_accuracy_percent': round(overall_accuracy, 2)
        },
        'by_minute_type': {
            'minutes_1_2_440hz': {
                'total': minute_1_2['total'],
                'correct': minute_1_2['correct'],
                'accuracy_percent': round(minute_1_2_accuracy, 2)
            },
            'bcd_minutes_0_8_10_29_30': {
                'total': bcd_minutes['total'],
                'correct': bcd_minutes['correct'],
                'accuracy_percent': round(bcd_accuracy, 2)
            },
            'normal_minutes': {
                'total': normal_minutes['total'],
                'correct': normal_minutes['correct'],
                'accuracy_percent': round(normal_accuracy, 2)
            }
        },
        'by_confidence': {
            'high': {
                'total': by_confidence['high']['total'],
                'correct': by_confidence['high']['correct'],
                'accuracy_percent': round(high_conf_accuracy, 2)
            },
            'medium': {
                'total': by_confidence['medium']['total'],
                'correct': by_confidence['medium']['correct'],
                'accuracy_percent': round(med_conf_accuracy, 2)
            },
            'low': {
                'total': by_confidence['low']['total'],
                'correct': by_confidence['low']['correct'],
                'accuracy_percent': round(low_conf_accuracy, 2)
            }
        },
        'by_snr': {
            'strong_15db': {
                'total': by_snr['strong']['total'],
                'correct': by_snr['strong']['correct'],
                'accuracy_percent': round(strong_snr_accuracy, 2)
            },
            'medium_6_15db': {
                'total': by_snr['medium']['total'],
                'correct': by_snr['medium']['correct'],
                'accuracy_percent': round(medium_snr_accuracy, 2)
            },
            'weak_0_6db': {
                'total': by_snr['weak']['total'],
                'correct': by_snr['weak']['correct'],
                'accuracy_percent': round(weak_snr_accuracy, 2)
            }
        },
        'temporal_consistency': {
            'adjacent_minute_comparisons': comparisons,
            'agreements': agreements,
            'consistency_percent': round(temporal_consistency, 2)
        },
        'confusion_matrix': {
            gt: dict(predicted) for gt, predicted in confusion.items()
        }
    }

def print_results(metrics: Dict):
    """Print results in human-readable format"""
    print("\n" + "="*70)
    print("DISCRIMINATION ACCURACY REPORT")
    print("="*70)
    
    print(f"\nOVERALL:")
    print(f"  Total Minutes: {metrics['summary']['total_minutes']}")
    print(f"  Correct: {metrics['summary']['correct']}")
    print(f"  Accuracy: {metrics['summary']['overall_accuracy_percent']:.2f}%")
    
    print(f"\nBY MINUTE TYPE:")
    print(f"  Minutes 1/2 (440 Hz):    {metrics['by_minute_type']['minutes_1_2_440hz']['accuracy_percent']:.2f}% ({metrics['by_minute_type']['minutes_1_2_440hz']['correct']}/{metrics['by_minute_type']['minutes_1_2_440hz']['total']})")
    print(f"  BCD Minutes (0,8-10,29-30): {metrics['by_minute_type']['bcd_minutes_0_8_10_29_30']['accuracy_percent']:.2f}% ({metrics['by_minute_type']['bcd_minutes_0_8_10_29_30']['correct']}/{metrics['by_minute_type']['bcd_minutes_0_8_10_29_30']['total']})")
    print(f"  Normal Minutes:          {metrics['by_minute_type']['normal_minutes']['accuracy_percent']:.2f}% ({metrics['by_minute_type']['normal_minutes']['correct']}/{metrics['by_minute_type']['normal_minutes']['total']})")
    
    print(f"\nBY CONFIDENCE LEVEL:")
    print(f"  High:   {metrics['by_confidence']['high']['accuracy_percent']:.2f}% ({metrics['by_confidence']['high']['correct']}/{metrics['by_confidence']['high']['total']})")
    print(f"  Medium: {metrics['by_confidence']['medium']['accuracy_percent']:.2f}% ({metrics['by_confidence']['medium']['correct']}/{metrics['by_confidence']['medium']['total']})")
    print(f"  Low:    {metrics['by_confidence']['low']['accuracy_percent']:.2f}% ({metrics['by_confidence']['low']['correct']}/{metrics['by_confidence']['low']['total']})")
    
    print(f"\nBY SIGNAL STRENGTH (Power Ratio):")
    print(f"  Strong (>15 dB):  {metrics['by_snr']['strong_15db']['accuracy_percent']:.2f}% ({metrics['by_snr']['strong_15db']['correct']}/{metrics['by_snr']['strong_15db']['total']})")
    print(f"  Medium (6-15 dB): {metrics['by_snr']['medium_6_15db']['accuracy_percent']:.2f}% ({metrics['by_snr']['medium_6_15db']['correct']}/{metrics['by_snr']['medium_6_15db']['total']})")
    print(f"  Weak (<6 dB):     {metrics['by_snr']['weak_0_6db']['accuracy_percent']:.2f}% ({metrics['by_snr']['weak_0_6db']['correct']}/{metrics['by_snr']['weak_0_6db']['total']})")
    
    print(f"\nTEMPORAL CONSISTENCY:")
    print(f"  Adjacent Minute Agreement: {metrics['temporal_consistency']['consistency_percent']:.2f}%")
    print(f"  Comparisons: {metrics['temporal_consistency']['adjacent_minute_comparisons']}")
    
    print(f"\nCONFUSION MATRIX:")
    for gt in ['WWV', 'WWVH', 'BALANCED']:
        if gt in metrics['confusion_matrix']:
            print(f"  Actual {gt:8s} →", end="")
            for pred in ['WWV', 'WWVH', 'BALANCED']:
                count = metrics['confusion_matrix'][gt].get(pred, 0)
                print(f" {pred}: {count:3d}", end="")
            print()
    
    print("\n" + "="*70)

def main():
    parser = argparse.ArgumentParser(description='Measure discrimination accuracy against ground truth')
    parser.add_argument('--date', required=True, help='Date in YYYYMMDD format')
    parser.add_argument('--channel', required=True, help='Channel name (e.g., "WWV 10 MHz")')
    parser.add_argument('--ground-truth', required=True, help='Path to ground truth CSV file')
    parser.add_argument('--discrimination-csv', required=True, help='Path to discrimination CSV directory')
    parser.add_argument('--output', required=True, help='Output JSON file path')
    
    args = parser.parse_args()
    
    # Load data
    print(f"Loading ground truth from: {args.ground_truth}")
    ground_truth = load_ground_truth(args.ground_truth)
    
    if not ground_truth:
        print("ERROR: Failed to load ground truth data")
        sys.exit(1)
    
    print(f"\nLoading discrimination results for {args.date}")
    discrimination = load_discrimination_results(args.discrimination_csv, args.date)
    
    if not discrimination:
        print("ERROR: Failed to load discrimination results")
        sys.exit(1)
    
    # Calculate metrics
    print("\nCalculating accuracy metrics...")
    metrics = calculate_accuracy(ground_truth, discrimination)
    
    if not metrics:
        print("ERROR: Failed to calculate metrics")
        sys.exit(1)
    
    # Print results
    print_results(metrics)
    
    # Save to JSON
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    
    with open(output_path, 'w') as f:
        json.dump(metrics, f, indent=2)
    
    print(f"\nResults saved to: {output_path}")
    
    # Exit with success/failure code based on minimum accuracy threshold
    min_accuracy = 85.0
    if metrics['summary']['overall_accuracy_percent'] >= min_accuracy:
        print(f"\n✓ PASS: Accuracy {metrics['summary']['overall_accuracy_percent']:.2f}% >= {min_accuracy}%")
        sys.exit(0)
    else:
        print(f"\n✗ FAIL: Accuracy {metrics['summary']['overall_accuracy_percent']:.2f}% < {min_accuracy}%")
        sys.exit(1)

if __name__ == '__main__':
    main()

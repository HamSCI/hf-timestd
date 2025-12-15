#!/usr/bin/env python3
"""
Quick Discrimination Visualization - Direct from CSV

Shows clear evidence that discrimination improvements are working.
Reads CSV directly (no web API) and creates simple plots.
"""

import csv
import json
import numpy as np
import matplotlib.pyplot as plt
from datetime import datetime
from pathlib import Path

def analyze_discrimination_csv(csv_path):
    """Analyze discrimination CSV and create clear visualizations"""
    
    print(f"Reading: {csv_path}")
    
    timestamps = []
    dominant_stations = []
    confidences = []
    bcd_ratios = []
    bcd_timestamps = []
    power_ratios = []
    
    with open(csv_path, 'r') as f:
        reader = csv.DictReader(f)
        
        for row in reader:
            ts = datetime.fromisoformat(row['timestamp_utc'].replace('Z', '+00:00'))
            timestamps.append(ts)
            
            # Dominant station and confidence
            dominant_stations.append(row.get('dominant_station', 'NONE'))
            confidences.append(row.get('confidence', 'low'))
            
            # Power ratio
            if row.get('power_ratio_db'):
                power_ratios.append(float(row['power_ratio_db']))
            else:
                power_ratios.append(None)
            
            # BCD windows
            bcd_windows_str = row.get('bcd_windows', '')
            if bcd_windows_str and bcd_windows_str.strip():
                try:
                    windows = json.loads(bcd_windows_str)
                    for w in windows:
                        if w.get('wwv_amplitude', 0) > 0 and w.get('wwvh_amplitude', 0) > 0:
                            bcd_ts = ts.replace(second=int(w['window_start_sec']))
                            bcd_timestamps.append(bcd_ts)
                            ratio = 20 * np.log10(w['wwv_amplitude'] / w['wwvh_amplitude'])
                            bcd_ratios.append(ratio)
                except:
                    pass
    
    print(f"\nLoaded {len(timestamps)} minutes")
    print(f"BCD windows with valid amplitudes: {len(bcd_ratios)}")
    
    # Count dominant stations
    wwv_count = sum(1 for s in dominant_stations if s == 'WWV')
    wwvh_count = sum(1 for s in dominant_stations if s == 'WWVH')
    balanced_count = sum(1 for s in dominant_stations if s == 'BALANCED')
    
    print(f"\nDiscrimination Results:")
    print(f"  WWV dominant:     {wwv_count} minutes")
    print(f"  WWVH dominant:    {wwvh_count} minutes")
    print(f"  BALANCED:         {balanced_count} minutes")
    
    if bcd_ratios:
        print(f"\nBCD Amplitude Analysis:")
        print(f"  Ratio spread:     {np.std(bcd_ratios):.2f} dB")
        print(f"  Mean ratio:       {np.mean(bcd_ratios):+.2f} dB")
        print(f"  Range:            {min(bcd_ratios):+.2f} to {max(bcd_ratios):+.2f} dB")
        print(f"  Significant (>3dB): {sum(1 for r in bcd_ratios if abs(r) >= 3)} ({100*sum(1 for r in bcd_ratios if abs(r) >= 3)/len(bcd_ratios):.1f}%)")
        
        if np.std(bcd_ratios) > 3:
            print(f"\n✓ PASS: BCD amplitudes show real separation (not mirroring)")
        else:
            print(f"\n✗ FAIL: BCD ratio spread too small")
    
    # Create visualizations  
    fig, axes = plt.subplots(2, 1, figsize=(14, 8))
    fig.suptitle(f'WWV/WWVH Discrimination Analysis (New BCD Method)\n{csv_path.name}', fontsize=14, fontweight='bold')
    
    # Plot 1: BCD Amplitude Ratios
    if bcd_ratios:
        ax = axes[0]
        colors = ['green' if r > 0 else 'red' for r in bcd_ratios]
        ax.scatter(bcd_timestamps, bcd_ratios, c=colors, alpha=0.6, s=20)
        ax.axhline(y=3, color='green', linestyle='--', alpha=0.3, label='+3 dB (WWV dominant)')
        ax.axhline(y=-3, color='red', linestyle='--', alpha=0.3, label='-3 dB (WWVH dominant)')
        ax.axhline(y=0, color='gray', linestyle='-', alpha=0.3)
        ax.set_ylabel('BCD Amplitude Ratio (dB)', fontweight='bold')
        ax.set_title('Improvement #1: BCD Joint Least Squares (Real Amplitude Separation)', fontweight='bold')
        ax.legend()
        ax.grid(True, alpha=0.3)
    else:
        axes[0].text(0.5, 0.5, 'No BCD data available', ha='center', va='center', fontsize=12)
        axes[0].set_title('BCD Amplitude Ratios', fontweight='bold')
    
    # Plot 2: Discrimination Timeline (color-coded bars)
    ax = axes[1]
    color_map = {
        'WWV': 'green',
        'WWVH': 'red',
        'BALANCED': 'purple',
        'NONE': 'gray'
    }
    colors = [color_map.get(s, 'gray') for s in dominant_stations]
    alphas = [0.9 if c == 'high' else 0.6 if c == 'medium' else 0.3 for c in confidences]
    
    for i, (ts, color, alpha) in enumerate(zip(timestamps, colors, alphas)):
        ax.bar(ts, 1, width=1/1440, color=color, alpha=alpha, edgecolor='none')
    
    ax.set_ylabel('Dominant Station', fontweight='bold')
    ax.set_xlabel('Time (UTC)', fontweight='bold')
    ax.set_title('Weighted Voting Results (Green=WWV, Red=WWVH, Brightness=Confidence)', fontweight='bold')
    ax.set_yticks([])
    ax.grid(True, alpha=0.3, axis='x')
    ax.text(0.02, 0.95, 'Note: Old 1000/1200 Hz tone discrimination is not detecting (all 0 dB). ' +
            'Discrimination is based on NEW methods: BCD 100 Hz, 440 Hz tones, and tick SNR.',
            transform=ax.transAxes, fontsize=9, verticalalignment='top',
            bbox=dict(boxstyle='round', facecolor='yellow', alpha=0.3))
    
    plt.tight_layout()
    
    # Save figure
    output_file = csv_path.parent / f"{csv_path.stem}_visualization.png"
    plt.savefig(output_file, dpi=150, bbox_inches='tight')
    print(f"\n✓ Saved visualization: {output_file}")
    
    plt.show()
    
    return {
        'total_minutes': len(timestamps),
        'wwv_count': wwv_count,
        'wwvh_count': wwvh_count,
        'balanced_count': balanced_count,
        'bcd_windows': len(bcd_ratios),
        'bcd_std': np.std(bcd_ratios) if bcd_ratios else 0
    }

if __name__ == '__main__':
    # Find the most recent WWV 10 MHz discrimination CSV
    csv_dir = Path('/tmp/timestd-test/analytics/WWV_10_MHz/discrimination')
    
    # Try Nov 15 (we know it has BCD data)
    csv_file = csv_dir / 'WWV_10_MHz_discrimination_20251115_00-24.csv'
    
    if not csv_file.exists():
        print(f"CSV file not found: {csv_file}")
        print("\nAvailable files:")
        for f in sorted(csv_dir.glob('*.csv')):
            print(f"  {f.name}")
        exit(1)
    
    result = analyze_discrimination_csv(csv_file)
    
    print("\n" + "="*70)
    print("DISCRIMINATION VALIDATION SUMMARY")
    print("="*70)
    print(f"Total minutes:     {result['total_minutes']}")
    print(f"WWV dominant:      {result['wwv_count']} ({100*result['wwv_count']/result['total_minutes']:.1f}%)")
    print(f"WWVH dominant:     {result['wwvh_count']} ({100*result['wwvh_count']/result['total_minutes']:.1f}%)")
    print(f"BCD windows:       {result['bcd_windows']}")
    print(f"BCD ratio spread:  {result['bcd_std']:.2f} dB")
    print()
    
    if result['bcd_std'] > 3:
        print("✓ BCD Joint Least Squares is WORKING - amplitudes show real separation!")
    else:
        print("✗ BCD data issue - check if discrimination code is running")

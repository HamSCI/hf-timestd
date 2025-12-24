#!/usr/bin/env python3
"""
Insert TEC CSV methods into phase2_analytics_service.py

This script adds _init_tec_csv() and _write_tec() methods after the
_write_transmission_time() method.
"""

import sys
from pathlib import Path

# TEC methods to insert
TEC_METHODS = '''
    def _init_tec_csv(self):
        """Initialize TEC estimation CSV for today."""
        today = datetime.now(timezone.utc).strftime('%Y%m%d')
        # TEC is station-based, not channel-based (aggregates across frequencies)
        # Use simplified naming: tec_YYYYMMDD.csv
        self.tec_csv = self.tec_dir / f'tec_{today}.csv'
        
        if not self.tec_csv.exists():
            with open(self.tec_csv, 'w', newline='') as f:
                writer = csv.writer(f)
                writer.writerow([
                    'timestamp_utc', 'minute_boundary', 'station',
                    'tec_tecu', 't_vacuum_error_ms', 'confidence', 'residuals_ms',
                    'n_frequencies', 'frequencies_mhz',
                    'group_delay_2_5_mhz', 'group_delay_5_mhz', 'group_delay_10_mhz',
                    'group_delay_15_mhz', 'group_delay_20_mhz', 'group_delay_25_mhz'
                ])
            logger.info(f"Created TEC CSV: {self.tec_csv}")
    
    def _write_tec(self, minute_boundary: int, station: str, measurements: List[Dict]):
        """Write TEC estimation from multi-frequency measurements.
        
        Args:
            minute_boundary: Unix timestamp of minute boundary
            station: Station name (WWV, WWVH, CHU, BPM)
            measurements: List of dicts with 'frequency_hz', 'toa_ms', 'uncertainty_ms'
        """
        try:
            # Need at least 2 frequencies for TEC estimation
            if len(measurements) < 2:
                return
            
            # Use data timestamp for filename to support backfilling
            dt = datetime.fromtimestamp(minute_boundary, timezone.utc)
            date_str = dt.strftime('%Y%m%d')
            
            expected_csv = self.tec_dir / f'tec_{date_str}.csv'
            
            # Initialize if file changed or doesn't exist (handle daily rotation)
            if self.tec_csv != expected_csv or not expected_csv.exists():
                self.tec_csv = expected_csv
                if not self.tec_csv.exists():
                    with open(self.tec_csv, 'w', newline='') as f:
                        writer = csv.writer(f)
                        writer.writerow([
                            'timestamp_utc', 'minute_boundary', 'station',
                            'tec_tecu', 't_vacuum_error_ms', 'confidence', 'residuals_ms',
                            'n_frequencies', 'frequencies_mhz',
                            'group_delay_2_5_mhz', 'group_delay_5_mhz', 'group_delay_10_mhz',
                            'group_delay_15_mhz', 'group_delay_20_mhz', 'group_delay_25_mhz'
                        ])
                    logger.info(f"Created/Rotated TEC CSV: {self.tec_csv}")
            
            # Estimate TEC using multi-frequency least squares
            tec_result = self.tec_estimator.estimate_tec(
                measurements=measurements,
                station=station,
                timestamp=float(minute_boundary)
            )
            
            if not tec_result:
                return  # Estimation failed
            
            # Extract per-frequency group delays (for visualization)
            freq_list = sorted([m['frequency_hz'] / 1e6 for m in measurements])
            freq_str = ';'.join([f"{f:.2f}" for f in freq_list])
            
            # Map group delays to standard frequencies (fill with empty if not present)
            delay_map = tec_result.group_delay_ms  # Dict[float, float] keyed by MHz
            
            with open(self.tec_csv, 'a', newline='') as f:
                writer = csv.writer(f)
                utc_time = datetime.fromtimestamp(minute_boundary, timezone.utc).isoformat()
                
                writer.writerow([
                    utc_time,
                    minute_boundary,
                    station,
                    round(tec_result.tec_u, 3),  # TEC in TECU
                    round(tec_result.t_vacuum_error_ms, 3),
                    round(tec_result.confidence, 4),
                    round(tec_result.residuals_ms, 3),
                    tec_result.n_frequencies,
                    freq_str,
                    round(delay_map.get(2.5, 0), 3) if 2.5 in delay_map else '',
                    round(delay_map.get(5.0, 0), 3) if 5.0 in delay_map else '',
                    round(delay_map.get(10.0, 0), 3) if 10.0 in delay_map else '',
                    round(delay_map.get(15.0, 0), 3) if 15.0 in delay_map else '',
                    round(delay_map.get(20.0, 0), 3) if 20.0 in delay_map else '',
                    round(delay_map.get(25.0, 0), 3) if 25.0 in delay_map else ''
                ])
                
            logger.info(
                f"TEC estimated for {station}: {tec_result.tec_u:.2f} TECU "
                f"(confidence={tec_result.confidence:.2f}, n_freq={tec_result.n_frequencies})"
            )
            
        except Exception as e:
            logger.error(f"Failed to write TEC: {e}")
'''

def main():
    file_path = Path("/home/mjh/git/hf-timestd/src/hf_timestd/core/phase2_analytics_service.py")
    
    # Read the file
    with open(file_path, 'r') as f:
        lines = f.readlines()
    
    # Find the line after _write_transmission_time method ends
    # Look for "    def _read_drf_minute"
    insert_index = None
    for i, line in enumerate(lines):
        if line.strip().startswith("def _read_drf_minute"):
            insert_index = i
            break
    
    if insert_index is None:
        print("ERROR: Could not find insertion point (_read_drf_minute method)")
        return 1
    
    # Insert the TEC methods before _read_drf_minute
    lines.insert(insert_index, TEC_METHODS + '\n')
    
    # Write back
    with open(file_path, 'w') as f:
        f.writelines(lines)
    
    print(f"✓ Successfully inserted TEC methods at line {insert_index}")
    return 0

if __name__ == "__main__":
    sys.exit(main())

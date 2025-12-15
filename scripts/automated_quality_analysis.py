#!/usr/bin/env python3
"""
Automated Quality Analysis for Dashboard Integration

Runs regularly to:
1. Compare decimation quality (200Hz→10Hz vs 16kHz→10Hz)
2. Compare timing methods (time_snap vs NTP)
3. Generate JSON metrics for web dashboard
4. Store historical trends

Output: JSON files for dashboard consumption
"""

import argparse
import json
import logging
import sys
from pathlib import Path
from datetime import datetime, timezone, timedelta
import numpy as np
from scipy import signal as scipy_signal
from scipy.stats import entropy

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

from grape_recorder.paths import GRAPEPaths

class QualityAnalyzer:
    """Analyze decimation and timing quality for dashboard"""
    
    def __init__(self, data_root: Path):
        paths = GRAPEPaths(data_root)
        self.data_root = data_root
        self.analytics_dir = paths.get_analytics_dir()
        self.output_dir = paths.get_quality_dir()
        self.output_dir.mkdir(parents=True, exist_ok=True)
    
    def analyze_decimation_quality_fast(self, wide_file: Path, carrier_file: Path):
        """Fast decimation quality check for dashboard"""
        try:
            wide_data = np.load(wide_file)
            carrier_data = np.load(carrier_file)
            
            wide_iq = wide_data['iq']
            carrier_iq = carrier_data['iq']
            
            # Take matching segments (first 600 samples = 1 minute)
            n = min(len(wide_iq), len(carrier_iq), 600)
            wide_iq = wide_iq[:n]
            carrier_iq = carrier_iq[:n]
            
            # Quick spectral analysis
            wide_spectral = self._quick_spectral_analysis(wide_iq, 10.0)
            carrier_spectral = self._quick_spectral_analysis(carrier_iq, 10.0)
            
            # Phase jitter
            wide_phase_jitter = self._phase_jitter(wide_iq, 10.0)
            carrier_phase_jitter = self._phase_jitter(carrier_iq, 10.0)
            
            return {
                'wide': {
                    'snr_db': wide_spectral['snr'],
                    'sfdr_db': wide_spectral['sfdr'],
                    'phase_jitter_rad': wide_phase_jitter,
                    'noise_floor_db': wide_spectral['noise_floor']
                },
                'carrier': {
                    'snr_db': carrier_spectral['snr'],
                    'sfdr_db': carrier_spectral['sfdr'],
                    'phase_jitter_rad': carrier_phase_jitter,
                    'noise_floor_db': carrier_spectral['noise_floor']
                },
                'comparison': {
                    'snr_advantage_db': carrier_spectral['snr'] - wide_spectral['snr'],
                    'phase_jitter_ratio': wide_phase_jitter / carrier_phase_jitter if carrier_phase_jitter > 0 else 1.0,
                    'carrier_cleaner': carrier_phase_jitter < wide_phase_jitter
                }
            }
        except Exception as e:
            logger.error(f"Decimation analysis failed: {e}")
            return None
    
    def _quick_spectral_analysis(self, iq_samples, fs):
        """Fast spectral analysis for quality metrics"""
        freqs, psd = scipy_signal.welch(iq_samples, fs=fs, nperseg=min(256, len(iq_samples)))
        psd_db = 10 * np.log10(psd + 1e-10)
        
        peak_idx = np.argmax(psd_db)
        peak_power = psd_db[peak_idx]
        peak_freq = freqs[peak_idx]
        
        noise_floor = np.percentile(psd_db, 20)
        snr = peak_power - noise_floor
        
        # SFDR
        mask = np.abs(freqs - peak_freq) > 0.5
        if np.any(mask):
            spurious_peak = np.max(psd_db[mask])
            sfdr = peak_power - spurious_peak
        else:
            sfdr = 100.0
        
        return {
            'snr': float(snr),
            'sfdr': float(sfdr),
            'noise_floor': float(noise_floor),
            'peak_freq': float(peak_freq)
        }
    
    def _phase_jitter(self, iq_samples, fs):
        """Measure phase jitter"""
        phase = np.unwrap(np.angle(iq_samples))
        sos = scipy_signal.butter(4, 0.5, btype='high', fs=fs, output='sos')
        phase_hp = scipy_signal.sosfilt(sos, phase)
        return float(np.std(phase_hp))
    
    def analyze_timing_quality_fast(self, wide_dir: Path, carrier_dir: Path, date_str: str):
        """Fast timing quality check for dashboard"""
        try:
            # Load metadata from files
            wide_files = sorted(wide_dir.glob(f"{date_str}*.npz"))[:60]  # First hour
            carrier_files = sorted(carrier_dir.glob(f"{date_str}*.npz"))[:60]
            
            if not wide_files or not carrier_files:
                return None
            
            # Extract timing metadata
            wide_timing_dist = self._extract_timing_distribution(wide_files)
            carrier_timing_dist = self._extract_timing_distribution(carrier_files)
            
            # Time snap age analysis (wide only)
            wide_snap_ages = []
            for f in wide_files[:12]:  # First 12 minutes
                data = np.load(f, allow_pickle=True)
                if 'timing_metadata' in data:
                    meta = data['timing_metadata'].item()
                    age = meta.get('time_snap_age_seconds')
                    if age is not None:
                        wide_snap_ages.append(age)
            
            # NTP offset analysis (carrier)
            carrier_ntp_offsets = []
            for f in carrier_files[:12]:
                data = np.load(f, allow_pickle=True)
                if 'timing_metadata' in data:
                    meta = data['timing_metadata'].item()
                    offset = meta.get('ntp_offset_ms')
                    if offset is not None:
                        carrier_ntp_offsets.append(offset)
            
            return {
                'wide': {
                    'timing_distribution': wide_timing_dist,
                    'mean_snap_age_s': float(np.mean(wide_snap_ages)) if wide_snap_ages else None,
                    'max_snap_age_s': float(np.max(wide_snap_ages)) if wide_snap_ages else None
                },
                'carrier': {
                    'timing_distribution': carrier_timing_dist,
                    'mean_ntp_offset_ms': float(np.mean(carrier_ntp_offsets)) if carrier_ntp_offsets else None,
                    'ntp_offset_std_ms': float(np.std(carrier_ntp_offsets)) if carrier_ntp_offsets else None
                }
            }
        except Exception as e:
            logger.error(f"Timing analysis failed: {e}")
            return None
    
    def _extract_timing_distribution(self, files):
        """Extract timing quality distribution from NPZ files"""
        distribution = {}
        for f in files:
            try:
                data = np.load(f, allow_pickle=True)
                if 'timing_metadata' in data:
                    meta = data['timing_metadata'].item()
                    quality = meta.get('quality', 'unknown')
                    distribution[quality] = distribution.get(quality, 0) + 1
            except:
                pass
        
        # Convert to percentages
        total = sum(distribution.values())
        if total > 0:
            return {k: round(100 * v / total, 1) for k, v in distribution.items()}
        return {}
    
    def generate_channel_report(self, channel_base: str, date_str: str):
        """Generate report for one channel pair (wide + carrier)"""
        logger.info(f"Analyzing {channel_base} for {date_str}...")
        
        # Paths
        wide_dir = self.analytics_dir / channel_base / 'decimated'
        carrier_dir = self.analytics_dir / f"{channel_base}_carrier" / 'decimated'
        
        if not wide_dir.exists() or not carrier_dir.exists():
            logger.warning(f"Skipping {channel_base} - directories not found")
            return None
        
        # Find matching files (use 08:00 UTC as reference time)
        wide_file = list(wide_dir.glob(f"{date_str}T08*.npz"))
        carrier_file = list(carrier_dir.glob(f"{date_str}T08*.npz"))
        
        if not wide_file or not carrier_file:
            logger.warning(f"No matching files for {channel_base}")
            return None
        
        # Decimation quality
        decimation = self.analyze_decimation_quality_fast(wide_file[0], carrier_file[0])
        
        # Timing quality
        timing = self.analyze_timing_quality_fast(wide_dir, carrier_dir, date_str)
        
        if not decimation or not timing:
            return None
        
        return {
            'channel': channel_base,
            'date': date_str,
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'decimation_quality': decimation,
            'timing_quality': timing
        }
    
    def generate_daily_report(self, date_str: str = None):
        """Generate report for all channels"""
        if date_str is None:
            date_str = datetime.now(timezone.utc).strftime('%Y%m%d')
        
        logger.info(f"Generating quality report for {date_str}")
        
        # Define channel pairs
        channels = [
            'WWV_2.5_MHz',
            'WWV_5_MHz',
            'WWV_10_MHz',
            'WWV_15_MHz',
            'WWV_20_MHz',
            'WWV_25_MHz',
            'CHU_3.33_MHz',
            'CHU_7.85_MHz',
            'CHU_14.67_MHz'
        ]
        
        report = {
            'generated_at': datetime.now(timezone.utc).isoformat(),
            'date': date_str,
            'channels': {}
        }
        
        for channel in channels:
            result = self.generate_channel_report(channel, date_str)
            if result:
                report['channels'][channel] = result
        
        # Summary statistics
        report['summary'] = self._compute_summary(report['channels'])
        
        # Save to JSON
        output_file = self.output_dir / f'quality_report_{date_str}.json'
        with open(output_file, 'w') as f:
            json.dump(report, f, indent=2)
        
        logger.info(f"✅ Report saved: {output_file}")
        
        # Also save as "latest" for dashboard
        latest_file = self.output_dir / 'quality_report_latest.json'
        with open(latest_file, 'w') as f:
            json.dump(report, f, indent=2)
        
        logger.info(f"✅ Latest report updated: {latest_file}")
        
        return report
    
    def _compute_summary(self, channels):
        """Compute summary statistics across all channels"""
        if not channels:
            return {}
        
        # Collect metrics
        snr_advantages = []
        jitter_ratios = []
        tone_locked_pcts = []
        ntp_synced_pcts = []
        
        for channel, data in channels.items():
            if 'decimation_quality' in data:
                comp = data['decimation_quality']['comparison']
                snr_advantages.append(comp['snr_advantage_db'])
                jitter_ratios.append(comp['phase_jitter_ratio'])
            
            if 'timing_quality' in data:
                wide_dist = data['timing_quality']['wide']['timing_distribution']
                carrier_dist = data['timing_quality']['carrier']['timing_distribution']
                
                tone_locked_pcts.append(wide_dist.get('tone_locked', 0))
                ntp_synced_pcts.append(carrier_dist.get('ntp_synced', 0))
        
        return {
            'mean_snr_advantage_db': round(np.mean(snr_advantages), 2) if snr_advantages else None,
            'mean_jitter_ratio': round(np.mean(jitter_ratios), 2) if jitter_ratios else None,
            'mean_tone_locked_pct': round(np.mean(tone_locked_pcts), 1) if tone_locked_pcts else None,
            'mean_ntp_synced_pct': round(np.mean(ntp_synced_pcts), 1) if ntp_synced_pcts else None,
            'channels_analyzed': len(channels)
        }


def main():
    parser = argparse.ArgumentParser(description='Generate quality analysis for dashboard')
    parser.add_argument('--data-root', default='/tmp/timestd-test', help='Data root directory')
    parser.add_argument('--date', help='Date (YYYYMMDD), default=today')
    
    args = parser.parse_args()
    
    analyzer = QualityAnalyzer(Path(args.data_root))
    report = analyzer.generate_daily_report(args.date)
    
    if report:
        print(f"\n{'='*60}")
        print(f"QUALITY ANALYSIS SUMMARY - {report['date']}")
        print(f"{'='*60}")
        summary = report['summary']
        print(f"Channels analyzed: {summary['channels_analyzed']}")
        print(f"Mean SNR advantage (carrier): {summary['mean_snr_advantage_db']} dB")
        print(f"Mean phase jitter ratio (wide/carrier): {summary['mean_jitter_ratio']}x")
        print(f"Mean tone-locked coverage: {summary['mean_tone_locked_pct']}%")
        print(f"Mean NTP-synced coverage: {summary['mean_ntp_synced_pct']}%")
        print(f"\n✅ Report available for dashboard consumption")


if __name__ == '__main__':
    main()

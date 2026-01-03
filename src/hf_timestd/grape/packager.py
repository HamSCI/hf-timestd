#!/usr/bin/env python3
"""
Daily DRF Packager - Package decimated binary data for PSWS upload

Runs once daily (typically at 00:15 UTC) to package the previous day's
decimated 10 Hz data into PSWS-compatible Digital RF format.

Input:  phase2/{CHANNEL}/decimated/{YYYYMMDD}.bin + _meta.json
Output: upload/{YYYYMMDD}/{CALLSIGN}_{GRID}/{RECEIVER}@{ID}/OBS.../ch0/

Features:
---------
- Combines all 9 channels into single multi-subchannel DRF
- Includes comprehensive metadata (station info, timing quality, gaps)
- PSWS/wsprdaemon compatible format

Usage:
------
    from hf_timestd.grape.daily_drf_packager import DailyDRFPackager
    
    packager = DailyDRFPackager(data_root, station_config)
    packager.package_day('2025-12-05')
    
    # Or via CLI:
    python -m hf_timestd.core.daily_drf_packager \\
        --data-root /tmp/grape-test \\
        --date 2025-12-05 \\
        --callsign AC0G --grid EM28
"""

import numpy as np
import json
import logging
import sys
from pathlib import Path
from datetime import datetime, timezone, date, timedelta
from typing import Optional, Dict, List, Tuple, Any
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Check for Digital RF
try:
    import digital_rf as drf
    DRF_AVAILABLE = True
except ImportError:
    DRF_AVAILABLE = False
    logger.warning("digital_rf not available - DRF packaging disabled")

from .decimated_buffer import DecimatedBuffer, SAMPLE_RATE, SAMPLES_PER_DAY

# Standard GRAPE channels (sorted by frequency)
STANDARD_CHANNELS = [
    ('WWV 2.5 MHz', 2.5e6),
    ('CHU 3.33 MHz', 3.33e6),
    ('WWV 5 MHz', 5e6),
    ('CHU 7.85 MHz', 7.85e6),
    ('WWV 10 MHz', 10e6),
    ('CHU 14.67 MHz', 14.67e6),
    ('WWV 15 MHz', 15e6),
    ('WWV 20 MHz', 20e6),
    ('WWV 25 MHz', 25e6),
]


@dataclass
class StationConfig:
    """Station configuration for DRF metadata."""
    callsign: str
    grid_square: str
    receiver_name: str = 'GRAPE'
    psws_station_id: str = ''
    psws_instrument_id: str = '1'
    
    def __post_init__(self):
        if not self.psws_station_id:
            self.psws_station_id = f"{self.callsign}_1"


def maidenhead_to_latlon(grid: str) -> Tuple[float, float]:
    """Convert Maidenhead grid square to latitude/longitude."""
    grid = grid.upper()
    lon = (ord(grid[0]) - ord('A')) * 20 + (ord(grid[2]) - ord('0')) * 2 - 180
    lat = (ord(grid[1]) - ord('A')) * 10 + (ord(grid[3]) - ord('0')) - 90
    if len(grid) >= 6:
        lon += (ord(grid[4]) - ord('A')) * 5.0 / 60.0
        lat += (ord(grid[5]) - ord('A')) * 2.5 / 60.0
    return lat, lon


class DailyDRFPackager:
    """
    Package decimated binary data into PSWS-compatible Digital RF.
    
    Creates a single multi-subchannel DRF file containing all 9 GRAPE channels,
    ready for upload to the PSWS/GRAPE data repository.
    """
    
    def __init__(
        self,
        data_root: Path,
        station_config: StationConfig,
        channels: Optional[List[Tuple[str, float]]] = None
    ):
        """
        Initialize DRF packager.
        
        Args:
            data_root: Root data directory
            station_config: Station configuration
            channels: List of (channel_name, frequency_hz) tuples
                      Default: STANDARD_CHANNELS
        """
        if not DRF_AVAILABLE:
            raise ImportError(
                "digital_rf required for DRF packaging. "
                "Install with: pip install digital_rf"
            )
        
        self.data_root = Path(data_root)
        self.station = station_config
        self.channels = channels or STANDARD_CHANNELS
        
        # Output directory
        self.upload_dir = self.data_root / 'upload'
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        
        logger.info(f"DailyDRFPackager initialized")
        logger.info(f"  Station: {station_config.callsign} @ {station_config.grid_square}")
        logger.info(f"  Channels: {len(self.channels)}")
        logger.info(f"  Output: {self.upload_dir}")
    
    def package_day(self, date_str: str) -> Optional[Path]:
        """
        Package a day's decimated data into DRF format.
        
        Args:
            date_str: Date in YYYYMMDD or YYYY-MM-DD format
            
        Returns:
            Path to output directory or None if failed
        """
        # Normalize date
        if '-' in date_str:
            date_str = date_str.replace('-', '')
        
        logger.info(f"Packaging {date_str} for upload")
        
        # Load all channel data
        channel_data = {}
        channel_metadata = {}
        frequencies = []
        
        for channel_name, freq_hz in self.channels:
            buffer = DecimatedBuffer(self.data_root, channel_name)
            iq_data, day_meta = buffer.read_day(date_str)
            
            if iq_data is not None:
                channel_data[channel_name] = iq_data
                channel_metadata[channel_name] = day_meta
                frequencies.append(freq_hz)
                logger.info(f"  ✓ {channel_name}: {len(iq_data)} samples")
            else:
                logger.warning(f"  ✗ {channel_name}: no data")
        
        if not channel_data:
            logger.error("No channel data found - cannot package")
            return None
        
        # Build output directory structure
        date_obj = datetime.strptime(date_str, '%Y%m%d').replace(tzinfo=timezone.utc)
        output_dir = self._build_output_structure(date_obj)
        
        # Write DRF
        self._write_drf(
            output_dir=output_dir,
            channel_data=channel_data,
            frequencies=frequencies,
            date_obj=date_obj
        )
        
        # Write metadata
        self._write_metadata(
            output_dir=output_dir,
            frequencies=frequencies,
            channel_metadata=channel_metadata,
            date_obj=date_obj
        )
        
        # Write gap analysis summary
        self._write_gap_summary(
            output_dir=output_dir.parent,
            channel_metadata=channel_metadata,
            date_obj=date_obj
        )
        
        logger.info(f"✅ Package complete: {output_dir.parent}")
        return output_dir.parent
    
    def _build_output_structure(self, date_obj: datetime) -> Path:
        """Build PSWS-compatible directory structure."""
        date_str = date_obj.strftime('%Y%m%d')
        obs_date = date_obj.strftime('%Y-%m-%dT00-00')
        
        # Structure: upload/{YYYYMMDD}/{CALLSIGN}_{GRID}/{RECEIVER}@{ID}/OBS.../ch0/
        output_dir = (
            self.upload_dir / date_str /
            f"{self.station.callsign}_{self.station.grid_square}" /
            f"{self.station.receiver_name}@{self.station.psws_station_id}_{self.station.psws_instrument_id}" /
            f"OBS{obs_date}" / 'ch0'
        )
        output_dir.mkdir(parents=True, exist_ok=True)
        
        return output_dir
    
    def _write_drf(
        self,
        output_dir: Path,
        channel_data: Dict[str, np.ndarray],
        frequencies: List[float],
        date_obj: datetime
    ):
        """Write multi-subchannel Digital RF file."""
        num_channels = len(channel_data)
        
        # Get max samples (should all be SAMPLES_PER_DAY but handle partial)
        max_samples = max(len(d) for d in channel_data.values())
        
        # Build stacked IQ array: shape (samples, 2 * num_channels)
        # Format: [ch1_I, ch1_Q, ch2_I, ch2_Q, ...]
        stacked = np.zeros((max_samples, 2 * num_channels), dtype=np.float32)
        
        for i, (channel_name, _) in enumerate(self.channels):
            if channel_name in channel_data:
                iq = channel_data[channel_name]
                n_samples = len(iq)
                stacked[:n_samples, i*2] = iq.real.astype(np.float32)
                stacked[:n_samples, i*2+1] = iq.imag.astype(np.float32)
        
        # Calculate start index (samples since Unix epoch at 10 Hz)
        start_utc = date_obj.timestamp()
        start_global_index = int(start_utc * SAMPLE_RATE)
        
        logger.info(f"Writing DRF: {max_samples} samples × {num_channels} channels")
        logger.info(f"  Shape: {stacked.shape}")
        logger.info(f"  Start index: {start_global_index}")
        
        # Create DRF writer
        writer = drf.DigitalRFWriter(
            str(output_dir),
            dtype='f4',  # float32
            subdir_cadence_secs=86400,
            file_cadence_millisecs=3600000,
            start_global_index=start_global_index,
            sample_rate_numerator=SAMPLE_RATE,
            sample_rate_denominator=1,
            uuid_str=self.station.psws_station_id,
            compression_level=0,
            checksum=False,
            is_complex=True,
            num_subchannels=num_channels,
            is_continuous=True,
            marching_periods=False
        )
        
        # Write data
        writer.rf_write(stacked)
        writer.close()
        
        logger.info(f"  ✓ DRF data written")
    
    def _write_metadata(
        self,
        output_dir: Path,
        frequencies: List[float],
        channel_metadata: Dict,
        date_obj: datetime
    ):
        """Write DRF metadata file."""
        metadata_dir = output_dir / 'metadata'
        metadata_dir.mkdir(parents=True, exist_ok=True)
        
        lat, lon = maidenhead_to_latlon(self.station.grid_square)
        
        start_utc = date_obj.timestamp()
        start_global_index = int(start_utc * SAMPLE_RATE)
        
        metadata = {
            'callsign': self.station.callsign,
            'grid_square': self.station.grid_square,
            'receiver_name': self.station.receiver_name,
            'lat': np.float32(lat),
            'long': np.float32(lon),
            'center_frequencies': np.ascontiguousarray(frequencies, dtype=np.float64),
            'uuid_str': self.station.psws_station_id
        }
        
        md_writer = drf.DigitalMetadataWriter(
            str(metadata_dir),
            subdir_cadence_secs=86400,
            file_cadence_secs=86400,
            sample_rate_numerator=SAMPLE_RATE,
            sample_rate_denominator=1,
            file_name='metadata'
        )
        md_writer.write(start_global_index, metadata)
        
        logger.info(f"  ✓ Metadata written")
        logger.info(f"    Frequencies: {[f/1e6 for f in frequencies]} MHz")
    
    def _write_gap_summary(
        self,
        output_dir: Path,
        channel_metadata: Dict,
        date_obj: datetime
    ):
        """Write gap analysis summary as JSON."""
        summary = {
            'date': date_obj.strftime('%Y-%m-%d'),
            'station': self.station.callsign,
            'grid': self.station.grid_square,
            'channels': {}
        }
        
        for channel_name, day_meta in channel_metadata.items():
            if day_meta:
                meta_dict = day_meta.to_dict() if hasattr(day_meta, 'to_dict') else {}
                summary['channels'][channel_name] = meta_dict.get('summary', {})
        
        # Overall summary
        total_valid = sum(
            s.get('valid_minutes', 0) 
            for s in summary['channels'].values()
        )
        total_expected = len(summary['channels']) * 1440
        
        summary['overall'] = {
            'total_channels': len(summary['channels']),
            'total_valid_minutes': total_valid,
            'total_expected_minutes': total_expected,
            'completeness_pct': round(total_valid / total_expected * 100, 2) if total_expected > 0 else 0
        }
        
        summary_file = output_dir / 'gap_summary.json'
        with open(summary_file, 'w') as f:
            json.dump(summary, f, indent=2)
        
        logger.info(f"  ✓ Gap summary written: {summary['overall']['completeness_pct']}% complete")
    
    def package_yesterday(self) -> Optional[Path]:
        """Package yesterday's data (convenience method)."""
        yesterday = datetime.now(tz=timezone.utc).date() - timedelta(days=1)
        return self.package_day(yesterday.strftime('%Y%m%d'))


def package_for_upload(
    data_root: Path,
    callsign: str,
    grid_square: str,
    date_str: str,
    receiver_name: str = 'GRAPE',
    psws_station_id: Optional[str] = None
) -> Optional[Path]:
    """
    Convenience function to package a day's data for upload.
    
    Args:
        data_root: Root data directory
        callsign: Station callsign
        grid_square: Maidenhead grid square
        date_str: Date to package (YYYYMMDD or YYYY-MM-DD)
        receiver_name: Receiver name (default: GRAPE)
        psws_station_id: PSWS station ID (default: {callsign}_1)
        
    Returns:
        Path to output directory or None if failed
    """
    station = StationConfig(
        callsign=callsign,
        grid_square=grid_square,
        receiver_name=receiver_name,
        psws_station_id=psws_station_id or f"{callsign}_1"
    )
    
    packager = DailyDRFPackager(data_root, station)
    return packager.package_day(date_str)


# CLI interface
if __name__ == '__main__':
    import argparse
    
    parser = argparse.ArgumentParser(
        description='Package decimated data into PSWS-compatible DRF for upload'
    )
    parser.add_argument('--data-root', type=Path, required=True,
                       help='Root data directory')
    parser.add_argument('--date', type=str,
                       help='Date to package (YYYYMMDD or YYYY-MM-DD)')
    parser.add_argument('--yesterday', action='store_true',
                       help='Package yesterday\'s data')
    parser.add_argument('--callsign', type=str, required=True,
                       help='Station callsign')
    parser.add_argument('--grid', type=str, required=True,
                       help='Maidenhead grid square')
    parser.add_argument('--receiver-name', type=str, default='GRAPE',
                       help='Receiver name (default: GRAPE)')
    parser.add_argument('--psws-station-id', type=str,
                       help='PSWS station ID (default: {callsign}_1)')
    parser.add_argument('--log-level', type=str, default='INFO',
                       choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'])
    
    args = parser.parse_args()
    
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format='%(asctime)s - %(levelname)s - %(message)s'
    )
    
    station = StationConfig(
        callsign=args.callsign,
        grid_square=args.grid,
        receiver_name=args.receiver_name,
        psws_station_id=args.psws_station_id or f"{args.callsign}_1"
    )
    
    packager = DailyDRFPackager(args.data_root, station)
    
    if args.yesterday:
        result = packager.package_yesterday()
    elif args.date:
        result = packager.package_day(args.date)
    else:
        parser.print_help()
        sys.exit(1)
    
    if result:
        print(f"SUCCESS: {result}")
        sys.exit(0)
    else:
        print("FAILED")
        sys.exit(1)

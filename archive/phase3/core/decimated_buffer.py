#!/usr/bin/env python3
"""
Decimated Buffer - Binary 10 Hz IQ Storage with Gap/Timing Metadata

Stores decimated 10 Hz IQ data in a simple binary format for:
1. Efficient spectrogram generation (read anytime)
2. Daily DRF packaging for upload

Format:
-------
Data file:   {YYYYMMDD}.bin  - Raw complex64, 600 samples/minute × 1440 minutes
Metadata:    {YYYYMMDD}_meta.json - Per-minute timing and gap info

Storage location: products/{CHANNEL}/decimated/  (Phase 3 derived products)

Usage:
------
    # Writing (called from Phase 2 analytics, once per minute)
    buffer = DecimatedBuffer(data_root, channel_name)
    buffer.write_minute(minute_utc, decimated_iq, d_clock_ms, quality_grade, gap_info)
    
    # Reading (for spectrograms or DRF packaging)
    iq_data, metadata = buffer.read_day('2025-12-06')
    iq_data, metadata = buffer.read_hours(hours=6)  # Last 6 hours
"""

import numpy as np
import json
import logging
import fcntl
from pathlib import Path
from datetime import datetime, timezone, date, timedelta
from typing import Optional, Dict, Tuple, List, Any
from dataclasses import dataclass, field, asdict

logger = logging.getLogger(__name__)

# Constants
SAMPLE_RATE = 10  # 10 Hz output
SAMPLES_PER_MINUTE = 600  # 10 Hz × 60 seconds
SAMPLES_PER_DAY = 864000  # 10 Hz × 86400 seconds
BYTES_PER_SAMPLE = 8  # complex64 = 4 bytes real + 4 bytes imag


@dataclass
class MinuteMetadata:
    """Metadata for one minute of decimated data."""
    minute_index: int  # 0-1439
    utc_timestamp: float
    d_clock_ms: float
    uncertainty_ms: float
    quality_grade: str  # A, B, C, D, X
    gap_samples: int  # Number of gap samples in this minute
    valid: bool  # Was this minute successfully processed?
    
    def to_dict(self) -> Dict:
        return asdict(self)


@dataclass
class DayMetadata:
    """Metadata for a full day of decimated data."""
    channel: str
    date: str  # YYYY-MM-DD
    sample_rate: int = SAMPLE_RATE
    samples_per_minute: int = SAMPLES_PER_MINUTE
    start_utc: float = 0.0
    minutes: Dict[str, Dict] = field(default_factory=dict)
    
    # Summary (computed)
    valid_minutes: int = 0
    total_gap_samples: int = 0
    completeness_pct: float = 0.0
    
    def update_summary(self):
        """Recompute summary statistics."""
        self.valid_minutes = sum(1 for m in self.minutes.values() if m.get('valid', False))
        self.total_gap_samples = sum(m.get('gap_samples', 0) for m in self.minutes.values())
        expected_samples = len(self.minutes) * SAMPLES_PER_MINUTE
        if expected_samples > 0:
            self.completeness_pct = ((expected_samples - self.total_gap_samples) / expected_samples) * 100
    
    def to_dict(self) -> Dict:
        self.update_summary()
        return {
            'channel': self.channel,
            'date': self.date,
            'sample_rate': self.sample_rate,
            'samples_per_minute': self.samples_per_minute,
            'start_utc': self.start_utc,
            'minutes': self.minutes,
            'summary': {
                'valid_minutes': self.valid_minutes,
                'total_gap_samples': self.total_gap_samples,
                'completeness_pct': round(self.completeness_pct, 2)
            }
        }
    
    @classmethod
    def from_dict(cls, data: Dict) -> 'DayMetadata':
        meta = cls(
            channel=data.get('channel', ''),
            date=data.get('date', ''),
            sample_rate=data.get('sample_rate', SAMPLE_RATE),
            samples_per_minute=data.get('samples_per_minute', SAMPLES_PER_MINUTE),
            start_utc=data.get('start_utc', 0.0),
            minutes=data.get('minutes', {})
        )
        summary = data.get('summary', {})
        meta.valid_minutes = summary.get('valid_minutes', 0)
        meta.total_gap_samples = summary.get('total_gap_samples', 0)
        meta.completeness_pct = summary.get('completeness_pct', 0.0)
        return meta


class DecimatedBuffer:
    """
    Binary storage for decimated 10 Hz IQ data.
    
    Stores data in flat binary files for efficient random access.
    One file per day per channel.
    """
    
    def __init__(self, data_root: Path, channel_name: str):
        """
        Initialize decimated buffer.
        
        Args:
            data_root: Root data directory
            channel_name: Channel name (e.g., "WWV 10 MHz")
        """
        self.data_root = Path(data_root)
        self.channel_name = channel_name
        self.channel_dir = channel_name.replace(' ', '_')
        
        # Storage location: products/{CHANNEL}/decimated/ (Phase 3 derived products)
        self.buffer_dir = self.data_root / 'products' / self.channel_dir / 'decimated'
        self.buffer_dir.mkdir(parents=True, exist_ok=True)
        
        logger.debug(f"DecimatedBuffer initialized: {self.buffer_dir}")
    
    def _get_paths(self, date_str: str) -> Tuple[Path, Path]:
        """Get data and metadata file paths for a date."""
        bin_path = self.buffer_dir / f"{date_str}.bin"
        meta_path = self.buffer_dir / f"{date_str}_meta.json"
        return bin_path, meta_path
    
    def _load_metadata(self, date_str: str) -> DayMetadata:
        """Load or create metadata for a date."""
        _, meta_path = self._get_paths(date_str)
        
        if meta_path.exists():
            try:
                with open(meta_path, 'r') as f:
                    return DayMetadata.from_dict(json.load(f))
            except Exception as e:
                logger.warning(f"Error loading metadata {meta_path}: {e}")
        
        # Create new metadata
        date_obj = datetime.strptime(date_str, '%Y%m%d').replace(tzinfo=timezone.utc)
        return DayMetadata(
            channel=self.channel_name,
            date=date_obj.strftime('%Y-%m-%d'),
            start_utc=date_obj.timestamp()
        )
    
    def _save_metadata(self, date_str: str, metadata: DayMetadata):
        """Save metadata to JSON file."""
        _, meta_path = self._get_paths(date_str)
        
        with open(meta_path, 'w') as f:
            json.dump(metadata.to_dict(), f, indent=2)
    
    def write_minute(
        self,
        minute_utc: float,
        decimated_iq: np.ndarray,
        d_clock_ms: float = 0.0,
        uncertainty_ms: float = 999.0,
        quality_grade: str = 'X',
        gap_samples: int = 0
    ) -> bool:
        """
        Write one minute of decimated data to buffer.
        
        Args:
            minute_utc: UTC timestamp of minute start
            decimated_iq: Complex64 array of 600 samples (10 Hz × 60s)
            d_clock_ms: D_clock correction in milliseconds
            uncertainty_ms: Timing uncertainty
            quality_grade: Quality grade (A-X)
            gap_samples: Number of gap samples in source data
            
        Returns:
            True if write succeeded
        """
        # Determine date and minute index
        dt = datetime.fromtimestamp(minute_utc, tz=timezone.utc)
        date_str = dt.strftime('%Y%m%d')
        minute_index = dt.hour * 60 + dt.minute
        
        # Validate input
        if len(decimated_iq) != SAMPLES_PER_MINUTE:
            logger.warning(f"Expected {SAMPLES_PER_MINUTE} samples, got {len(decimated_iq)}")
            # Pad or truncate
            if len(decimated_iq) < SAMPLES_PER_MINUTE:
                padded = np.zeros(SAMPLES_PER_MINUTE, dtype=np.complex64)
                padded[:len(decimated_iq)] = decimated_iq
                decimated_iq = padded
            else:
                decimated_iq = decimated_iq[:SAMPLES_PER_MINUTE]
        
        decimated_iq = decimated_iq.astype(np.complex64)
        
        bin_path, _ = self._get_paths(date_str)
        
        try:
            # Create file if needed, preallocated to full day size
            if not bin_path.exists():
                self._create_day_file(bin_path)
            
            # Write at correct offset with file locking
            byte_offset = minute_index * SAMPLES_PER_MINUTE * BYTES_PER_SAMPLE
            
            with open(bin_path, 'r+b') as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                try:
                    f.seek(byte_offset)
                    f.write(decimated_iq.tobytes())
                finally:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            
            # Update metadata
            metadata = self._load_metadata(date_str)
            metadata.minutes[str(minute_index)] = MinuteMetadata(
                minute_index=minute_index,
                utc_timestamp=minute_utc,
                d_clock_ms=d_clock_ms,
                uncertainty_ms=uncertainty_ms,
                quality_grade=quality_grade,
                gap_samples=gap_samples,
                valid=True
            ).to_dict()
            self._save_metadata(date_str, metadata)
            
            logger.debug(f"Wrote minute {minute_index} for {date_str} ({self.channel_name})")
            return True
            
        except Exception as e:
            logger.error(f"Error writing minute {minute_index}: {e}")
            return False
    
    def _create_day_file(self, bin_path: Path):
        """Create a preallocated day file filled with zeros."""
        file_size = SAMPLES_PER_DAY * BYTES_PER_SAMPLE  # 6.9 MB
        
        with open(bin_path, 'wb') as f:
            # Write in chunks to avoid memory issues
            chunk_size = SAMPLES_PER_MINUTE * BYTES_PER_SAMPLE * 60  # 1 hour at a time
            zeros = np.zeros(SAMPLES_PER_MINUTE * 60, dtype=np.complex64)
            for _ in range(24):
                f.write(zeros.tobytes())
        
        logger.info(f"Created day file: {bin_path} ({file_size / 1e6:.1f} MB)")
    
    def read_minute(self, minute_utc: float) -> Tuple[Optional[np.ndarray], Optional[Dict]]:
        """
        Read one minute of decimated data.
        
        Returns:
            Tuple of (iq_samples, minute_metadata) or (None, None)
        """
        dt = datetime.fromtimestamp(minute_utc, tz=timezone.utc)
        date_str = dt.strftime('%Y%m%d')
        minute_index = dt.hour * 60 + dt.minute
        
        bin_path, _ = self._get_paths(date_str)
        
        if not bin_path.exists():
            return None, None
        
        try:
            byte_offset = minute_index * SAMPLES_PER_MINUTE * BYTES_PER_SAMPLE
            
            with open(bin_path, 'rb') as f:
                f.seek(byte_offset)
                data = f.read(SAMPLES_PER_MINUTE * BYTES_PER_SAMPLE)
            
            iq = np.frombuffer(data, dtype=np.complex64)
            
            # Get metadata
            metadata = self._load_metadata(date_str)
            minute_meta = metadata.minutes.get(str(minute_index))
            
            return iq, minute_meta
            
        except Exception as e:
            logger.error(f"Error reading minute {minute_index}: {e}")
            return None, None
    
    def read_day(self, date_str: str) -> Tuple[Optional[np.ndarray], Optional[DayMetadata]]:
        """
        Read a full day of decimated data.
        
        Args:
            date_str: Date string (YYYYMMDD or YYYY-MM-DD)
            
        Returns:
            Tuple of (iq_samples, day_metadata) or (None, None)
        """
        # Normalize date format
        if '-' in date_str:
            date_str = date_str.replace('-', '')
        
        bin_path, _ = self._get_paths(date_str)
        
        if not bin_path.exists():
            logger.warning(f"No data file for {date_str}: {bin_path}")
            return None, None
        
        try:
            with open(bin_path, 'rb') as f:
                data = f.read()
            
            iq = np.frombuffer(data, dtype=np.complex64)
            metadata = self._load_metadata(date_str)
            
            logger.info(f"Read {len(iq)} samples for {date_str} ({self.channel_name})")
            return iq, metadata
            
        except Exception as e:
            logger.error(f"Error reading day {date_str}: {e}")
            return None, None
    
    def read_hours(self, hours: int = 6) -> Tuple[Optional[np.ndarray], List[Dict]]:
        """
        Read the last N hours of data (may span day boundaries).
        
        Args:
            hours: Number of hours to read
            
        Returns:
            Tuple of (iq_samples, list of minute metadata)
        """
        now = datetime.now(tz=timezone.utc)
        start_time = now - timedelta(hours=hours)
        
        samples_needed = hours * 60 * SAMPLES_PER_MINUTE
        all_samples = []
        all_metadata = []
        
        current_time = start_time
        while current_time < now:
            iq, meta = self.read_minute(current_time.timestamp())
            if iq is not None:
                all_samples.append(iq)
                all_metadata.append(meta or {})
            else:
                # Fill with zeros for missing minutes
                all_samples.append(np.zeros(SAMPLES_PER_MINUTE, dtype=np.complex64))
                all_metadata.append({'valid': False})
            
            current_time += timedelta(minutes=1)
        
        if not all_samples:
            return None, []
        
        combined = np.concatenate(all_samples)
        return combined, all_metadata
    
    def get_available_dates(self) -> List[str]:
        """Get list of dates with data available."""
        dates = []
        for bin_file in self.buffer_dir.glob('????????.bin'):
            date_str = bin_file.stem
            dates.append(date_str)
        return sorted(dates)
    
    def get_day_summary(self, date_str: str) -> Optional[Dict]:
        """Get summary info for a day without loading all data."""
        if '-' in date_str:
            date_str = date_str.replace('-', '')
        
        _, meta_path = self._get_paths(date_str)
        
        if not meta_path.exists():
            return None
        
        try:
            with open(meta_path, 'r') as f:
                data = json.load(f)
            return data.get('summary', {})
        except Exception:
            return None


def get_decimated_buffer(data_root: Path, channel_name: str) -> DecimatedBuffer:
    """Factory function to get a DecimatedBuffer instance."""
    return DecimatedBuffer(data_root, channel_name)

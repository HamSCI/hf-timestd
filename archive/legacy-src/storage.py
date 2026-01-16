"""
Storage manager module

Organizes recorded files in structured hierarchy and tracks processing state.
"""

import json
import logging
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, List, Optional
from dataclasses import dataclass, asdict

logger = logging.getLogger(__name__)


@dataclass
class ProcessingState:
    """Tracks processing state for a band/date"""
    date: str  # YYYYMMDD
    station_id: str
    instrument_id: str
    band: str
    files_expected: int = 1440  # 1 minute files per day
    files_received: int = 0
    files_missing: List[str] = None
    wav_24h_created: bool = False
    wav_24h_path: Optional[str] = None
    drf_created: bool = False
    drf_path: Optional[str] = None
    upload_complete: bool = False
    upload_attempts: int = 0
    last_upload_attempt: Optional[str] = None
    last_updated: Optional[str] = None
    
    def __post_init__(self):
        if self.files_missing is None:
            self.files_missing = []
        if self.last_updated is None:
            self.last_updated = datetime.now(timezone.utc).isoformat()
    
    def to_dict(self) -> Dict:
        """Convert to dictionary for JSON serialization"""
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: Dict) -> 'ProcessingState':
        """Create from dictionary"""
        return cls(**data)


class StorageManager:
    """
    Manages file organization and processing state tracking
    
    Directory structure:
        {archive_dir}/
            {YYYYMMDD}/
                {REPORTER_GRID}/
                    {RECEIVER@PSWS_ID}/
                        {BAND}/
                            YYYYMMDDTHHMMSS.wav
                            processing_state.json
    """
    
    def __init__(self, config: Dict):
        """
        Initialize storage manager
        
        Args:
            config: Configuration dictionary
        """
        self.config = config
        self.archive_dir = Path(config['recorder']['archive_dir'])
        self.station_id = config['station']['id']
        self.instrument_id = config['station']['instrument_id']
        self.callsign = config['station']['callsign']
        self.grid_square = config['station']['grid_square']
        
        # Create reporter string
        self.reporter = f"{self.callsign}_{self.grid_square}"
        
        # Create receiver string
        self.receiver = f"KA9Q_0@{self.station_id}_{self.instrument_id}"
    
    def get_band_dir(self, date: str, band: str) -> Path:
        """
        Get directory path for a specific band and date
        
        Args:
            date: Date string (YYYYMMDD)
            band: Band name (e.g., "WWV_2_5")
            
        Returns:
            Path to band directory
        """
        return self.archive_dir / date / self.reporter / self.receiver / band
    
    def get_output_dir(self, date: str, band: str) -> Path:
        """Get output directory for processed data"""
        return self.get_band_dir(date, band)
    
    def ensure_directories(self, date: str, band: str):
        """Ensure directory structure exists"""
        band_dir = self.get_band_dir(date, band)
        band_dir.mkdir(parents=True, exist_ok=True)
        logger.debug(f"Ensured directory: {band_dir}")
    
    def get_state_file(self, date: str, band: str) -> Path:
        """Get path to processing state file"""
        return self.get_band_dir(date, band) / "processing_state.json"
    
    def load_state(self, date: str, band: str) -> ProcessingState:
        """
        Load processing state from file
        
        Args:
            date: Date string (YYYYMMDD)
            band: Band name
            
        Returns:
            ProcessingState object
        """
        state_file = self.get_state_file(date, band)
        
        if state_file.exists():
            try:
                with open(state_file, 'r') as f:
                    data = json.load(f)
                return ProcessingState.from_dict(data)
            except Exception as e:
                logger.error(f"Error loading state from {state_file}: {e}")
        
        # Return new state
        return ProcessingState(
            date=date,
            station_id=self.station_id,
            instrument_id=self.instrument_id,
            band=band
        )
    
    def save_state(self, state: ProcessingState):
        """
        Save processing state to file
        
        Args:
            state: ProcessingState object
        """
        state.last_updated = datetime.now(timezone.utc).isoformat()
        state_file = self.get_state_file(state.date, state.band)
        
        try:
            state_file.parent.mkdir(parents=True, exist_ok=True)
            with open(state_file, 'w') as f:
                json.dump(state.to_dict(), f, indent=2)
            logger.debug(f"Saved state to {state_file}")
        except Exception as e:
            logger.error(f"Error saving state to {state_file}: {e}")
    
    def scan_files(self, date: str, band: str) -> List[Path]:
        """
        Scan directory for recorded files
        
        Args:
            date: Date string (YYYYMMDD)
            band: Band name
            
        Returns:
            List of file paths
        """
        band_dir = self.get_band_dir(date, band)
        
        if not band_dir.exists():
            return []
        
        # Find all .wav files (or .wv if compressed)
        wav_files = list(band_dir.glob("*.wav"))
        wv_files = list(band_dir.glob("*.wv"))
        
        return sorted(wav_files + wv_files)
    
    def update_file_counts(self, date: str, band: str) -> ProcessingState:
        """
        Update file counts in processing state
        
        Args:
            date: Date string (YYYYMMDD)
            band: Band name
            
        Returns:
            Updated ProcessingState
        """
        state = self.load_state(date, band)
        files = self.scan_files(date, band)
        
        state.files_received = len(files)
        
        # Identify missing files
        state.files_missing = self._find_missing_files(date, band, files)
        
        self.save_state(state)
        return state
    
    def _find_missing_files(self, date: str, band: str, existing_files: List[Path]) -> List[str]:
        """
        Find missing minute files for a complete day
        
        Args:
            date: Date string (YYYYMMDD)
            band: Band name
            existing_files: List of existing file paths
            
        Returns:
            List of missing file timestamps (HHMMSS)
        """
        # Extract timestamps from existing files
        existing_timestamps = set()
        for f in existing_files:
            # Parse filename: YYYYMMDDTHHMMSS.wav or .wv
            name = f.stem  # Remove extension
            if 'T' in name:
                timestamp = name.split('T')[1]  # Get HHMMSS part
                existing_timestamps.add(timestamp)
        
        # Generate expected timestamps (every minute of the day)
        expected_timestamps = set()
        for hour in range(24):
            for minute in range(60):
                timestamp = f"{hour:02d}{minute:02d}00"
                expected_timestamps.add(timestamp)
        
        # Find missing
        missing = sorted(expected_timestamps - existing_timestamps)
        
        if missing:
            logger.info(f"Found {len(missing)} missing files for {date}/{band}")
        
        return missing
    
    def mark_24h_wav_created(self, date: str, band: str, wav_path: Path):
        """Mark that 24-hour WAV file has been created"""
        state = self.load_state(date, band)
        state.wav_24h_created = True
        state.wav_24h_path = str(wav_path)
        self.save_state(state)
    
    def mark_drf_created(self, date: str, band: str, drf_path: Path):
        """Mark that Digital RF dataset has been created"""
        state = self.load_state(date, band)
        state.drf_created = True
        state.drf_path = str(drf_path)
        self.save_state(state)
    
    def mark_upload_complete(self, date: str, band: str):
        """Mark that upload has completed successfully"""
        state = self.load_state(date, band)
        state.upload_complete = True
        self.save_state(state)
    
    def mark_upload_attempted(self, date: str, band: str):
        """Mark that an upload attempt was made"""
        state = self.load_state(date, band)
        state.upload_attempts += 1
        state.last_upload_attempt = datetime.now(timezone.utc).isoformat()
        self.save_state(state)
    
    def get_dates_needing_processing(self, max_age_days: int = 7) -> List[str]:
        """
        Get list of dates that need processing
        
        Args:
            max_age_days: Maximum age of dates to consider
            
        Returns:
            List of date strings (YYYYMMDD)
        """
        dates = []
        
        if not self.archive_dir.exists():
            return dates
        
        # Scan for date directories
        for date_dir in self.archive_dir.iterdir():
            if not date_dir.is_dir():
                continue
            
            date_str = date_dir.name
            
            # Validate date format
            try:
                date_obj = datetime.strptime(date_str, "%Y%m%d")
                
                # Check age
                age_days = (datetime.now(timezone.utc).replace(tzinfo=None) - date_obj).days
                if age_days > max_age_days:
                    continue
                
                dates.append(date_str)
            except ValueError:
                continue
        
        return sorted(dates)
    
    def get_bands_for_date(self, date: str) -> List[str]:
        """
        Get list of bands that have data for a specific date
        
        Args:
            date: Date string (YYYYMMDD)
            
        Returns:
            List of band names
        """
        bands = []
        
        date_dir = self.archive_dir / date / self.reporter / self.receiver
        
        if not date_dir.exists():
            return bands
        
        for band_dir in date_dir.iterdir():
            if band_dir.is_dir():
                bands.append(band_dir.name)
        
        return sorted(bands)
    
    def cleanup_old_files(self, retention_days: int = 30):
        """
        Clean up old files after successful upload
        
        Args:
            retention_days: Keep files for this many days after upload
        """
        logger.info(f"Cleaning up files older than {retention_days} days")
        
        cutoff_date = datetime.now(timezone.utc).replace(tzinfo=None) - \
                      datetime.timedelta(days=retention_days)
        
        deleted_count = 0
        
        for date_dir in self.archive_dir.iterdir():
            if not date_dir.is_dir():
                continue
            
            try:
                date_obj = datetime.strptime(date_dir.name, "%Y%m%d")
                
                if date_obj < cutoff_date:
                    # Check if all bands are uploaded
                    bands = self.get_bands_for_date(date_dir.name)
                    all_uploaded = True
                    
                    for band in bands:
                        state = self.load_state(date_dir.name, band)
                        if not state.upload_complete:
                            all_uploaded = False
                            break
                    
                    if all_uploaded:
                        logger.info(f"Deleting old date directory: {date_dir}")
                        import shutil
                        shutil.rmtree(date_dir)
                        deleted_count += 1
            
            except Exception as e:
                logger.error(f"Error cleaning up {date_dir}: {e}")
        
        logger.info(f"Cleanup complete: deleted {deleted_count} date directories")


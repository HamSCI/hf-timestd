"""
Main application controller

Orchestrates all modules and provides daemon interface.
"""

import logging
import signal
import time
from pathlib import Path
from typing import Dict
from datetime import datetime, timezone, timedelta

from ..channel_manager import ChannelManager
from ka9q import discover_channels, ChannelInfo
from ..core import CoreRecorderManager

# Legacy alias
discover_channels_via_control = discover_channels
from .processor import get_processor
from .storage import StorageManager
# Note: Upload functionality moved to grape-recorder package
# See: https://github.com/mijahauan/grape-recorder

logger = logging.getLogger(__name__)


class SignalRecorderApp:
    """Main application controller"""

    def __init__(self, config: Dict):
        """
        Initialize application

        Args:
            config: Configuration dictionary
        """
        self.config = config
        self.running = False

        # Initialize modules
        logger.info("Initializing Signal Recorder application")

        # Initialize GRAPE recorder manager for direct RTP recording
        self.core_recorder = CoreRecorderManager(config)

        # Keep legacy storage for backward compatibility (if needed)
        self.storage = StorageManager(config)

        # Note: Upload functionality moved to grape-recorder package
        self.uploader = None

        # Setup signal handlers
        signal.signal(signal.SIGTERM, self._signal_handler)
        signal.signal(signal.SIGINT, self._signal_handler)

    def _signal_handler(self, signum, frame):
        """Handle shutdown signals"""
        logger.info(f"Received signal {signum}, shutting down")
        self.running = False

    def initialize_recorders(self):
        """Start GRAPE recorder (handles both channel creation and recording)"""
        logger.info("Initializing GRAPE recorder")

        # Check for required channels configuration
        channels_config = self.config.get('recorder', {}).get('channels', [])
        if not channels_config:
            logger.error("No channels configured in [recorder.channels]")
            return False

        logger.info(f"Found {len(channels_config)} channel configurations")

        # Check if any channels are GRAPE-enabled
        grape_channels = [ch for ch in channels_config if ch.get('processor') == 'grape' and ch.get('enabled', True)]
        if not grape_channels:
            logger.error("No GRAPE-enabled channels found. Set processor='grape' in channel configuration.")
            return False

        logger.info(f"Starting GRAPE recorder for {len(grape_channels)} channels")

        try:
            # Start GRAPE recorder (this handles channel creation and recording)
            self.core_recorder.start()
            logger.info("GRAPE recorder started successfully")
            return True

        except Exception as e:
            logger.error(f"Failed to start GRAPE recorder: {e}", exc_info=True)
            return False
    
    def run_daemon(self):
        """Run as background daemon"""
        logger.info("Starting Signal Recorder daemon")
        
        # Initialize recorders
        if not self.initialize_recorders():
            logger.error("Failed to initialize recorders")
            return
        
        self.running = True
        
        # Main loop
        last_process_check = 0
        last_upload_check = 0
        process_interval = 3600  # Check for processing every hour
        upload_interval = 600  # Process upload queue every 10 minutes
        
        try:
            while self.running:
                current_time = time.time()
                
                # Check for dates needing processing
                processor_config = self.config.get('processor', {})
                if processor_config.get('enabled', True):
                    if current_time - last_process_check >= process_interval:
                        self._process_complete_days()
                        last_process_check = current_time
                
                # Process upload queue
                if self.uploader and (current_time - last_upload_check >= upload_interval):
                    self.uploader.process_queue()
                    self.uploader.clear_completed()
                    last_upload_check = current_time
                
                # Sleep for a bit
                time.sleep(60)  # Wake up every minute
        
        except Exception as e:
            logger.error(f"Error in main loop: {e}", exc_info=True)
        
        finally:
            self.shutdown()
    
    def run_once(self, date: str):
        """
        Process a specific date (for manual/cron execution)
        
        Args:
            date: Date string (YYYYMMDD)
        """
        logger.info(f"Processing date: {date}")
        
        self._process_date(date)
        
        # Process upload queue
        if self.uploader:
            self.uploader.process_queue()
    
    def _process_complete_days(self):
        """Check for complete days and process them"""
        logger.info("Checking for complete days to process")
        
        # Get dates that might need processing (last 7 days)
        dates = self.storage.get_dates_needing_processing(max_age_days=7)
        
        # Get yesterday's date (don't process today)
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).date()
        yesterday_str = yesterday.strftime("%Y%m%d")
        
        for date in dates:
            # Only process dates up to yesterday
            if date <= yesterday_str:
                self._process_date(date)
    
    def _process_date(self, date: str):
        """
        Process all bands for a given date
        
        Args:
            date: Date string (YYYYMMDD)
        """
        logger.info(f"Processing date: {date}")
        
        # Get all bands for this date
        bands = self.storage.get_bands_for_date(date)
        
        if not bands:
            logger.warning(f"No bands found for date {date}")
            return
        
        logger.info(f"Found {len(bands)} bands for {date}: {bands}")
        
        # Process each band
        for band in bands:
            self._process_band(date, band)
    
    def _process_band(self, date: str, band: str):
        """
        Process a specific band for a date
        
        Args:
            date: Date string (YYYYMMDD)
            band: Band name
        """
        logger.info(f"Processing {date}/{band}")
        
        # Load processing state
        state = self.storage.load_state(date, band)
        
        # Skip if already uploaded
        if state.upload_complete:
            logger.info(f"Already uploaded: {date}/{band}")
            return
        
        # Skip if already processed
        if state.drf_created and state.drf_path:
            logger.info(f"Already processed: {date}/{band}, enqueueing for upload")
            if self.uploader:
                self._enqueue_upload(date, band, Path(state.drf_path))
            return
        
        # Update file counts
        state = self.storage.update_file_counts(date, band)
        
        # Get processor for this band
        processor_config = self._get_processor_config_for_band(band)
        if not processor_config:
            logger.error(f"No processor configuration found for band {band}")
            return
        
        processor_type = processor_config.get('processor', 'grape')
        processor = get_processor(
            processor_type,
            self.config.get('processor', {}).get(processor_type, {})
        )
        
        # Get files
        band_dir = self.storage.get_band_dir(date, band)
        files = self.storage.scan_files(date, band)
        
        if not files:
            logger.warning(f"No files found for {date}/{band}")
            return
        
        # Validate files
        validation = processor.validate_files(files)
        
        logger.info(f"Validation: {validation['files_found']}/{validation['files_expected']} files "
                   f"({validation['completeness']:.1f}% complete)")
        
        if not validation['valid']:
            logger.warning(f"Incomplete data for {date}/{band}, attempting repair")
            
            # Attempt repair
            if not processor.repair_gaps(band_dir, validation['missing_minutes']):
                logger.error(f"Failed to repair gaps for {date}/{band}")
                return
            
            # Re-scan files after repair
            files = self.storage.scan_files(date, band)
        
        # Process
        output_dir = self.storage.get_output_dir(date, band)
        
        metadata = {
            'date': date,
            'band': band,
            'station_id': self.config['station']['id'],
            'instrument_id': self.config['station']['instrument_id'],
            'callsign': self.config['station']['callsign'],
            'grid_square': self.config['station']['grid_square'],
        }
        
        logger.info(f"Processing {len(files)} files for {date}/{band}")
        
        result = processor.process(band_dir, output_dir, processor_config, metadata)
        
        if result:
            logger.info(f"Processing successful: {result}")
            
            # Mark as processed
            if processor.get_upload_format() == "digital_rf":
                self.storage.mark_drf_created(date, band, result)
            
            # Enqueue for upload
            if self.uploader:
                self._enqueue_upload(date, band, result)
        else:
            logger.error(f"Processing failed for {date}/{band}")
    
    def _get_processor_config_for_band(self, band: str) -> Dict:
        """Get processor configuration for a band"""
        # Find the channel config for this band
        for ch_config in self.config.get('recorder', {}).get('channels', []):
            description = ch_config.get('description', '')
            if description == band or f"{ch_config.get('frequency_hz', 0)/1e6:.3f}_MHz" == band:
                return ch_config
        
        # Return default config
        return {}
    
    def _enqueue_upload(self, date: str, band: str, dataset_path: Path):
        """Enqueue dataset for upload"""
        if not self.uploader:
            return
            
        metadata = {
            'date': date,
            'band': band,
            'station_id': self.config['station']['id'],
            'instrument_id': self.config['station']['instrument_id'],
            'callsign': self.config['station']['callsign'],
            'grid_square': self.config['station']['grid_square'],
        }
        
        self.uploader.enqueue(dataset_path, metadata)
    
    def shutdown(self):
        """Shutdown application gracefully"""
        logger.info("Shutting down Signal Recorder")

        # Stop GRAPE recorder
        if hasattr(self, 'core_recorder'):
            self.core_recorder.stop()

        # Save upload queue
        if self.uploader:
            self.uploader._save_queue()

        logger.info("Shutdown complete")
    
    def get_status(self) -> Dict:
        """Get application status"""
        status = {
            'recorders': self.core_recorder.get_status() if hasattr(self, 'core_recorder') else {},
        }

        if self.uploader:
            status['uploads'] = self.uploader.get_status()

        return status



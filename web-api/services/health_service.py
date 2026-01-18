"""
System health monitoring service.

Provides system status, channel health, and process monitoring.
"""

import sys
import json
import subprocess
from pathlib import Path
from datetime import datetime
from typing import Dict, Any, List, Optional
import logging

# Add parent directory to path for hf_timestd imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent / 'src'))

from hf_timestd.io.hdf5_reader import DataProductReader

logger = logging.getLogger(__name__)


class HealthService:
    """Service for system health monitoring."""
    
    def __init__(self, data_root: Path, channels: List[Dict[str, Any]]):
        """
        Initialize health service.
        
        Args:
            data_root: Root data directory
            channels: List of channel configurations
        """
        self.data_root = Path(data_root)
        self.channels = channels
        self.status_dir = self.data_root / 'data' / 'status'
        self.phase2_dir = self.data_root / 'phase2'
    
    def get_system_health(self) -> Dict[str, Any]:
        """
        Get overall system health status.
        
        Returns:
            Dictionary with system health information
        """
        try:
            # Get channel statuses
            channel_statuses = self._get_channel_statuses()
            
            # Get process statuses
            process_statuses = self._get_process_statuses()
            
            # Determine overall status
            overall_status = self._determine_overall_status(
                channel_statuses,
                process_statuses
            )
            
            # Get disk usage
            disk_usage = self._get_disk_usage()
            
            # Calculate data completeness
            data_completeness = self._calculate_data_completeness(channel_statuses)
            
            return {
                'status': overall_status,
                'timestamp': datetime.utcnow().isoformat() + 'Z',
                'uptime': self._get_uptime(),
                'channels': channel_statuses,
                'processes': process_statuses,
                'disk_usage_percent': disk_usage,
                'data_completeness': data_completeness,
                'errors': []
            }
        
        except Exception as e:
            logger.error(f"Error getting system health: {e}")
            return {
                'status': 'error',
                'timestamp': datetime.utcnow().isoformat() + 'Z',
                'uptime': 'unknown',
                'channels': [],
                'processes': [],
                'disk_usage_percent': None,
                'data_completeness': None,
                'errors': [str(e)]
            }
    
    def _get_channel_statuses(self) -> List[Dict[str, Any]]:
        """Get status for all channels."""
        statuses = []
        
        for channel in self.channels:
            channel_name = channel['channel_name']
            freq_mhz = channel['frequency_mhz']
            
            # Default status
            status_dict = {
                'channel_name': channel_name,
                'frequency_mhz': freq_mhz,
                'status': 'inactive',
                'last_update': None,
                'carrier_snr_db': None,
                'data_quality': None,
                'completeness': None
            }
            
            try:
                # Check if channel directory exists
                channel_dir = self.phase2_dir / channel_name
                
                if not channel_dir.exists():
                    logger.debug(f"Channel directory does not exist: {channel_dir}")
                    statuses.append(status_dict)
                    continue
                
                # L2 timing_measurements are in clock_offset subdirectory
                clock_offset_dir = channel_dir / 'clock_offset'
                
                if not clock_offset_dir.exists():
                    logger.debug(f"Clock offset directory does not exist: {clock_offset_dir}")
                    status_dict['status'] = 'stale'
                    statuses.append(status_dict)
                    continue
                
                # Check if any HDF5 files exist
                h5_files = list(clock_offset_dir.glob('*_timing_measurements_*.h5'))
                if not h5_files:
                    logger.debug(f"No HDF5 files found in {clock_offset_dir}")
                    status_dict['status'] = 'stale'
                    statuses.append(status_dict)
                    continue
                
                # Try to read recent L2 timing data
                reader = DataProductReader(
                    data_dir=channel_dir,
                    product_level='L2',
                    product_name='timing_measurements',
                    channel=channel_name
                )
                
                # Get last hour of data
                end_time = datetime.utcnow().isoformat() + 'Z'
                start_time = (datetime.utcnow() - timedelta(hours=1)).isoformat() + 'Z'
                
                measurements = reader.read_time_range(start=start_time, end=end_time)
                
                if measurements:
                    latest = measurements[-1]
                    
                    # Calculate completeness as ratio of measurements to expected (60 per hour)
                    completeness = min(1.0, len(measurements) / 60.0)
                    
                    status_dict.update({
                        'status': 'active',
                        'last_update': latest.get('timestamp_utc'),
                        'carrier_snr_db': latest.get('snr_db'),
                        'data_quality': latest.get('quality_grade'),
                        'completeness': completeness
                    })
                else:
                    # No data in last hour - try to get last known data from last 24h
                    start_24h = (datetime.utcnow() - timedelta(hours=24)).isoformat() + 'Z'
                    older_measurements = reader.read_time_range(start=start_24h, end=end_time)
                    
                    if older_measurements:
                        latest = older_measurements[-1]
                        status_dict.update({
                            'status': 'stale',
                            'last_update': latest.get('timestamp_utc'),
                            'carrier_snr_db': latest.get('snr_db'),
                            'data_quality': latest.get('quality_grade'),
                            'completeness': 0.0
                        })
                    else:
                        status_dict['status'] = 'stale'
                
                statuses.append(status_dict)
            
            except Exception as e:
                logger.warning(f"Error getting status for {channel_name}: {e}")
                status_dict['status'] = 'stale'
                statuses.append(status_dict)
        
        return statuses
    
    def _get_process_statuses(self) -> List[Dict[str, Any]]:
        """Get status of key processes."""
        processes = []
        
        # Check for timestd processes with actual command patterns
        process_checks = [
            ('Recorder', 'core_recorder_v2'),
            ('Metrology', 'metrology_service'),
            ('Fusion', 'multi_broadcast_fusion'),
            ('Physics', 'physics_fusion_service'),
            ('GNSS VTEC', 'live_vtec.py'),
        ]
        
        for display_name, search_pattern in process_checks:
            try:
                # Use pgrep to check if process is running
                result = subprocess.run(
                    ['pgrep', '-f', search_pattern],
                    capture_output=True,
                    text=True
                )
                
                if result.returncode == 0:
                    pids = result.stdout.strip().split('\n')
                    # Count number of instances
                    count = len([p for p in pids if p])
                    
                    if count > 1:
                        display = f"{display_name} ({count} instances)"
                    else:
                        display = display_name
                    
                    processes.append({
                        'name': display,
                        'status': 'running',
                        'uptime': self._get_process_uptime(int(pids[0])) if pids and pids[0] else None,
                        'pid': int(pids[0]) if pids and pids[0] else None
                    })
                else:
                    processes.append({
                        'name': display_name,
                        'status': 'stopped',
                        'uptime': None,
                        'pid': None
                    })
            
            except Exception as e:
                logger.warning(f"Error checking process {display_name}: {e}")
                processes.append({
                    'name': display_name,
                    'status': 'unknown',
                    'uptime': None,
                    'pid': None
                })
        
        return processes
    
    def _get_process_uptime(self, pid: int) -> Optional[str]:
        """Get uptime for a specific process ID."""
        try:
            # Use ps to get elapsed time
            result = subprocess.run(
                ['ps', '-p', str(pid), '-o', 'etime='],
                capture_output=True,
                text=True
            )
            
            if result.returncode == 0:
                return result.stdout.strip()
        except Exception as e:
            logger.debug(f"Error getting uptime for PID {pid}: {e}")
        
        return None
    
    def _determine_overall_status(
        self,
        channels: List[Dict[str, Any]],
        processes: List[Dict[str, Any]]
    ) -> str:
        """Determine overall system status."""
        # If no channels configured, status is based on processes only
        if not channels:
            return 'degraded'
        
        # Check channel health
        active_channels = sum(1 for c in channels if c['status'] == 'active')
        total_channels = len(channels)
        
        # If we have active channels, system is at least degraded
        if active_channels > 0:
            if active_channels >= total_channels * 0.8:
                return 'healthy'
            else:
                return 'degraded'
        
        # No active channels - check if data exists at all
        has_data = any(c['status'] in ['active', 'stale'] for c in channels)
        if has_data:
            return 'degraded'
        
        # No data yet - system might be starting up
        return 'degraded'
    
    def _get_disk_usage(self) -> Optional[float]:
        """Get disk usage percentage."""
        try:
            result = subprocess.run(
                ['df', '-h', str(self.data_root)],
                capture_output=True,
                text=True
            )
            
            if result.returncode == 0:
                lines = result.stdout.strip().split('\n')
                if len(lines) > 1:
                    fields = lines[1].split()
                    if len(fields) >= 5:
                        usage_str = fields[4].rstrip('%')
                        return float(usage_str)
        
        except Exception as e:
            logger.warning(f"Error getting disk usage: {e}")
        
        return None
    
    def _calculate_data_completeness(
        self,
        channels: List[Dict[str, Any]]
    ) -> Optional[float]:
        """Calculate overall data completeness."""
        completeness_values = [
            c['completeness'] 
            for c in channels 
            if c['completeness'] is not None
        ]
        
        if completeness_values:
            return sum(completeness_values) / len(completeness_values)
        
        return None
    
    def _get_uptime(self) -> str:
        """Get system uptime."""
        try:
            result = subprocess.run(
                ['uptime', '-p'],
                capture_output=True,
                text=True
            )
            
            if result.returncode == 0:
                return result.stdout.strip()
        
        except Exception:
            pass
        
        return 'unknown'


# Fix missing import
from datetime import timedelta

"""
HDF5 Data Product Reader with Quality Filtering

Reads hf-timestd data products from HDF5 format with:
- Quality filtering (by grade, flag, confidence)
- Time range queries
- Station filtering
- Metadata access
- Automatic path resolution via DataProductRegistry
"""

import h5py
import numpy as np
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple
import logging

from hf_timestd.schemas import get_schema
from hf_timestd.data_product_registry import DataProductRegistry

logger = logging.getLogger(__name__)


class DataProductReader:
    """
    HDF5 data product reader with quality filtering.
    
    Features:
    - Read measurements with quality filtering
    - Time range queries
    - Station filtering
    - Metadata access
    - Efficient chunked reading
    
    Example:
        >>> reader = DataProductReader(
        ...     data_dir='/var/lib/timestd/phase2/WWV_10000',
        ...     product_level='L2',
        ...     product_name='timing_measurements',
        ...     channel='WWV_10000'
        ... )
        >>> measurements = reader.read_time_range(
        ...     start='2025-12-24T00:00:00Z',
        ...     end='2025-12-24T23:59:59Z',
        ...     min_quality_grade='B',
        ...     quality_flags=['GOOD', 'MARGINAL']
        ... )
    """
    
    def __init__(
        self,
        data_dir: Path,
        product_level: str,
        product_name: str,
        channel: str,
        version: str = 'v1',
        use_registry: bool = True
    ):
        """
        Initialize HDF5 data product reader.
        
        Args:
            data_dir: Directory containing HDF5 files (can be channel dir or specific subdir)
            product_level: Data product level (L1, L2, L3)
            product_name: Product name (e.g., 'timing_measurements')
            channel: Channel name (e.g., 'WWV_10000')
            version: Schema version (default: 'v1')
            use_registry: If True, use DataProductRegistry to resolve subdirectory (default: True)
        """
        self.product_level = product_level
        self.product_name = product_name
        self.channel = channel
        self.version = version
        
        # Resolve data directory using registry if enabled
        if use_registry and DataProductRegistry.is_registered(product_level, product_name):
            # Check if data_dir looks like a channel directory (has subdirectories)
            # or if it's already pointing to a specific subdirectory
            subdirectory = DataProductRegistry.get_subdirectory(product_level, product_name)
            
            if subdirectory:
                # Check if data_dir already points to the subdirectory
                if data_dir.name == subdirectory:
                    # Already pointing to correct subdirectory
                    self.data_dir = Path(data_dir)
                else:
                    # Assume data_dir is channel directory, resolve subdirectory
                    resolved_dir = data_dir / subdirectory
                    
                    # Fallback to root if subdirectory doesn't exist (legacy data)
                    if resolved_dir.exists():
                        self.data_dir = resolved_dir
                        logger.debug(f"Using registry-resolved path: {resolved_dir}")
                    else:
                        self.data_dir = Path(data_dir)
                        logger.debug(f"Subdirectory {subdirectory} not found, using root: {data_dir}")
            else:
                # Product stored in root
                self.data_dir = Path(data_dir)
        else:
            # Registry disabled or product not registered
            self.data_dir = Path(data_dir)
        
        # Load schema
        self.schema = get_schema(product_level, product_name, version)
        logger.info(
            f"Initialized {product_level} {product_name} reader for {channel} "
            f"(schema v{self.schema['schema_version']}, data_dir={self.data_dir.name})"
        )
    
    def _get_hdf5_path(self, date_str: str) -> Path:
        """
        Get HDF5 file path for a given date.
        
        Args:
            date_str: Date string in YYYYMMDD format
            
        Returns:
            Path to HDF5 file
        """
        filename = f"{self.channel}_{self.product_name}_{date_str}.h5"
        return self.data_dir / filename
    
    def _get_date_range(self, start: str, end: str) -> List[str]:
        """
        Get list of date strings between start and end.
        
        Args:
            start: ISO 8601 start timestamp
            end: ISO 8601 end timestamp
            
        Returns:
            List of date strings in YYYYMMDD format
        """
        start_dt = datetime.fromisoformat(start.replace('Z', '+00:00'))
        end_dt = datetime.fromisoformat(end.replace('Z', '+00:00'))
        
        dates = []
        current_dt = start_dt.replace(hour=0, minute=0, second=0, microsecond=0)
        
        while current_dt <= end_dt:
            dates.append(current_dt.strftime('%Y%m%d'))
            current_dt += timedelta(days=1)
        
        return dates
    
    def read_file_metadata(self, date_str: str) -> Dict[str, Any]:
        """
        Read file-level metadata for a given date.
        
        Args:
            date_str: Date string in YYYYMMDD format
            
        Returns:
            Dictionary of file metadata
            
        Raises:
            FileNotFoundError: If HDF5 file doesn't exist
        """
        hdf5_path = self._get_hdf5_path(date_str)
        
        if not hdf5_path.exists():
            raise FileNotFoundError(f"HDF5 file not found: {hdf5_path}")
        
        with h5py.File(hdf5_path, 'r', libver='latest', swmr=True) as f:
            metadata = dict(f.attrs)
        return metadata
    
    def read_time_range(
        self,
        start: str,
        end: str,
        min_quality_grade: Optional[str] = None,
        quality_flags: Optional[List[str]] = None,
        min_confidence: Optional[float] = None,
        station: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        Read measurements within a time range with quality filtering.
        
        Args:
            start: ISO 8601 start timestamp
            end: ISO 8601 end timestamp
            min_quality_grade: Minimum quality grade ('A', 'B', 'C', 'D')
            quality_flags: Allowed quality flags (e.g., ['GOOD', 'MARGINAL'])
            min_confidence: Minimum confidence score (0-1)
            station: Filter by station (e.g., 'WWV')
            
        Returns:
            List of measurement dictionaries
        """
        measurements = []
        
        # Get date range
        dates = self._get_date_range(start, end)
        
        # Quality grade ordering
        grade_order = {'A': 0, 'B': 1, 'C': 2, 'D': 3}
        min_grade_value = grade_order.get(min_quality_grade, 3) if min_quality_grade else 3
        
        # Read from each file
        for date_str in dates:
            hdf5_path = self._get_hdf5_path(date_str)
            
            if not hdf5_path.exists():
                logger.debug(f"HDF5 file not found: {hdf5_path}")
                continue
            
            try:
                with h5py.File(hdf5_path, 'r', libver='latest', swmr=True) as f:
                    # Get number of measurements
                    if 'timestamp_utc' not in f:
                        logger.warning(f"No timestamp_utc dataset in {hdf5_path}")
                        continue
                    
                    # Read all datasets, handling corrupt trailing chunks.
                    # gzip-compressed HDF5 datasets can have a truncated final
                    # chunk if the writer was killed mid-write.  When that
                    # happens, f[name][:] raises OSError.  We binary-search
                    # for the last readable row and truncate all fields there.
                    data = {}
                    truncate_to = None
                    for field in self.schema['fields']:
                        field_name = field['name']
                        if field_name in f:
                            try:
                                data[field_name] = f[field_name][:]
                            except OSError as read_err:
                                # Corrupt trailing chunk (write-while-read race).
                                # The corrupt chunk is always the last one being
                                # written.  Use the HDF5 chunk size to find the
                                # last COMPLETE chunk boundary in O(1) — no
                                # binary search needed.
                                ds = f[field_name]
                                n = ds.shape[0]
                                chunk_size = (ds.chunks or (1024,))[0]
                                # Last complete chunk boundary
                                safe_n = (n // chunk_size) * chunk_size
                                if safe_n == 0 and n > 0:
                                    safe_n = 0  # No complete chunks at all
                                logger.warning(
                                    f"Corrupt chunk in {hdf5_path.name}/{field_name} "
                                    f"at row {safe_n}/{n} — truncating read "
                                    f"({read_err})"
                                )
                                if safe_n > 0:
                                    try:
                                        data[field_name] = ds[:safe_n]
                                    except OSError:
                                        # Even the truncated read failed — skip field
                                        data[field_name] = np.empty(0, dtype=ds.dtype)
                                        safe_n = 0
                                    if truncate_to is None or safe_n < truncate_to:
                                        truncate_to = safe_n
                                else:
                                    data[field_name] = np.empty(0, dtype=ds.dtype)
                                    truncate_to = 0
                    
                    # If any field was truncated, trim all fields to the same length
                    if truncate_to is not None and truncate_to > 0:
                        for field_name in data:
                            if len(data[field_name]) > truncate_to:
                                data[field_name] = data[field_name][:truncate_to]
                    
                    # Use timestamp_utc length as the canonical measurement count
                    # Other datasets may be empty if optional fields were never written
                    if not data or 'timestamp_utc' not in data:
                        logger.warning(f"No data fields found in {hdf5_path}")
                        continue
                    
                    # Use timestamp_utc as the canonical length
                    # Optional fields may have length 0 if never written
                    n_measurements = len(data['timestamp_utc'])
                    
                    # Filter measurements
                    for i in range(n_measurements):
                        # Build measurement dict
                        measurement = {}
                        for field_name, values in data.items():
                            # Safe access with bounds check
                            if i < len(values):
                                value = values[i]
                            else:
                                logger.warning(
                                    f"Index {i} out of bounds for field {field_name} (len={len(values)}) "
                                    f"in {hdf5_path.name}. n_meas={n_measurements}"
                                )
                                continue
                            
                            # Decode strings
                            if isinstance(value, bytes):
                                value = value.decode('utf-8')
                            
                            measurement[field_name] = value
                        
                        # Time range filter
                        # Convert ISO timestamps to datetime for proper comparison
                        # String comparison fails with fractional seconds
                        timestamp = measurement.get('timestamp_utc', '')
                        if timestamp:
                            try:
                                ts_dt = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
                                start_dt = datetime.fromisoformat(start.replace('Z', '+00:00'))
                                end_dt = datetime.fromisoformat(end.replace('Z', '+00:00'))
                                
                                if ts_dt < start_dt or ts_dt > end_dt:
                                    continue
                            except (ValueError, AttributeError):
                                # Fall back to string comparison if parsing fails
                                if timestamp < start or timestamp > end:
                                    continue
                        else:
                            continue
                        
                        # Quality grade filter
                        if min_quality_grade:
                            grade = measurement.get('quality_grade', 'D')
                            if grade_order.get(grade, 3) > min_grade_value:
                                continue
                        
                        # Quality flag filter
                        if quality_flags:
                            flag = measurement.get('quality_flag', 'BAD')
                            if flag not in quality_flags:
                                continue
                        
                        # Confidence filter
                        if min_confidence is not None:
                            confidence = measurement.get('confidence', 0.0)
                            if confidence < min_confidence:
                                continue
                        
                        # Station filter
                        if station:
                            meas_station = measurement.get('station', '')
                            if meas_station != station:
                                continue
                        
                        measurements.append(measurement)
            
            except Exception as e:
                logger.error(f"Error reading {hdf5_path}: {e}")
                continue
        
        logger.info(
            f"Read {len(measurements)} measurements from {start} to {end} "
            f"(quality_grade >= {min_quality_grade}, flags={quality_flags})"
        )
        
        return measurements
    
    def get_quality_summary(self, date_str: str) -> Dict[str, Any]:
        """
        Get quality summary statistics for a given date.
        
        Args:
            date_str: Date string in YYYYMMDD format
            
        Returns:
            Dictionary with quality statistics
        """
        hdf5_path = self._get_hdf5_path(date_str)
        
        if not hdf5_path.exists():
            raise FileNotFoundError(f"HDF5 file not found: {hdf5_path}")
        
        with h5py.File(hdf5_path, 'r', libver='latest', swmr=True) as f:
            # Get datasets
            quality_grades = f.get('quality_grade', None)
            quality_flags = f.get('quality_flag', None)
            confidence = f.get('confidence', None)
            uncertainty = f.get('uncertainty_ms', None)
            
            summary = {
                'total_measurements': 0,
                'grade_distribution': {},
                'flag_distribution': {},
                'mean_confidence': 0.0,
                'mean_uncertainty_ms': 0.0,
            }
            
            if quality_grades is not None:
                summary['total_measurements'] = quality_grades.shape[0]
                
                # Grade distribution
                grades, counts = np.unique(quality_grades[:], return_counts=True)
                for grade, count in zip(grades, counts):
                    if isinstance(grade, bytes):
                        grade = grade.decode('utf-8')
                    summary['grade_distribution'][grade] = int(count)
            
            if quality_flags is not None:
                # Flag distribution
                flags, counts = np.unique(quality_flags[:], return_counts=True)
                for flag, count in zip(flags, counts):
                    if isinstance(flag, bytes):
                        flag = flag.decode('utf-8')
                    summary['flag_distribution'][flag] = int(count)
            
            if confidence is not None:
                summary['mean_confidence'] = float(np.mean(confidence[:]))
            
            if uncertainty is not None:
                summary['mean_uncertainty_ms'] = float(np.mean(uncertainty[:]))
        
        return summary
    
    def list_available_dates(self) -> List[str]:
        """
        List all available dates with HDF5 files.
        
        Returns:
            List of date strings in YYYYMMDD format
        """
        pattern = f"{self.channel}_{self.product_name}_*.h5"
        files = sorted(self.data_dir.glob(pattern))
        
        dates = []
        for file_path in files:
            # Extract date from filename
            parts = file_path.stem.split('_')
            if len(parts) >= 3:
                date_str = parts[-1]  # Last part is date
                dates.append(date_str)
        
        return dates

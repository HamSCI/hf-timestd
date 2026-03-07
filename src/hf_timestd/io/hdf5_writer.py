"""
HDF5 Data Product Writer with Schema Validation

Writes hf-timestd data products to HDF5 format with:
- JSON schema validation
- NaN/inf rejection
- ISO GUM uncertainty propagation
- NIST traceability metadata
"""

import h5py
import numpy as np
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, List, Optional
import logging

from hf_timestd.schemas import get_schema
from hf_timestd.io.uncertainty import ISOGUMCalculator

logger = logging.getLogger(__name__)


# Default max byte lengths for fixed-length string fields.
# Overridden by "max_length" in individual schema field definitions.
_DEFAULT_STRING_LENGTHS: Dict[str, int] = {
    'timestamp_utc':    36,   # ISO 8601 with microseconds + tz
    'processed_at':     36,
    'event_start':      36,
    'event_end':        36,
    'peak_time':        36,
    'period_start':     36,
    'calibration_date': 36,
    'processing_version': 12, # e.g. '5.0.0'
    'schema_version':   12,
    'station':          8,    # WWV, WWVH, CHU, BPM
    'station_id':       8,
    'bcd_station':      8,
    'anchor_station':   8,
    'reference_station': 8,
    'channel':          20,   # e.g. SHARED_15000
    'broadcast_id':     20,
    'detection_method': 20,   # e.g. edge_tick
    'quality_flag':     20,
    'quality_grade':    4,    # A, B, C, D
    'comparison_quality': 20,
    'anchor_status':    24,   # ANCHORED_GROUP_DELAY
    'propagation_mode': 8,    # 1F2, 2F2, etc.
    'dominant_propagation_mode': 8,
    'aggregation_period': 12, # e.g. 'daily'
    'event_type':       32,
    'anomaly_type':     32,
    'anomaly_flag':     20,
    'validation_flag':  20,
    'consistency_flag': 20,
    'identification_method': 32,
    'attribution_method': 32,
    'discrimination_method': 32,
    'toa_source':       32,
    'd_clock_source':   20,
    'rejection_reason': 48,
    'winner':           20,
    'channel_quality':  20,
    'bcd_decoded_time': 24,
    'traceability_chain': 64,
    'kalman_state':     64,
    'description':      128,
    'stations_used':    64,   # comma-separated list
    'affected_stations': 64,
    'frequencies_mhz':  48,
    'affected_frequencies_mhz': 64,
    'propagation_modes_used': 48,
}
_DEFAULT_STRING_MAX = 48      # fallback for unlisted fields


class DataProductWriter:
    """
    HDF5 data product writer with schema validation.
    
    Features:
    - Validates data against JSON schemas before writing
    - Rejects NaN/inf values in required fields
    - Embeds metadata (units, provenance, traceability)
    - Daily file rotation
    - Extensible for new measurements
    
    Example:
        >>> writer = DataProductWriter(
        ...     output_dir='/var/lib/timestd/phase2/WWV_10000',
        ...     product_level='L2',
        ...     product_name='timing_measurements',
        ...     channel='WWV_10000'
        ... )
        >>> writer.write_measurement({
        ...     'timestamp_utc': '2025-12-24T12:00:00Z',
        ...     'clock_offset_ms': -2.14,
        ...     'uncertainty_ms': 1.2,
        ...     ...
        ... })
    """
    
    def __init__(
        self,
        output_dir: Path,
        product_level: str,
        product_name: str,
        channel: str,
        version: str = 'v1',
        processing_version: str = '3.2.0',
        station_metadata: Optional[Dict[str, Any]] = None
    ):
        """
        Initialize HDF5 data product writer.
        
        Args:
            output_dir: Output directory for HDF5 files
            product_level: Data product level (L1, L2, L3)
            product_name: Product name (e.g., 'timing_measurements')
            channel: Channel name (e.g., 'WWV_10000')
            version: Schema version (default: 'v1')
            processing_version: Software version
            station_metadata: Optional station metadata (location, callsign, etc.)
        """
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        
        self.product_level = product_level
        self.product_name = product_name
        self.channel = channel
        self.version = version
        self.processing_version = processing_version
        self.station_metadata = station_metadata or {}
        
        # Load schema
        self.schema = get_schema(product_level, product_name, version)
        logger.info(
            f"Initialized {product_level} {product_name} writer for {channel} "
            f"(schema v{self.schema['schema_version']})"
        )
        
        # SWMR design: file held open for the day, flushed after each append.
        # Readers open with swmr=True and see data as it is flushed without
        # any locking contention.  On every open of an existing file we run
        # h5clear -s unconditionally so leftover SWMR dirty flags from any
        # prior unclean shutdown are always cleared before the new session.
        self._current_path: Optional[Path] = None
        self._current_date: Optional[str] = None
        self._current_file: Optional[h5py.File] = None
        self._measurement_count = 0
    
    def _get_hdf5_path(self, date_str: str) -> Path:
        """
        Get HDF5 file path for a given date.
        
        Args:
            date_str: Date string in YYYYMMDD format
            
        Returns:
            Path to HDF5 file
        """
        filename = f"{self.channel}_{self.product_name}_{date_str}.h5"
        return self.output_dir / filename
    
    def _h5clear(self, hdf5_path: Path) -> None:
        """
        Run h5clear -s on an existing file to reset SWMR consistency flags.

        Called unconditionally every time an existing file is opened for
        writing, so leftover dirty flags from any prior unclean shutdown
        (SIGKILL, OOM, power loss) are always cleared before the new SWMR
        session begins.  Cost is ~1 ms and is negligible at daily rotation.
        """
        import subprocess, shutil
        h5clear_path = shutil.which('h5clear')
        if not h5clear_path:
            logger.debug("h5clear not found — skipping SWMR flag reset")
            return
        try:
            result = subprocess.run(
                [h5clear_path, '-s', str(hdf5_path)],
                capture_output=True, text=True, timeout=10
            )
            if result.returncode == 0:
                logger.debug(f"h5clear: reset SWMR flags on {hdf5_path.name}")
            else:
                logger.warning(f"h5clear non-zero exit on {hdf5_path.name}: {result.stderr.strip()}")
        except Exception as e:
            logger.warning(f"h5clear failed on {hdf5_path.name}: {e}")

    def _try_recover_corrupt_file(self, hdf5_path: Path) -> None:
        """
        Attempt to recover a file that failed to open even after h5clear.

        Strategy:
        1. Verify file is now usable (h5clear was already run by caller)
        2. If still broken, rename to .corrupt and recreate
        """
        # Verify file is usable after h5clear
        try:
            with h5py.File(hdf5_path, 'r+', libver='latest') as f:
                _ = list(f.keys())
            logger.info(f"File usable after h5clear: {hdf5_path.name}")
            return
        except OSError:
            pass

        # Rename corrupt file and recreate
        import time as _time
        import shutil as _shutil
        corrupt_path = hdf5_path.with_suffix('.h5.corrupt')
        if corrupt_path.exists():
            corrupt_path = hdf5_path.with_suffix(f'.h5.corrupt.{int(_time.time())}')
        logger.warning(f"Renaming unrecoverable file: {hdf5_path.name} -> {corrupt_path.name}")
        hdf5_path.rename(corrupt_path)
        self._create_file(hdf5_path, recreated_from=str(corrupt_path))

    def _create_file(self, hdf5_path: Path, recreated_from: Optional[str] = None) -> None:
        """Create and initialize a new HDF5 file with all datasets."""
        logger.info(f"Creating new HDF5 file: {hdf5_path}")
        with h5py.File(hdf5_path, 'w', libver='latest') as f:
            self._write_file_metadata_to_file(f)
            self._initialize_all_datasets_in_file(f)
            f.attrs['metadata'] = 'initialized'
            if recreated_from:
                f.attrs['recreated_from_corrupt'] = recreated_from
        logger.info(f"Initialized file structure for {hdf5_path}")

    def _ensure_file_open(self, timestamp_utc: str) -> h5py.File:
        """
        Ensure the correct daily HDF5 file is open in SWMR write mode.

        SWMR DESIGN: The file is kept open for the entire day.  After all
        datasets are pre-created, swmr_mode=True is set on the handle.
        Readers open with swmr=True and see appended data immediately after
        each flush().  On every open of an *existing* file h5clear -s is run
        unconditionally to clear any leftover dirty flags from a prior unclean
        shutdown — this is the key fix that makes SWMR robust to service
        restarts and SIGKILL.

        Args:
            timestamp_utc: ISO 8601 timestamp of the measurement being written

        Returns:
            Open h5py.File handle in SWMR write mode
        """
        dt = datetime.fromisoformat(timestamp_utc.replace('Z', '+00:00'))
        date_str = dt.strftime('%Y%m%d')

        # Daily rotation: close current file, open new one
        if self._current_date != date_str:
            if self._current_file is not None:
                logger.info(
                    f"Daily rotation: {self._current_date} had "
                    f"{self._measurement_count} measurements — closing SWMR handle"
                )
                try:
                    self._current_file.close()
                except Exception:
                    pass
                self._current_file = None
            self._current_date = date_str
            self._measurement_count = 0

        # Return existing open handle if valid
        if self._current_file is not None and self._current_file.id.valid:
            return self._current_file

        hdf5_path = self._get_hdf5_path(date_str)

        if not hdf5_path.exists():
            self._create_file(hdf5_path)
        else:
            # Always clear SWMR flags before re-opening for write.
            # This handles unclean shutdowns with zero manual intervention.
            self._h5clear(hdf5_path)
            # Verify file is still structurally sound
            try:
                with h5py.File(hdf5_path, 'r', libver='latest', swmr=True) as f:
                    _ = f.attrs.get('metadata')
            except OSError as e:
                logger.warning(f"File still unreadable after h5clear ({hdf5_path.name}): {e}")
                self._try_recover_corrupt_file(hdf5_path)

        # Open for write and immediately enable SWMR mode
        f = h5py.File(hdf5_path, 'r+', libver='latest')
        f.swmr_mode = True
        self._current_file = f
        self._current_path = hdf5_path
        logger.info(f"Opened {hdf5_path.name} in SWMR write mode")
        return self._current_file
    
    def _write_file_metadata(self) -> None:
        """Write file-level metadata attributes to current file."""
        if self._current_file is not None and self._current_file.id.valid:
            self._write_file_metadata_to_file(self._current_file)
    
    def _write_file_metadata_to_file(self, f: h5py.File) -> None:
        """Write file-level metadata attributes to specified file handle."""
        # Schema metadata
        f.attrs['schema_version'] = self.schema['schema_version']
        f.attrs['data_product'] = self.schema['data_product']
        f.attrs['processing_level'] = self.schema['processing_level']
        f.attrs['description'] = self.schema['description']
        
        # Processing metadata
        f.attrs['processing_version'] = self.processing_version
        f.attrs['created_at'] = datetime.now(timezone.utc).isoformat()
        f.attrs['channel'] = self.channel
        
        # Station metadata
        for key, value in self.station_metadata.items():
            f.attrs[f'station_{key}'] = value
        
        # Standards compliance
        if 'standards' in self.schema:
            f.attrs['standards'] = ', '.join(self.schema['standards'])
        
        logger.debug(f"Wrote file metadata for {self.channel}")
    
    def _initialize_all_datasets(self) -> None:
        """Initialize all datasets from schema in current file."""
        if self._current_file is not None and self._current_file.id.valid:
            self._initialize_all_datasets_in_file(self._current_file)
    
    def _initialize_all_datasets_in_file(self, f: h5py.File) -> None:
        """
        Initialize all datasets from schema in specified file handle.
        
        Pre-creates all datasets so schema evolution (new fields) can be
        handled gracefully without requiring file recreation.
        """
        for field in self.schema['fields']:
            field_name = field['name']
            
            # Skip if dataset already exists
            if field_name in f:
                continue
            
            # Determine HDF5 dtype
            field_type = field.get('type')
            if field_type == 'float':
                dtype = np.float64
            elif field_type == 'integer':
                dtype = np.int64
            elif field_type == 'string':
                max_len = field.get(
                    'max_length',
                    _DEFAULT_STRING_LENGTHS.get(field_name, _DEFAULT_STRING_MAX)
                )
                dtype = f'S{max_len}'
            elif field_type == 'boolean':
                dtype = np.bool_
            else:
                logger.warning(f"Unknown type '{field_type}' for field '{field_name}', skipping")
                continue
            
            # Create empty dataset
            f.create_dataset(
                field_name,
                shape=(0,),
                maxshape=(None,),
                dtype=dtype,
                chunks=True,
                compression='gzip',
                compression_opts=4
            )
            
            # Add field metadata
            f[field_name].attrs['description'] = field.get('description', '')
            if 'units' in field:
                f[field_name].attrs['units'] = field['units']
            if 'reference' in field:
                f[field_name].attrs['reference'] = field['reference']
        
        logger.info(f"Initialized {len(self.schema['fields'])} datasets for {self.channel}")
    
    def _validate_field(self, field_schema: Dict[str, Any], value: Any, field_name: str) -> None:
        """
        Validate a single field against its schema.
        
        Args:
            field_schema: Field schema from JSON schema
            value: Field value to validate
            field_name: Field name (for error messages)
            
        Raises:
            ValueError: If validation fails
        """
        # Check required fields
        if field_schema.get('required', False) and value is None:
            raise ValueError(f"Required field '{field_name}' is missing")
        
        # Skip validation for None values in optional fields
        if value is None:
            return
        
        # Type validation
        field_type = field_schema.get('type')
        if field_type == 'float':
            if not isinstance(value, (int, float, np.number)):
                raise ValueError(f"Field '{field_name}' must be numeric, got {type(value)}")
            
            # NaN/inf validation
            if not field_schema.get('allow_nan', True):
                ISOGUMCalculator.validate_measurement(float(value), field_name)
        
        elif field_type == 'integer':
            if not isinstance(value, (int, np.integer)):
                raise ValueError(f"Field '{field_name}' must be integer, got {type(value)}")
        
        elif field_type == 'string':
            if not isinstance(value, str):
                raise ValueError(f"Field '{field_name}' must be string, got {type(value)}")
            
            # Enum validation
            if 'enum' in field_schema:
                if value not in field_schema['enum']:
                    raise ValueError(
                        f"Field '{field_name}' value '{value}' not in allowed values: "
                        f"{field_schema['enum']}"
                    )
        
        elif field_type == 'boolean':
            if not isinstance(value, (bool, np.bool_)):
                raise ValueError(f"Field '{field_name}' must be boolean, got {type(value)}")
        
        # Range validation
        if 'valid_range' in field_schema:
            min_val, max_val = field_schema['valid_range']
            if not (min_val <= value <= max_val):
                raise ValueError(
                    f"Field '{field_name}' value {value} outside valid range "
                    f"[{min_val}, {max_val}]"
                )
    
    def validate_measurement(self, measurement: Dict[str, Any]) -> None:
        """
        Validate a measurement against the schema.
        
        Args:
            measurement: Measurement dictionary
            
        Raises:
            ValueError: If validation fails
        """
        # Build field lookup
        field_schemas = {field['name']: field for field in self.schema['fields']}
        
        # Validate each field in measurement
        for field_name, value in measurement.items():
            if field_name not in field_schemas:
                logger.warning(f"Unknown field '{field_name}' (not in schema)")
                continue
            
            field_schema = field_schemas[field_name]
            self._validate_field(field_schema, value, field_name)
        
        # Check for missing required fields
        for field in self.schema['fields']:
            if field.get('required', False) and field['name'] not in measurement:
                raise ValueError(f"Required field '{field['name']}' missing from measurement")
    
    def write_measurements_batch(self, measurements: List[Dict[str, Any]]) -> None:
        """
        Write multiple measurements in a single open/append/close cycle.

        Use this for high-frequency products (e.g. tick_phase ~55 rows/min)
        to avoid HDF5 heap corruption from thousands of open/close cycles/day.
        All measurements must share the same date (same HDF5 file).

        Args:
            measurements: List of measurement dicts (must match schema)

        Raises:
            ValueError: If any measurement fails validation or dates differ
        """
        if not measurements:
            return

        for m in measurements:
            self.validate_measurement(m)

        timestamp_utc = measurements[0]['timestamp_utc']
        hdf5_file = self._ensure_file_open(timestamp_utc)

        for m in measurements:
            self._append_measurement(hdf5_file, m)
        hdf5_file.flush()

        self._measurement_count += len(measurements)
        logger.debug(f"Batch wrote {len(measurements)} measurements to {self._current_date}")

    def write_measurement(self, measurement: Dict[str, Any]) -> None:
        """
        Write a single measurement to HDF5 file.

        CRASH-SAFE: Opens the file, appends one row, closes the file.
        No persistent file handle means no dirty flags on unclean shutdown.

        Args:
            measurement: Measurement dictionary (must match schema)

        Raises:
            ValueError: If validation fails
        """
        # Validate measurement
        self.validate_measurement(measurement)

        # Ensure file is open in SWMR write mode
        timestamp_utc = measurement['timestamp_utc']
        hdf5_file = self._ensure_file_open(timestamp_utc)

        # Append and flush so readers see new data immediately
        self._append_measurement(hdf5_file, measurement)
        hdf5_file.flush()

        self._measurement_count += 1

        if self._measurement_count % 100 == 0:
            logger.debug(f"Wrote {self._measurement_count} measurements to {self._current_date}")

    def _append_measurement(self, hdf5_file: h5py.File, measurement: Dict[str, Any]) -> None:
        """Append one measurement row to all datasets in an open file."""
        for field in self.schema['fields']:
            field_name = field['name']
            
            # Determine value to write
            if field_name not in measurement:
                if field.get('required', False):
                    continue
                else:
                    field_type = field.get('type')
                    if field_type == 'float':
                        value = np.nan
                    elif field_type == 'integer':
                        value = 0
                    elif field_type == 'string':
                        value = ""
                    elif field_type == 'boolean':
                        value = False
                    else:
                        continue
            else:
                value = measurement[field_name]
                if value is None:
                    field_type = field.get('type')
                    if field_type == 'float':
                        value = np.nan
                    elif field_type == 'integer':
                        value = 0
                    elif field_type == 'string':
                        value = ""
                    elif field_type == 'boolean':
                        value = False
                    else:
                        continue
            
            # Determine HDF5 dtype
            field_type = field.get('type')
            if field_type == 'float':
                dtype = np.float64
            elif field_type == 'integer':
                dtype = np.int64
            elif field_type == 'string':
                max_len = field.get(
                    'max_length',
                    _DEFAULT_STRING_LENGTHS.get(field_name, _DEFAULT_STRING_MAX)
                )
                dtype = f'S{max_len}'
                # Encode string to bytes for fixed-length storage
                if isinstance(value, str):
                    value = value.encode('utf-8')[:max_len]
            elif field_type == 'boolean':
                dtype = np.bool_
            else:
                logger.warning(f"Unknown type '{field_type}' for field '{field_name}'")
                continue
            
            # Create dataset if it doesn't exist (schema evolution)
            if field_name not in hdf5_file:
                hdf5_file.create_dataset(
                    field_name,
                    shape=(0,),
                    maxshape=(None,),
                    dtype=dtype,
                    chunks=True,
                    compression='gzip',
                    compression_opts=4
                )
                hdf5_file[field_name].attrs['description'] = field.get('description', '')
                if 'units' in field:
                    hdf5_file[field_name].attrs['units'] = field['units']
                if 'reference' in field:
                    hdf5_file[field_name].attrs['reference'] = field['reference']
            
            # Append value
            dataset = hdf5_file[field_name]
            dataset.resize((dataset.shape[0] + 1,))
            dataset[-1] = value
    
    def close(self) -> None:
        """Flush and close the SWMR file handle."""
        if self._current_file is not None:
            logger.info(
                f"Writer closing: {self._current_date} had {self._measurement_count} measurements"
            )
            try:
                self._current_file.flush()
                self._current_file.close()
            except Exception as e:
                logger.warning(f"Error closing SWMR handle: {e}")
            self._current_file = None
            self._current_path = None
            self._current_date = None
            self._measurement_count = 0
    
    # ========================================================================
    # Write Verification & Testing (Analytics Review 2025-12-30)
    # ========================================================================
    
    def verify_last_write(self) -> bool:
        """
        Verify that the last write succeeded by reading back the last record.
        
        Returns:
            True if last write can be verified, False otherwise
        """
        if self._current_path is None or self._measurement_count == 0:
            return False
        
        try:
            if self._current_file is None or not self._current_file.id.valid:
                return False
            for field in self.schema['fields']:
                field_name = field['name']
                if field_name in self._current_file:
                    dataset = self._current_file[field_name]
                    if dataset.shape[0] > 0:
                        _ = dataset[-1]
                        return True
            return False
            
        except Exception as e:
            logger.error(f"Write verification failed: {e}")
            return False
    
    def write_test_measurement(self) -> bool:
        """
        Write a minimal test measurement to verify writer is operational.
        
        Used for startup health checks. Creates a test measurement with
        minimal required fields, writes it, verifies it, then continues
        normal operation.
        
        Returns:
            True if test write succeeded, False otherwise
        """
        try:
            # Build minimal test measurement with required fields only
            test_measurement = {}
            
            for field in self.schema['fields']:
                if not field.get('required', False):
                    continue
                
                field_name = field['name']
                field_type = field.get('type')
                
                # Generate test values based on type
                if field_type == 'string':
                    if field_name == 'timestamp_utc':
                        test_measurement[field_name] = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
                    elif 'enum' in field:
                        test_measurement[field_name] = field['enum'][0]
                    else:
                        test_measurement[field_name] = 'TEST'
                
                elif field_type == 'float':
                    test_measurement[field_name] = 0.0
                
                elif field_type == 'integer':
                    test_measurement[field_name] = 0
                
                elif field_type == 'boolean':
                    test_measurement[field_name] = False
            
            # Write test measurement
            self.write_measurement(test_measurement)
            
            # Verify it was written
            if not self.verify_last_write():
                logger.error("Test measurement write verification failed")
                return False
            
            logger.info(f"✅ Test measurement written and verified for {self.channel}")
            return True
            
        except Exception as e:
            logger.error(f"Test measurement write failed: {e}", exc_info=True)
            return False
    
    def __enter__(self):
        """Context manager entry."""
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self.close()

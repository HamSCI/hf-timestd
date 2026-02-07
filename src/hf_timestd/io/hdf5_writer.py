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
        
        # Daily file tracking (no persistent file handle - crash safe)
        self._current_path: Optional[Path] = None
        self._current_date: Optional[str] = None
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
    
    def _try_recover_corrupt_file(self, hdf5_path: Path) -> None:
        """
        Attempt to recover a corrupt HDF5 file.
        
        Strategy:
        1. Try h5clear to fix stale SWMR consistency flags
        2. Try to open the file to verify it's usable
        3. If still broken, rename to .corrupt and recreate
        """
        import subprocess
        import shutil
        
        # Step 1: Try h5clear if available
        h5clear_path = shutil.which('h5clear')
        if h5clear_path:
            logger.warning(f"Attempting h5clear recovery on {hdf5_path}")
            try:
                result = subprocess.run(
                    [h5clear_path, '-s', str(hdf5_path)],
                    capture_output=True, text=True, timeout=10
                )
                if result.returncode == 0:
                    logger.info(f"h5clear succeeded on {hdf5_path}")
            except Exception as e:
                logger.warning(f"h5clear failed: {e}")
        
        # Step 2: Verify file is usable
        try:
            with h5py.File(hdf5_path, 'r+', libver='latest', locking=False) as f:
                _ = list(f.keys())
            logger.info(f"File recovered successfully: {hdf5_path}")
            return
        except OSError:
            pass
        
        # Step 3: Rename corrupt file and recreate
        corrupt_path = hdf5_path.with_suffix('.h5.corrupt')
        if corrupt_path.exists():
            import time as _time
            corrupt_path = hdf5_path.with_suffix(f'.h5.corrupt.{int(_time.time())}')
        
        logger.warning(f"Renaming corrupt file: {hdf5_path} -> {corrupt_path}")
        hdf5_path.rename(corrupt_path)
        self._create_file(hdf5_path, recreated_from=str(corrupt_path))

    def _create_file(self, hdf5_path: Path, recreated_from: Optional[str] = None) -> None:
        """Create and initialize a new HDF5 file with all datasets."""
        logger.info(f"Creating new HDF5 file: {hdf5_path}")
        with h5py.File(hdf5_path, 'w', libver='latest', locking=False) as f:
            self._write_file_metadata_to_file(f)
            self._initialize_all_datasets_in_file(f)
            f.attrs['metadata'] = 'initialized'
            if recreated_from:
                f.attrs['recreated_from_corrupt'] = recreated_from
        logger.info(f"Initialized file structure for {hdf5_path}")

    def _ensure_file_exists(self, timestamp_utc: str) -> Path:
        """
        Ensure HDF5 file exists for the given timestamp (daily rotation).
        
        CRASH-SAFE DESIGN (2026-02-06): Files are NOT held open. Each
        write_measurement() call opens, appends, and closes the file.
        This eliminates SWMR dirty-flag corruption from unclean shutdowns.
        Concurrent readers work fine with libver='latest' files.
        
        Args:
            timestamp_utc: ISO 8601 timestamp
            
        Returns:
            Path to the HDF5 file (guaranteed to exist and be valid)
        """
        dt = datetime.fromisoformat(timestamp_utc.replace('Z', '+00:00'))
        date_str = dt.strftime('%Y%m%d')
        
        # Daily rotation
        if self._current_date != date_str:
            if self._current_date is not None:
                logger.info(
                    f"Daily rotation: {self._current_date} had {self._measurement_count} measurements"
                )
            self._current_date = date_str
            self._measurement_count = 0
        
        hdf5_path = self._get_hdf5_path(date_str)
        
        if not hdf5_path.exists():
            self._create_file(hdf5_path)
        else:
            # Verify existing file is readable (may be corrupt from old SWMR crash)
            try:
                with h5py.File(hdf5_path, 'r', libver='latest', locking=False) as f:
                    _ = f.attrs.get('metadata')
            except OSError as e:
                logger.warning(f"Existing file corrupt ({hdf5_path}): {e}")
                self._try_recover_corrupt_file(hdf5_path)
        
        self._current_path = hdf5_path
        return hdf5_path
    
    def _write_file_metadata(self) -> None:
        """Write file-level metadata attributes to current file."""
        if self._current_path is None or not self._current_path.exists():
            return
        with h5py.File(self._current_path, 'r+', libver='latest', locking=False) as f:
            self._write_file_metadata_to_file(f)
    
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
        if self._current_path is None or not self._current_path.exists():
            return
        with h5py.File(self._current_path, 'r+', libver='latest', locking=False) as f:
            self._initialize_all_datasets_in_file(f)
    
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
                dtype = h5py.string_dtype(encoding='utf-8')
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
        
        # Ensure file exists (creates if needed, handles daily rotation)
        timestamp_utc = measurement['timestamp_utc']
        hdf5_path = self._ensure_file_exists(timestamp_utc)
        
        # Open, append, close — crash-safe write
        with h5py.File(hdf5_path, 'r+', libver='latest', locking=False) as hdf5_file:
            self._append_measurement(hdf5_file, measurement)
        
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
                dtype = h5py.string_dtype(encoding='utf-8')
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
        """Clean up writer state. No file handle to close (crash-safe design)."""
        if self._current_date is not None:
            logger.info(
                f"Writer closing: {self._current_date} had {self._measurement_count} measurements"
            )
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
            with h5py.File(self._current_path, 'r', libver='latest', locking=False) as f:
                for field in self.schema['fields']:
                    field_name = field['name']
                    if field_name in f:
                        dataset = f[field_name]
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

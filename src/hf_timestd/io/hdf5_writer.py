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
        
        # Current file handle (daily rotation)
        self._current_file: Optional[h5py.File] = None
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
    
    def _ensure_file_open(self, timestamp_utc: str) -> h5py.File:
        """
        Ensure HDF5 file is open for the given timestamp (daily rotation).
        
        Opens file in SWMR (Single Writer Multiple Reader) mode to enable
        concurrent read access while writing.
        
        Args:
            timestamp_utc: ISO 8601 timestamp
            
        Returns:
            Open HDF5 file handle
        """
        # Extract date from timestamp
        dt = datetime.fromisoformat(timestamp_utc.replace('Z', '+00:00'))
        date_str = dt.strftime('%Y%m%d')
        
        # Check if we need to rotate to a new file
        if self._current_date != date_str:
            # Close previous file
            if self._current_file is not None:
                self._current_file.close()
                logger.info(
                    f"Closed {self._current_date} file with {self._measurement_count} measurements"
                )
            
            # Open new file with SWMR mode
            hdf5_path = self._get_hdf5_path(date_str)
            
            # Use libver='latest' for SWMR support
            # Open in append mode, will enable SWMR after initialization
            self._current_file = h5py.File(
                hdf5_path, 
                'a',
                libver='latest'  # Required for SWMR
            )
            self._current_date = date_str
            self._measurement_count = 0
            
            # Initialize file metadata if new file
            if 'metadata' not in self._current_file.attrs:
                self._write_file_metadata()
            
            # Enable SWMR mode after file is initialized
            # This allows concurrent readers to access the file
            try:
                if not self._current_file.swmr_mode:
                    self._current_file.swmr_mode = True
                    logger.info(f"Enabled SWMR mode for {hdf5_path}")
            except Exception as e:
                logger.warning(f"Could not enable SWMR mode: {e}")
            
            logger.info(f"Opened HDF5 file: {hdf5_path}")
        
        return self._current_file
    
    def _write_file_metadata(self) -> None:
        """Write file-level metadata attributes."""
        if self._current_file is None:
            return
        
        # Schema metadata
        self._current_file.attrs['schema_version'] = self.schema['schema_version']
        self._current_file.attrs['data_product'] = self.schema['data_product']
        self._current_file.attrs['processing_level'] = self.schema['processing_level']
        self._current_file.attrs['description'] = self.schema['description']
        
        # Processing metadata
        self._current_file.attrs['processing_version'] = self.processing_version
        self._current_file.attrs['created_at'] = datetime.now(timezone.utc).isoformat()
        self._current_file.attrs['channel'] = self.channel
        
        # Station metadata
        for key, value in self.station_metadata.items():
            self._current_file.attrs[f'station_{key}'] = value
        
        # Standards compliance
        if 'standards' in self.schema:
            self._current_file.attrs['standards'] = ', '.join(self.schema['standards'])
        
        logger.debug(f"Wrote file metadata for {self.channel}")
    
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
        
        Args:
            measurement: Measurement dictionary (must match schema)
            
        Raises:
            ValueError: If validation fails
        """
        # Validate measurement
        self.validate_measurement(measurement)
        
        # Ensure file is open
        timestamp_utc = measurement['timestamp_utc']
        hdf5_file = self._ensure_file_open(timestamp_utc)
        
        # Create or extend datasets
        for field in self.schema['fields']:
            field_name = field['name']
            
            # Skip if field not in measurement
            if field_name not in measurement:
                continue
            
            value = measurement[field_name]
            
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
            
            # Create dataset if it doesn't exist
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
                
                # Add field metadata
                hdf5_file[field_name].attrs['description'] = field.get('description', '')
                if 'units' in field:
                    hdf5_file[field_name].attrs['units'] = field['units']
                if 'reference' in field:
                    hdf5_file[field_name].attrs['reference'] = field['reference']
            
            # Append value to dataset
            dataset = hdf5_file[field_name]
            dataset.resize((dataset.shape[0] + 1,))
            dataset[-1] = value
        
        # Flush to disk and refresh SWMR metadata
        # In SWMR mode, flush() alone isn't enough - we need to ensure
        # the metadata is updated so readers can see the new data
        hdf5_file.flush()
        
        # Force metadata refresh for SWMR readers
        # This is critical for real-time data visibility
        if hdf5_file.swmr_mode:
            # Refresh all datasets to make new data visible to SWMR readers
            for field in self.schema['fields']:
                field_name = field['name']
                if field_name in hdf5_file:
                    hdf5_file[field_name].refresh()
        
        self._measurement_count += 1
        
        if self._measurement_count % 100 == 0:
            logger.debug(f"Wrote {self._measurement_count} measurements to {self._current_date}")
    
    def close(self) -> None:
        """Close the current HDF5 file."""
        if self._current_file is not None:
            self._current_file.close()
            logger.info(
                f"Closed {self._current_date} file with {self._measurement_count} measurements"
            )
            self._current_file = None
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
        if self._current_file is None or self._measurement_count == 0:
            return False
        
        try:
            # Check that at least one dataset exists and has data
            for field in self.schema['fields']:
                field_name = field['name']
                if field_name in self._current_file:
                    dataset = self._current_file[field_name]
                    if dataset.shape[0] > 0:
                        # Successfully read last value
                        _ = dataset[-1]
                        return True
            
            # No datasets found with data
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

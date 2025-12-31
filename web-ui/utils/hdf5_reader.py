"""
HDF5 Reader Utility for hf-timestd Monitoring Server

Provides functions to read L1A and L2 data products from HDF5 files
with quality metadata extraction and CSV fallback support.
Uses h5py for native HDF5 access with SWMR support.
"""

import h5py
import numpy as np
from pathlib import Path
from typing import Dict, List, Optional, Any
import logging

logger = logging.getLogger(__name__)


def read_l2_timing_measurements(
    hdf5_path: Path,
    max_records: Optional[int] = None,
    min_quality_grade: Optional[str] = None,
    quality_flag: Optional[str] = None
) -> Dict[str, Any]:
    """
    Read L2 timing measurements from HDF5 file
    
    Args:
        hdf5_path: Path to HDF5 file
        max_records: Maximum number of records to return
        min_quality_grade: Minimum quality grade (A/B/C/D)
        quality_flag: Filter by quality flag (GOOD/MARGINAL/BAD)
    
    Returns:
        Dictionary with measurements, statistics, and metadata
    """
    try:
        if not hdf5_path.exists():
            raise FileNotFoundError(f"HDF5 file not found: {hdf5_path}")
        
        # Open with SWMR mode for concurrent access
        with h5py.File(hdf5_path, 'r', swmr=True) as f:
            # Read required datasets
            timestamps = decode_strings(f['timestamp_utc'][:])
            clock_offsets = f['clock_offset_ms'][:]
            uncertainties = f['uncertainty_ms'][:]
            expanded_uncertainties = f['expanded_uncertainty_ms'][:]
            quality_grades = decode_strings(f['quality_grade'][:])
            quality_flags = decode_strings(f['quality_flag'][:])
            confidences = f['confidence'][:]
            stations = decode_strings(f['station'][:])
            discrimination_methods = decode_strings(f['discrimination_method'][:])
            discrimination_confidences = f['discrimination_confidence'][:]
            
            # Optional fields
            snr_db = safe_read_dataset(f, 'snr_db')
            doppler_hz = safe_read_dataset(f, 'doppler_hz')
            propagation_mode = decode_strings(safe_read_dataset(f, 'propagation_mode'))
            
            # Build measurements list
            measurements = []
            
            # SWMR Safety: Determine minimum length across all datasets
            num_records = len(timestamps)
            min_len = min(
                len(timestamps),
                len(clock_offsets),
                len(uncertainties),
                len(expanded_uncertainties),
                len(quality_grades),
                len(quality_flags),
                len(confidences),
                len(stations),
                len(discrimination_methods),
                len(discrimination_confidences)
            )
            
            # Use safe minimum length
            num_records = min_len

            
            grade_order = {'A': 0, 'B': 1, 'C': 2, 'D': 3}
            
            for i in range(num_records):
                grade = quality_grades[i]
                flag = quality_flags[i]
                
                # Apply quality filters
                if min_quality_grade and grade_order.get(grade, 99) > grade_order.get(min_quality_grade, 0):
                    continue
                
                if quality_flag and flag != quality_flag:
                    continue
                
                measurement = {
                    'timestamp': timestamps[i],
                    'clock_offset_ms': float(clock_offsets[i]),
                    'uncertainty_ms': float(uncertainties[i]),
                    'expanded_uncertainty_ms': float(expanded_uncertainties[i]),
                    'quality_grade': grade,
                    'quality_flag': flag,
                    'confidence': float(confidences[i]),
                    'station': stations[i],
                    'discrimination_method': discrimination_methods[i],
                    'discrimination_confidence': float(discrimination_confidences[i])
                }
                
                # Add optional fields if available
                # Add optional fields if available with bounds check
                if snr_db is not None and i < len(snr_db):
                    measurement['snr_db'] = float(snr_db[i])
                if doppler_hz is not None and i < len(doppler_hz):
                    measurement['doppler_hz'] = float(doppler_hz[i])
                if propagation_mode is not None and i < len(propagation_mode):
                    measurement['propagation_mode'] = propagation_mode[i]
                
                measurements.append(measurement)
                
                # Limit records if specified
                if max_records and len(measurements) >= max_records:
                    break
            
            # Calculate statistics
            valid_offsets = [m['clock_offset_ms'] for m in measurements 
                           if np.isfinite(m['clock_offset_ms'])]
            
            statistics = {
                'count': len(measurements),
                'total_records': num_records,
                'min': float(np.min(valid_offsets)) if valid_offsets else None,
                'max': float(np.max(valid_offsets)) if valid_offsets else None,
                'mean': float(np.mean(valid_offsets)) if valid_offsets else None,
                'std': float(np.std(valid_offsets)) if len(valid_offsets) > 1 else None
            }
            
            # Calculate grade distribution
            grade_distribution = {'A': 0, 'B': 0, 'C': 0, 'D': 0}
            for m in measurements:
                if m['quality_grade'] in grade_distribution:
                    grade_distribution[m['quality_grade']] += 1
            
            return {
                'measurements': measurements,
                'statistics': statistics,
                'grade_distribution': grade_distribution,
                'source': 'hdf5',
                'file_path': str(hdf5_path),
                'status': 'OK'
            }
    
    except Exception as e:
        logger.error(f"Error reading L2 HDF5 file {hdf5_path}: {e}")
        raise


def read_l1a_channel_observables(
    hdf5_path: Path,
    max_records: Optional[int] = None,
    quality_flag: Optional[str] = None
) -> Dict[str, Any]:
    """
    Read L1A channel observables from HDF5 file
    
    Args:
        hdf5_path: Path to HDF5 file
        max_records: Maximum number of records to return
        quality_flag: Filter by quality flag (GOOD/MARGINAL/BAD)
    
    Returns:
        Dictionary with records and metadata
    """
    try:
        if not hdf5_path.exists():
            raise FileNotFoundError(f"HDF5 file not found: {hdf5_path}")
        
        # Open with SWMR mode for concurrent access
        with h5py.File(hdf5_path, 'r', swmr=True) as f:
            # Read required datasets
            timestamps = decode_strings(f['timestamp_utc'][:])
            quality_flags = decode_strings(f['quality_flag'][:])
            data_completeness = f['data_completeness'][:]
            
            # Read optional observables
            observable_fields = [
                'carrier_power_db', 'carrier_snr_db', 'carrier_doppler_hz',
                'doppler_std_hz', 'coherence_time_sec', 'phase_variance_rad',
                'wwv_tone_500hz_db', 'wwv_tone_600hz_db',
                'wwvh_tone_1200hz_db', 'wwvh_tone_1500hz_db',
                'chu_tone_db'
            ]
            
            datasets = {field: safe_read_dataset(f, field) for field in observable_fields}
            
            # Build records list
            records = []
            
            # SWMR Safety: Determine minimum length
            num_records = len(timestamps)
            record_lengths = [len(timestamps), len(quality_flags), len(data_completeness)]
            
            # Add lengths of successful optional reads
            for ds in datasets.values():
                if ds is not None:
                    record_lengths.append(len(ds))
            
            num_records = min(record_lengths)

            
            for i in range(num_records):
                flag = quality_flags[i]
                
                # Apply quality filter
                if quality_flag and flag != quality_flag:
                    continue
                
                record = {
                    'timestamp': timestamps[i],
                    'quality_flag': flag,
                    'data_completeness': float(data_completeness[i])
                }
                
                # Add all available observables
                for field, dataset in datasets.items():
                    if dataset is not None:
                        value = dataset[i]
                        if np.isfinite(value):
                            record[field] = float(value)
                
                records.append(record)
                
                # Limit records if specified
                if max_records and len(records) >= max_records:
                    break
            
            return {
                'records': records,
                'count': len(records),
                'total_records': num_records,
                'source': 'hdf5',
                'file_path': str(hdf5_path),
                'status': 'OK'
            }
    
    except Exception as e:
        logger.error(f"Error reading L1A HDF5 file {hdf5_path}: {e}")
        raise

def read_l1b_discrimination(
    hdf5_path: Path,
    max_records: Optional[int] = None,
    quality_flag: Optional[str] = None
) -> Dict[str, Any]:
    """
    Read L1B discrimination results from HDF5 file
    
    Args:
        hdf5_path: Path to HDF5 file
        max_records: Maximum number of records to return
        quality_flag: Filter by quality flag (GOOD/MARGINAL/BAD)
    
    Returns:
        Dictionary with records and metadata
    """
    try:
        if not hdf5_path.exists():
            raise FileNotFoundError(f"HDF5 file not found: {hdf5_path}")
        
        # Open with SWMR mode for concurrent access
        with h5py.File(hdf5_path, 'r', swmr=True) as f:
            # Read required datasets
            timestamps = decode_strings(f['timestamp_utc'][:])
            quality_flags = decode_strings(f['quality_flag'][:])
            data_completeness = f['data_completeness'][:]
            
            # Read discrimination fields
            dominant_station = decode_strings(f['dominant_station'][:])
            confidence = f['confidence'][:]
            
            # Optional fields
            wwv_snr = safe_read_dataset(f, 'wwv_snr_db')
            wwvh_snr = safe_read_dataset(f, 'wwvh_snr_db')
            bpm_snr = safe_read_dataset(f, 'bpm_snr_db')
            chu_snr = safe_read_dataset(f, 'chu_snr_db')
            
            # Build records list
            records = []
            
            # SWMR Safety: Determine minimum length
            num_records = len(timestamps)
            record_lengths = [
                len(timestamps), 
                len(quality_flags), 
                len(data_completeness),
                len(dominant_station),
                len(confidence)
            ]
            
            # Add lengths of optional SNRs
            optionals = [wwv_snr, wwvh_snr, bpm_snr, chu_snr]
            for opt in optionals:
                if opt is not None:
                    record_lengths.append(len(opt))
            
            num_records = min(record_lengths)

            
            for i in range(num_records):
                flag = quality_flags[i]
                
                # Apply quality filter
                if quality_flag and flag != quality_flag:
                    continue
                
                record = {
                    'timestamp': timestamps[i],
                    'quality_flag': flag,
                    'data_completeness': float(data_completeness[i]),
                    'dominant_station': dominant_station[i],
                    'confidence': float(confidence[i])
                }
                
                # Add SNRs if available
                if wwv_snr is not None and i < len(wwv_snr) and np.isfinite(wwv_snr[i]): record['wwv_snr_db'] = float(wwv_snr[i])
                if wwvh_snr is not None and i < len(wwvh_snr) and np.isfinite(wwvh_snr[i]): record['wwvh_snr_db'] = float(wwvh_snr[i])
                if bpm_snr is not None and i < len(bpm_snr) and np.isfinite(bpm_snr[i]): record['bpm_snr_db'] = float(bpm_snr[i])
                if chu_snr is not None and i < len(chu_snr) and np.isfinite(chu_snr[i]): record['chu_snr_db'] = float(chu_snr[i])
                
                records.append(record)
                
                # Limit records if specified
                if max_records and len(records) >= max_records:
                    break
            
            return {
                'records': records,
                'count': len(records),
                'total_records': num_records,
                'source': 'hdf5',
                'file_path': str(hdf5_path),
                'status': 'OK'
            }
    
    except Exception as e:
        logger.error(f"Error reading L1B HDF5 file {hdf5_path}: {e}")
        raise


def safe_read_dataset(file: h5py.File, dataset_name: str) -> Optional[np.ndarray]:
    """Safely read a dataset, returning None if not found"""
    try:
        if dataset_name in file:
            return file[dataset_name][:]
        return None
    except Exception:
        return None


def decode_strings(data: np.ndarray) -> List[str]:
    """Decode HDF5 string arrays to Python strings"""
    if data.dtype.kind in ('S', 'O'):  # Byte string or object
        return [s.decode('utf-8') if isinstance(s, bytes) else str(s) for s in data]
    return [str(s) for s in data]


def get_l2_timing_path(channel_name: str, date: str, data_root: Path) -> Path:
    """
    Get HDF5 file path for L2 timing measurements
    
    Args:
        channel_name: Channel name (e.g., "WWV 10 MHz")
        date: Date in YYYYMMDD format
        data_root: Data root directory
    
    Returns:
        Path to HDF5 file
    """
    # Convert channel name to key format (e.g., "WWV 10 MHz" -> "SHARED_10000")
    channel_key = channel_name_to_key(channel_name)
    timing_dir = data_root / 'phase2' / channel_key / 'clock_offset'
    return timing_dir / f"{channel_key}_timing_measurements_{date}.h5"


def get_l1a_observables_path(channel_name: str, date: str, data_root: Path) -> Path:
    """
    Get HDF5 file path for L1A channel observables
    
    Args:
        channel_name: Channel name (e.g., "WWV 10 MHz")
        date: Date in YYYYMMDD format
        data_root: Data root directory
    
    Returns:
        Path to HDF5 file
    """
    channel_key = channel_name_to_key(channel_name)
    observables_dir = data_root / 'phase2' / channel_key / 'carrier_power'
    return observables_dir / f"{channel_key}_channel_observables_{date}.h5"


def get_l1b_discrimination_path(channel_name: str, date: str, data_root: Path) -> Path:
    """
    Get HDF5 file path for L1B discrimination results
    
    Args:
        channel_name: Channel name (e.g., "WWV 10 MHz")
        date: Date in YYYYMMDD format
        data_root: Data root directory
    
    Returns:
        Path to HDF5 file
    """
    channel_key = channel_name_to_key(channel_name)
    discrim_dir = data_root / 'phase2' / channel_key / 'bcd_timecode'
    return discrim_dir / f"{channel_key}_bcd_timecode_{date}.h5"


def channel_name_to_key(channel_name: str) -> str:
    """
    Convert channel name to directory key format
    
    Examples:
        "WWV 10 MHz" -> "SHARED_10000"
        "CHU 3.33 MHz" -> "CHU_3330"
        "WWV 5 MHz" -> "SHARED_5000"
    """
    # Extract frequency
    parts = channel_name.split()
    if len(parts) >= 2:
        try:
            freq_mhz = float(parts[1])
            freq_khz = int(freq_mhz * 1000)
            
            # Determine prefix based on station
            station = parts[0].upper()
            if station in ['WWV', 'WWVH']:
                prefix = 'SHARED'
            else:
                prefix = station
            
            return f"{prefix}_{freq_khz}"
        except (ValueError, IndexError):
            pass
    
    # Fallback: sanitize the name
    return channel_name.replace(' ', '_').replace('.', '').upper()

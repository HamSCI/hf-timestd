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
from datetime import datetime, timezone
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


def read_l3_fusion_result(
    hdf5_path: Path,
    max_records: Optional[int] = None,
    min_quality_grade: Optional[str] = None
) -> Dict[str, Any]:
    """
    Read L3 Fusion timing results from HDF5 file
    
    Args:
        hdf5_path: Path to HDF5 file (fusion_timing_YYYYMMDD.h5)
        max_records: Maximum number of records to return
        min_quality_grade: Minimum quality grade (A/B/C/D)
    
    Returns:
        Dictionary with fusion results, statistics, and metadata
    """
    try:
        if not hdf5_path.exists():
            raise FileNotFoundError(f"HDF5 file not found: {hdf5_path}")
        
        # Open with SWMR mode for concurrent access
        with h5py.File(hdf5_path, 'r', swmr=True) as f:
            # Read required datasets
            timestamps = f['timestamp'][:]
            d_clock_fused = f['d_clock_fused_ms'][:]
            d_clock_raw = f['d_clock_raw_ms'][:]
            uncertainty = f['uncertainty_ms'][:]
            n_broadcasts = f['n_broadcasts'][:]
            n_stations = f['n_stations'][:]
            quality_grades = decode_strings(f['quality_grade'][:])
            
            # Optional per-station breakdown
            wwv_mean = safe_read_dataset(f, 'wwv_mean_ms')
            wwvh_mean = safe_read_dataset(f, 'wwvh_mean_ms')
            chu_mean = safe_read_dataset(f, 'chu_mean_ms')
            bpm_mean = safe_read_dataset(f, 'bpm_mean_ms')
            
            wwv_count = safe_read_dataset(f, 'wwv_count')
            wwvh_count = safe_read_dataset(f, 'wwvh_count')
            chu_count = safe_read_dataset(f, 'chu_count')
            bpm_count = safe_read_dataset(f, 'bpm_count')
            
            # Optional consistency metrics
            wwv_intra_std = safe_read_dataset(f, 'wwv_intra_std_ms')
            wwvh_intra_std = safe_read_dataset(f, 'wwvh_intra_std_ms')
            chu_intra_std = safe_read_dataset(f, 'chu_intra_std_ms')
            bpm_intra_std = safe_read_dataset(f, 'bpm_intra_std_ms')
            
            outliers_rejected = safe_read_dataset(f, 'outliers_rejected')
            
            # SWMR Safety: Determine minimum length
            num_records = min(
                len(timestamps),
                len(d_clock_fused),
                len(d_clock_raw),
                len(uncertainty),
                len(n_broadcasts),
                len(n_stations),
                len(quality_grades)
            )
            
            # Build records list
            records = []
            grade_order = {'A': 0, 'B': 1, 'C': 2, 'D': 3}
            
            for i in range(num_records):
                grade = quality_grades[i]
                
                # Apply quality filter
                if min_quality_grade and grade_order.get(grade, 99) > grade_order.get(min_quality_grade, 0):
                    continue
                
                record = {
                    'timestamp': float(timestamps[i]),
                    'timestamp_utc': datetime.fromtimestamp(timestamps[i], timezone.utc).isoformat().replace('+00:00', 'Z'),
                    'd_clock_fused_ms': float(d_clock_fused[i]),
                    'd_clock_raw_ms': float(d_clock_raw[i]),
                    'uncertainty_ms': float(uncertainty[i]),
                    'n_broadcasts': int(n_broadcasts[i]),
                    'n_stations': int(n_stations[i]),
                    'quality_grade': grade
                }
                
                # Add per-station breakdown if available
                station_stats = {}
                if wwv_mean is not None and i < len(wwv_mean):
                    station_stats['WWV'] = {
                        'mean_ms': float(wwv_mean[i]) if np.isfinite(wwv_mean[i]) else None,
                        'count': int(wwv_count[i]) if wwv_count is not None and i < len(wwv_count) else 0,
                        'intra_std_ms': float(wwv_intra_std[i]) if wwv_intra_std is not None and i < len(wwv_intra_std) and np.isfinite(wwv_intra_std[i]) else None
                    }
                if wwvh_mean is not None and i < len(wwvh_mean):
                    station_stats['WWVH'] = {
                        'mean_ms': float(wwvh_mean[i]) if np.isfinite(wwvh_mean[i]) else None,
                        'count': int(wwvh_count[i]) if wwvh_count is not None and i < len(wwvh_count) else 0,
                        'intra_std_ms': float(wwvh_intra_std[i]) if wwvh_intra_std is not None and i < len(wwvh_intra_std) and np.isfinite(wwvh_intra_std[i]) else None
                    }
                if chu_mean is not None and i < len(chu_mean):
                    station_stats['CHU'] = {
                        'mean_ms': float(chu_mean[i]) if np.isfinite(chu_mean[i]) else None,
                        'count': int(chu_count[i]) if chu_count is not None and i < len(chu_count) else 0,
                        'intra_std_ms': float(chu_intra_std[i]) if chu_intra_std is not None and i < len(chu_intra_std) and np.isfinite(chu_intra_std[i]) else None
                    }
                if bpm_mean is not None and i < len(bpm_mean):
                    station_stats['BPM'] = {
                        'mean_ms': float(bpm_mean[i]) if np.isfinite(bpm_mean[i]) else None,
                        'count': int(bpm_count[i]) if bpm_count is not None and i < len(bpm_count) else 0,
                        'intra_std_ms': float(bpm_intra_std[i]) if bpm_intra_std is not None and i < len(bpm_intra_std) and np.isfinite(bpm_intra_std[i]) else None
                    }
                
                if station_stats:
                    record['station_stats'] = station_stats
                
                # Add outliers if available
                if outliers_rejected is not None and i < len(outliers_rejected):
                    record['outliers_rejected'] = int(outliers_rejected[i])
                
                records.append(record)
                
                # Limit records if specified
                if max_records and len(records) >= max_records:
                    break
            
            # Calculate statistics
            valid_fused = [r['d_clock_fused_ms'] for r in records if np.isfinite(r['d_clock_fused_ms'])]
            
            statistics = {
                'count': len(records),
                'total_records': num_records,
                'min': float(np.min(valid_fused)) if valid_fused else None,
                'max': float(np.max(valid_fused)) if valid_fused else None,
                'mean': float(np.mean(valid_fused)) if valid_fused else None,
                'std': float(np.std(valid_fused)) if len(valid_fused) > 1 else None
            }
            
            # Calculate grade distribution
            grade_distribution = {'A': 0, 'B': 0, 'C': 0, 'D': 0}
            for r in records:
                if r['quality_grade'] in grade_distribution:
                    grade_distribution[r['quality_grade']] += 1
            
            return {
                'records': records,
                'statistics': statistics,
                'grade_distribution': grade_distribution,
                'source': 'hdf5',
                'file_path': str(hdf5_path),
                'status': 'OK'
            }
    
    except Exception as e:
        logger.error(f"Error reading L3 Fusion HDF5 file {hdf5_path}: {e}")
        raise


def get_l3_fusion_path(date: str, data_root: Path) -> Path:
    """
    Get HDF5 file path for L3 Fusion results
    
    Args:
        date: Date in YYYYMMDD format
        data_root: Data root directory
    
    Returns:
        Path to HDF5 file
    """
    fusion_dir = data_root / 'phase2' / 'fusion'
    return fusion_dir / f"fusion_fusion_timing_{date}.h5"



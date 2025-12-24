#!/usr/bin/env python3
"""
CSV Schema Validator for hf-timestd Phase 2 Analytics

Validates that CSV files conform to documented schemas and don't contain
invalid values like NaN, inf, or empty required fields.

Usage:
    python validate_csv_schemas.py --csv-file /path/to/file.csv --schema carrier_power
    python validate_csv_schemas.py --directory /var/lib/timestd/phase2/SHARED_10000
"""

import argparse
import csv
import sys
from pathlib import Path
from typing import Dict, List, Optional, Any
import re

# Define expected schemas for each CSV type
SCHEMAS = {
    'carrier_power': {
        'required_columns': ['timestamp', 'utc_time', 'power_db', 'snr_db', 'station', 'quality_grade'],
        'optional_columns': ['wwv_tone_db', 'wwvh_tone_db'],
        'numeric_columns': ['timestamp', 'power_db', 'snr_db', 'wwv_tone_db', 'wwvh_tone_db'],
        'allow_empty': ['power_db', 'snr_db', 'wwv_tone_db', 'wwvh_tone_db'],  # Can be empty if no data
        'description': 'Carrier power and SNR measurements'
    },
    'clock_offset': {
        'required_columns': [
            'system_time', 'utc_time', 'minute_boundary_utc', 'clock_offset_ms',
            'station', 'frequency_mhz', 'propagation_mode', 'confidence',
            'uncertainty_ms', 'quality_grade'
        ],
        'optional_columns': [
            'propagation_delay_ms', 'n_hops', 'snr_db', 'delay_spread_ms',
            'doppler_std_hz', 'fss_db', 'wwv_power_db', 'wwvh_power_db',
            'discrimination_confidence', 'utc_verified', 'multi_station_verified',
            'rtp_timestamp', 'processed_at', 'wwv_tick_snr_db', 'wwvh_tick_snr_db',
            'chu_tick_snr_db', 'bpm_tick_snr_db'
        ],
        'numeric_columns': [
            'system_time', 'minute_boundary_utc', 'clock_offset_ms', 'frequency_mhz',
            'propagation_delay_ms', 'n_hops', 'confidence', 'uncertainty_ms',
            'snr_db', 'delay_spread_ms', 'doppler_std_hz', 'fss_db',
            'wwv_power_db', 'wwvh_power_db', 'discrimination_confidence'
        ],
        'allow_empty': [
            'snr_db', 'delay_spread_ms', 'doppler_std_hz', 'fss_db',
            'wwv_power_db', 'wwvh_power_db', 'discrimination_confidence'
        ],
        'description': 'Clock offset and timing measurements'
    },
    'doppler': {
        'required_columns': ['timestamp_utc', 'minute_boundary'],
        'optional_columns': [
            'wwv_doppler_hz', 'wwvh_doppler_hz', 'wwv_doppler_std_hz',
            'wwvh_doppler_std_hz', 'doppler_quality', 'max_coherent_window_sec',
            'phase_variance_rad', 'carrier_doppler_hz'
        ],
        'numeric_columns': [
            'minute_boundary', 'wwv_doppler_hz', 'wwvh_doppler_hz',
            'wwv_doppler_std_hz', 'wwvh_doppler_std_hz', 'max_coherent_window_sec',
            'phase_variance_rad', 'carrier_doppler_hz'
        ],
        'allow_empty': [
            'wwv_doppler_hz', 'wwvh_doppler_hz', 'wwv_doppler_std_hz',
            'wwvh_doppler_std_hz', 'max_coherent_window_sec', 'phase_variance_rad',
            'carrier_doppler_hz'
        ],
        'description': 'Doppler shift measurements'
    },
    'tec': {
        'required_columns': [
            'timestamp_utc', 'minute_boundary', 'station', 'tec_tecu',
            'confidence', 'n_frequencies'
        ],
        'optional_columns': [
            't_vacuum_error_ms', 'residuals_ms', 'frequencies_mhz',
            'group_delay_2_5_mhz', 'group_delay_5_mhz', 'group_delay_10_mhz',
            'group_delay_15_mhz', 'group_delay_20_mhz', 'group_delay_25_mhz'
        ],
        'numeric_columns': [
            'minute_boundary', 'tec_tecu', 't_vacuum_error_ms', 'confidence',
            'residuals_ms', 'n_frequencies', 'group_delay_2_5_mhz',
            'group_delay_5_mhz', 'group_delay_10_mhz', 'group_delay_15_mhz',
            'group_delay_20_mhz', 'group_delay_25_mhz'
        ],
        'allow_empty': [
            'group_delay_2_5_mhz', 'group_delay_5_mhz', 'group_delay_10_mhz',
            'group_delay_15_mhz', 'group_delay_20_mhz', 'group_delay_25_mhz'
        ],
        'description': 'Total Electron Content estimates'
    }
}


class ValidationError(Exception):
    """CSV validation error."""
    pass


def detect_schema_type(csv_path: Path) -> Optional[str]:
    """Detect schema type from filename."""
    filename = csv_path.name.lower()
    
    if 'carrier_power' in filename:
        return 'carrier_power'
    elif 'clock_offset' in filename:
        return 'clock_offset'
    elif 'doppler' in filename and 'tec' not in filename:
        return 'doppler'
    elif 'tec' in filename:
        return 'tec'
    
    return None


def validate_csv(csv_path: Path, schema_type: str, verbose: bool = False) -> Dict[str, Any]:
    """
    Validate a CSV file against its schema.
    
    Returns:
        Dict with validation results: {
            'valid': bool,
            'errors': List[str],
            'warnings': List[str],
            'stats': Dict[str, int]
        }
    """
    if schema_type not in SCHEMAS:
        raise ValueError(f"Unknown schema type: {schema_type}")
    
    schema = SCHEMAS[schema_type]
    errors = []
    warnings = []
    stats = {
        'total_rows': 0,
        'invalid_rows': 0,
        'nan_values': 0,
        'inf_values': 0,
        'empty_required': 0
    }
    
    try:
        with open(csv_path, 'r') as f:
            reader = csv.DictReader(f)
            
            # Validate header
            if reader.fieldnames is None:
                errors.append("CSV has no header row")
                return {'valid': False, 'errors': errors, 'warnings': warnings, 'stats': stats}
            
            # Check for required columns
            missing_cols = set(schema['required_columns']) - set(reader.fieldnames)
            if missing_cols:
                errors.append(f"Missing required columns: {missing_cols}")
            
            # Validate each row
            for row_num, row in enumerate(reader, start=2):  # Start at 2 (header is line 1)
                stats['total_rows'] += 1
                row_errors = []
                
                # Check each numeric column
                for col in schema['numeric_columns']:
                    if col not in row:
                        continue
                    
                    value = row[col].strip()
                    
                    # Check if empty
                    if not value:
                        if col in schema.get('allow_empty', []):
                            continue  # Empty is OK for this column
                        else:
                            row_errors.append(f"Column '{col}' is empty but required")
                            stats['empty_required'] += 1
                        continue
                    
                    # Check for literal "nan" or "inf" strings
                    if value.lower() in ['nan', '-nan', '+nan']:
                        row_errors.append(f"Column '{col}' contains literal 'nan'")
                        stats['nan_values'] += 1
                    elif value.lower() in ['inf', '-inf', '+inf', 'infinity', '-infinity']:
                        row_errors.append(f"Column '{col}' contains literal 'inf'")
                        stats['inf_values'] += 1
                    else:
                        # Try to parse as number
                        try:
                            num_val = float(value)
                            # Check for NaN/inf in parsed value
                            if num_val != num_val:  # NaN check
                                row_errors.append(f"Column '{col}' is NaN")
                                stats['nan_values'] += 1
                            elif abs(num_val) == float('inf'):
                                row_errors.append(f"Column '{col}' is infinite")
                                stats['inf_values'] += 1
                        except ValueError:
                            row_errors.append(f"Column '{col}' has invalid numeric value: '{value}'")
                
                if row_errors:
                    stats['invalid_rows'] += 1
                    if verbose or stats['invalid_rows'] <= 10:  # Show first 10 errors
                        errors.append(f"Row {row_num}: {'; '.join(row_errors)}")
                    elif stats['invalid_rows'] == 11:
                        warnings.append(f"... and {stats['total_rows'] - row_num} more rows with errors (use --verbose to see all)")
        
        # Summary
        if stats['nan_values'] > 0:
            warnings.append(f"Found {stats['nan_values']} NaN values across all rows")
        if stats['inf_values'] > 0:
            warnings.append(f"Found {stats['inf_values']} infinite values across all rows")
        if stats['invalid_rows'] > 0:
            warnings.append(f"{stats['invalid_rows']}/{stats['total_rows']} rows have validation errors")
        
        valid = len(errors) == 0 or (len(errors) > 0 and all('Row' in e for e in errors))
        
        return {
            'valid': valid and stats['invalid_rows'] == 0,
            'errors': errors,
            'warnings': warnings,
            'stats': stats
        }
        
    except Exception as e:
        errors.append(f"Failed to read CSV: {e}")
        return {'valid': False, 'errors': errors, 'warnings': warnings, 'stats': stats}


def validate_directory(directory: Path, schema_type: Optional[str] = None, verbose: bool = False):
    """Validate all CSV files in a directory."""
    csv_files = list(directory.glob('**/*.csv'))
    
    if not csv_files:
        print(f"No CSV files found in {directory}")
        return
    
    print(f"Found {len(csv_files)} CSV files in {directory}\n")
    
    total_valid = 0
    total_invalid = 0
    
    for csv_file in csv_files:
        # Auto-detect schema if not specified
        detected_schema = schema_type or detect_schema_type(csv_file)
        
        if not detected_schema:
            print(f"⚠️  {csv_file.name}: Unknown schema type, skipping")
            continue
        
        print(f"Validating {csv_file.name} ({detected_schema})...")
        result = validate_csv(csv_file, detected_schema, verbose)
        
        if result['valid']:
            print(f"  ✅ VALID ({result['stats']['total_rows']} rows)")
            total_valid += 1
        else:
            print(f"  ❌ INVALID")
            total_invalid += 1
            
            for error in result['errors'][:5]:  # Show first 5 errors
                print(f"     ERROR: {error}")
            
            if len(result['errors']) > 5:
                print(f"     ... and {len(result['errors']) - 5} more errors")
        
        for warning in result['warnings']:
            print(f"     WARNING: {warning}")
        
        print()
    
    print(f"\nSummary: {total_valid} valid, {total_invalid} invalid")
    return total_invalid == 0


def main():
    parser = argparse.ArgumentParser(description='Validate hf-timestd CSV files')
    parser.add_argument('--csv-file', type=Path, help='Single CSV file to validate')
    parser.add_argument('--directory', type=Path, help='Directory containing CSV files')
    parser.add_argument('--schema', choices=list(SCHEMAS.keys()), help='Schema type (auto-detected if not specified)')
    parser.add_argument('--verbose', action='store_true', help='Show all errors')
    parser.add_argument('--list-schemas', action='store_true', help='List available schemas')
    
    args = parser.parse_args()
    
    if args.list_schemas:
        print("Available schemas:\n")
        for name, schema in SCHEMAS.items():
            print(f"  {name}:")
            print(f"    {schema['description']}")
            print(f"    Required columns: {', '.join(schema['required_columns'])}")
            print()
        return 0
    
    if args.csv_file:
        schema_type = args.schema or detect_schema_type(args.csv_file)
        if not schema_type:
            print(f"ERROR: Could not detect schema type for {args.csv_file}")
            print("Please specify --schema explicitly")
            return 1
        
        print(f"Validating {args.csv_file} ({schema_type})...\n")
        result = validate_csv(args.csv_file, schema_type, args.verbose)
        
        if result['valid']:
            print(f"✅ VALID ({result['stats']['total_rows']} rows)")
            return 0
        else:
            print(f"❌ INVALID\n")
            for error in result['errors']:
                print(f"  ERROR: {error}")
            for warning in result['warnings']:
                print(f"  WARNING: {warning}")
            return 1
    
    elif args.directory:
        success = validate_directory(args.directory, args.schema, args.verbose)
        return 0 if success else 1
    
    else:
        parser.print_help()
        return 1


if __name__ == '__main__':
    sys.exit(main())

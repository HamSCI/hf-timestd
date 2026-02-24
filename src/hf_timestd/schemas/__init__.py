"""
Schema Registry for hf-timestd Data Products

Provides access to JSON Schema definitions for all data product levels (L0-L3).
Follows NASA data product standards and NIST metrology requirements.
"""

import json
from pathlib import Path
from typing import Dict, Any, Optional

__all__ = ['get_schema', 'list_schemas', 'validate_schema_version', 'get_data_dictionary', 'check_field']

SCHEMA_DIR = Path(__file__).parent


def get_schema(product_level: str, product_name: str, version: str = 'v1') -> Dict[str, Any]:
    """
    Load JSON Schema for a data product.
    
    Args:
        product_level: Data product level (L1, L2, L3)
        product_name: Product name (e.g., 'channel_observables', 'timing_measurements')
        version: Schema version (default: 'v1')
        
    Returns:
        JSON Schema dictionary
        
    Raises:
        FileNotFoundError: If schema file doesn't exist
        json.JSONDecodeError: If schema file is invalid JSON
        
    Example:
        >>> schema = get_schema('L2', 'timing_measurements')
        >>> schema['schema_version']
        '1.0.0'
    """
    # Try to resolve via registry first
    registry = get_registry()
    registry_key = f"{product_level.upper()}_{product_name}"
    
    if registry_key in registry.get('data_products', {}):
        schema_filename = registry['data_products'][registry_key]['schema_file']
        schema_file = SCHEMA_DIR / schema_filename
    else:
        # Fallback to legacy naming convention
        schema_file = SCHEMA_DIR / f"{product_level.lower()}_{product_name}_{version}.json"
    
    if not schema_file.exists():
        raise FileNotFoundError(
            f"Schema not found: {schema_file}\n"
            f"Available schemas: {list_schemas()}"
        )
    
    with open(schema_file, 'r') as f:
        return json.load(f)


def list_schemas() -> Dict[str, list]:
    """
    List all available schemas by product level.
    
    Returns:
        Dictionary mapping product level to list of available schemas
        
    Example:
        >>> schemas = list_schemas()
        >>> schemas['L2']
        ['timing_measurements_v1']
    """
    schemas = {}
    
    for schema_file in SCHEMA_DIR.glob('*.json'):
        if schema_file.name == 'registry.json':
            continue
            
        # Parse filename: l2_timing_measurements_v1.json
        parts = schema_file.stem.split('_', 1)
        if len(parts) < 2:
            continue
            
        level = parts[0].upper()
        product = '_'.join(parts[1:])
        
        if level not in schemas:
            schemas[level] = []
        schemas[level].append(product)
    
    return schemas


def validate_schema_version(schema: Dict[str, Any]) -> bool:
    """
    Validate that a schema has required version metadata.
    
    Args:
        schema: JSON Schema dictionary
        
    Returns:
        True if valid, False otherwise
    """
    required_fields = ['schema_version', 'data_product', 'description', 'fields']
    return all(field in schema for field in required_fields)


def get_registry() -> Dict[str, Any]:
    """
    Load the schema registry mapping file.
    
    Returns:
        Registry dictionary mapping data products to schema versions
    """
    registry_file = SCHEMA_DIR / 'registry.json'
    
    if not registry_file.exists():
        return {}
    
    with open(registry_file, 'r') as f:
        return json.load(f)


_data_dictionary_cache: Optional[Dict[str, Any]] = None


def get_data_dictionary() -> Dict[str, Any]:
    """
    Load the canonical data dictionary — the single authoritative definition
    of every observable and derived quantity in the hf-timestd pipeline.

    Returns:
        Data dictionary with 'observables', 'derived_quantities',
        'consistency_rules', and 'pipeline_data_flow' sections.

    Example:
        >>> dd = get_data_dictionary()
        >>> dd['derived_quantities']['clock_offset_ms']['formula']
        'clock_offset_ms = raw_arrival_time_ms - propagation_delay_ms'
    """
    global _data_dictionary_cache
    if _data_dictionary_cache is None:
        dd_file = SCHEMA_DIR / 'data_dictionary.json'
        if not dd_file.exists():
            raise FileNotFoundError(f"Data dictionary not found: {dd_file}")
        with open(dd_file, 'r') as f:
            _data_dictionary_cache = json.load(f)
    return _data_dictionary_cache


def check_field(field_name: str) -> Optional[Dict[str, Any]]:
    """
    Look up a field's canonical definition from the data dictionary.

    Returns the full entry (description, formula, sign convention, pitfalls)
    or None if the field is not in the dictionary.

    Use this before using any field in a calculation to verify its meaning.

    Example:
        >>> entry = check_field('clock_offset_ms')
        >>> print(entry['description'])
        >>> for pitfall in entry['known_pitfalls']:
        ...     print('PITFALL:', pitfall)
    """
    dd = get_data_dictionary()
    entry = dd.get('observables', {}).get(field_name)
    if entry is None:
        entry = dd.get('derived_quantities', {}).get(field_name)
    if entry is None:
        entry = dd.get('structural_fields', {}).get(field_name)
    return entry

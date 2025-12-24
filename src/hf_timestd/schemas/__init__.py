"""
Schema Registry for hf-timestd Data Products

Provides access to JSON Schema definitions for all data product levels (L0-L3).
Follows NASA data product standards and NIST metrology requirements.
"""

import json
from pathlib import Path
from typing import Dict, Any, Optional

__all__ = ['get_schema', 'list_schemas', 'validate_schema_version']

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

"""
Unit tests for schema registry and schema validation.
"""

import pytest
import json
from pathlib import Path
from hf_timestd.schemas import get_schema, list_schemas, validate_schema_version, get_registry


class TestSchemaRegistry:
    """Test schema registry functionality."""
    
    def test_get_schema_l1a(self):
        """Test loading L1A channel observables schema."""
        schema = get_schema('L1', 'channel_observables')
        
        assert schema['schema_version'] == '1.0.0'
        assert schema['data_product'] == 'L1A_channel_observables'
        assert 'fields' in schema
        assert len(schema['fields']) > 0
    
    def test_get_schema_l2(self):
        """Test loading L2 timing measurements schema."""
        schema = get_schema('L2', 'timing_measurements')
        
        assert schema['schema_version'] == '1.0.0'
        assert schema['data_product'] == 'L2_timing_measurements'
        assert 'uncertainty_calculation' in schema
        assert 'ISO GUM' in schema['standards'][0]
    
    def test_get_schema_not_found(self):
        """Test error handling for non-existent schema."""
        with pytest.raises(FileNotFoundError):
            get_schema('L99', 'nonexistent')
    
    def test_list_schemas(self):
        """Test listing all available schemas."""
        schemas = list_schemas()
        
        assert 'L1' in schemas
        assert 'L2' in schemas
        assert 'L3' in schemas
        
        # Check that expected schemas are present
        l1_schemas = schemas['L1']
        assert any('channel_observables' in s for s in l1_schemas)
        assert any('bcd_timecode' in s for s in l1_schemas)
    
    def test_validate_schema_version(self):
        """Test schema version validation."""
        valid_schema = {
            'schema_version': '1.0.0',
            'data_product': 'test',
            'description': 'test schema',
            'fields': []
        }
        
        assert validate_schema_version(valid_schema) is True
        
        invalid_schema = {
            'schema_version': '1.0.0'
            # Missing required fields
        }
        
        assert validate_schema_version(invalid_schema) is False
    
    def test_get_registry(self):
        """Test loading schema registry."""
        registry = get_registry()
        
        assert 'data_products' in registry
        assert 'L2_timing_measurements' in registry['data_products']
        
        l2_product = registry['data_products']['L2_timing_measurements']
        assert l2_product['current_version'] == 'v1'
        assert 'clock_offset' in l2_product['replaces']


class TestSchemaStructure:
    """Test schema structure and content."""
    
    def test_l1a_required_fields(self):
        """Test L1A schema has required fields."""
        schema = get_schema('L1', 'channel_observables')
        
        field_names = [f['name'] for f in schema['fields']]
        
        # Check critical fields are present
        assert 'timestamp_utc' in field_names
        assert 'minute_boundary' in field_names
        assert 'carrier_power_db' in field_names
        assert 'quality_flag' in field_names
    
    def test_l2_uncertainty_fields(self):
        """Test L2 schema has ISO GUM uncertainty fields."""
        schema = get_schema('L2', 'timing_measurements')
        
        field_names = [f['name'] for f in schema['fields']]
        
        # Check ISO GUM required fields
        assert 'uncertainty_ms' in field_names
        assert 'expanded_uncertainty_ms' in field_names
        assert 'coverage_factor' in field_names
        assert 'confidence_level' in field_names
        
        # Check Type A uncertainty components
        assert 'u_rtp_timestamp_ms' in field_names
        assert 'u_ionospheric_ms' in field_names
        assert 'u_multipath_ms' in field_names
        
        # Check Type B uncertainty components
        assert 'u_discrimination_ms' in field_names
        assert 'u_gpsdo_ms' in field_names
        assert 'u_propagation_model_ms' in field_names
        
        # Check metrology traceability
        assert 'traceability_chain' in field_names
        assert 'calibration_date' in field_names
        assert 'gpsdo_locked' in field_names
    
    def test_quality_flags_defined(self):
        """Test all schemas define quality flags."""
        for level in ['L1', 'L2', 'L3']:
            schemas_list = list_schemas().get(level, [])
            
            for schema_name in schemas_list:
                # Extract product name from schema filename
                product_name = schema_name.replace(f'{level.lower()}_', '').replace('_v1', '')
                schema = get_schema(level, product_name)
                
                assert 'quality_flags' in schema, f"{schema['data_product']} missing quality_flags"
                assert len(schema['quality_flags']) > 0


if __name__ == '__main__':
    pytest.main([__file__, '-v'])

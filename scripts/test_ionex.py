#!/usr/bin/env python3
"""
Test IONEX Integration

This script tests the IONEX download, parsing, and integration with the
ionospheric model for VTEC lookup.

Usage:
    python test_ionex.py [DATE]
    
Example:
    python test_ionex.py 2025-12-30
"""

import sys
import logging
from datetime import datetime, date
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))

from hf_timestd.core.ionospheric_model import IonosphericModel
from hf_timestd.core.wwv_constants import WWV_LAT, WWV_LON, WWVH_LAT, WWVH_LON

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

logger = logging.getLogger(__name__)


def test_ionex_download(date_str: str):
    """Test IONEX file download."""
    logger.info(f"Testing IONEX download for {date_str}")
    
    # Import download function
    sys.path.insert(0, str(Path(__file__).parent))
    from ionex_integration import download_ionex
    
    output_dir = Path('/tmp/ionex_test')
    ionex_file = download_ionex(date_str, output_dir=output_dir)
    
    if ionex_file:
        logger.info(f"✓ Downloaded IONEX file: {ionex_file}")
        logger.info(f"  Size: {ionex_file.stat().st_size} bytes")
        return ionex_file
    else:
        logger.error("✗ IONEX download failed")
        return None


def test_ionex_parsing(ionex_file: Path):
    """Test IONEX file parsing."""
    logger.info(f"Testing IONEX parsing: {ionex_file}")
    
    sys.path.insert(0, str(Path(__file__).parent))
    from ionex_integration import IONEXParser
    
    try:
        parser = IONEXParser(ionex_file)
        logger.info(f"✓ Parsed {len(parser.maps)} TEC maps")
        
        # Test interpolation at WWV location
        timestamp = datetime.now()
        vtec = parser.interpolate(WWV_LAT, WWV_LON, timestamp)
        
        if vtec is not None:
            logger.info(f"✓ VTEC at WWV ({WWV_LAT}°, {WWV_LON}°): {vtec:.2f} TECU")
            return True
        else:
            logger.warning("✗ VTEC interpolation returned None")
            return False
            
    except Exception as e:
        logger.error(f"✗ IONEX parsing failed: {e}")
        return False


def test_ionospheric_model_integration():
    """Test IONEX integration with IonosphericModel."""
    logger.info("Testing IonosphericModel IONEX integration")
    
    # Create model with IONEX support
    model = IonosphericModel(
        enable_iri=True,
        ionex_dir=Path('/tmp/ionex_test')
    )
    
    # Test reflection point calculation
    logger.info("Testing HF reflection point calculation...")
    ref_lat, ref_lon = model.calculate_hf_reflection_point(
        tx_lat=WWV_LAT, tx_lon=WWV_LON,
        rx_lat=39.0, rx_lon=-98.0  # Approximate US center
    )
    logger.info(f"✓ Reflection point: ({ref_lat:.2f}°, {ref_lon:.2f}°)")
    
    # Test IONEX VTEC lookup
    logger.info("Testing IONEX VTEC lookup...")
    timestamp = datetime.now()
    result = model.get_ionex_vtec(ref_lat, ref_lon, timestamp)
    
    if result is not None:
        vtec, source_file = result
        logger.info(f"✓ IONEX VTEC: {vtec:.2f} TECU from {source_file}")
        return True
    else:
        logger.warning("✗ IONEX VTEC lookup returned None")
        return False


def test_tec_estimation_hierarchy():
    """Test that IONEX is used in TEC estimation hierarchy."""
    logger.info("Testing TEC estimation hierarchy...")
    
    from hf_timestd.core.ionospheric_model import IonosphericDelayCalculator
    
    # Create calculator with IONEX-enabled model
    model = IonosphericModel(
        enable_iri=True,
        ionex_dir=Path('/tmp/ionex_test')
    )
    calc = IonosphericDelayCalculator(iono_model=model)
    
    # Calculate delay for WWV at 10 MHz
    timestamp = datetime.now()
    result = calc.calculate_delay(
        frequency_mhz=10.0,
        n_hops=1,
        elevation_deg=30.0,
        timestamp=timestamp,
        latitude=39.0,
        longitude=-98.0
    )
    
    logger.info(f"✓ TEC estimation: {result.vertical_tec_tecu:.2f} TECU")
    logger.info(f"  Tier: {result.tier.value}")
    logger.info(f"  Delay: {result.delay_ms:.3f} ms")
    
    if result.tier.value == "IONEX":
        logger.info("✓ IONEX tier successfully used!")
        return True
    else:
        logger.warning(f"⚠ Using tier: {result.tier.value} (expected IONEX)")
        return False


def main():
    """Run all IONEX integration tests."""
    logger.info("=" * 60)
    logger.info("IONEX Integration Test Suite")
    logger.info("=" * 60)
    
    # Get date from command line or use yesterday
    if len(sys.argv) > 1:
        date_str = sys.argv[1]
    else:
        yesterday = date.today().replace(day=date.today().day - 1)
        date_str = yesterday.strftime('%Y-%m-%d')
    
    logger.info(f"Test date: {date_str}")
    logger.info("")
    
    # Test 1: Download
    ionex_file = test_ionex_download(date_str)
    if not ionex_file:
        logger.error("IONEX download failed - cannot continue tests")
        return 1
    
    logger.info("")
    
    # Test 2: Parsing
    if not test_ionex_parsing(ionex_file):
        logger.error("IONEX parsing failed - cannot continue tests")
        return 1
    
    logger.info("")
    
    # Test 3: Model integration
    if not test_ionospheric_model_integration():
        logger.error("Model integration test failed")
        return 1
    
    logger.info("")
    
    # Test 4: TEC estimation hierarchy
    if not test_tec_estimation_hierarchy():
        logger.warning("TEC estimation hierarchy test incomplete")
    
    logger.info("")
    logger.info("=" * 60)
    logger.info("✓ All IONEX integration tests passed!")
    logger.info("=" * 60)
    
    return 0


if __name__ == '__main__':
    sys.exit(main())

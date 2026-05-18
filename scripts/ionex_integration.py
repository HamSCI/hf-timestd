#!/usr/bin/env python3
"""
IONEX Data Integration - Download and Parse IGS Global Ionosphere Maps

This module handles downloading and parsing IONEX (IONosphere Map EXchange) files
from NASA CDDIS IGS Global Ionosphere Maps (GIM) for GPS TEC data.

IONEX Format:
- 2.5° × 5° grid (latitude × longitude) or 5° × 5° for rapid products
- 2-hour cadence (12 maps per day)
- VTEC in TECU (Total Electron Content Units, 10^16 electrons/m²)

Data Source: NASA CDDIS IGS Global Ionosphere Maps
- Base URL: https://cddis.nasa.gov/archive/gnss/products/ionex/
- Directory Structure: YYYY/DDD/ (year/day-of-year)
- Products: IGS Final (most accurate, 1-2 week latency)

Filename Formats:
- Modern (post-Nov 27, 2022): IGS0OPSFIN_YYYYDDD0000_01D_02H_GIM.INX.gz
- Legacy (pre-Nov 27, 2022): igsgDDD0.YYi.Z

Authentication:
Requires NASA Earthdata Login credentials in ~/.netrc:
    machine urs.earthdata.nasa.gov
        login YOUR_USERNAME
        password YOUR_PASSWORD

Usage:
    # Download IONEX for a specific date
    ionex_file = download_ionex('2025-12-23', output_dir='/var/lib/timestd/ionex')
    
    # Parse and interpolate
    vtec = interpolate_ionex_vtec(ionex_file, lat=40.5, lon=-100.2, timestamp='2025-12-23T12:00:00Z')
"""

import os
import gzip
import requests
from datetime import datetime, timedelta, date
from pathlib import Path
from typing import Optional, Tuple, List
import numpy as np
import logging

# IONEXParser is owned by the package (P-H18); re-exported here so the
# standalone ionex_* scripts keep importing it from ionex_integration.
from hf_timestd.core.ionex_parser import IONEXParser  # noqa: F401

try:
    from hf_timestd.cddis_auth import get_cddis_session, check_earthdata_credentials
    _HAS_CDDIS_AUTH = True
except ImportError:
    _HAS_CDDIS_AUTH = False

logger = logging.getLogger(__name__)


def get_ionex_filename(target_date: date, product_type: str = 'FIN') -> str:
    """
    Returns the correct IGS GIM filename based on the naming convention and product type.
    
    IGS switched from legacy 8.3 format to long product filenames on Nov 27, 2022.
    
    Product Types:
        - FIN (Final): Most accurate, ~14-17 day latency
        - RAP (Rapid): Good accuracy (~2-3 TECU), ~1 day latency
    
    Args:
        target_date: Date for which to get filename
        product_type: 'FIN' for Final (default) or 'RAP' for Rapid
        
    Returns:
        Filename string (without path)
    """
    year = target_date.year
    yy = target_date.strftime("%y")
    doy = target_date.strftime("%j")
    
    # Switch logic: Nov 27, 2022 (GPS Week 2238)
    if target_date >= date(2022, 11, 27):
        # Modern Long Filename
        # Final: IGS0OPSFIN_YYYYDDD0000_01D_02H_GIM.INX.gz
        # Rapid: IGS0OPSRAP_YYYYDDD0000_01D_02H_GIM.INX.gz
        return f"IGS0OPS{product_type}_{year}{doy}0000_01D_02H_GIM.INX.gz"
    else:
        # Legacy Short Filename
        # Final: igsgDDD0.YYi.Z
        # Rapid: igrgDDD0.YYi.Z
        prefix = 'igsg' if product_type == 'FIN' else 'igrg'
        return f"{prefix}{doy}0.{yy}i.Z"


def download_ionex(
    date_str: str, 
    output_dir: Path = Path('/tmp/ionex'), 
    max_days_back: int = 14,
    prefer_rapid: bool = False
) -> Optional[Path]:
    """
    Download IGS Global Ionosphere Map (IONEX) file for a specific date.
    
    Uses NASA CDDIS archive with Earthdata Login authentication via ~/.netrc.
    Automatically selects correct filename format based on date.
    
    Strategy:
    1. Try Final product for requested date (most accurate, ~14-17 day latency)
    2. If not found, try Rapid product for same date (~1 day latency, ~2-3 TECU accuracy)
    3. Search backwards up to max_days_back days, trying Final then Rapid for each
    
    Args:
        date_str: Date string (YYYY-MM-DD)
        output_dir: Directory to save IONEX files
        max_days_back: Maximum days to search backwards if requested date unavailable (default: 14)
        prefer_rapid: If True, try Rapid before Final (for lower latency)
    
    Returns:
        Path to downloaded file, or None if failed
    """
    # Parse date
    try:
        dt = datetime.strptime(date_str, '%Y-%m-%d')
        target_date = dt.date()
    except ValueError as e:
        logger.error(f"Invalid date format '{date_str}': {e}")
        return None
    
    # Ensure output directory exists
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # Product types to try (order depends on prefer_rapid)
    if prefer_rapid:
        product_types = ['RAP', 'FIN']  # Rapid first for freshness
    else:
        product_types = ['FIN', 'RAP']  # Final first for accuracy
    
    base_url = "https://cddis.nasa.gov/archive/gnss/products/ionex/"
    
    # Try requested date first, then search backwards
    for days_offset in range(max_days_back + 1):
        attempt_date = target_date - timedelta(days=days_offset)
        year = attempt_date.year
        doy = attempt_date.strftime("%j")
        
        # Try each product type for this date
        for product_type in product_types:
            filename = get_ionex_filename(attempt_date, product_type)
            file_url = f"{base_url}{year}/{doy}/{filename}"
            output_file = output_dir / filename
            
            # Check if already downloaded
            if output_file.exists():
                product_name = "Final" if product_type == 'FIN' else "Rapid"
                if days_offset > 0:
                    logger.info(f"Using existing {product_name} IONEX from {days_offset} days ago: {output_file}")
                else:
                    logger.info(f"IONEX {product_name} file already exists: {output_file}")
                return output_file
            
            # Log what we're trying
            product_name = "Final" if product_type == 'FIN' else "Rapid"
            if days_offset == 0 and product_type == product_types[0]:
                logger.info(f"Downloading IONEX {product_name}: {filename}")
            else:
                logger.debug(f"Trying {product_name} {days_offset} days back: {filename}")
            
            try:
                if _HAS_CDDIS_AUTH:
                    session = get_cddis_session()
                else:
                    session = requests.Session()
                with session:
                    response = session.get(file_url, allow_redirects=True, stream=True, timeout=60)
                    
                    if response.status_code == 200:
                        with open(output_file, 'wb') as f:
                            for chunk in response.iter_content(chunk_size=8192):
                                if chunk:
                                    f.write(chunk)
                        
                        if output_file.stat().st_size == 0:
                            logger.warning(f"Downloaded file is empty: {output_file}")
                            output_file.unlink()
                            continue
                        
                        logger.info(f"✓ Downloaded {product_name} IONEX: {output_file} ({output_file.stat().st_size} bytes)")
                        if days_offset > 0:
                            logger.info(f"  (from {days_offset} days before requested date)")
                        return output_file
                        
                    elif response.status_code == 401:
                        logger.error("Authentication failed (401). Check credentials:")
                        logger.error("  File: /etc/hf-timestd/earthdata-netrc (or ~/.netrc)")
                        logger.error("  Required contents:")
                        logger.error("    machine urs.earthdata.nasa.gov")
                        logger.error("    login YOUR_USERNAME")
                        logger.error("    password YOUR_PASSWORD")
                        logger.error("  See docs/NASA_EARTHDATA_SETUP.md for details.")
                        return None
                        
                    elif response.status_code == 404:
                        # Try next product type or date
                        continue
                        
                    else:
                        logger.debug(f"HTTP {response.status_code} for {filename}")
                        continue
            
            except requests.exceptions.Timeout:
                logger.warning(f"Download timeout: {filename}")
                continue
            except requests.exceptions.RequestException as e:
                logger.debug(f"Download failed for {filename}: {e}")
                continue
            except Exception as e:
                logger.warning(f"Unexpected error downloading {filename}: {e}")
                continue
    
    logger.error(f"Could not find any IONEX files (Final or Rapid) within {max_days_back} days of {date_str}")
    return None


def interpolate_ionex_vtec(
    ionex_file: Path,
    lat: float,
    lon: float,
    timestamp: str
) -> Optional[float]:
    """
    Convenience function to parse and interpolate IONEX VTEC.
    
    Args:
        ionex_file: Path to IONEX file
        lat: Latitude (degrees)
        lon: Longitude (degrees)
        timestamp: ISO 8601 timestamp string
    
    Returns:
        VTEC in TECU, or None if not available
    """
    parser = IONEXParser(ionex_file)
    dt = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
    return parser.interpolate(lat, lon, dt)


if __name__ == '__main__':
    # Command-line interface for downloading IONEX files
    import sys
    import argparse
    
    parser = argparse.ArgumentParser(description='Download IONEX files from NASA CDDIS')
    parser.add_argument('date', help='Date to download (YYYY-MM-DD)')
    parser.add_argument('--output-dir', '-o', type=Path, default=Path('/var/lib/timestd/ionex'),
                        help='Output directory for IONEX files')
    parser.add_argument('--max-days-back', '-d', type=int, default=7,
                        help='Maximum days to search backwards (default: 7 with Rapid fallback)')
    parser.add_argument('--prefer-rapid', '-r', action='store_true',
                        help='Prefer Rapid products over Final (fresher but slightly less accurate)')
    
    args = parser.parse_args()
    
    # Download IONEX (now tries both Final and Rapid)
    ionex_file = download_ionex(
        args.date, 
        output_dir=args.output_dir, 
        max_days_back=args.max_days_back,
        prefer_rapid=args.prefer_rapid
    )
    
    if ionex_file:
        print(f"✓ IONEX file ready: {ionex_file}")
        
        # Verify by parsing
        try:
            ionex_parser = IONEXParser(ionex_file)
            print(f"  Maps: {len(ionex_parser.maps)} epochs")
            if ionex_parser.maps:
                first_epoch = ionex_parser.maps[0][0]
                last_epoch = ionex_parser.maps[-1][0]
                print(f"  Coverage: {first_epoch} to {last_epoch}")
        except Exception as e:
            print(f"  Warning: Could not parse file: {e}")
        
        sys.exit(0)
    else:
        print(f"✗ Failed to download IONEX for {args.date}")
        sys.exit(1)  # EXIT WITH ERROR CODE

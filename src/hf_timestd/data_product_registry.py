"""
Data Product Registry - Central Source of Truth for Data Locations

This module provides a centralized registry that maps data product types to their
storage locations, eliminating confusion about where HDF5 files are written and read.

The analytics service writes different data products to organized subdirectories:
- L2 timing measurements → clock_offset/
- L1 channel observables → carrier_power/
- L1 tone detections → tone_detections/
- etc.

This registry ensures all readers and writers use consistent paths.
"""

from pathlib import Path
from typing import Dict, Optional, List, Tuple


class DataProductRegistry:
    """
    Central registry for data product locations.
    
    Eliminates confusion by providing a single source of truth for
    where each data product type is stored within channel directories.
    
    Example:
        >>> from pathlib import Path
        >>> channel_dir = Path('/var/lib/timestd/phase2/CHU_14670')
        >>> 
        >>> # Get location for L2 timing measurements
        >>> data_dir = DataProductRegistry.get_data_dir(
        ...     channel_dir, 'L2', 'timing_measurements'
        ... )
        >>> print(data_dir)
        /var/lib/timestd/phase2/CHU_14670/clock_offset
    """
    
    # Map: (product_level, product_name) -> subdirectory
    # Empty string means root of channel directory
    PRODUCT_LOCATIONS: Dict[Tuple[str, str], str] = {
        # L1 Products (Raw/Processed Observables)
        ('L1', 'channel_observables'): 'carrier_power',
        ('L1', 'tone_detections'): 'tone_detections',
        ('L1', 'bcd_timecode'): 'bcd_discrimination',
        ('L1', 'metrology_measurements'): 'metrology',
        
        # L2 Products (Calibrated Measurements)
        ('L2', 'timing_measurements'): 'clock_offset',
        ('L2', 'test_signal'): 'test_signal',
        ('L2', 'physics_interpretation'): 'physics',
        
        # L3 Products (Derived/Fused)
        ('L3', 'tec'): 'tec',
        ('L3', 'fusion_timing'): '',  # Fusion is at phase2/fusion/ not channel-specific
        ('L3', 'gnss_vtec'): 'vtec',
        
        # L3B Products (Ionospheric Events)
        ('L3B', 'absorption'): 'absorption',
        ('L3B', 'iono_events'): 'iono_events',
        
        # L3C Products (Statistics)
        ('L3C', 'propagation_stats'): 'propagation_stats',
    }
    
    @classmethod
    def get_data_dir(
        cls,
        channel_dir: Path,
        product_level: str,
        product_name: str,
        create: bool = False
    ) -> Path:
        """
        Get the correct data directory for a product.
        
        Args:
            channel_dir: Base channel directory (e.g., /var/lib/timestd/phase2/CHU_14670)
            product_level: L1, L2, L3, L3B, L3C
            product_name: Product name (e.g., 'timing_measurements')
            create: If True, create the directory if it doesn't exist
            
        Returns:
            Full path to data directory
            
        Raises:
            ValueError: If product type is not registered
            
        Example:
            >>> channel_dir = Path('/var/lib/timestd/phase2/CHU_14670')
            >>> path = DataProductRegistry.get_data_dir(
            ...     channel_dir, 'L2', 'timing_measurements'
            ... )
            >>> print(path)
            /var/lib/timestd/phase2/CHU_14670/clock_offset
        """
        key = (product_level, product_name)
        subdirectory = cls.PRODUCT_LOCATIONS.get(key)
        
        if subdirectory is None:
            raise ValueError(
                f"Unknown data product: {product_level}/{product_name}\n"
                f"Known products: {cls.list_product_keys()}"
            )
        
        if subdirectory:
            data_dir = channel_dir / subdirectory
        else:
            data_dir = channel_dir
        
        if create:
            data_dir.mkdir(parents=True, exist_ok=True)
        
        return data_dir
    
    @classmethod
    def get_subdirectory(
        cls,
        product_level: str,
        product_name: str
    ) -> Optional[str]:
        """
        Get the subdirectory name for a product (without base path).
        
        Args:
            product_level: L1, L2, L3, etc.
            product_name: Product name
            
        Returns:
            Subdirectory name, or None if not registered, or '' if stored in root
            
        Example:
            >>> DataProductRegistry.get_subdirectory('L2', 'timing_measurements')
            'clock_offset'
        """
        key = (product_level, product_name)
        return cls.PRODUCT_LOCATIONS.get(key)
    
    @classmethod
    def is_registered(
        cls,
        product_level: str,
        product_name: str
    ) -> bool:
        """
        Check if a product type is registered.
        
        Args:
            product_level: L1, L2, L3, etc.
            product_name: Product name
            
        Returns:
            True if registered, False otherwise
        """
        return (product_level, product_name) in cls.PRODUCT_LOCATIONS
    
    @classmethod
    def list_products(cls) -> Dict[str, List[Dict[str, str]]]:
        """
        List all registered data products organized by level.
        
        Returns:
            Dictionary mapping level to list of product info
            
        Example:
            >>> products = DataProductRegistry.list_products()
            >>> print(products['L2'])
            [
                {'name': 'timing_measurements', 'subdirectory': 'clock_offset'},
                {'name': 'test_signal', 'subdirectory': 'test_signal'}
            ]
        """
        products = {}
        for (level, name), subdir in sorted(cls.PRODUCT_LOCATIONS.items()):
            if level not in products:
                products[level] = []
            products[level].append({
                'name': name,
                'subdirectory': subdir if subdir else '(root)'
            })
        return products
    
    @classmethod
    def list_product_keys(cls) -> List[str]:
        """
        List all registered product keys as strings.
        
        Returns:
            List of "level/name" strings
            
        Example:
            >>> keys = DataProductRegistry.list_product_keys()
            >>> print(keys[:3])
            ['L1/channel_observables', 'L1/tone_detections', 'L1/bcd_timecode']
        """
        return [f"{level}/{name}" for level, name in sorted(cls.PRODUCT_LOCATIONS.keys())]
    
    @classmethod
    def register_product(
        cls,
        product_level: str,
        product_name: str,
        subdirectory: str
    ) -> None:
        """
        Register a new data product type (for extensions).
        
        Args:
            product_level: L1, L2, L3, etc.
            product_name: Product name
            subdirectory: Subdirectory name (empty string for root)
            
        Example:
            >>> DataProductRegistry.register_product('L3', 'custom_product', 'custom')
        """
        key = (product_level, product_name)
        if key in cls.PRODUCT_LOCATIONS:
            raise ValueError(f"Product {product_level}/{product_name} already registered")
        cls.PRODUCT_LOCATIONS[key] = subdirectory
    
    @classmethod
    def print_registry(cls) -> None:
        """Print a formatted view of the registry."""
        print("Data Product Registry")
        print("=" * 70)
        
        products = cls.list_products()
        for level in sorted(products.keys()):
            print(f"\n{level} Products:")
            for product in products[level]:
                subdir = product['subdirectory']
                print(f"  {product['name']:30s} → {subdir}")
        
        print(f"\nTotal registered products: {len(cls.PRODUCT_LOCATIONS)}")


# Convenience function for backward compatibility
def get_product_data_dir(
    channel_dir: Path,
    product_level: str,
    product_name: str,
    create: bool = False
) -> Path:
    """
    Convenience function to get data directory for a product.
    
    This is a module-level function for easier imports.
    
    Args:
        channel_dir: Base channel directory
        product_level: L1, L2, L3, etc.
        product_name: Product name
        create: If True, create directory if it doesn't exist
        
    Returns:
        Full path to data directory
    """
    return DataProductRegistry.get_data_dir(
        channel_dir, product_level, product_name, create
    )


if __name__ == '__main__':
    # Demo usage
    DataProductRegistry.print_registry()

"""
Data Product Registry - Central Source of Truth for Data Locations

This module provides a centralized registry that maps data product types to their
storage locations, eliminating confusion about where HDF5 files are written and read.

================================================================================
STORAGE ARCHITECTURE (Broadcast-Centric)
================================================================================
The system uses a broadcast-centric storage model where each of the 17 broadcasts
(station + frequency) has its own directory for L1/L2 products:

    /var/lib/timestd/phase2/
    ├── broadcasts/                    # Broadcast-centric (scientific data)
    │   ├── WWV_10000/                # One directory per broadcast
    │   │   ├── L1_measurements_YYYYMMDD.h5
    │   │   └── L2_timing_YYYYMMDD.h5
    │   ├── CHU_7850/
    │   └── ...
    │
    ├── channels/                      # Channel-centric (receiver data)
    │   ├── SHARED_10000/             # Raw IQ, carrier power
    │   └── CHU_7850/
    │
    └── fusion/                        # L3 cross-broadcast products
        ├── d_clock_YYYYMMDD.h5
        └── tec_YYYYMMDD.h5

================================================================================
FREQUENCY CONVENTION
================================================================================
All frequencies are in kHz (integers) to avoid floating-point issues:
    10000 kHz = 10 MHz
    7850 kHz = 7.85 MHz
    14670 kHz = 14.67 MHz

This registry ensures all readers and writers use consistent paths.
"""

from pathlib import Path
from typing import Dict, Optional, List, Tuple, Union


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
    # Empty string means root of base directory
    # 
    # STORAGE TYPES:
    #   'broadcast:subdir' - Stored under broadcasts/{broadcast_id}/subdir
    #   'channel:subdir'   - Stored under channels/{channel}/subdir  
    #   'fusion:subdir'    - Stored under fusion/subdir
    #   'subdir'           - Legacy: stored under {base}/subdir (channel-centric)
    #
    PRODUCT_LOCATIONS: Dict[Tuple[str, str], str] = {
        # =====================================================================
        # BROADCAST-CENTRIC PRODUCTS (per broadcast_id, e.g., WWV_10000)
        # =====================================================================
        # L1 Broadcast Products
        ('L1', 'broadcast_measurements'): 'broadcast:',
        ('L1', 'tick_analysis'): 'broadcast:ticks',
        
        # L2 Broadcast Products  
        ('L2', 'broadcast_timing'): 'broadcast:',
        ('L2', 'broadcast_physics'): 'broadcast:physics',
        
        # Station-specific L2 products
        ('L2', 'chu_fsk'): 'broadcast:fsk',           # CHU only
        ('L2', 'wwv_bcd'): 'broadcast:bcd',           # WWV/WWVH only
        ('L2', 'wwv_test_signal'): 'broadcast:test',  # WWV/WWVH only
        
        # =====================================================================
        # CHANNEL-CENTRIC PRODUCTS (per receiver channel, e.g., SHARED_10000)
        # =====================================================================
        ('L1', 'channel_observables'): 'channel:carrier_power',
        ('L1', 'iq_archive'): 'channel:iq',
        ('L1', 'carrier_snr'): 'channel:snr',
        
        # =====================================================================
        # FUSION PRODUCTS (cross-broadcast, L3)
        # =====================================================================
        ('L3', 'fusion_timing'): 'fusion:',
        ('L3', 'd_clock'): 'fusion:d_clock',
        ('L3', 'tec'): 'fusion:tec',
        ('L3', 'gnss_vtec'): 'fusion:vtec',
        
        # L3B Products (Ionospheric Events)
        ('L3B', 'absorption'): 'fusion:absorption',
        ('L3B', 'iono_events'): 'fusion:events',
        
        # L3C Products (Statistics)
        ('L3C', 'propagation_stats'): 'fusion:propagation_stats',
        
        # =====================================================================
        # LEGACY CHANNEL-CENTRIC PRODUCTS (backward compatibility)
        # These use the old {channel_dir}/subdir pattern
        # =====================================================================
        ('L1', 'tone_detections'): 'tone_detections',
        ('L1', 'bcd_timecode'): 'bcd_discrimination',
        ('L1', 'metrology_measurements'): 'metrology',
        ('L2', 'timing_measurements'): 'clock_offset',
        ('L2', 'test_signal'): 'test_signal',
        ('L2', 'physics_interpretation'): 'physics',
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
    def get_broadcast_data_dir(
        cls,
        base_dir: Path,
        broadcast_id: str,
        product_level: str,
        product_name: str,
        create: bool = False
    ) -> Path:
        """
        Get data directory for a broadcast-centric product.
        
        Args:
            base_dir: Base phase2 directory (e.g., /var/lib/timestd/phase2)
            broadcast_id: Broadcast ID (e.g., 'WWV_10000', 'CHU_7850')
            product_level: L1, L2
            product_name: Product name (e.g., 'broadcast_measurements')
            create: If True, create directory if it doesn't exist
            
        Returns:
            Full path to data directory
            
        Example:
            >>> base_dir = Path('/var/lib/timestd/phase2')
            >>> path = DataProductRegistry.get_broadcast_data_dir(
            ...     base_dir, 'CHU_7850', 'L1', 'broadcast_measurements'
            ... )
            >>> print(path)
            /var/lib/timestd/phase2/broadcasts/CHU_7850
        """
        key = (product_level, product_name)
        location = cls.PRODUCT_LOCATIONS.get(key)
        
        if location is None:
            raise ValueError(
                f"Unknown data product: {product_level}/{product_name}\n"
                f"Known products: {cls.list_product_keys()}"
            )
        
        # Parse location string
        if location.startswith('broadcast:'):
            subdir = location[10:]  # Remove 'broadcast:' prefix
            if subdir:
                data_dir = base_dir / 'broadcasts' / broadcast_id / subdir
            else:
                data_dir = base_dir / 'broadcasts' / broadcast_id
        else:
            raise ValueError(
                f"Product {product_level}/{product_name} is not a broadcast-centric product. "
                f"Use get_data_dir() for legacy products or get_fusion_data_dir() for L3."
            )
        
        if create:
            data_dir.mkdir(parents=True, exist_ok=True)
        
        return data_dir
    
    @classmethod
    def get_channel_data_dir(
        cls,
        base_dir: Path,
        channel_name: str,
        product_level: str,
        product_name: str,
        create: bool = False
    ) -> Path:
        """
        Get data directory for a channel-centric product.
        
        Args:
            base_dir: Base phase2 directory (e.g., /var/lib/timestd/phase2)
            channel_name: Channel name (e.g., 'SHARED_10000', 'CHU_7850')
            product_level: L1
            product_name: Product name (e.g., 'channel_observables')
            create: If True, create directory if it doesn't exist
            
        Returns:
            Full path to data directory
            
        Example:
            >>> base_dir = Path('/var/lib/timestd/phase2')
            >>> path = DataProductRegistry.get_channel_data_dir(
            ...     base_dir, 'SHARED_10000', 'L1', 'channel_observables'
            ... )
            >>> print(path)
            /var/lib/timestd/phase2/channels/SHARED_10000/carrier_power
        """
        key = (product_level, product_name)
        location = cls.PRODUCT_LOCATIONS.get(key)
        
        if location is None:
            raise ValueError(
                f"Unknown data product: {product_level}/{product_name}\n"
                f"Known products: {cls.list_product_keys()}"
            )
        
        # Parse location string
        if location.startswith('channel:'):
            subdir = location[8:]  # Remove 'channel:' prefix
            if subdir:
                data_dir = base_dir / 'channels' / channel_name / subdir
            else:
                data_dir = base_dir / 'channels' / channel_name
        else:
            raise ValueError(
                f"Product {product_level}/{product_name} is not a channel-centric product. "
                f"Use get_broadcast_data_dir() for broadcast products."
            )
        
        if create:
            data_dir.mkdir(parents=True, exist_ok=True)
        
        return data_dir
    
    @classmethod
    def get_fusion_data_dir(
        cls,
        base_dir: Path,
        product_level: str,
        product_name: str,
        create: bool = False
    ) -> Path:
        """
        Get data directory for a fusion (L3) product.
        
        Args:
            base_dir: Base phase2 directory (e.g., /var/lib/timestd/phase2)
            product_level: L3, L3B, L3C
            product_name: Product name (e.g., 'd_clock', 'tec')
            create: If True, create directory if it doesn't exist
            
        Returns:
            Full path to data directory
            
        Example:
            >>> base_dir = Path('/var/lib/timestd/phase2')
            >>> path = DataProductRegistry.get_fusion_data_dir(
            ...     base_dir, 'L3', 'd_clock'
            ... )
            >>> print(path)
            /var/lib/timestd/phase2/fusion/d_clock
        """
        key = (product_level, product_name)
        location = cls.PRODUCT_LOCATIONS.get(key)
        
        if location is None:
            raise ValueError(
                f"Unknown data product: {product_level}/{product_name}\n"
                f"Known products: {cls.list_product_keys()}"
            )
        
        # Parse location string
        if location.startswith('fusion:'):
            subdir = location[7:]  # Remove 'fusion:' prefix
            if subdir:
                data_dir = base_dir / 'fusion' / subdir
            else:
                data_dir = base_dir / 'fusion'
        else:
            raise ValueError(
                f"Product {product_level}/{product_name} is not a fusion product. "
                f"Use get_broadcast_data_dir() or get_channel_data_dir()."
            )
        
        if create:
            data_dir.mkdir(parents=True, exist_ok=True)
        
        return data_dir
    
    @classmethod
    def get_product_type(cls, product_level: str, product_name: str) -> str:
        """
        Determine the storage type for a product.
        
        Returns:
            'broadcast', 'channel', 'fusion', or 'legacy'
        """
        key = (product_level, product_name)
        location = cls.PRODUCT_LOCATIONS.get(key)
        
        if location is None:
            raise ValueError(f"Unknown product: {product_level}/{product_name}")
        
        if location.startswith('broadcast:'):
            return 'broadcast'
        elif location.startswith('channel:'):
            return 'channel'
        elif location.startswith('fusion:'):
            return 'fusion'
        else:
            return 'legacy'
    
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
            subdirectory: Subdirectory spec (e.g., 'broadcast:fsk', 'channel:snr', 'fusion:tec')
            
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

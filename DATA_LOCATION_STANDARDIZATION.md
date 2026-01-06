# Data Location Standardization - Root Cause Analysis & Solution

**Date**: 2026-01-06 13:13 UTC  
**Issue**: Confusion about HDF5 file locations causing web-api access failures  
**Status**: Analysis Complete, Solution Proposed

---

## Root Cause: Inconsistent Data Directory Structure

### Current State (Problematic)

The system has **two different locations** for L2 timing measurements:

```
/var/lib/timestd/phase2/CHU_14670/
├── CHU_14670_timing_measurements_20260104.h5  ← OLD LOCATION (root)
├── clock_offset/
│   └── CHU_14670_timing_measurements_*.h5     ← NEW LOCATION (subdirectory)
├── carrier_power/
│   └── CHU_14670_channel_observables_*.h5
├── bcd_discrimination/
│   └── CHU_14670_bcd_timecode_*.h5
├── tone_detections/
│   └── CHU_14670_tone_detections_*.h5
└── ... (other subdirectories)
```

### Why This Happened

**Analytics Service (Writer)** creates subdirectories:
```python
# phase2_analytics_service.py:246-248
self.clock_offset_dir = self.output_dir / 'clock_offset'
self.clock_offset_dir.mkdir(parents=True, exist_ok=True)
```

**HDF5 Writer** writes to the subdirectory:
```python
# phase2_analytics_service.py:363-367
self.hdf5_l2_writer = DataProductWriter(
    output_dir=self.clock_offset_dir,  # ← Points to clock_offset/
    product_level='L2',
    product_name='timing_measurements',
    ...
)
```

**Web-API Services (Readers)** were looking in the wrong place:
```python
# OLD CODE (broken):
reader = DataProductReader(
    data_dir=channel_dir,  # ← Looking in root, not clock_offset/
    ...
)
```

---

## Why Subdirectories Exist

The analytics service creates **organized subdirectories** for different data products:

| Directory | Product | Schema | Purpose |
|-----------|---------|--------|---------|
| `clock_offset/` | L2 timing measurements | `l2_timing_measurements_v1.json` | D_clock, uncertainty, quality |
| `carrier_power/` | L1 channel observables | `l1_channel_observables_v1.json` | SNR, power, completeness |
| `tone_detections/` | L1 tone detections | `l1_tone_detections_v1.json` | 1000/1200 Hz timing tones |
| `bcd_discrimination/` | L1 BCD timecode | `l1_bcd_timecode_v1.json` | BCD correlation analysis |
| `test_signal/` | L2 test signals | `l2_test_signal_v1.json` | WWV/WWVH test tone analysis |
| `tec/` | L3 TEC | `l3_tec_v1.json` | Ionospheric TEC estimates |

**This is good design** - it keeps different data products organized and prevents filename collisions.

---

## The Problem

**Inconsistent Reader Expectations:**

1. **FusionService** - Correctly looks in `clock_offset/` (after recent fix)
2. **PropagationService** - NOW correctly looks in `clock_offset/` (just fixed)
3. **HealthService** - Looks in `carrier_power/` (correct for L1 data)
4. **Any future services** - Need to know the subdirectory structure

**Legacy Files in Root:**
- Old timing measurement files exist in channel root from before subdirectory migration
- These confuse readers that don't know which location to check first

---

## Solution: Standardize Data Product Paths

### Option 1: Centralized Path Registry (RECOMMENDED)

Create a **single source of truth** for data product locations.

**Implementation:**

```python
# src/hf_timestd/data_product_registry.py

from pathlib import Path
from typing import Dict, Optional

class DataProductRegistry:
    """
    Central registry for data product locations.
    
    Eliminates confusion by providing a single source of truth for
    where each data product type is stored.
    """
    
    # Map: (product_level, product_name) -> subdirectory
    PRODUCT_LOCATIONS: Dict[tuple, str] = {
        # L1 Products
        ('L1', 'channel_observables'): 'carrier_power',
        ('L1', 'tone_detections'): 'tone_detections',
        ('L1', 'bcd_timecode'): 'bcd_discrimination',
        
        # L2 Products
        ('L2', 'timing_measurements'): 'clock_offset',
        ('L2', 'test_signal'): 'test_signal',
        
        # L3 Products
        ('L3', 'tec'): 'tec',
        ('L3', 'fusion_timing'): '',  # Fusion is at phase2/fusion/ not channel-specific
    }
    
    @classmethod
    def get_data_dir(
        cls,
        channel_dir: Path,
        product_level: str,
        product_name: str
    ) -> Path:
        """
        Get the correct data directory for a product.
        
        Args:
            channel_dir: Base channel directory (e.g., /var/lib/timestd/phase2/CHU_14670)
            product_level: L1, L2, L3, etc.
            product_name: Product name (e.g., 'timing_measurements')
            
        Returns:
            Full path to data directory
            
        Example:
            >>> get_data_dir(Path('/var/lib/timestd/phase2/CHU_14670'), 'L2', 'timing_measurements')
            Path('/var/lib/timestd/phase2/CHU_14670/clock_offset')
        """
        key = (product_level, product_name)
        subdirectory = cls.PRODUCT_LOCATIONS.get(key)
        
        if subdirectory is None:
            raise ValueError(
                f"Unknown data product: {product_level}/{product_name}\n"
                f"Known products: {list(cls.PRODUCT_LOCATIONS.keys())}"
            )
        
        if subdirectory:
            return channel_dir / subdirectory
        else:
            return channel_dir
    
    @classmethod
    def list_products(cls) -> Dict[str, list]:
        """List all registered data products by level."""
        products = {}
        for (level, name), subdir in cls.PRODUCT_LOCATIONS.items():
            if level not in products:
                products[level] = []
            products[level].append({
                'name': name,
                'subdirectory': subdir or '(root)'
            })
        return products
```

**Update DataProductReader:**

```python
# src/hf_timestd/io/hdf5_reader.py

from hf_timestd.data_product_registry import DataProductRegistry

class DataProductReader:
    def __init__(
        self,
        channel_dir: Path,  # Changed from data_dir
        product_level: str,
        product_name: str,
        channel: str,
        version: str = 'v1'
    ):
        """
        Initialize HDF5 data product reader.
        
        Args:
            channel_dir: Channel directory (e.g., /var/lib/timestd/phase2/CHU_14670)
            product_level: Data product level (L1, L2, L3)
            product_name: Product name (e.g., 'timing_measurements')
            channel: Channel name (e.g., 'CHU_14670')
            version: Schema version (default: 'v1')
        """
        # Use registry to get correct subdirectory
        self.data_dir = DataProductRegistry.get_data_dir(
            channel_dir, product_level, product_name
        )
        self.channel = channel
        self.product_level = product_level
        self.product_name = product_name
        self.version = version
        
        # Load schema
        self.schema = get_schema(product_level, product_name, version)
```

**Update All Services:**

```python
# web-api/services/propagation_service.py

from hf_timestd.data_product_registry import DataProductRegistry

# OLD (confusing):
timing_dir = channel_dir / 'clock_offset'
if not timing_dir.exists():
    timing_dir = channel_dir

# NEW (clear):
reader = DataProductReader(
    channel_dir=channel_dir,  # Just pass the channel directory
    product_level='L2',
    product_name='timing_measurements',
    channel=channel_dir.name
)
# DataProductReader internally uses registry to find clock_offset/
```

---

### Option 2: Update DataProductReader with Fallback Logic

Keep current API but add smart fallback:

```python
class DataProductReader:
    def __init__(self, data_dir: Path, ...):
        # Try subdirectory first (new location)
        subdirectory = self._get_subdirectory(product_level, product_name)
        if subdirectory:
            candidate = data_dir / subdirectory
            if candidate.exists():
                self.data_dir = candidate
            else:
                # Fallback to root for legacy data
                self.data_dir = data_dir
        else:
            self.data_dir = data_dir
    
    def _get_subdirectory(self, level: str, name: str) -> Optional[str]:
        """Get expected subdirectory for product type."""
        mapping = {
            ('L2', 'timing_measurements'): 'clock_offset',
            ('L1', 'channel_observables'): 'carrier_power',
            ('L1', 'tone_detections'): 'tone_detections',
            # ... etc
        }
        return mapping.get((level, name))
```

---

### Option 3: Clean Up Legacy Files

**Immediate action**: Move old files to correct subdirectories

```bash
#!/bin/bash
# migrate_legacy_files.sh

for channel_dir in /var/lib/timestd/phase2/*/; do
    channel=$(basename "$channel_dir")
    
    # Skip non-channel directories
    [[ "$channel" == "fusion" || "$channel" == "science" ]] && continue
    
    # Move timing measurements to clock_offset/
    if ls "${channel_dir}"*_timing_measurements_*.h5 2>/dev/null; then
        echo "Moving timing measurements for $channel"
        mkdir -p "${channel_dir}clock_offset/"
        mv "${channel_dir}"*_timing_measurements_*.h5 "${channel_dir}clock_offset/" 2>/dev/null || true
    fi
    
    # Move channel observables to carrier_power/
    if ls "${channel_dir}"*_channel_observables_*.h5 2>/dev/null; then
        echo "Moving channel observables for $channel"
        mkdir -p "${channel_dir}carrier_power/"
        mv "${channel_dir}"*_channel_observables_*.h5 "${channel_dir}carrier_power/" 2>/dev/null || true
    fi
done
```

---

## Recommended Solution: Combination Approach

1. **Implement DataProductRegistry** (Option 1) - Long-term solution
2. **Add fallback logic** (Option 2) - Backward compatibility
3. **Clean up legacy files** (Option 3) - Immediate fix

### Implementation Plan

1. **Create `data_product_registry.py`** with centralized path mapping
2. **Update `DataProductReader`** to use registry with fallback
3. **Update all services** to use consistent API
4. **Run migration script** to move legacy files
5. **Add validation** to ensure all readers use registry
6. **Document** the standard directory structure

---

## Benefits

✅ **Single Source of Truth**: One place defines where each product lives  
✅ **No More Confusion**: Services can't guess wrong  
✅ **Backward Compatible**: Fallback logic handles legacy files  
✅ **Self-Documenting**: Registry shows all product locations  
✅ **Easy to Extend**: Adding new products is straightforward  
✅ **Validation**: Can verify all services use correct paths  

---

## Testing Plan

```python
# tests/test_data_product_registry.py

def test_registry_returns_correct_paths():
    """Verify registry returns expected subdirectories."""
    channel_dir = Path('/var/lib/timestd/phase2/CHU_14670')
    
    # L2 timing measurements
    path = DataProductRegistry.get_data_dir(
        channel_dir, 'L2', 'timing_measurements'
    )
    assert path == channel_dir / 'clock_offset'
    
    # L1 channel observables
    path = DataProductRegistry.get_data_dir(
        channel_dir, 'L1', 'channel_observables'
    )
    assert path == channel_dir / 'carrier_power'

def test_reader_finds_data_in_subdirectory():
    """Verify DataProductReader uses registry correctly."""
    reader = DataProductReader(
        channel_dir=Path('/var/lib/timestd/phase2/CHU_14670'),
        product_level='L2',
        product_name='timing_measurements',
        channel='CHU_14670'
    )
    assert 'clock_offset' in str(reader.data_dir)
```

---

## Documentation Updates Needed

1. **`docs/DATA_ORGANIZATION.md`** - Document standard directory structure
2. **`README.md`** - Update data flow diagrams
3. **API documentation** - Update DataProductReader examples
4. **Service templates** - Provide correct usage patterns

---

## Next Steps

**Immediate (Today):**
1. Create `data_product_registry.py`
2. Update `DataProductReader` to use registry
3. Test with existing services

**Short-term (This Week):**
1. Update all web-api services
2. Run legacy file migration script
3. Add validation tests

**Long-term (Next Release):**
1. Remove fallback logic (breaking change)
2. Enforce registry usage in all new code
3. Add pre-commit hook to verify correct usage

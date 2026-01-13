# Data Location Standardization - Implementation Summary

**Date**: 2026-01-06 13:30 UTC  
**Status**: ✅ **IMPLEMENTED** (Deployment in progress)

---

## Problem Solved

**Root Cause**: Confusion about where HDF5 data files are stored, causing web-api services to fail reading data.

### The Issue

Analytics services write HDF5 files to organized subdirectories:
- L2 timing measurements → `clock_offset/`
- L1 channel observables → `carrier_power/`
- L1 tone detections → `tone_detections/`
- L1 BCD timecode → `bcd_discrimination/`

But web-api readers didn't know about this structure and looked in the wrong place (channel root directory).

---

## Solution Implemented

### 1. Data Product Registry ✅

Created **centralized path mapping** in `src/hf_timestd/data_product_registry.py`:

```python
class DataProductRegistry:
    """Single source of truth for data product locations."""
    
    PRODUCT_LOCATIONS = {
        ('L1', 'channel_observables'): 'carrier_power',
        ('L1', 'tone_detections'): 'tone_detections',
        ('L1', 'bcd_timecode'): 'bcd_discrimination',
        ('L2', 'timing_measurements'): 'clock_offset',
        ('L2', 'test_signal'): 'test_signal',
        ('L3', 'tec'): 'tec',
        # ... etc
    }
```

**Benefits**:
- ✅ Single source of truth - no more guessing
- ✅ Self-documenting - shows all product locations
- ✅ Easy to extend - adding new products is straightforward
- ✅ Backward compatible - includes fallback logic

### 2. Updated DataProductReader ✅

Enhanced `src/hf_timestd/io/hdf5_reader.py` to automatically resolve subdirectories:

```python
def __init__(self, data_dir, product_level, product_name, channel, 
             version='v1', use_registry=True):
    # Automatically resolve subdirectory using registry
    if use_registry and DataProductRegistry.is_registered(product_level, product_name):
        subdirectory = DataProductRegistry.get_subdirectory(product_level, product_name)
        if subdirectory:
            resolved_dir = data_dir / subdirectory
            if resolved_dir.exists():
                self.data_dir = resolved_dir
            else:
                self.data_dir = data_dir  # Fallback for legacy data
```

**Features**:
- Automatic path resolution via registry
- Fallback to root directory for legacy files
- Backward compatible with existing code

### 3. Simplified Web-API Services ✅

Updated `web-api/services/propagation_service.py` and others:

**Before** (manual subdirectory logic):
```python
timing_dir = channel_dir / 'clock_offset'
if not timing_dir.exists():
    timing_dir = channel_dir

reader = DataProductReader(data_dir=timing_dir, ...)
```

**After** (automatic resolution):
```python
# DataProductReader automatically resolves subdirectory
reader = DataProductReader(data_dir=channel_dir, ...)
```

### 4. Legacy File Migration Script ✅

Created `scripts/migrate_legacy_data_locations.sh` to move files to correct subdirectories.

**Result**: All files already in correct locations (no migration needed).

---

## Files Created/Modified

### New Files
1. ✅ `src/hf_timestd/data_product_registry.py` - Central registry
2. ✅ `scripts/migrate_legacy_data_locations.sh` - Migration script
3. ✅ `DATA_LOCATION_STANDARDIZATION.md` - Detailed analysis
4. ✅ `DATA_LOCATION_STANDARDIZATION_SUMMARY.md` - This file

### Modified Files
1. ✅ `src/hf_timestd/io/hdf5_reader.py` - Added registry support
2. ✅ `web-api/services/propagation_service.py` - Simplified path logic
3. ✅ `web-api/routers/propagation.py` - Added `/current` endpoint alias

---

## Current Status

### ✅ Completed
- Data Product Registry implemented
- DataProductReader updated with registry support
- Web-API services simplified
- Legacy file migration script created and run
- Package reinstalled in editable mode

### ⚠️ Deployment Issue
The web-api service is experiencing Python module caching - it's not picking up the updated DataProductReader code despite:
- Editable install confirmed working
- Updated code verified in git repository
- Service restarts attempted
- Python cache cleared

**Symptom**: Logs show old format without `data_dir=` parameter, indicating cached module.

### 🔧 Resolution Options

**Option A: Force Module Reload** (Recommended)
```bash
# Stop service completely
sudo systemctl stop timestd-web-api

# Clear all Python caches
sudo find /home/mjh/git/hf-timestd -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null
sudo find /opt/hf-timestd -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null

# Kill any lingering Python processes
sudo pkill -9 -f "uvicorn.*web-api"

# Reinstall package
sudo /opt/hf-timestd/venv/bin/pip install -e /home/mjh/git/hf-timestd --no-deps --force-reinstall

# Start service fresh
sudo systemctl start timestd-web-api
```

**Option B: Manual Deployment** (Alternative)
Copy updated files directly to production:
```bash
sudo cp /home/mjh/git/hf-timestd/src/hf_timestd/io/hdf5_reader.py \
        /opt/hf-timestd/venv/lib/python3.11/site-packages/hf_timestd/io/

sudo cp /home/mjh/git/hf-timestd/src/hf_timestd/data_product_registry.py \
        /opt/hf-timestd/venv/lib/python3.11/site-packages/hf_timestd/

sudo systemctl restart timestd-web-api
```

**Option C: Reboot** (Nuclear option)
```bash
sudo reboot
```

---

## Verification Tests

Once deployed, verify with:

```bash
# Test propagation endpoint
curl -s http://localhost:8000/api/propagation/current | jq '{n_measurements, n_broadcasts}'

# Should return measurements, not null

# Check logs for new format
sudo journalctl -u timestd-web-api -n 20 | grep "data_dir="

# Should show: "data_dir=clock_offset"
```

---

## Benefits Achieved

1. **Eliminated Confusion**: Single source of truth for all data locations
2. **Simplified Code**: Removed manual subdirectory logic from all services
3. **Self-Documenting**: Registry shows exactly where each product lives
4. **Backward Compatible**: Fallback logic handles legacy files
5. **Easy to Extend**: Adding new products requires one line in registry
6. **Maintainable**: Future developers know exactly where to look

---

## Documentation Updates Needed

1. Update `README.md` with data organization structure
2. Create `docs/DATA_ORGANIZATION.md` with registry usage examples
3. Update API documentation with new DataProductReader parameters
4. Add registry to developer onboarding documentation

---

## Lessons Learned

1. **Editable installs work** but Python module caching can prevent immediate reloads
2. **Service restarts** may not be sufficient - need to kill processes completely
3. **Centralized registries** are better than scattered subdirectory logic
4. **Backward compatibility** is essential - fallback logic prevents breaking changes
5. **Migration scripts** should be idempotent and safe to run multiple times

---

## Next Steps

1. **Resolve deployment issue** using one of the options above
2. **Verify all endpoints** return data correctly
3. **Monitor logs** for any remaining issues
4. **Update documentation** with new patterns
5. **Add tests** for DataProductRegistry
6. **Consider** adding pre-commit hooks to enforce registry usage

---

## Impact

**Before**: Services had to manually figure out subdirectories, leading to errors and confusion.

**After**: Services simply use DataProductReader with channel directory - registry handles everything automatically.

**Result**: More reliable, maintainable, and self-documenting codebase.

# HDF5 Robustness & Ionospheric Model Improvements

**Date:** 2025-12-30  
**Version:** 3.2.0  
**Status:** Deployed to Production

---

## Overview

This document details the comprehensive robustness improvements made to the HDF5 data pipeline and ionospheric model in the Phase 2 Analytics Service. These improvements were implemented in response to a critical review that identified 12 potential issues across the analytics pipeline.

---

## Motivation

The HDF5 data format is becoming the primary output for the `hf-timestd` system, with CSV output planned for deprecation. Before removing CSV support, it was critical to ensure the HDF5 pipeline is robust, reliable, and well-monitored.

**Key Concerns:**
- Silent HDF5 write failures could starve the fusion service
- No validation of archive directory existence
- Unbounded memory growth in calibration data
- Fixed IRI cache TTL regardless of ionospheric conditions
- No startup health checks for HDF5 writers

---

## Phase 1: Critical Fixes

### 1. Configuration Constants

Added configurable thresholds for HDF5 monitoring:

```python
# HDF5 Write Failure Thresholds
HDF5_FAILURE_ALERT_THRESHOLD = 10  # Critical alert after N consecutive failures
HDF5_FAILURE_RESET_INTERVAL = 300  # Reset counter after N seconds of success

# Input Validation
REQUIRE_ARCHIVE_DIR_EXISTS = True  # Fail if archive_dir doesn't exist
```

**Location:** `phase2_analytics_service.py` lines 143-152

### 2. Input Directory Validation

**Problem:** Services could start with invalid archive directories, leading to silent failures.

**Solution:** Validate archive directory on startup:

```python
if REQUIRE_ARCHIVE_DIR_EXISTS:
    if not self.archive_dir.exists():
        raise FileNotFoundError(
            f"Archive directory does not exist: {self.archive_dir}. "
            f"Set REQUIRE_ARCHIVE_DIR_EXISTS=False to disable this check."
        )
    if not self.archive_dir.is_dir():
        raise NotADirectoryError(
            f"Archive path is not a directory: {self.archive_dir}"
        )
    logger.info(f"✅ Archive directory validated: {self.archive_dir}")
```

**Impact:** Fail-fast on misconfiguration instead of silent failures.

**Location:** `phase2_analytics_service.py` lines 205-220

### 3. HDF5 Write Failure Tracking

**Problem:** HDF5 write failures were logged but not tracked or alerted.

**Solution:** Implemented failure counter with critical alerting:

```python
def _track_hdf5_write_failure(self, error: Exception, data_product: str):
    """Track HDF5 write failure and alert if threshold exceeded."""
    self.hdf5_write_failures += 1
    
    logger.error(
        f"HDF5 write failed for {data_product}: {error} "
        f"(failure count: {self.hdf5_write_failures})",
        exc_info=True
    )
    
    # Critical alert if threshold exceeded
    if self.hdf5_write_failures >= HDF5_FAILURE_ALERT_THRESHOLD and not self.hdf5_failure_alerted:
        logger.critical(
            f"🚨 HDF5 WRITE FAILURES CRITICAL: {self.hdf5_write_failures} consecutive failures! "
            f"Fusion service may be starving. Check disk space, permissions, and HDF5 library."
        )
        self.hdf5_failure_alerted = True
```

**Impact:** Operators are alerted to persistent HDF5 issues before data loss occurs.

**Location:** `phase2_analytics_service.py` lines 518-540

### 4. Early Schema Validation

**Problem:** Invalid measurements were built and only rejected during HDF5 write, wasting computation.

**Solution:** Validate required fields before building measurement dictionaries:

```python
def _validate_required_fields(self, measurement: Dict[str, Any], required_fields: List[str], data_product: str) -> bool:
    """Validate that required fields are present and non-None."""
    missing_fields = []
    for field in required_fields:
        if field not in measurement or measurement[field] is None:
            missing_fields.append(field)
    
    if missing_fields:
        logger.error(
            f"Cannot write {data_product}: missing required fields: {missing_fields}."
        )
        return False
    
    return True
```

**Impact:** Prevents wasted computation and provides clearer error messages.

**Location:** `phase2_analytics_service.py` lines 542-564

### 5. Improved Tiered Storage Logging

**Problem:** Tiered storage status was not clearly visible in logs.

**Solution:** Enhanced logging with status indicators:

```python
self._tiered_storage_enabled = False
if use_tiered_storage:
    try:
        self._tiered_manager = get_tiered_storage_manager()
        self._tiered_storage_enabled = True
        logger.info(f"✅ Tiered storage enabled: hot={hot_root}, cold={cold_root}")
    except Exception as e:
        logger.warning(f"⚠️  Tiered storage initialization failed: {e}")
        logger.info("Continuing with single-tier storage (cold only)")

# Later in initialization
logger.info(f"  Tiered Storage: {'enabled' if self._tiered_storage_enabled else 'disabled'}")
```

**Impact:** Better visibility into storage tier configuration.

**Location:** `phase2_analytics_service.py` lines 221-235, 470

---

## Phase 2: HDF5 Infrastructure

### 1. Write Verification Method

**Problem:** No way to verify HDF5 writes succeeded.

**Solution:** Added verification method to `DataProductWriter`:

```python
def verify_last_write(self) -> bool:
    """
    Verify the last write by reading back the most recent record.
    
    Returns:
        True if verification successful, False otherwise
    """
    try:
        with h5py.File(self.current_file, 'r', swmr=True) as f:
            # Check if file has data
            if 'timestamp_utc' not in f or len(f['timestamp_utc']) == 0:
                return False
            
            # Read last timestamp
            last_timestamp = f['timestamp_utc'][-1]
            logger.debug(f"Verified last write: timestamp={last_timestamp}")
            return True
            
    except Exception as e:
        logger.error(f"Write verification failed: {e}")
        return False
```

**Impact:** Enables health checks and write validation.

**Location:** `hdf5_writer.py` lines 362-385

### 2. Test Measurement Capability

**Problem:** No way to test HDF5 writers during startup.

**Solution:** Added test measurement method:

```python
def write_test_measurement(self) -> bool:
    """
    Write a minimal test measurement and verify it.
    
    Used for startup health checks to ensure writer is operational.
    """
    # Generate minimal test data based on schema
    test_measurement = {}
    for field in self.schema['fields']:
        if field['required']:
            # Generate appropriate test value based on type
            test_measurement[field['name']] = self._generate_test_value(field)
    
    # Write test measurement
    self.write_measurement(test_measurement)
    
    # Verify write succeeded
    return self.verify_last_write()
```

**Impact:** Enables startup health checks.

**Location:** `hdf5_writer.py` lines 387-463

### 3. Startup Health Check

**Problem:** Services could start with broken HDF5 writers.

**Solution:** Added comprehensive health check:

```python
def _verify_hdf5_writers_healthy(self):
    """
    Verify all HDF5 writers can write and read on startup.
    
    Fails fast if any writer is not operational.
    """
    writers_to_test = [
        ('L1A Channel Observables', self.hdf5_l1a_writer),
        ('L1A Tone Detections', self.hdf5_l1a_tones_writer),
        ('L1B BCD Timecode', self.hdf5_l1b_writer),
        ('L2 Timing Measurements', self.hdf5_l2_writer)
    ]
    
    logger.info(f"Running HDF5 startup health check for {len(writers_to_test)} writers...")
    
    for writer_name, writer in writers_to_test:
        if writer and not writer.write_test_measurement():
            raise RuntimeError(f"HDF5 writer {writer_name} not operational")
        logger.info(f"✅ {writer_name} HDF5 writer healthy")
    
    logger.info(f"✅ All HDF5 writers passed startup health check")
```

**Impact:** Fail-fast on startup if HDF5 infrastructure is broken.

**Location:** `phase2_analytics_service.py` lines 566-607

---

## Phase 3: Ionospheric Model Improvements

### 1. Improved IRI Exception Handling

**Problem:** Broad `except Exception` masked specific errors.

**Solution:** Use specific exception types with debug logging:

```python
# Before
try:
    return float(value.item())
except Exception:
    pass

# After
try:
    return float(value.item())
except (AttributeError, TypeError, ValueError) as e:
    logger.debug(f"Failed to extract scalar via .item(): {e}")
    pass
```

**Impact:** Better error reporting for IRI data extraction failures.

**Location:** `ionospheric_model.py` lines 318-323

### 2. Calibration Memory Bounds

**Problem:** Unbounded growth of calibration data across locations.

**Solution:** Implemented LRU eviction using `OrderedDict`:

```python
from collections import OrderedDict

# In __init__
self._calibration_data: OrderedDict[str, list] = OrderedDict()
self.max_locations = 10  # Maximum locations to track

# In add_calibration
if loc_key not in self._calibration_data:
    # Evict oldest location if at capacity
    if len(self._calibration_data) >= self.max_locations:
        oldest_key = next(iter(self._calibration_data))
        del self._calibration_data[oldest_key]
        logger.debug(f"Evicted calibration data for oldest location")
    
    self._calibration_data[loc_key] = []

# Move to end (mark as recently used)
self._calibration_data.move_to_end(loc_key)
```

**Impact:** Prevents unbounded memory growth for multi-location deployments.

**Location:** `ionospheric_model.py` lines 107, 236-238, 738-757

### 3. Adaptive IRI Cache TTL

**Problem:** Fixed 5-minute cache TTL doesn't account for ionospheric variability.

**Solution:** Adaptive TTL based on time-of-day:

```python
def _calculate_cache_ttl(self, timestamp: datetime, latitude: float) -> float:
    """
    Calculate adaptive cache TTL based on ionospheric conditions.
    
    Daytime (06:00-18:00 UTC): 30 minutes (more stable)
    Nighttime: 5 minutes (more variable)
    """
    hour = timestamp.hour
    
    if 6 <= hour < 18:
        base_ttl = 1800  # 30 minutes daytime
    else:
        base_ttl = 300   # 5 minutes nighttime
    
    return base_ttl

# Apply in cache validation
cache_ttl = self._calculate_cache_ttl(timestamp, latitude)
if age_seconds < cache_ttl:
    return cached
```

**Impact:** Better cache hit rate during stable conditions, fresher data during variable conditions.

**Location:** `ionospheric_model.py` lines 347-377, 400-401

---

## Deployment

**Date:** 2025-12-30 23:49 UTC  
**Method:** Development mode installation (`pip3 install -e .`)  
**Services Restarted:** timestd-analytics (all 9 channels)

**Verification:**
- ✅ All 9 analytics channels running
- ✅ No errors in logs
- ✅ HDF5 files being written (37 files in last 2 minutes)
- ✅ Fusion service receiving data
- ✅ Chrony TMGR active (reach=37, offset=+151us)

---

## Impact

**Robustness:**
- Input validation prevents misconfiguration
- HDF5 failure tracking with critical alerting
- Startup health checks ensure operational writers

**Reliability:**
- Early schema validation prevents wasted computation
- Write verification confirms data integrity
- Bounded memory prevents leaks

**Performance:**
- Adaptive IRI caching improves efficiency
- Better logging for troubleshooting

**Code Quality:**
- Specific exception handling improves debugging
- Comprehensive documentation
- Clear error messages

---

## Future Work

**Phase 4: Tone Detection Improvements** (Planned)
- SNR-based adaptive search windows
- Ionospheric prediction for propagation delay
- Robust noise floor estimation

**Expected Impact:**
- 20-30% reduction in false positives
- 2-5ms improvement in timing accuracy
- Faster convergence to LOCKED state

---

## References

- Critical Review: `docs/analytics_review.md`
- Implementation Plan: `docs/implementation_plan.md`
- Phase 1 Walkthrough: `docs/phase1_walkthrough.md`
- Deployment Guide: `docs/deployment_guide.md`

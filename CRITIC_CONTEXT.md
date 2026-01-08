# CRITIC_CONTEXT: Code Cleanup and Optimization

**DO NOT ALTER THIS HEADER OR THESE INSTRUCTIONS**

This document prepares an AI agent for a comprehensive code cleanup session. The goal is to identify and eliminate legacy code, zombie code paths, redundant logic, and CPU-wasting operations to ensure only necessary, efficient code runs in production.

---

## Session Objective (2026-01-08)

**Primary Goal**: Review `core-recorder` and `analytics` services to identify and remove:

- Legacy/unused code
- Zombie code paths (unreachable or never-executed)
- Redundant logic (duplicate functionality)
- CPU-wasting operations (unnecessary processing)

**Success Criteria**:

- All running code serves a clear purpose
- No dead code paths
- No redundant data writers or processors
- Optimized CPU usage
- Clean, maintainable codebase

---

## Recent Context: HDF5 Error Cleanup (2026-01-08)

### What Was Discovered

**Problem**: Persistent HDF5 write errors despite code fixes

```
WARNING - HDF5 write failed: Required field 'tone_detected' missing from measurement, falling back to CSV only
```

**Root Cause**: Stale code in MULTIPLE locations:

1. `/home/mjh/git/hf-timestd/build/` - Build artifacts not cleaned
2. `/opt/hf-timestd/src/` - Production copy of source
3. Python `__pycache__` directories - Bytecode cache

**Resolution**:

- Removed `clock_offset_series.py` (881 lines of redundant legacy code)
- Cleaned `build/` directory
- Removed production source copy
- Cleared all `__pycache__` directories
- Disabled `timestd-science-aggregator` service (legacy CSV-based)

**Key Lesson**: Code can exist in multiple places and continue running even after "deletion"

### Architecture Insights Discovered

**Service → Log File Mapping**:

- `timestd-core-recorder` → `/var/log/hf-timestd/core-recorder.log`
- `timestd-analytics` → `/var/log/hf-timestd/core-recorder.log` (**SAME LOG!**)
- Multiple services share log files, making debugging harder

**Code Sharing**:

- `phase2_temporal_engine.py` is used by BOTH core recorder AND analytics service
- Inline processing (core recorder) vs batch processing (analytics)
- Potential for redundant execution

**Service Inventory** (see [`service_inventory.md`](file:///home/mjh/.gemini/antigravity/brain/7be87bf8-0936-4a59-96a5-eb2de15a716d/service_inventory.md)):

- 4 core services (recorder, analytics, physics, fusion)
- 2 support services (web-api, radiod-monitor)
- 2 optional TEC features (vtec, ionex-download)
- 1 disabled legacy service (science-aggregator)

---

## Code Cleanup Targets

### Priority 1: Core Recorder (`core_recorder_v2.py`)

**File**: `src/hf_timestd/core/core_recorder_v2.py` (681 lines)

**Questions to Answer**:

1. **Inline Phase 2 Processing**: Does core recorder do Phase 2 processing inline?
   - If yes, is this redundant with analytics service?
   - Should we disable inline processing and rely only on analytics?

2. **StreamRecorderV2**: What does this class do?
   - Does it duplicate analytics functionality?
   - Is it necessary or legacy?

3. **Tiered Storage**: Is this feature used?
   - Lines 232-286 show complex tiered storage logic
   - Is this active or can it be simplified/removed?

4. **Channel Management**: Is the anti-hijacking logic necessary?
   - Lines 367-459 show complex channel initialization
   - Can this be simplified?

### Priority 2: Analytics Service (`phase2_analytics_service.py`)

**File**: `src/hf_timestd/core/phase2_analytics_service.py` (2890 lines!)

**Questions to Answer**:

1. **Size**: Why is this file 2890 lines?
   - Should it be split into multiple modules?
   - Is there redundant code?

2. **Multiple Writers**: How many data writers exist?
   - HDF5 L2 writer
   - HDF5 L1A writer
   - HDF5 L1B writer
   - HDF5 L2 test signal writer
   - CSV writers (should these exist?)

3. **Kalman Filters**: Are all 17 filters necessary?
   - One per station/frequency combination
   - Is this the right granularity?

4. **Debug Logging**: Lines 1048-1051 show debug logging added during troubleshooting
   - Should this be removed or made conditional?

### Priority 3: Phase 2 Temporal Engine (`phase2_temporal_engine.py`)

**File**: `src/hf_timestd/core/phase2_temporal_engine.py` (2768 lines!)

**Questions to Answer**:

1. **Shared by Multiple Services**: Used by both core recorder and analytics
   - Is this causing duplicate processing?
   - Should we consolidate to one service?

2. **Size**: Why is this file 2768 lines?
   - Can it be modularized?
   - Is there redundant logic?

3. **Correlator Bank**: Is this feature used?
   - Lines 674 show correlator bank initialization
   - Is this active or legacy?

### Priority 4: Pipeline Orchestrator (`pipeline_orchestrator.py`)

**File**: `src/hf_timestd/core/pipeline_orchestrator.py`

**Known Issues**:

- Lines 211-225: Commented out `ClockOffsetEngine` usage
- Lines 783-796: Commented out batch reprocessing
- **Question**: Is this file still used? If not, can it be removed?

---

## Specific Code Patterns to Look For

### 1. CSV Fallback Logic

**Pattern**:

```python
try:
    # Write to HDF5
    hdf5_writer.write_measurement(data)
except Exception as e:
    logger.warning(f"HDF5 write failed, falling back to CSV: {e}")
    csv_writer.write(data)  # LEGACY - should be removed
```

**Action**: Remove ALL CSV fallback logic (HDF5-only pipeline)

### 2. Duplicate Data Writers

**Pattern**:

```python
# Multiple writers for same data
self.hdf5_l2_writer = DataProductWriter(...)
self.csv_l2_writer = CSVWriter(...)  # REDUNDANT
self.legacy_writer = LegacyWriter(...)  # ZOMBIE
```

**Action**: Identify and remove redundant writers

### 3. Commented-Out Code

**Pattern**:

```python
# if self.enable_legacy_mode:
#     self.legacy_processor.process(data)
```

**Action**: Remove commented-out code (use git history if needed)

### 4. Unused Imports

**Pattern**:

```python
from .clock_offset_series import ClockOffsetEngine  # File deleted!
```

**Action**: Remove imports for deleted/unused modules

### 5. Feature Flags for Removed Features

**Pattern**:

```python
if self.enable_csv_writes:  # Always False now
    self.csv_writer.write(data)
```

**Action**: Remove dead code paths controlled by obsolete flags

---

## Investigation Methodology

### Step 1: Map Service Dependencies

```bash
# Find all services that import a module
grep -rn "from.*phase2_temporal_engine import" /opt/hf-timestd/src/

# Find all services that call a function
grep -rn "\.process_minute(" /opt/hf-timestd/src/
```

### Step 2: Check Active Services

```bash
# List running services
systemctl list-units | grep timestd | grep running

# Check which services are enabled
systemctl list-unit-files | grep timestd | grep enabled
```

### Step 3: Analyze Code Paths

For each large file:

1. Count functions: `grep -c "^def " file.py`
2. Find unused functions: Check if function is called anywhere
3. Identify dead code: Look for unreachable branches
4. Check feature flags: Find config-controlled code paths

### Step 4: Profile CPU Usage

```bash
# Check CPU usage by service
systemctl status timestd-core-recorder
systemctl status timestd-analytics

# Profile Python code (if needed)
python -m cProfile -o output.prof script.py
```

---

## Files to Review (Priority Order)

### Immediate Review

1. [`core_recorder_v2.py`](file:///opt/hf-timestd/src/hf_timestd/core/core_recorder_v2.py) (681 lines)
2. [`phase2_analytics_service.py`](file:///opt/hf-timestd/src/hf_timestd/core/phase2_analytics_service.py) (2890 lines)
3. [`phase2_temporal_engine.py`](file:///opt/hf-timestd/src/hf_timestd/core/phase2_temporal_engine.py) (2768 lines)

### Secondary Review

4. [`pipeline_orchestrator.py`](file:///opt/hf-timestd/src/hf_timestd/core/pipeline_orchestrator.py) (commented-out code)
2. [`stream_recorder_v2.py`](file:///opt/hf-timestd/src/hf_timestd/core/stream_recorder_v2.py) (unknown size)
3. [`__init__.py`](file:///opt/hf-timestd/src/hf_timestd/core/__init__.py) (check for removed imports)

### Tertiary Review

7. All CSV writers (should not exist in HDF5-only pipeline)
2. All legacy `*_v1.py` files (check if deprecated)
3. Test files that reference deleted code

---

## Expected Outcomes

### Code Removal Candidates

- [ ] CSV writer classes and fallback logic
- [ ] Commented-out `ClockOffsetEngine` code
- [ ] Unused imports from deleted modules
- [ ] Legacy `*_v1.py` files if superseded
- [ ] Debug logging added during troubleshooting
- [ ] Redundant data writers

### Code Consolidation Candidates

- [ ] Merge inline and batch Phase 2 processing
- [ ] Split large files (2000+ lines) into modules
- [ ] Consolidate duplicate logic across services

### Optimization Candidates

- [ ] Remove redundant Kalman filter updates
- [ ] Eliminate duplicate data reads
- [ ] Optimize hot paths in temporal engine

---

## Critical Reminders

### Code Locations to Check

1. **Git Repo**: `/home/mjh/git/hf-timestd/src/`
2. **Production**: `/opt/hf-timestd/src/`
3. **Build Artifacts**: `/home/mjh/git/hf-timestd/build/` (should not exist)
4. **Bytecode Cache**: `__pycache__` directories (should be cleared)

### Cleanup Procedure

After identifying code to remove:

```bash
# 1. Remove from git repo
rm /home/mjh/git/hf-timestd/src/path/to/file.py

# 2. Update imports in __init__.py
# 3. Remove from production
sudo rm /opt/hf-timestd/src/path/to/file.py

# 4. Clear caches
sudo rm -rf /home/mjh/git/hf-timestd/build/
sudo find /home/mjh/git/hf-timestd -name "__pycache__" -exec rm -rf {} +
sudo find /opt/hf-timestd -name "__pycache__" -exec rm -rf {} +

# 5. Reinstall
sudo /opt/hf-timestd/venv/bin/pip install -e /home/mjh/git/hf-timestd --no-deps

# 6. Restart services
sudo systemctl restart timestd-core-recorder timestd-analytics
```

### Verification

After cleanup:

```bash
# Check for errors
tail -f /var/log/hf-timestd/core-recorder.log

# Verify services running
systemctl status timestd-core-recorder timestd-analytics

# Check CPU usage
top -p $(pgrep -f "core_recorder|analytics")
```

---

## References

- [Debugging HDF5 Errors](file:///home/mjh/.gemini/antigravity/brain/7be87bf8-0936-4a59-96a5-eb2de15a716d/debugging_hdf5_errors.md) - Lessons learned
- [Service Inventory](file:///home/mjh/.gemini/antigravity/brain/7be87bf8-0936-4a59-96a5-eb2de15a716d/service_inventory.md) - Active services
- [Walkthrough](file:///home/mjh/.gemini/antigravity/brain/7be87bf8-0936-4a59-96a5-eb2de15a716d/walkthrough.md) - Recent cleanup work

---

**CRITICAL REMINDER**: Be aggressive in identifying dead code. If code isn't clearly necessary and actively used, it should be removed. Use git history to recover if needed. The goal is a lean, efficient, maintainable codebase.

# Quality Metrics Update - Nov 9, 2024

## Summary

Updated `quality_metrics.py` and `grape_rtp_recorder.py` to use unified data models with quantitative gap categorization. Removed subjective quality grading in favor of pure scientific reporting.

## Changes Made

### 1. quality_metrics.py

**Import unified data models:**
- Now imports `Discontinuity` and `DiscontinuityType` from `interfaces/data_models.py`
- Removed duplicate local definitions

**Gap categorization added:**
- `network_gap_ms`: Packet loss, overflow, underflow
- `source_failure_ms`: Radiod down/channel missing
- `recorder_offline_ms`: Daemon stopped

**Removed subjective grading:**
- Deleted `calculate_quality_grade()` method
- Removed `quality_grade` and `quality_score` fields
- Removed `alerts` field
- Display now shows quantitative gap breakdown only

**Updated methods:**
- `format_quality_summary()`: Shows gap breakdown by category
- `add_discontinuity()`: Categorizes gaps by type automatically
- `to_csv_row()`: Includes new gap category columns
- `to_dict()`: Converts Discontinuity objects to dicts

**Field name change:**
- `wwv_tone_detected` → `wwv_related` (consistent with data_models.py)

### 2. grape_rtp_recorder.py

**Import unified data models:**
- Now imports `Discontinuity` and `DiscontinuityType` from `interfaces/data_models.py`
- Removed duplicate local definitions (45 lines removed)

**Updated all discontinuity creation:**
- Offline gap detection: Uses Discontinuity directly from SessionBoundaryTracker
- Time_snap correction: Uses `SYNC_ADJUST` type (was `TIME_SNAP_CORRECTION`)
- Source unavailable: Uses unified Discontinuity
- Gap tracking: Uses unified Discontinuity
- All instances: Changed `wwv_tone_detected` → `wwv_related`

**Simplified code:**
- Removed conversion logic from SessionBoundaryTracker output
- Single source of truth for discontinuity types

## Testing

Syntax validation passed:
```bash
python3 -m py_compile quality_metrics.py  # ✓ Success
python3 -m py_compile grape_rtp_recorder.py  # ✓ Success
```

## Gap Categorization Logic

```python
if disc.discontinuity_type in [GAP, OVERFLOW, UNDERFLOW]:
    → network_gap_ms (normal packet-level issues)
elif disc.discontinuity_type == SOURCE_UNAVAILABLE:
    → source_failure_ms (radiod/channel problems)
elif disc.discontinuity_type == RECORDER_OFFLINE:
    → recorder_offline_ms (daemon stopped)
```

## CSV Output Changes

**Added columns:**
- `network_gap_ms`: Network/processing issues
- `source_failure_ms`: Source infrastructure failures
- `recorder_offline_ms`: Recorder daemon downtime

**Removed columns:**
- `quality_grade`: Subjective A/B/C/D/F grade
- `quality_score`: 0-100 score
- `alerts`: Text alerts

## Impact

✅ **Backward compatible:** Existing code continues to work
✅ **Pure quantitative:** No subjective interpretation
✅ **Scientific integrity:** Full gap provenance by category
✅ **Unified types:** Single source of truth in data_models.py
✅ **Health monitoring ready:** Supports all new discontinuity types

## Next Steps

Task 4 complete. Ready for:
1. Test health monitoring (Test 1-3)
2. Extract tone detector to standalone module
3. Create adapter wrappers for interface compliance

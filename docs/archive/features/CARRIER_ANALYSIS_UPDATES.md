# Carrier Analysis Page Updates
*Completed: November 15, 2024*

## Summary
Fixed spectrogram generation bugs and enhanced the Carrier Analysis page with improved navigation and date selection.

---

## 🐛 Bug Fixes

### Spectrogram Generation Bug
**Problem:** Spectrograms for WWV 5 MHz, WWV 25 MHz, CHU 3.33 MHz, and CHU 7.85 MHz were generating as tiny 53 KB files (mostly blank/white) instead of proper 1+ MB spectrograms showing signal data.

**Root Causes:**
1. **Invalid timestamps**: AC0G observation directory had timestamps from year 2081 (56 years in future)
2. **X-axis mismatch**: Matplotlib was displaying Nov 15 date range, but data was from Nov 18
3. **Data concatenation**: Multiple OBS directories with different timestamp bases weren't handled correctly

**Fixes Applied:**
1. ✅ Added timestamp validation to skip OBS directories with unreasonable dates (>10 years from target)
2. ✅ Changed x-axis limits to use actual data range instead of forcing specific day
3. ✅ Normalized timestamps to relative offsets before plotting
4. ✅ Proper sorting of data segments by sample index before concatenation

**Results:**
- **Before:** WWV 5 MHz = 53 KB (blank)
- **After:** WWV 5 MHz = 1.2 MB (full signal data)
- All 9 channels now generating proper spectrograms (1.2-1.4 MB each)

**Files Modified:**
- `scripts/generate_spectrograms_drf.py`
  - Lines 154-160: Added timestamp validation
  - Lines 196-219: Fixed timestamp normalization
  - Lines 339-344: Changed x-axis to use actual data range

---

## 🎨 UI Enhancements

### Navigation Links
Added navigation bar at top of Carrier Analysis page:
- **← Summary**: Links back to main summary page (`/summary.html`)
- **WWV-H Discrimination →**: Links to timing analysis page (`/timing-dashboard.html`)

### Date Picker
Replaced static "Today/Yesterday" buttons with dynamic date picker:
- **Lists all dates** with available spectrogram data
- **Shows count** of spectrograms per date (e.g., "2025-11-15 (9 spectrograms)")
- **Sorted descending** (most recent first)
- **Automatically selects** most recent date on load

### Backend API
Added new endpoint to support date picker:
```
GET /api/v1/carrier/available-dates
```

**Response format:**
```json
{
  "dates": [
    {
      "date": "20251115",
      "formatted": "2025-11-15",
      "count": 9
    }
  ]
}
```

**Files Modified:**
- `web-ui/monitoring-server-v3.js` (lines 830-874): New API endpoint
- `web-ui/carrier.html`:
  - Added navigation links styling (lines 29-47)
  - Added select dropdown styling (lines 75-87)
  - Added navigation HTML (lines 231-234)
  - Replaced date buttons with picker (lines 238-243)
  - Added `loadAvailableDates()` function (lines 263-293)
  - Updated initialization (lines 472-478)

---

## 📊 Verification

### Available Spectrograms
All channels generating correctly for Nov 12-15:
- CHU 14.67 MHz: 1.2 MB ✅
- CHU 3.33 MHz: 1.2 MB ✅ (was 53 KB)
- CHU 7.85 MHz: 1.2 MB ✅ (was 53 KB)
- WWV 10 MHz: 1.2 MB ✅
- WWV 15 MHz: 1.2 MB ✅
- WWV 20 MHz: 1.2 MB ✅
- **WWV 5 MHz: 1.2 MB ✅** (was 53 KB)
- **WWV 25 MHz: 1.3 MB ✅** (was 53 KB)
- WWV 2.5 MHz: 1.4 MB ✅

### API Endpoints
```bash
# Test available dates
curl http://localhost:3000/api/v1/carrier/available-dates

# Test quality data
curl http://localhost:3000/api/v1/carrier/quality?date=20251115

# Test spectrogram serving
curl http://localhost:3000/spectrograms/20251115/WWV_5_MHz_20251115_carrier_spectrogram.png
```

---

## 🚀 Usage

1. **Navigate**: Open http://localhost:3000/carrier.html
2. **Select date**: Use dropdown to choose from available dates
3. **Navigate**: Click "← Summary" or "WWV-H Discrimination →" links
4. **Refresh**: Manual refresh button or auto-refresh (30s)

---

## 📝 Notes

### Timestamp Issue
The AC0G observation directory timestamps (year 2081) indicate a potential timing issue in the data collection. This is filtered out automatically now, but should be investigated:
- Check NTP synchronization on collection system
- Verify RTP timestamp handling in core recorder
- Review time_snap application logic

### Future Enhancements
- Add date range selector (start/end dates)
- Add "Jump to today" button
- Cache available dates (refresh every 5 minutes)
- Show date in page title
- Add breadcrumb navigation

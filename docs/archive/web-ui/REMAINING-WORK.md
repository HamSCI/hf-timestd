# Remaining Work

## Config Sync Feature Status

### ✅ What's Working

1. **Channel creation via ka9q-python** - Tested and confirmed
2. **Channel verification** - Works with proper timing
3. **TOML config parsing** - Can read grape-S000171.toml
4. **API endpoints** - All 3 implemented
5. **Test script** - Confirms end-to-end functionality

### ⚠️ What Needs Fixing

## 1. Filter UI to Show Only Config Channels

**Issue**: UI currently shows ALL radiod channels, not just the 9 WWV/CHU channels defined in `grape-S000171.toml`

**Required Change**: Modify `loadActiveChannels()` function in `index.html` to:
1. Load channels from `/api/config/parse` (gets grape-S000171.toml channels)
2. Check status of each in radiod
3. Display only those 9 channels with active/inactive status

**Why**: This is a WWV/CHU-specific app, not a general radiod monitor.

**Example Output**:
```
🟢 WWV 2.5 MHz (2500000) - Active
🟢 WWV 5 MHz (5000000) - Active  
⚫ WWV 10 MHz (10000000) - Not running
🟢 WWV 15 MHz (15000000) - Active
...all 9 WWV/CHU channels...
```

**NOT**:
```
All 37 radiod channels including ham repeaters, etc.
```

## 2. Restart and Test

After fixing #1:
1. Restart server
2. Hard refresh browser
3. Login
4. Click "🔄 Sync Default Config"
5. Verify all 9 channels sync correctly
6. Check UI only shows those 9 channels

---

## Summary

The core functionality works (channels ARE being created), but the UI needs one more fix to filter the display to only show WWV/CHU channels from the config file.

**Current Commit Status**:
- `utils/radiod.js` ✅ Working  
- `utils/config-sync.js` ✅ Working
- `simple-server.js` ✅ API endpoints working
- `index.html` ⚠️ Needs channel filtering fix

# Web UI Fixes Applied

**Date**: November 3, 2025  
**Session**: Security & Bug Fixes

---

## Issues Fixed

### 1. Auto-Login with Old Credentials ✅

**Problem**: Page was automatically logging in with hardcoded `admin/admin` credentials

**Root Causes**:
1. Login form had `value="admin"` hardcoded in username/password fields
2. JavaScript auto-submitted login if fields contained "admin/admin"
3. `monitoring.html` was setting `localStorage.authToken = 'admin-token'`

**Fixes Applied**:
- ✅ Removed hardcoded values from login form fields
- ✅ Added placeholders instead: "Enter username" / "Enter password"
- ✅ Removed auto-login logic
- ✅ Added proper token validation on page load
- ✅ Fixed `monitoring.html` to redirect to login instead of setting fake token
- ✅ Updated server startup message to show "🔐 Secure JWT authentication enabled"

**Files Modified**:
- `index.html` - Login form and initialization
- `monitoring.html` - Removed hardcoded token, added redirect to login
- `simple-server.js` - Updated startup messages

---

### 2. Edit Button Not Working ✅

**Problems Found**:
1. Double `/api` prefix bug in `loadChannels()` function
2. Missing error handling and logging
3. Potential authentication issues

**Fixes Applied**:
- ✅ Fixed `/api/api/monitoring/channels` → `/api/monitoring/channels`
- ✅ Added console logging to `loadConfigurationForEdit()` for debugging
- ✅ Added better error messages
- ✅ Added fallback to return to config list on error
- ✅ Added null-safe value assignments (using `|| ''`)

**Files Modified**:
- `index.html` - Fixed `loadChannels()` and `loadConfigurationForEdit()`

---

### 3. Verbose RTP Packet Logging ✅

**Problem**: Console flooded with RTP packet messages

**Fix Applied**:
- ✅ Commented out `📨 Received RTP packet:` logging
- ✅ Commented out `🎵 Forwarding RTP packet` logging
- ✅ Preserved logs as comments for debugging

**Files Modified**:
- `simple-server.js` - Disabled verbose RTP logging

---

### 4. Additional Improvements ✅

- ✅ Added Enter key support in password field (press Enter to login)
- ✅ Improved token validation on page load
- ✅ Better error handling throughout

---

## How to Test

### 1. Clear Browser Cache & Storage

**Important**: Clear everything to remove old tokens

```bash
# In browser:
# Chrome/Edge: Shift+Ctrl+Delete → Clear all data
# Or use DevTools: F12 → Application → Storage → Clear site data
```

### 2. Start Fresh Server

```bash
cd /home/mjh/git/signal-recorder/web-ui

# Make sure old server is stopped
pkill -f "node simple-server"

# Start new server
node simple-server.js
```

Expected output:
```
🚀 GRAPE Configuration UI Server running on http://localhost:3000/
📊 Monitoring Dashboard available at http://localhost:3000/monitoring
📁 Using JSON database in ./data/ directory
🔐 Secure JWT authentication enabled
👤 Login at http://localhost:3000/ with your credentials
```

### 3. Test Login

1. Open http://localhost:3000/
2. Should see login form with **empty** username/password fields
3. Enter your credentials:
   - Username: `mjh`
   - Password: `zmphb4me`
4. Click Login or press Enter
5. Should see dashboard

### 4. Test Edit Button

1. Create a test configuration (or use existing one)
2. Click the **Edit** button
3. Should see the edit form with all fields populated
4. Check browser console (F12) for any errors

**If Edit still doesn't work**:
- Open browser DevTools (F12)
- Go to Console tab
- Click Edit button
- Look for console messages:
  - `Loading configuration for edit: <id>`
  - `Loaded config: {....}`
- If you see errors, share them

### 5. Test Monitoring Page

1. Navigate to http://localhost:3000/monitoring
2. Should be redirected to login if not authenticated
3. After logging in, monitoring page should load
4. **No RTP packet spam** in server console

---

## Debugging Tips

### Check Authentication Token

Open browser console (F12) and run:
```javascript
localStorage.getItem('authToken')
```

Should return a JWT token like: `eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...`

If it shows `admin-token` or null, clear it:
```javascript
localStorage.removeItem('authToken')
```

### Check Server Logs

```bash
# If you started server in background
tail -f /tmp/server-test.log

# Filter out RTP messages if needed
tail -f /tmp/server-test.log | grep -v "RTP packet"
```

### Enable Debug Logging

To re-enable RTP logging for debugging:

Edit `simple-server.js` lines 160 and 166, uncomment:
```javascript
// Line 160
console.log(`📨 Received RTP packet: SSRC=${ssrc}...`);

// Line 166  
console.log(`🎵 Forwarding RTP packet for SSRC ${ssrc}`);
```

---

## Known Issues

### If You Still Can't Login

1. **Clear localStorage completely**:
   ```javascript
   // In browser console
   localStorage.clear()
   ```

2. **Verify user exists**:
   ```bash
   cat /home/mjh/git/signal-recorder/web-ui/data/users.json
   ```

3. **Check JWT secret**:
   ```bash
   ls -la /home/mjh/git/signal-recorder/web-ui/data/jwt-secret.txt
   ```

4. **Recreate admin user**:
   ```bash
   cd /home/mjh/git/signal-recorder/web-ui
   rm data/users.json
   node scripts/create-admin.js mjh NewPassword123
   ```

### If Edit Button Still Doesn't Work

The console logs will now show what's happening:
- Check browser console for errors
- Check network tab (F12 → Network) to see API calls
- Look for 401/403 errors (authentication issues)
- Look for 404 errors (endpoint not found)

---

## Summary of Changes

| File | Changes | Lines |
|------|---------|-------|
| `simple-server.js` | Disabled RTP logging, updated messages | 160, 166, 2103-2104 |
| `index.html` | Fixed auto-login, added debugging, fixed Edit button | 260, 264, 586-612, 618, 1396-1419 |
| `monitoring.html` | Remove hardcoded token, redirect to login | 734, 1748 |

**Total Changes**: 3 files modified  
**Backup Created**: `simple-server.js.backup-*`

---

## Next Steps

Once login and Edit button are working:

1. **Test all functionality**:
   - Create configuration
   - Edit configuration  
   - Add channels
   - Delete configuration
   - Export TOML

2. **Complete command injection fixes** (remaining work):
   - Replace 23 `exec()` calls with `execFile()`
   - Add PID validation
   - See `SECURITY-FIXES-PLAN.md`

3. **Production deployment**:
   - Change default admin password
   - Enable HTTPS
   - Restrict CORS origins
   - Review audit logs

---

## Getting Help

If issues persist, provide:
1. Browser console errors (F12 → Console)
2. Network errors (F12 → Network)
3. Server logs (`/tmp/server-test.log`)
4. What you clicked and what happened

The debugging logs we added will help identify the issue!

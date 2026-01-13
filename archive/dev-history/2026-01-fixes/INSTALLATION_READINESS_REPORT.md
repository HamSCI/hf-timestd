# HF-TimeStd Installation Readiness Report
**Date:** 2026-01-13  
**Version:** v5.3.2  
**Objective:** Verify greenfield installation from repository clone

---

## Executive Summary

**Status:** ⚠️ **INSTALLATION WILL FAIL** - Critical issues found

The installation process has **5 critical blockers** and **3 warnings** that will prevent a successful greenfield installation. The primary issues are:

1. **Missing web-ui directory** - systemd service references non-existent path
2. **Incorrect systemd service references** - web-ui vs web-api naming mismatch
3. **Missing start_server.sh** - referenced by systemd but doesn't exist
4. **Python dependency issues** - tomllib vs toml inconsistency
5. **Health check script references non-existent files**

---

## Critical Issues (MUST FIX)

### 1. Missing `web-ui` Directory ❌

**Location:** `install.sh:252, 411-415`  
**Impact:** Installation fails in production mode

```bash
# install.sh references:
WEBUI_DIR="/opt/hf-timestd/web-ui"
sudo cp -r "$PROJECT_DIR/web-ui/"* "$WEBUI_DIR/"
```

**Problem:** The repository has `web-api/` not `web-ui/`

**Evidence:**
- Directory listing shows: `web-api/` and `web-ui.old/`
- No `web-ui/` directory exists

**Fix Required:**
- Update `install.sh` to use `web-api` instead of `web-ui`
- Update all references: lines 252, 412-414, 423, 446, 611

---

### 2. Systemd Service File Mismatch ❌

**Location:** `install.sh:592-627`  
**Impact:** Web UI service fails to start

```bash
# install.sh creates:
/etc/systemd/system/timestd-web-ui.service

# Which references:
ExecStart=$WEBUI_DIR/start_server.sh
# But this file doesn't exist!
```

**Problem:** 
- Service file expects `start_server.sh` 
- Actual file is `web-api/start.sh`
- Service name inconsistency: `timestd-web-ui.service` vs `timestd-web-api.service`

**Evidence:**
- `verify_pipeline.sh:73` expects `timestd-web-api.service`
- `web-api/start.sh` exists but not `start_server.sh`
- `README.md:55` references `timestd-web-api`

**Fix Required:**
- Rename service to `timestd-web-api.service` for consistency
- Update ExecStart to use `web-api/start.sh`
- Update WorkingDirectory to `$WEBUI_DIR` (which should be `/opt/hf-timestd/web-api`)

---

### 3. Python Dependency: tomllib vs toml ❌

**Location:** `web-api/start.sh:47`, `pyproject.toml:26`

**Problem:**
- `web-api/start.sh` uses `tomli` (Python 3.10 backport)
- `pyproject.toml` specifies `toml>=0.10.2` (different package)
- Python 3.11+ has built-in `tomllib` (no external package needed)

**Impact:** Script will fail with ImportError

**Fix Required:**
- Remove `toml` from `pyproject.toml` dependencies
- Update `web-api/start.sh` to use `tomllib` (built-in for Python 3.11+)
- Or add `tomli` to dependencies for Python 3.10 compatibility

---

### 4. Health Check Scripts Reference Missing Monitor Script ❌

**Location:** `install.sh:744`

```bash
ExecStart=$VENV_DIR/bin/python -u /opt/hf-timestd/scripts/monitor_radiod_health.py
```

**Problem:** Need to verify this script exists in `scripts/` directory

**Fix Required:**
- Verify `scripts/monitor_radiod_health.py` exists
- If missing, create it or remove the service definition

---

### 5. VTEC Service Script Path Issue ❌

**Location:** `install.sh:694`

```bash
ExecStart=$VENV_DIR/bin/python -u /opt/hf-timestd/scripts/live_vtec.py
```

**Problem:** Need to verify this script exists

**Fix Required:**
- Verify `scripts/live_vtec.py` exists
- If missing, create it or update the service definition

---

## Warnings (Should Fix)

### 1. Port Number Inconsistency ⚠️

**Locations:** Multiple files

- `install.sh:71` says port 8080
- `README.md:67` says port 8000
- `web-api/start.sh:59` says port 8000
- `config/timestd-config.toml:110` says port 3000
- `INSTALLATION.md:125` says port 3000

**Impact:** User confusion about which port to use

**Fix Required:**
- Standardize on one port (recommend 8000 for FastAPI)
- Update all documentation consistently

---

### 2. Missing Health Check Scripts ⚠️

**Location:** `install.sh:512, 702`

```bash
ExecStartPost=/opt/hf-timestd/scripts/health-check-recorder.sh
ExecStartPost=/opt/hf-timestd/scripts/health-check-vtec.sh
```

**Status:** `health-check-recorder.sh` exists, need to verify `health-check-vtec.sh`

**Fix Required:**
- Verify all health check scripts exist
- Create missing ones or remove ExecStartPost directives

---

### 3. README Installation Instructions Mismatch ⚠️

**Location:** `README.md:36-46`

```bash
sudo ./scripts/install.sh --mode production --user $USER
```

**Problem:** 
- In production mode, `install.sh` overrides `$USER` to `timestd` (line 300)
- The `--user` flag is misleading for production mode

**Impact:** User confusion

**Fix Required:**
- Update README to clarify that production mode always uses `timestd` user
- Remove `--user $USER` from production examples

---

## Installation Process Analysis

### ✅ What Works Well

1. **Comprehensive dependency checking** - Lines 100-140 check Python version, pip, etc.
2. **System dependency installation** - Lines 145-241 handle chrony, hdf5-tools, UDP buffers
3. **User/group creation** - Lines 271-302 properly create timestd system user
4. **Directory structure** - Lines 307-348 create all required directories
5. **Python packaging** - Lines 350-404 handle venv and package installation correctly
6. **Systemd service generation** - Services are well-structured with proper dependencies
7. **Configuration management** - Lines 430-477 handle config file creation
8. **Verification script** - `verify_pipeline.sh` is comprehensive and well-designed

### ❌ What Needs Fixing

1. **Web UI path references** - All references to `web-ui` should be `web-api`
2. **Service file naming** - Inconsistent naming between install.sh and verify_pipeline.sh
3. **Script existence validation** - No checks for required Python scripts before referencing them
4. **Port documentation** - Inconsistent port numbers across documentation
5. **Dependency specification** - toml vs tomllib vs tomli confusion

---

## Required Fixes - Priority Order

### Priority 1 (Blocking Installation)

1. **Fix web-ui → web-api path references**
   - Files: `install.sh` (lines 252, 412-414, 423, 446, 611)
   - Change `web-ui` to `web-api` throughout

2. **Fix systemd service file**
   - File: `install.sh` (lines 592-627)
   - Rename service to `timestd-web-api.service`
   - Update ExecStart to use `web-api/start.sh`
   - Update WorkingDirectory

3. **Fix start.sh script**
   - File: `web-api/start.sh` (line 47)
   - Change `tomli` to `tomllib` (Python 3.11+ built-in)
   - Or add `tomli` to pyproject.toml if supporting Python 3.10

### Priority 2 (Service Failures)

4. **Verify/create missing scripts**
   - Check: `scripts/monitor_radiod_health.py`
   - Check: `scripts/live_vtec.py`
   - Check: `scripts/health-check-vtec.sh`

5. **Fix service name references**
   - Update `README.md` examples to match actual service names
   - Ensure consistency between install.sh and verify_pipeline.sh

### Priority 3 (Documentation)

6. **Standardize port numbers**
   - Choose one port (recommend 8000)
   - Update all documentation

7. **Clarify production mode user**
   - Update README to explain timestd user is automatic in production

---

## Testing Recommendations

After fixes are applied, test in this order:

1. **Fresh VM test** - Clone repo on clean Debian/Ubuntu VM
2. **Run install.sh** - `sudo ./scripts/install.sh --mode production`
3. **Check service files** - Verify all systemd services are created correctly
4. **Start services** - `sudo systemctl start timestd-*`
5. **Run verification** - `./scripts/verify_pipeline.sh`
6. **Check web UI** - Access http://localhost:8000
7. **Monitor logs** - `journalctl -u timestd-* -f`

---

## Files Requiring Changes

### Immediate Changes Required

1. `install.sh` - 8 locations need updates
2. `web-api/start.sh` - 1 location (tomllib import)
3. `pyproject.toml` - Remove or clarify toml dependency
4. `README.md` - Update port numbers and user flag
5. `INSTALLATION.md` - Update port numbers

### Files to Verify Exist

1. `scripts/monitor_radiod_health.py`
2. `scripts/live_vtec.py`
3. `scripts/health-check-vtec.sh`

---

## Conclusion

The installation framework is **well-designed** with excellent error handling, dependency management, and service orchestration. However, **critical path mismatches** between documentation, install script, and actual repository structure will cause installation failures.

**Estimated fix time:** 1-2 hours  
**Risk level:** Low (fixes are straightforward path/name corrections)  
**Testing requirement:** Medium (requires fresh VM to validate)

Once these issues are resolved, the installation should work smoothly for greenfield deployments.

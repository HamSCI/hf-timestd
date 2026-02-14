# Deployment Plan - TEC Validation Tools

## Current Status

**Testing Results**:

- ❌ tec_geometry: Import blocked by ka9q dependency (not needed for standalone use)
- ⏳ IONEX parser: Fixing .Z decompression
- ✅ Validation script: Ready (uses standalone imports)

## Recommendation: Test in Development First

**Don't deploy to production yet** - test in development environment first:

### Step 1: Test IONEX Download (Development)

```bash
cd /home/mjh/git/hf-timestd
python3 scripts/ionex_integration.py 2025-12-24
```

### Step 2: Wait for TEC Data

```bash
# Check if TEC data exists
ls -lh /var/lib/timestd/phase2/science/tec/

# If empty, wait 1-2 hours for Science Aggregator to generate data
sudo journalctl -u timestd-science-aggregator -f
```

### Step 3: Test Validation Script (Development)

```bash
cd /home/mjh/git/hf-timestd
python3 scripts/validate_tec.py --date 2025-12-24 --station WWV
```

### Step 4: Review Results

```bash
# Check plots
ls -lh /tmp/tec_validation/

# View report
cat /tmp/tec_validation/validation_report_WWV_20251224.txt
```

## If Tests Pass: Deploy to Production

**Only after successful testing**, copy to production:

```bash
# Copy validation scripts
sudo cp scripts/ionex_integration.py /opt/hf-timestd/scripts/
sudo cp scripts/validate_tec.py /opt/hf-timestd/scripts/
sudo cp scripts/scintpi_integration.py /opt/hf-timestd/scripts/

# Copy geometry module
sudo cp src/hf_timestd/core/tec_geometry.py /opt/hf-timestd/src/hf_timestd/core/

# Set ownership
sudo chown -R timestd:timestd /opt/hf-timestd/scripts/
sudo chown -R timestd:timestd /opt/hf-timestd/src/hf_timestd/core/
```

## Current Blockers

1. **No TEC data yet** - Science Aggregator needs multi-frequency measurements
2. **IONEX parser** - Fixing .Z decompression (in progress)

## Next Actions

1. ✅ Fix IONEX parser
2. ⏳ Wait for TEC data (1-2 hours)
3. ✅ Test validation in development
4. ⏸️ Deploy to production (only after testing)

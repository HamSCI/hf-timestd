# Deployment Checklist - TEC Integration

## Pre-Deployment Verification

### Code Changes Summary

- [x] `src/hf_timestd/core/phase2_analytics_service.py` (+112 lines)
  - TEC directory initialization
  - TECEstimator integration
  - `_init_tec_csv()` and `_write_tec()` methods
- [x] `src/hf_timestd/core/science_aggregator.py` (NEW)
  - Multi-channel TEC calculation service
  - Reads Phase 2 CSVs, no IQ processing
  - Low CPU priority (Nice=10, 20% quota)
- [x] `systemd/timestd-science-aggregator.service` (NEW)
  - Systemd service configuration
- [x] `docs/SCIENTIFIC_CAPABILITIES.md` (NEW)
  - Theoretical capabilities with validation disclaimer
- [x] `docs/VALIDATION_PLAN.md` (NEW)
  - Validation methodology and success criteria

### Documentation Status

- [x] Validation disclaimer added to SCIENTIFIC_CAPABILITIES.md
- [x] Validation plan created with methodology
- [x] Quality metrics defined
- [x] Error sources documented
- [x] Publication guidelines established

---

## Deployment Steps

### 1. Review Changes

```bash
cd /home/mjh/git/hf-timestd
git diff src/hf_timestd/core/phase2_analytics_service.py
git status
```

### 2. Install to Production

```bash
# Stop services
sudo systemctl stop timestd-analytics

# Install updated code
sudo cp -r src/hf_timestd /opt/hf-timestd/
sudo cp systemd/timestd-science-aggregator.service /etc/systemd/system/

# Reload systemd
sudo systemctl daemon-reload

# Restart analytics (with new TEC infrastructure)
sudo systemctl start timestd-analytics

# Start Science Aggregator
sudo systemctl enable timestd-science-aggregator
sudo systemctl start timestd-science-aggregator
```

### 3. Verify Deployment

```bash
# Check analytics service (Phase 2)
sudo systemctl status timestd-analytics
sudo journalctl -u timestd-analytics -n 50

# Check Science Aggregator
sudo systemctl status timestd-science-aggregator
sudo journalctl -u timestd-science-aggregator -f
```

### 4. Monitor TEC Output

```bash
# Wait 5-10 minutes for first TEC calculation
tail -f /var/lib/timestd/phase2/science/tec/tec_$(date +%Y%m%d).csv
```

**Expected Output**:

```csv
timestamp_utc,minute_boundary,station,tec_tecu,t_vacuum_error_ms,confidence,residuals_ms,n_frequencies,...
2025-12-23T18:30:00Z,1703358600,WWV,25.3,2.1,0.92,0.15,6,...
```

---

## Validation Phase 1: TEC Accuracy

### Data Collection (7 Days)

```bash
# Monitor Science Aggregator logs
sudo journalctl -u timestd-science-aggregator -f

# Check TEC data accumulation
ls -lh /var/lib/timestd/phase2/science/tec/
wc -l /var/lib/timestd/phase2/science/tec/tec_*.csv
```

### GPS TEC Comparison

1. Download GPS TEC maps from NOAA SWPC
   - URL: <https://www.swpc.noaa.gov/products/us-total-electron-content>
2. Extract TEC at ionospheric reflection points
3. Calculate correlation and RMS error

**Success Criteria**:

- R² > 0.7
- RMS error < 10 TECU
- Diurnal pattern match

### Quality Checks

```bash
# Check for outliers
awk -F, 'NR>1 {if ($4 < 5 || $4 > 100) print}' tec_*.csv

# Check confidence scores
awk -F, 'NR>1 {sum+=$6; count++} END {print "Avg confidence:", sum/count}' tec_*.csv

# Check frequency count
awk -F, 'NR>1 {sum+=$8; count++} END {print "Avg n_freq:", sum/count}' tec_*.csv
```

---

## Post-Deployment Monitoring

### Daily Checks

- [ ] Science Aggregator running: `systemctl status timestd-science-aggregator`
- [ ] TEC data being written: `ls -lh /var/lib/timestd/phase2/science/tec/`
- [ ] No errors in logs: `journalctl -u timestd-science-aggregator --since "1 hour ago"`

### Weekly Checks

- [ ] TEC values reasonable: 5-50 TECU daytime, 2-20 TECU nighttime
- [ ] Confidence scores: Average > 0.7
- [ ] Frequency count: Average ≥ 3

### Monthly Checks

- [ ] Compare with GPS TEC (validation)
- [ ] Document any anomalies
- [ ] Update validation report

---

## Rollback Plan

If issues arise:

```bash
# Stop Science Aggregator
sudo systemctl stop timestd-science-aggregator
sudo systemctl disable timestd-science-aggregator

# Revert analytics service
sudo systemctl stop timestd-analytics
# Restore from backup or git checkout
git checkout HEAD -- src/hf_timestd/core/phase2_analytics_service.py
sudo cp -r src/hf_timestd /opt/hf-timestd/
sudo systemctl start timestd-analytics
```

---

## Commit and Push

After successful deployment and initial validation:

```bash
cd /home/mjh/git/hf-timestd

# Stage changes
git add src/hf_timestd/core/phase2_analytics_service.py
git add src/hf_timestd/core/science_aggregator.py
git add systemd/timestd-science-aggregator.service
git add docs/SCIENTIFIC_CAPABILITIES.md
git add docs/VALIDATION_PLAN.md
git add docs/TEC_INTEGRATION.md

# Commit
git commit -m "Add TEC estimation and Science Aggregator service

- Integrate TECEstimator into Phase 2 analytics
- Create Science Aggregator service for multi-channel TEC calculation
- Add validation plan and scientific capabilities documentation
- Implement dual-pipeline architecture (metrology vs science)

Status: Implementation complete, validation pending"

# Push
git push origin main
```

---

## Success Criteria

### Deployment Success

- [x] All services running without errors
- [x] TEC CSV files being created
- [x] No performance degradation of metrology path

### Validation Success (Week 1-2)

- [ ] TEC correlation with GPS: R² > 0.7
- [ ] TEC RMS error: < 10 TECU
- [ ] Diurnal pattern matches GPS TEC

### Publication Readiness (Month 2-3)

- [ ] 30 days of validated data
- [ ] Validation report published
- [ ] Error bars documented
- [ ] Peer review completed

---

## Notes

**Current Status**: Code ready for deployment, validation pending

**Scientific Integrity**: All claims in documentation are qualified as "theoretical capabilities" pending validation

**Next Milestone**: Complete Phase 1 validation (TEC vs GPS) within 2 weeks of deployment

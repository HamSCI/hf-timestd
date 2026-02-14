# Documentation Conformance Audit — 2026-02-14

**Auditor:** Cascade (AI Assistant)  
**Scope:** All non-archived `.md` files in repo root and `docs/`  
**Method:** Cross-check doc claims against code, scripts, systemd units, and CRITIC_CONTEXT baseline truths  
**Hard constraint:** No code changes — documentation and archival only

---

## 1. Mismatch Matrix

### 1.1 Critical (could cause bad operation or wrong scientific interpretation)

| # | Doc | Claim | Reality (code evidence) | Fix |
|---|-----|-------|------------------------|-----|
| C1 | `README.md:12` | "Digital RF (HDF5) format" for raw recording | Raw recording uses **binary `.bin.zst` + JSON sidecars**, not Digital RF HDF5. Core recorder writes via `binary_archive_writer.py`. Digital RF was the v5.0 plan but was replaced by the binary archive format. | Correct README to "Binary IQ archive with JSON metadata sidecars" |
| C2 | `README.md:126` | Fusion reads L2 HDF5 via "SWMR" | SWMR was **eliminated** (2026-02-06). All HDF5 uses crash-safe open-write-close pattern with `locking=False`. | Remove all SWMR references |
| C3 | `README.md:143` | "Digital RF: Efficient HDF5-based format for continuous IQ recording" | Raw IQ is binary `.bin.zst`, not Digital RF. Digital RF is used only for GRAPE DRF packaging (upload stage). | Correct to "Binary IQ archive" for raw recording; note Digital RF is used for GRAPE packaging only |
| C4 | `TECHNICAL_REFERENCE.md:20` | "24 kHz Digital RF (HDF5) — Phase 1 immutable raw archive (`raw_archive/{CHANNEL}/`)" | Phase 1 writes to `raw_buffer/{CHANNEL}/` as `.bin.zst` + `.json`. `raw_archive/` is a legacy path that no longer exists in production. | Correct format and path |
| C5 | `TECHNICAL_REFERENCE.md:344` | Raw Data path: `/var/lib/timestd/raw_archive/` | Actual path: `/var/lib/timestd/raw_buffer/` (confirmed in `verify_pipeline.sh:198`, `check-freshness-alert.sh:12`) | Fix path |
| C6 | `TECHNICAL_REFERENCE.md:578` | Production logs: `/var/log/grape-recorder/` | Actual: `/var/log/hf-timestd/` (confirmed in systemd units and `DIRECTORY_STRUCTURE.md:62`) | Fix path |
| C7 | `DIRECTORY_STRUCTURE.md:1-385` | Entire doc references `GRAPEPaths` API, `grape_recorder.paths`, `grape-paths.js`, NPZ format, `analytics/` directories, CSV-based outputs | Current system uses `hf_timestd` package, binary archive, HDF5 outputs, `phase2/` directories. The `GRAPEPaths` API, `grape_recorder` module, and `web-ui/` (Node.js) no longer exist. | **Archive entire file** — replace with brief pointer or rewrite |
| C8 | `ARCHITECTURE.md:337-344` | Data flow shows `phase2/{CHANNEL}/tone_detections/{date}.h5 (L1A)` and `fused_d_clock.csv` | Current metrology writes to `metrology/`, `tick_timing/`, `detection_attempts/`, `clock_offset/`. `tone_detections/` is legacy. `fused_d_clock.csv` is legacy. | Update data flow paths |
| C9 | `GPS_TEC_OPTIONAL.md:119-122` | "Science Aggregator Service" runs as `timestd-science-aggregator.service` | No such service exists in `systemd/`. TEC is produced by `timestd-physics.service` (`physics_fusion_service.py`). | Correct service name to `timestd-physics` |
| C10 | `GPS_TEC_OPTIONAL.md:34-37` | TEC output as CSV: `tec_YYYYMMDD.csv` | TEC output is HDF5: `*tec_*.h5` (confirmed in `verify_pipeline.sh:456`, `check-freshness-alert.sh:166`) | Correct format to HDF5 |

### 1.2 Major (significant drift/confusion)

| # | Doc | Claim | Reality | Fix |
|---|-----|-------|---------|-----|
| M1 | `README.md:109-138` | "The Eight Services" architecture diagram lists `timestd-analytics`, `timestd-web-ui` | Actual services: `timestd-metrology`, `timestd-l2-calibration`, `timestd-fusion`, `timestd-physics`, `timestd-web-api`, `timestd-vtec`, `timestd-radiod-monitor`, `timestd-core-recorder` (confirmed in `update-production.sh:277-284` and systemd dir). No `timestd-analytics` or `timestd-web-ui`. | Rewrite architecture diagram with current 8 services |
| M2 | `README.md:11` | "V6.7.1" | Version should reflect current state. The GRAPE pipeline, memory guardrails, freshness monitoring, and deployment correspondence are post-v6.7.1 features. | Update version or remove specific version from capabilities header |
| M3 | `INSTALLATION.md:53-55` | Lists optional services `timestd-ionex-download.timer`, `timestd-chrony-monitor.timer` | `timestd-ionex-download.timer` exists in systemd dir. `timestd-chrony-monitor.timer` exists. But `grape-daily.timer` is missing from the optional list. | Add `grape-daily.timer` to optional services |
| M4 | `INSTALLATION.md:4` | "Last Updated: January 20, 2026" | Many services and features added since then (edge ensemble, propagation model, GRAPE pipeline, freshness monitoring, memory guardrails). | Update date and content |
| M5 | `DEPLOYMENT_REVIEW.md` (entire) | References `timestd-analytics`, `timestd-web-ui`, `GRAPE_*` env vars, `raw_archive`, port 3000, `common.sh`, `timestd-all.sh`, `timestd-core.sh`, `timestd-analytics.sh`, `timestd-ui.sh` | All of these are legacy. Current system uses systemd services directly, port 8000, `update-production.sh`. None of the referenced shell scripts exist. | **Archive** — superseded by `DEPLOYMENT_CORRESPONDENCE_CHECKLIST.md` |
| M6 | `DEPLOYMENT_CHECKLIST.md` (entire) | References `timestd-analytics`, `timestd-science-aggregator`, `phase2_analytics_service.py`, `science_aggregator.py`, CSV TEC output, `sudo cp -r src/hf_timestd /opt/hf-timestd/` deployment | All legacy. Current deploy uses `update-production.sh`. Service names wrong. File paths wrong. | **Archive** — superseded by `DEPLOYMENT_CORRESPONDENCE_CHECKLIST.md` |
| M7 | `TECHNICAL_REFERENCE.md:29` | "The Six Services" | Current system has 8+ services (see CRITIC_CONTEXT service inventory). | Update count and list |
| M8 | `TECHNICAL_REFERENCE.md:457-671` | Key Modules section references `grape_recorder` package, `recording_session.py`, `rtp_receiver.py`, `packet_resequencer.py`, `stream_api.py`, `grape_npz_writer.py`, `analytics_service.py`, `wwvh_discrimination.py`, `web-ui/monitoring-server-v3.js`, `grape-paths.js` | None of these exist. Current package is `hf_timestd`. Web UI is FastAPI Python, not Node.js Express. | Rewrite or archive this section |
| M9 | `TECHNICAL_REFERENCE.md:487-503` | Environment config shows `GRAPE_MODE`, `GRAPE_DATA_ROOT`, `GRAPE_LOG_DIR=/var/log/grape-recorder` | Current env uses `TIMESTD_*` variables. Log dir is `/var/log/hf-timestd/`. | Update env var names and paths |
| M10 | `TECHNICAL_REFERENCE.md:677` | "Python 3.10+" and lists `digital_rf`, `zeroconf`, `soundfile` as deps; "Node.js 18+" for web-ui | Python 3.11+ (per README). No Node.js web-ui. `digital_rf` is optional (GRAPE only). Core deps are `numpy`, `scipy`, `h5py`, `ka9q-python`. | Update dependencies |
| M11 | `ARCHITECTURE.md:166` | "Process Noise: Extremely low Q (1e-10)" for Steel Ruler | Process noise was increased from 1e-10 to 0.01 (2026-02-06 fix for dead Kalman filter). | Update Q value |
| M12 | `ARCHITECTURE.md:467` | Lists `timestd-iono.service` | No such service in systemd dir. IonoDataService runs as a background thread within metrology, not a separate service. | Remove from service table or clarify it's a thread |
| M13 | `PSWS_SETUP_GUIDE.md:63` | SSH key: `psws_key` (ed25519) | Actual key: `/home/timestd/.ssh/id_rsa_psws` (RSA, comment `wsprdaemon@bee1`) per memory. | Correct key path and type |
| M14 | `PSWS_SETUP_GUIDE.md:94` | SFTP test uses `-o BatchMode=yes` | PSWS server is sftp-only (rejects ssh/scp shell connections). The `ssh-copy-id` step (line 81) won't work as described since the server doesn't accept shell connections. | Add note about sftp-only server; document actual key upload method |

### 1.3 Minor (wording/staleness)

| # | Doc | Claim | Reality | Fix |
|---|-----|-------|---------|-----|
| m1 | `ARCHITECTURE.md:5-6` | "V6.7.1 (Propagation Model Full Integration)" | Post-v6.7.1 features exist (GRAPE pipeline, memory guardrails, freshness). | Update version label |
| m2 | `ARCHITECTURE.md:498-501` | References `CONTEXT.md`, `CANONICAL_CONTRACTS.md` | `CONTEXT.md` exists but may be stale. `CANONICAL_CONTRACTS.md` — verify existence. | Check and update links |
| m3 | `README.md:155` | DeepWiki badge link | Fine to keep, but verify it resolves. | No action needed |
| m4 | `METROLOGY.md:6` | "Last Updated: February 12, 2026 (v6.7.0)" | Content is current and accurate. Minor version label staleness. | Update date |
| m5 | `INSTALLATION.md:97` | Links to `docs/STATION_SETUP_GUIDE.md` | Verify this file exists. | Check link |
| m6 | `INSTALLATION.md:127` | Links to `docs/ZED_F9P_TEC_CONFIGURATION.md` | Verify this file exists. | Check link |
| m7 | `GPS_TEC_OPTIONAL.md:434-437` | References `docs/TEC_VALIDATION_METHODOLOGY.md`, `docs/TEC_VALIDATION_DEPLOYMENT.md` | Likely don't exist (not in find results). | Remove or mark as planned |

---

## 2. Doc Action Plan

### 2.1 Top-Level Docs

| File | Action | Priority | Notes |
|------|--------|----------|-------|
| `README.md` | **Revise** | Critical | Fix C1-C3, M1-M2. Update architecture diagram, remove SWMR/DRF claims for raw recording, update service list |
| `INSTALLATION.md` | **Revise** | Critical | Fix M3-M4. Add `grape-daily.timer`, update date, verify doc links |
| `ARCHITECTURE.md` | **Revise** | High | Fix C8, M11-M12, m1-m2. Update data flow, Kalman Q, remove phantom service, update version |
| `TECHNICAL_REFERENCE.md` | **Major revision** | High | Fix C4-C6, M7-M10. Bottom half (lines 457-1094) is almost entirely legacy `grape_recorder` content. Top half (lines 1-456) is mostly current but needs path/service fixes |
| `METROLOGY.md` | **Keep as-is** | Low | Content is accurate and current. Minor date update (m4) |
| `DIRECTORY_STRUCTURE.md` | **Archive** | Critical | C7 — entire doc describes defunct `GRAPEPaths` API and legacy directory layout. Replace with brief current-state doc or fold into ARCHITECTURE.md |
| `CONTEXT.md` | **Evaluate** | Medium | May be stale; check content |
| `CRITIC_CONTEXT.md` | **Keep as-is** | — | Session control doc, not user-facing |
| `CHANGELOG.md` | **Keep as-is** | Low | Historical record |

### 2.2 `docs/` Subdirectory

| File | Action | Priority | Notes |
|------|--------|----------|-------|
| `DEPLOYMENT_CORRESPONDENCE_CHECKLIST.md` | **Keep as-is** | — | Current and accurate. Authoritative deploy reference. |
| `DEPLOYMENT_CHECKLIST.md` | **Archive** | High | M6 — entirely legacy (TEC integration checklist from Dec 2025) |
| `DEPLOYMENT_REVIEW.md` | **Archive** | High | M5 — entirely legacy (Dec 2025 review with wrong service names) |
| `GPS_TEC_OPTIONAL.md` | **Revise** | High | Fix C9-C10, m7. Correct service name, output format, dead links |
| `PSWS_SETUP_GUIDE.md` | **Revise** | High | Fix M13-M14. Correct SSH key details, note sftp-only server |
| `GRAPE_DAILY_PROCESSING.md` | **Evaluate** | Medium | Likely current given recent GRAPE work |
| `DRF_UPLOAD_SYSTEM.md` | **Evaluate** | Medium | May be current |
| `docs/changes/*` | **Keep as-is** | — | Historical session logs |
| `docs/design/*` | **Keep as-is** | — | Design rationale docs |
| `docs/features/*` | **Evaluate** | Low | May contain stale feature descriptions |
| `docs/archive/*` | **Keep as-is** | — | Already archived |

---

## 3. Audience Coverage Check

### 3.1 User: "What works today, what should I run, where are outputs, how do I know it's healthy?"

| Need | Covered By | Status |
|------|-----------|--------|
| What works today | `README.md` (after revision) | ⚠️ Needs revision — current version has wrong service names and format claims |
| Install & run | `INSTALLATION.md` | ⚠️ Mostly correct but missing `grape-daily.timer` and stale date |
| Deploy updates | `DEPLOYMENT_CORRESPONDENCE_CHECKLIST.md` | ✅ Current and accurate |
| Data locations | `README.md` Data Locations table | ✅ Correct paths |
| Health checks | `verify_pipeline.sh`, `check-freshness-alert.sh` | ✅ Scripts are current; docs reference them correctly |
| Web monitoring | `README.md` port 8000 reference | ✅ Correct |

### 3.2 Metrologist: "What are timing authorities, uncertainty assumptions, traceability boundaries?"

| Need | Covered By | Status |
|------|-----------|--------|
| RTP timing authority | `METROLOGY.md`, `ARCHITECTURE.md` | ✅ Thoroughly documented |
| Uncertainty analysis | `METROLOGY.md` (L1-L6 levels, error budget) | ✅ Comprehensive |
| Dual Chrony feeds | `METROLOGY.md` (TSL1/TSL2 section) | ✅ Current |
| Steel Ruler philosophy | `ARCHITECTURE.md` | ⚠️ Q value outdated (M11) |
| Traceability chain | `METROLOGY.md` (transmitter → receiver chain) | ✅ Current |

### 3.3 Ionospheric Scientist: "What physics products exist, cadence/quality, validated vs provisional?"

| Need | Covered By | Status |
|------|-----------|--------|
| Propagation model | `METROLOGY.md` (v6.7 section), `ARCHITECTURE.md` | ✅ Thorough |
| TEC products | `GPS_TEC_OPTIONAL.md` | ⚠️ Wrong service name (C9) and format (C10) |
| Phase/Doppler products | `CRITIC_CONTEXT.md` (session notes) | ⚠️ No user-facing doc for phase/Doppler dashboard |
| Validation caveats | `GPS_TEC_OPTIONAL.md` | ✅ Clear "theoretical capabilities pending validation" framing |
| GRAPE/PSWS data | `PSWS_SETUP_GUIDE.md`, `GRAPE_DAILY_PROCESSING.md` | ⚠️ PSWS guide has wrong SSH key (M13) |

### 3.4 Software Engineer: "Current architecture, deployment path, operational contract, deprecation?"

| Need | Covered By | Status |
|------|-----------|--------|
| Architecture | `ARCHITECTURE.md` | ⚠️ Mostly current but has phantom service (M12) and stale data flow (C8) |
| Deployment | `DEPLOYMENT_CORRESPONDENCE_CHECKLIST.md` | ✅ Authoritative |
| Service inventory | `CRITIC_CONTEXT.md` (service table) | ✅ Current but not in a user-facing doc |
| Deprecation status | `ARCHITECTURE.md` (deprecated modules table) | ✅ `PhysicsPropagationModel` clearly marked |
| Directory structure | `DIRECTORY_STRUCTURE.md` | ❌ Entirely legacy (C7) |
| Code modules | `TECHNICAL_REFERENCE.md` | ❌ Bottom half is legacy `grape_recorder` (M8) |

---

## 4. Navigation Path (Post-Revision)

An operator should be able to discover:

1. **Install** → `INSTALLATION.md` (from README Quick Start link)
2. **Update/Deploy** → `docs/DEPLOYMENT_CORRESPONDENCE_CHECKLIST.md` (from README Production Updates section)
3. **Health/Freshness** → `scripts/verify_pipeline.sh` + `scripts/check-freshness-alert.sh` (from DEPLOYMENT_CORRESPONDENCE_CHECKLIST Gate A/C)
4. **Scientific caveats** → `GPS_TEC_OPTIONAL.md` (from INSTALLATION.md optional section) + `METROLOGY.md` (from README Detailed Documentation)
5. **Architecture** → `ARCHITECTURE.md` (from README)
6. **GRAPE/PSWS** → `docs/PSWS_SETUP_GUIDE.md` (from INSTALLATION.md)

---

## 5. Execution Status

All planned changes have been executed:

| # | Action | Status |
|---|--------|--------|
| 1 | **Archive** `DIRECTORY_STRUCTURE.md` → `docs/archive/` | ✅ Done |
| 2 | **Archive** `docs/DEPLOYMENT_CHECKLIST.md` → `docs/archive/` | ✅ Done |
| 3 | **Archive** `docs/DEPLOYMENT_REVIEW.md` → `docs/archive/` | ✅ Done |
| 4 | **Revise** `README.md` — fix C1-C3, M1-M2 | ✅ Done |
| 5 | **Revise** `INSTALLATION.md` — fix M3-M4 | ✅ Done |
| 6 | **Revise** `ARCHITECTURE.md` — fix C8, M11-M12, m2 | ✅ Done |
| 7 | **Revise** `GPS_TEC_OPTIONAL.md` — fix C9-C10, m7 | ✅ Done |
| 8 | **Revise** `PSWS_SETUP_GUIDE.md` — fix M13-M14 | ✅ Done |
| 9 | **Revise** `TECHNICAL_REFERENCE.md` — fix C4-C6, M7-M10 | ✅ Done |
| 10 | **Fix** broken `METROLOGIST_DESCRIPTION.md` links in `METROLOGY.md` and `TECHNICAL_REFERENCE.md` | ✅ Done |
| 11 | **Fix** broken `DIRECTORY_STRUCTURE.md` link in `README.md` (replaced with deployment checklist) | ✅ Done |
| 12 | **Fix** broken `CANONICAL_CONTRACTS.md` and `CONTEXT.md` links in `ARCHITECTURE.md` | ✅ Done |

### Additional findings during link verification

- `docs/STATION_SETUP_GUIDE.md` — **exists** (m5 ✅)
- `docs/ZED_F9P_TEC_CONFIGURATION.md` — **exists** (m6 ✅)
- `CANONICAL_CONTRACTS.md` — moved to `archive/planning/`, no longer at root (link removed from ARCHITECTURE.md)
- `docs/METROLOGIST_DESCRIPTION.md` — **does not exist** (links removed from METROLOGY.md and TECHNICAL_REFERENCE.md)
- `docs/API_REFERENCE.md` — in `docs/archive/` (not referenced from active docs)

### Session 2 (February 14, 2026 — continued)

| # | Action | Status |
|---|--------|--------|
| 13 | **Rewrite** `TECHNICAL_REFERENCE.md` bottom half — replaced legacy `grape_recorder` module listings, dependencies, testing, debugging, performance, quality metrics, and references with current `hf_timestd` content | ✅ Done |
| 14 | **Fix** `TECHNICAL_REFERENCE.md` line 46 — "Digital RF files" → "binary IQ files" | ✅ Done |
| 15 | **Replace** `TECHNICAL_REFERENCE.md` GRAPEPaths section — current path management | ✅ Done |
| 16 | **Fix** `TECHNICAL_REFERENCE.md` install/startup — removed `grape-recorder`, `start-services.sh`, `grape-upload.timer` | ✅ Done |
| 17 | **Add** `TECHNICAL_REFERENCE.md` HDF5 file lock troubleshooting section | ✅ Done |
| 18 | **Update** `ARCHITECTURE.md` — version label, doc purpose refs, Phase 2 box, Web UI viz paths, directory tree, dates | ✅ Done |
| 19 | **Update** `METROLOGY.md` — dates, stale log path (`phase2-*.log` → `metrology.log`) | ✅ Done |
| 20 | **Fix** `GPS_TEC_OPTIONAL.md` line 29 — "CSV data" → "HDF5 data" | ✅ Done |
| 21 | **Archive** 56 obsolete `docs/*.md` files → `docs/archive/` (legacy grape_recorder, completed migrations, superseded docs) | ✅ Done |
| 22 | **Move** 4 session logs from `docs/` → `docs/changes/` | ✅ Done |
| 23 | **Remove** 1 duplicate (`grape-config-explained.md` already in archive) | ✅ Done |
| 24 | **Fix** broken link in `docs/PHYSICS.md` — `CHANNEL_CHARACTERIZATION.md` → `CARRIER_DOPPLER_INTERPRETATION.md` | ✅ Done |
| 25 | **Fix** broken link in `docs/STATION_SETUP_GUIDE.md` — `time-vtec.md` → `GPS_TEC_OPTIONAL.md` | ✅ Done |

### docs/ directory after cleanup

**20 active files remain in `docs/`** (down from 78):

| File | Purpose |
|------|---------|
| `CARRIER_DOPPLER_INTERPRETATION.md` | Science: carrier phase and Doppler methodology |
| `DEPLOYMENT_CORRESPONDENCE_CHECKLIST.md` | Ops: production deployment gates |
| `DETECTION_METHODOLOGY_REVIEW.md` | Science: detection pipeline review (Feb 2026) |
| `DOCUMENTATION_AUDIT_2026_02_14.md` | Meta: this audit document |
| `DRF_UPLOAD_SYSTEM.md` | Ops: GRAPE DRF packaging and upload |
| `DUAL_CHRONY_FEED_ARCHITECTURE.md` | Architecture: TSL1/TSL2 dual feed (v6.5.1) |
| `GPS_TEC_OPTIONAL.md` | Setup: optional GNSS TEC capabilities |
| `GRAPE_DAILY_PROCESSING.md` | Ops: daily decimation, spectrograms, upload |
| `HAMSCI_QUALITY_METADATA_PROPOSAL.md` | Proposal: quality metadata for HamSCI |
| `IONOSPHERIC_REANALYSIS.md` | Feature: weekly ionospheric reanalysis |
| `IONOSPHERIC_RESOLUTION.md` | Science: ionospheric measurement resolution |
| `METROLOGY.md` | Science: metrological description for time nuts |
| `NASA_EARTHDATA_SETUP.md` | Setup: IONEX data access credentials |
| `PHYSICS.md` | Science: ionospheric physics capabilities |
| `PIPELINE_VERIFICATION.md` | Ops: pipeline health verification |
| `PSWS_SETUP_GUIDE.md` | Setup: PSWS network upload |
| `SCIENTIFIC_CAPABILITIES.md` | Science: feature validation status |
| `STATION_SETUP_GUIDE.md` | Setup: site-specific configuration |
| `VALIDATION_PLAN.md` | Plan: scientific validation against ground truth |
| `ZED_F9P_TEC_CONFIGURATION.md` | Setup: ZED-F9P GNSS receiver |

**117 files in `docs/archive/`** (historical reference, not linked from active docs).
**26 files in `docs/changes/`** (session logs).

### Remaining work for future sessions

All critical and major items from the original audit have been addressed. No remaining documentation debt.

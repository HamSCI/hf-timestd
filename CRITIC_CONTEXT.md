# NEVER CHANGE THE FOLLOWING PRIMARY INSTRUCTION!!!

**Primary Instruction:** In this context you will perform a critical review of the HF Time Standard (hf-timestd) project, either in its entirety or in a specific component, as specified by the user. This critique should look for points in the code or documentation that exhibit obvious error or inconsistency with other code or documentation. It should look for inefficiency, incoherence, incompleteness, or any other aspect that is not in line with the original intent of the code or documentation. It should also look for obsolete, deprecated, or "zombie" code that should be removed. Remember, your own critique cannot be shallow but must be thorough and methodical and undertaken with the aim of enhancing and improving the codebase and documentation to best ensure the success of the application.

Make your criticism from the perspective of 1) a user of the system, 2) a metrologist, 3) a ionospheric scientist, and 4) a software engineer. These perspectives can differ in their priorities and interests, and your critique should reflect this. For example, a user of the system will be most interested in the accuracy and reliability of the system, while a metrologist will be most interested in the precision and traceability of the system, while an ionospheric scientist will be most interested in the ionospheric conditions and their impact on the system, and a software engineer will be most interested in the code quality, maintainability, coverage and adequacy of testing, and the resilience and robustness of the system over time and in edge cases. Ultimately, however, a consensus of these perspectives should guide your critique in service of meeting the application's objectives.

# NEVER CHANGE THE PRECEEDING PRIMARY INSTRUCTION!!!

# The following secondary instruction and information will guide your critique in this particular session (the instructions below will vary from session to session)

---

## NEXT SESSION: INSTALL & UPDATE PROCESS AUDIT

**Goal:** Critically review the hf-timestd installation and update process from usability, reliability, completeness, stability, and documentation perspectives. Identify gaps, failure modes, undocumented prerequisites, and places where the installer can be silently misconfigured. Produce a concrete remediation plan and implement agreed fixes.

**The four perspectives applied to install/update:**
- **User (operator):** Can a first-time installer complete a working installation with only the README and wizard prompts? Are error messages actionable? Is the update process safe to run without data loss?
- **Metrologist:** Does the install guarantee the timing chain is correctly configured — GPSDO authority, SHM ordering, IONEX data, chrony integration — so that time outputs are traceable from first boot?
- **Ionospheric scientist:** Are the optional science enhancements (GNSS VTEC, IONEX, GRAPE upload) clearly differentiated from core function? Does the wizard explain what each enables so the installer can make an informed choice?
- **Software engineer:** Is the install idempotent? Does the update script avoid data loss? Are failure modes recoverable? Is the config file forward-compatible? Are there silent partial-failure modes?

---

## 1. The Install/Update Script Inventory

All scripts live in `scripts/` in the git repo and are deployed to `/opt/hf-timestd/scripts/` on install.

| Script | Purpose | Entry Point |
|--------|---------|-------------|
| `install.sh` | Full first-time install (requires sudo, idempotent) | `sudo ./scripts/install.sh` |
| `setup-station.sh` | Interactive config wizard — generates `timestd-config.toml` | called by install.sh or standalone |
| `config-review.sh` | Config review + incremental update, called by update-production.sh | `sudo ./scripts/config-review.sh` |
| `update-production.sh` | Pull + reinstall Python package + sync files + restart services | `sudo ./scripts/update-production.sh [--pull] [--yes]` |
| `start-services.sh` | Start all services in dependency order (incl. SHM/chrony dance) | `sudo ./scripts/start-services.sh [--status]` |
| `stop-services.sh` | Stop all services | `sudo ./scripts/stop-services.sh` |
| `reinstall.sh` | Force-reinstall Python package into venv (wraps ensure-venv.sh) | `sudo ./scripts/reinstall.sh` |
| `ensure-venv.sh` | Create/update venv, install hf_timestd package | called by install.sh and as ExecStartPre |
| `setup-psws-keys.sh` | Generate/exchange SSH keys for PSWS/GRAPE SFTP upload | `sudo ./scripts/setup-psws-keys.sh` |
| `setup-cpu-affinity.sh` | Pin CPU affinity for radiod co-location | `sudo ./scripts/setup-cpu-affinity.sh` |
| `download_ionex_daily.sh` | Download today's IONEX ionospheric map from NASA CDDIS | run by timer, or manually |
| `uninstall.sh` | Remove installation (but preserve data) | `sudo ./scripts/uninstall.sh` |
| `reset-state.sh` | Clear Kalman/fusion state files (use after config changes) | `sudo ./scripts/reset-state.sh` |
| `config-review.sh` | Show/update critical config fields interactively | `sudo ./scripts/config-review.sh` |

---

## 2. Filesystem Layout (Deployed)

```
/etc/hf-timestd/
    timestd-config.toml       ← Station config (generated by setup-station.sh)
    environment               ← Env vars for systemd services (TIMESTD_* vars)

/opt/hf-timestd/
    venv/                     ← Python virtual environment
    web-api/                  ← FastAPI app (synced from repo by update-production.sh)
    scripts/                  ← Shell scripts (synced from repo)
    src/                      ← Python source tree (synced; needed for ensure-venv.sh)
    pyproject.toml            ← Needed for ensure-venv.sh on unattended restart
    config/
        timestd-config.toml   ← Symlink → /etc/hf-timestd/timestd-config.toml
    docs/                     ← Documentation (synced)

/var/lib/timestd/
    raw_buffer/               ← Phase 1: IQ archive (KEEP — never delete)
    phase2/                   ← Phase 2: L1/L2 HDF5 (KEEP)
    phase2/fusion/            ← Phase 3: L3 fusion HDF5 (KEEP)
    state/                    ← Kalman/bootstrap state files (safe to clear with reset-state.sh)
    space_weather_cache/      ← Space weather JSON cache (safe to delete)
    ionex/                    ← IONEX ionospheric maps (recreated by timer)
    grape/                    ← GRAPE export staging (safe to delete after upload)

/var/log/hf-timestd/          ← Per-service log files (rotated by logrotate)
/dev/shm/timestd/             ← Hot buffer (recreated on boot by tmpfiles.d)
```

---

## 3. The `timestd-config.toml` — Field Reference

This is the single most important file. The wizard generates it but operators need to understand it for troubleshooting and post-install tuning.

### 3a. Required Fields (install will not work correctly without these)

```toml
[station]
callsign = "W0XYZ"           # Amateur radio callsign — used in GRAPE metadata
grid_square = "EM38ab"       # Maidenhead, 6 or 10 chars — path geometry calculations
latitude = 38.9              # Decimal degrees, positive = North
longitude = -92.1            # Decimal degrees, positive = East

[ka9q]
status_address = "hf-status.local"  # ka9q-radio status multicast (mDNS or IP)
source = "radiod"            # "radiod" (standard) or "phase-engine" (coherent)

[recorder]
mode = "production"
compression = "zstd"         # "zstd" | "lz4" | "none"

[timing]
authority = "rtp"            # "rtp" (GPS+PPS via radiod) or "fusion" (NTP only)
rtp_expected_accuracy_ms = 0.001  # 0.0001 (direct GPS), 0.001 (LAN GPS), 1.0 (NTP)
```

### 3b. Optional Enhancements (disabled by default, enable for full capability)

```toml
# GNSS VTEC — dual-frequency GNSS receiver (e.g. u-blox ZED-F9P) provides
# real-time ionospheric TEC for improved L2 timing corrections.
# Requires: receiver accessible via TCP (e.g. ser2net), timestd-vtec.service enabled.
[gnss_vtec]
enabled = false
host = "192.168.0.203"       # IP/hostname of receiver
port = 9000                  # TCP port (ser2net default)

# PSWS/GRAPE Upload — contributes decimated IQ spectrograms to the
# Personal Space Weather Station network (HamSCI).
# Requires: PSWS account at https://pswsnetwork.caps.ua.edu/
#           SSH key pair generated by setup-psws-keys.sh
[uploader]
enabled = false
[uploader.sftp]
host = "pswsnetwork.eng.ua.edu"
ssh_key = "/home/timestd/.ssh/id_rsa_psws_S000171"
[uploader.metadata]
station_id = "S000171"       # From PSWS site admin page
instrument_id = "172"        # From PSWS site admin page
```

### 3c. Channels (`[[recorder.channels]]`)

Each `[[recorder.channels]]` block defines one monitored frequency. The standard deployment monitors 9 channels across 4 stations. The channel list is the most installation-specific part of the config and is **not set by the wizard** — it must be populated from the config template and edited to match the stations actually receivable at the operator's location.

**Key fields per channel:**
```toml
[[recorder.channels]]
channel = "WWV_5000"          # Unique key — used in file paths and logs
station = "WWV"               # Broadcast station identifier
frequency_hz = 5000000        # Exact carrier frequency
multicast_address = "239.x.x.x"  # ka9q-radio multicast output address
ssrc = 12345                  # RTP SSRC from radiod (find with: ka9q-radio status)
enabled = true
```

**Critical:** The `ssrc` values must match the actual radiod configuration. Finding them: `avahi-browse -r _ka9q-radio._udp` or the radiod web status page.

### 3d. Config Schema Versioning

The template in `config/timestd-config.toml.template` is the canonical schema. When new fields are added to the codebase, the template is updated. Running `config-review.sh` detects sections present in the template but absent from the production config and offers to add them.

---

## 4. Install Process — Step-by-Step (What Actually Happens)

Understanding the sequence helps diagnose failures at each step.

```
sudo ./scripts/install.sh
  │
  ├─ Step 1: apt dependencies (python3-dev, libhdf5-dev, hdf5-tools, avahi-utils, ...)
  ├─ Step 1b: Python 3.10+ check
  ├─ Step 2: chrony install + SHM refclock config (appends to chrony.conf)
  │          chronyd.service.d/timestd-shm.conf override (After=timestd-fusion)
  ├─ Step 2b: UDP buffer tuning (sysctl.d/99-timestd.conf)
  ├─ Step 3: Path constants established
  ├─ Step 4: timestd system user + chrony group membership
  ├─ Step 5: Directory tree creation (see §2 above)
  ├─ Step 6: ensure-venv.sh → creates /opt/hf-timestd/venv, pip installs hf_timestd
  ├─ Step 7: web-api copy, scripts copy, config symlink
  ├─ Step 8: setup-station.sh wizard → generates /etc/hf-timestd/timestd-config.toml
  ├─ Step 8b: radiod co-location question → sets TIMESTD_RADIOD_LOCAL in environment
  ├─ Step 9: systemd service/timer files installed + enabled
  │          (vtec.service conditional on gnss_vtec.enabled in config)
  │          (GNSS timeserver added to chrony if gnss_vtec enabled)
  ├─ Step 10: Initial IONEX download (requires ~/.netrc with NASA CDDIS credentials)
  ├─ Step 11: Stale SHM segments cleared
  ├─ Step 12: CPU affinity setup (radiod co-location only)
  └─ Offer: start-services.sh
```

### Critical Ordering Constraints

1. **fusion before chronyd**: `timestd-fusion` must start before `chronyd`. Fusion creates the Chrony SHM segments with `timestd:666` permissions. If chrony starts first, it creates them with `root:600`, blocking fusion writes. The `chronyd-timestd-shm.conf` override (`After=timestd-fusion`) and `start-services.sh` SHM-clearing both enforce this.

2. **ensure-venv.sh on restart**: `timestd-core-recorder.service` has `ExecStartPre=ensure-venv.sh`. This means `/opt/hf-timestd/pyproject.toml` and `/opt/hf-timestd/src/` must exist and be up to date. `update-production.sh` Step 1b syncs these.

3. **config symlink**: `web-api/main.py` reads config from `/opt/hf-timestd/config/timestd-config.toml` which is a symlink to `/etc/hf-timestd/timestd-config.toml`. The symlink is created at Step 7. If it breaks (e.g. config file moved), the web API silently fails to read config.

---

## 5. Update Process — Step-by-Step

```
sudo ./scripts/update-production.sh [--pull] [--yes]
  │
  ├─ Step 0:   git pull (--pull flag only; pulls as repo owner, not root)
  ├─ Step 0.5: config-review.sh (interactive unless --yes)
  │            → shows current settings, detects missing template sections
  ├─ Step 1:   pip reinstall hf_timestd (cleans .pyc, removes editable installs first)
  ├─ Step 1b:  rsync src/ and pyproject.toml to /opt/hf-timestd/ (for ensure-venv.sh)
  ├─ Step 2:   rsync scripts/ to /opt/hf-timestd/scripts/
  ├─ Step 2b:  rsync web-api/ to /opt/hf-timestd/web-api/
  ├─ Step 2b2: rsync schemas/ to /opt/hf-timestd/src/hf_timestd/schemas/
  ├─ Step 2c:  rsync docs/ to /opt/hf-timestd/docs/
  ├─ Step 2d:  update cron jobs (freshness-monitor)
  ├─ Step 2e:  update logrotate config
  ├─ Step 3:   diff-based systemd service file update + daemon-reload (only if changed)
  ├─ Step 4:   restart services (NOT core-recorder — to avoid data gaps)
  └─ Step 5:   verify venv using installed package (not repo path)
```

**What update does NOT restart:** `timestd-core-recorder` is intentionally skipped to avoid IQ data gaps. If recorder needs restarting (e.g. new channel config), do it manually: `sudo systemctl restart timestd-core-recorder`.

---

## 6. Known Issues, Gaps & Fragility Points

These are the areas requiring scrutiny in the next session. Review each and propose/implement fixes.

### 6a. Channel Configuration — Biggest Installer Stumbling Block
**Problem:** The wizard (setup-station.sh) collects station identity, timing mode, and optional features — but **does not configure channels**. The `[[recorder.channels]]` section must be populated manually from the template. New installers frequently miss this, get no data, and have no clear error pointing to the cause. The core-recorder will start but log "no channels configured" or silently receive nothing.

**What the AI agent should address:**
- Does the wizard warn clearly that channels must be configured manually post-install?
- Is there a `validate-channels.sh` or equivalent that confirms channels are reachable (via avahi/mDNS) before services start?
- Does the README/INSTALL guide walk through channel configuration with examples?

### 6b. IONEX / NASA CDDIS Credentials — Silent Failure
**Problem:** Step 10 of install.sh attempts an IONEX download. It requires a `~/.netrc` file with NASA CDDIS credentials (urs.earthdata.nasa.gov). This is an optional enhancement but is not clearly communicated as such. The install warns if the download fails, but the service starts anyway. Without IONEX data, the physics service uses a degraded ionospheric model. No indication of degraded mode is visible in normal operation.

**What the AI agent should address:**
- Is the IONEX credential requirement documented at the point of need (during install)?
- Does the physics service log clearly when running without IONEX?
- Is there a health check endpoint or status display that shows IONEX availability?

### 6c. PSWS SSH Key Setup — Deferred But Undiscoverable
**Problem:** If the user enables PSWS upload during setup-station.sh, they are told to run `setup-psws-keys.sh` after install. But this step is mentioned only in the post-wizard banner and is not enforced or re-checked by start-services.sh (beyond a non-fatal connectivity test). If the key setup is skipped, uploads silently fail for months.

**What the AI agent should address:**
- Does `start-services.sh` clearly distinguish "PSWS key missing" from "PSWS enabled but key not yet set up"?
- Is there a persistent status indicator in the web dashboard?
- Is `setup-psws-keys.sh` documented well enough to run without assistance?

### 6d. `config-review.sh` sed Fragility
**Problem:** The interactive config update in `config-review.sh` uses `sed -i "s/^callsign = .*/..."` to patch values. This works only if the key is at the start of a line and is unique in the file. TOML files can have duplicate keys in different sections (e.g. `host` appears in `[gnss_vtec]`, `[uploader.sftp]`, `[chrony]`). A bare `sed` on `host` would clobber the wrong section.

**What the AI agent should address:**
- Is the sed approach safe for all fields currently patched?
- Should this be rewritten to use the Python TOML-aware substitution from setup-station.sh?

### 6e. `update-production.sh` Does Not Re-run `config-review.sh` for New Required Fields
**Problem:** When a new required config field is introduced (e.g. `[timing]` section added in v5.4.0), existing installs will be missing it. `config-review.sh` detects this and offers to add it. But `update-production.sh --yes` skips the interactive review entirely (`--non-interactive` mode only shows status without adding missing sections). An automated update with `--yes` can leave the config in a broken state.

**What the AI agent should address:**
- Should `--yes` still apply non-destructive missing-section additions automatically?
- Should there be a `--check-config` exit code that blocks service restart if required fields are absent?

### 6f. Startup Race: SHM Permissions
**Problem:** The Chrony SHM segment ordering is handled by both the systemd override (`After=timestd-fusion`) and by `start-services.sh` (explicit SHM clear + fusion first). But on a fresh boot where fusion fails to start (e.g., config error), chrony starts without SHM, creates segments with root:600, and even when fusion recovers it cannot write. Recovery requires manual `ipcrm` + `systemctl restart chronyd`. This is documented in comments but not surfaced to the operator.

**What the AI agent should address:**
- Is there a health check that detects SHM permission problems and suggests the fix?
- Should `timestd-fusion.service` include an `ExecStartPre` that clears stale SHM?

### 6g. `ensure-venv.sh` on Unattended Restart
**Problem:** `ExecStartPre=ensure-venv.sh` runs before core-recorder on every start (including OOM/watchdog restarts). It requires `/opt/hf-timestd/pyproject.toml` and `/opt/hf-timestd/src/` to exist. `update-production.sh` Step 1b syncs these. But on a **fresh install followed by a pull** (i.e., the operator does `git pull` + runs `update-production.sh --pull`), if the pull changes the package version and `ensure-venv.sh` reinstalls, the version in the venv will differ from what was installed by `install.sh`. This is correct behavior but can surprise operators who see pip output in the core-recorder journal.

**What the AI agent should address:** Verify this is harmless and add a comment to the service file.

### 6h. No Post-Install Validation Script
**Problem:** After install completes, there is no single command that checks: (1) all services running, (2) chrony seeing TSL2 source, (3) raw_buffer being written, (4) L2 HDF5 being updated, (5) web API responding, (6) space weather cache populated. `start-services.sh --status` shows service state but not data pipeline health.

**What the AI agent should address:**
- Propose or implement a `verify-install.sh` (or extend `start-services.sh --status`) that runs these checks and gives pass/fail with actionable error messages.

### 6i. Stale `deploy-*.sh` Scripts
**Problem:** The `scripts/` directory contains several `deploy-*.sh` scripts (`deploy-pll-decoder.sh`, `deploy-service-improvements.sh`, `deploy-service-management.sh`, `deploy-v3.10.1-fixes.sh`, `deploy_ionex.sh`, `deploy_web_ui.sh`) that appear to be one-off migration helpers from pre-production development. They reference old paths, old service names, and `bee1` hostnames. They are dead code and confuse new installers reading the scripts/ directory.

**What the AI agent should address:** Confirm these are obsolete and remove or archive them.

---

## 7. Config Template Completeness Check

Run these before the session to understand the current state:

```bash
# What sections does the template have vs production config?
bash /home/mjh/git/hf-timestd/scripts/config-review.sh --non-interactive

# What channels are configured?
grep -c '^\[\[recorder.channels\]\]' /etc/hf-timestd/timestd-config.toml

# Is IONEX data present and fresh?
ls -la /var/lib/timestd/ionex/ | tail -5
find /var/lib/timestd/ionex -name "*.gz" -newer /var/lib/timestd/ionex -mtime -2 | wc -l

# Is SHM working?
ipcs -m | grep -E '0x4e545030|0x4e545031'
chronyc sources 2>/dev/null | grep -E 'TSL|HF'

# Are all services healthy?
sudo bash /home/mjh/git/hf-timestd/scripts/start-services.sh --status

# Check for stale/dead deploy scripts
ls /home/mjh/git/hf-timestd/scripts/deploy*.sh

# Verify config symlink integrity
ls -la /opt/hf-timestd/config/

# Check venv is using installed (not repo) package
/opt/hf-timestd/venv/bin/python3 -c "import hf_timestd; print(hf_timestd.__file__)"

# Check ensure-venv.sh target files exist
ls -la /opt/hf-timestd/pyproject.toml /opt/hf-timestd/src/ 2>&1
```

---

## 8. Recommended Review Order for the Session

Work through these in order — each builds on the previous:

1. **Channel config gap** (§6a): Highest impact on new installer success. Decide: wizard enhancement vs. post-install validator vs. README fix.
2. **Post-install validation script** (§6h): Implement `verify-install.sh` — gives the operator immediate pass/fail feedback.
3. **`config-review.sh` sed fragility** (§6d): Assess which fields are at risk; rewrite to Python-based TOML patching if needed.
4. **`update-production.sh --yes` config gap** (§6e): Fix so automated updates don't silently miss new required fields.
5. **IONEX credential documentation** (§6b): Improve in-wizard messaging and health API endpoint.
6. **PSWS key status visibility** (§6c): Web dashboard indicator + clearer start-services output.
7. **SHM health check** (§6f): Add to verify-install.sh and/or fusion ExecStartPre.
8. **Dead deploy scripts** (§6i): Remove after confirming obsolescence.
9. **README/INSTALL documentation pass**: Verify the end-to-end install narrative matches current scripts. Specifically: channel config, IONEX credentials, PSWS key setup, timing authority choice.

---

## 9. Output Format for This Session

For each issue identified, produce findings in this structure:

```markdown
### Issue: <short title>

**Severity:** CRITICAL | HIGH | MEDIUM | LOW
**Affected:** <script(s) / file(s)>

**Problem:** Precise description of what is wrong or missing.

**Failure mode:** What happens to the installer / operator when this goes wrong.

**Proposed fix:** Concrete change — code, script, or documentation.

**Verdict:** Implement now | Defer | Document only
```

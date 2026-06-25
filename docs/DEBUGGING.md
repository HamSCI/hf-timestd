# Debugging hf-timestd

Operator-facing runbook. Uses the v6.12 logging model: every `timestd-*`
systemd unit logs to journald; there are no per-service log files under
`/var/log/hf-timestd/` for any systemd-managed service. `journalctl` is
the one log tool.

---

## 1. First-minute triage

When something looks wrong, run these in order. Each one is fast and
tells you whether to dig deeper in that area.

```bash
# What should be running vs. what actually is
hf-timestd service status

# Config, contract, and SSRC sanity (sigmond client contract §12)
hf-timestd validate

# Recent problems across the whole pipeline
journalctl -u 'timestd-*' -p warning..err --since -30min

# Core recorder alive? (newest IQ file should be fresh)
ls -lt /var/lib/timestd/raw_buffer/*/ | head

# Metrology + L2 alive? (newest HDF5 should be fresh)
ls -lt /var/lib/timestd/phase2/*/ | head

# Chrony actually seeing us?
chronyc sources -v | grep SHM
```

If all six look healthy, the problem is above the data pipeline (web UI,
GRAPE upload, science products) — skip to the relevant section below.

---

## 2. Log access — the canonical method

### 2.1 Where logs live

Every `timestd-*` unit writes to the system journal via
`StandardOutput=journal` / `StandardError=journal`. Nothing else writes
service logs. The helpers `scripts/check-freshness-alert.sh` and
`scripts/data-retention` (cron-driven, not systemd) still write to
`/var/log/hf-timestd/freshness-monitor.log` and `data-retention.log`;
everything else is journald.

### 2.2 Recipes

```bash
# Live tail — all timestd services
journalctl -u 'timestd-*' -f

# Live tail — one service
journalctl -u timestd-fusion.service -f

# Live tail — every per-channel metrology instance
journalctl -u 'timestd-metrology@*' -f

# Last hour
journalctl -u 'timestd-*' --since -1h

# Warnings and errors only
journalctl -u 'timestd-*' -p warning..err --since today

# Since last boot
journalctl -u 'timestd-*' -b

# Structured JSON (for piping into jq)
journalctl -u timestd-fusion.service -o json --since -10min | jq

# Grep a specific phrase within a service journal
journalctl -u timestd-fusion.service --since today | grep "D_clock"

# Boundary between two timestamps
journalctl -u timestd-core-recorder.service \
    --since "2026-04-16 14:00" --until "2026-04-16 15:00"
```

### 2.3 Finding the right unit name

```bash
systemctl list-units 'timestd-*'           # running
systemctl list-unit-files 'timestd-*'       # all installed
systemctl list-timers 'timestd-*'           # timer-triggered units
```

### 2.4 From Python / the `hf-timestd` CLI

There is no `hf-timestd logs` subcommand (yet). Use raw `journalctl`
as above. `hf-timestd service status` gives you the unit names for
every service the active profile expects to run, which you can paste
into a `journalctl -u` one-liner.

### 2.5 Via the web UI

The FastAPI dashboard at `http://<host>:8000/static/logs.html` reads
the same journald stream. With the v6.12 logging unification this page
stays in sync with the services — previously the three file-sinked
services never reached journald, so the web UI went stale.

If the page still looks stale after v6.12:

1. Hard-refresh the browser (Ctrl-Shift-R).
2. `sudo systemctl restart timestd-web-api` — FastAPI caches some
   aggregates in memory.
3. Check `journalctl -u timestd-web-api.service -n 100` for handler
   errors.

### 2.6 Capacity

journald has a cap controlled by `SystemMaxUse=` in
`/etc/systemd/journald.conf`. On a dedicated timestd host, **2 GB is a
reasonable starting point; 4 GB gives headroom** for verbose runs. Check
current usage with:

```bash
journalctl --disk-usage
```

To change the cap:

```ini
# /etc/systemd/journald.conf
[Journal]
SystemMaxUse=2G
SystemKeepFree=1G
SystemMaxFileSize=200M
```

```bash
sudo systemctl restart systemd-journald
```

### 2.7 Log verbosity at runtime

Every timestd service honours `HF_TIMESTD_LOG_LEVEL` (or the generic
`CLIENT_LOG_LEVEL`) from `/etc/hf-timestd/environment`, and re-reads it
on `SIGHUP` (contract §11). To bump fusion to `DEBUG`:

```bash
# Edit the environment file, set HF_TIMESTD_LOG_LEVEL=DEBUG
sudo $EDITOR /etc/hf-timestd/environment
sudo systemctl kill -s HUP timestd-fusion.service
```

No restart required; the process re-reads and adjusts.

---

## 3. Pipeline-stage triage

For each stage: what it produces, how to confirm liveness, the single
journalctl one-liner to run when it misbehaves, and the usual fix.

### 3.1 ka9q-radio upstream (external to this repo)

This is `radiod`, not a `timestd-*` unit.

```bash
# radiod instance(s) on this host
systemctl list-units 'radiod@*'
journalctl -u 'radiod@*' -f

# Multicast actually flowing?
ip maddr show
sudo tcpdump -i <iface> -n udp | head   # should show UDP bursts

# Is our monitor complaining?
journalctl -u timestd-radiod-monitor.service --since -10min
```

Common failure: `radiod` died or USB device reset — restart radiod. Our
watchdog (`timestd-radiod-monitor`) is supposed to catch this; check
its journal for the alert.

### 3.2 Core recorder — `timestd-core-recorder`

- **Produces**: `/var/lib/timestd/raw_buffer/<CHANNEL>/*.bin.zst` plus
  JSON sidecars. Default chunk duration is 600 s (`file_duration_sec`
  in `timestd-config.toml`).
- **Liveness check**: newest `.bin.zst` in each channel directory should
  be < `file_duration_sec` old. The service is `Type=notify` with a
  180 s systemd watchdog — if it can't get samples, systemd restarts
  it.
- **Common failures**: radiod connection drop, disk full on
  `/var/lib/timestd` or `/dev/shm/timestd`, ring-buffer SHM contention.

```bash
journalctl -u timestd-core-recorder.service --since -15min
ls -lt /var/lib/timestd/raw_buffer/*/ | head
df -h /var/lib/timestd /dev/shm
```

### 3.3 Metrology — `timestd-metrology@<channel>` (template)

One instance per channel. Lists as `timestd-metrology@WWV_5MHz.service`,
`timestd-metrology@CHU_7850kHz.service`, etc.

- **Produces**: L1 HDF5 per channel under
  `/var/lib/timestd/phase2/<CHANNEL>/`.
- **Common failures**: no tone detections (SNR floor, antenna dead),
  HDF5 SWMR contention (should be eliminated post-v6.10 — but check),
  crash-loop from malformed sidecar.

```bash
systemctl list-units 'timestd-metrology@*'
journalctl -u 'timestd-metrology@*' -p warning..err --since -30min

# Single channel
journalctl -u timestd-metrology@WWV_5MHz.service -f
```

### 3.4 L2 calibration — `timestd-l2-calibration`

- **Consumes**: L1 HDF5 from metrology.
- **Produces**: L2 HDF5 in the same per-channel directory, applies
  geometric + ionospheric corrections.

```bash
journalctl -u timestd-l2-calibration.service --since -15min
```

### 3.5 Fusion — `timestd-fusion`

- **Consumes**: L2 HDF5.
- **Produces**: `/var/lib/timestd/phase2/fusion/<date>.h5`; feeds Chrony
  SHM segments TSL1 (0x4e545030) and TSL2 (0x4e545031).
- **Common failures**: SHM refclock dead, Kalman state corrupt after
  bad restart, discontinuity-filter latched (v6.5.1 fix).

```bash
journalctl -u timestd-fusion.service --since -15min

# SHM segments present?
ipcs -m | head

# Chrony actually attached?
chronyc sources -v | grep SHM
```

If SHM exists but chrony's `SHM` sources show `reach=0`, restart chrony:

```bash
sudo systemctl restart chrony
```

If the fix is persistent, check `journalctl -u timestd-fusion` for
repeated `SHM permission` errors — the ExecStartPre in
`timestd-fusion.service` should be clearing stale 0x4e5450XX segments
owned by root:0600.

### 3.6 Physics — `timestd-physics`

- **Produces**: carrier-phase dTEC and group-delay TEC validation
  records.
- **v6.8 resilience**: every HDF5 write runs under `_timed_write`
  (30 s cap); main loop pets the watchdog 17× per minute. If you see
  repeated `WATCHDOG=1` timeouts in the journal, the host is I/O-bound.

```bash
journalctl -u timestd-physics.service --since -15min
```

### 3.7 VTEC — `timestd-vtec`

Optional; requires a u-blox ZED-F9P dual-frequency GNSS receiver on the
host. Enabled only in `full` profile or by explicit override.

```bash
hf-timestd service status | grep vtec
journalctl -u timestd-vtec.service --since -15min
```

### 3.8 GRAPE daily — `grape-daily.timer` → `grape-daily.service`

- **Runs**: once per day (check `systemctl list-timers 'grape-daily*'`
  for next fire time).
- **Produces**: Digital RF packaged day, SFTP'd to PSWS.
- **Failure mode**: SFTP non-fatal (v6.10.0) — job logs the failure and
  exits 0 so the timer stays clean. Check the journal:

```bash
journalctl -u grape-daily.service -n 200
systemctl list-timers 'grape-daily*'
```

### 3.9 Web API — `timestd-web-api`

- **Provides**: FastAPI dashboard on port 8000.

```bash
curl -s http://localhost:8000/api/health || curl -v http://localhost:8000/
journalctl -u timestd-web-api.service --since -15min
```

### 3.10 Housekeeping

| Unit | Purpose |
|---|---|
| `timestd-prune.service` + `.timer` | Deletes old raw buffers + HDF5 per QuotaManager (v6.10 circular-buffer policy) |
| `timestd-pipeline-watchdog.service` + `.timer` | End-to-end freshness check, alerts on stale outputs |
| `timestd-chrony-monitor.service` + `.timer` | Chrony discipline sanity |
| `timestd-ionex-download.service` + `.timer` | Fetches daily IONEX maps |
| `timestd-iono-reanalysis.service` + `.timer` | Reprocesses recent days with updated ionospheric inputs |
| `timestd-radiod-monitor.service` | Continuous radiod hardware health watcher |
| `timestd-alert@.service` | On-failure handler invoked by `OnFailure=` on critical units |

All log to journald; triage is the same shape as above.

---

## 4. Data-path checkpoints

| Path | Writer | Expected freshness | Quick check |
|---|---|---|---|
| `/var/lib/timestd/raw_buffer/<ch>/*.bin.zst` | `timestd-core-recorder` | `file_duration_sec` (def 600 s) | `ls -lt /var/lib/timestd/raw_buffer/*/ \| head` |
| `/var/lib/timestd/phase2/<ch>/*.h5` | `timestd-metrology@<ch>` + `timestd-l2-calibration` | ~1 min after each IQ chunk closes | `ls -lt /var/lib/timestd/phase2/*/ \| head` |
| `/var/lib/timestd/phase2/fusion/<date>.h5` | `timestd-fusion` | ~10 s (cadence 8 s) | `ls -lt /var/lib/timestd/phase2/fusion/` |
| Chrony SHM TSL1/TSL2 | `timestd-fusion` | seconds | `chronyc sources -v \| grep SHM` |
| `/var/lib/timestd/ionex/*.INX` | `timestd-ionex-download` | daily | `ls -lt /var/lib/timestd/ionex/` |
| `/dev/shm/timestd/ring/*` | `timestd-core-recorder` (producer) + `timestd-metrology@*` (consumers) | present at all times | `ls /dev/shm/timestd/ring/` |

---

## 5. Failure recipes

Symptom → likely cause → diagnostic → fix.

### 5.1 Chrony shows no timing data from hf-timestd

**Symptom**: `chronyc sources -v | grep SHM` shows `reach=0` or no SHM
source at all.

**Likely causes**, in order:

1. `timestd-fusion.service` not running.
2. SHM segment stale (root:0600) from a previous fusion restart, chrony
   attached to wrong segments.
3. Fusion's discontinuity filter latched (v6.5.1 was supposed to fix
   this, but it can still happen on extreme glitches).

**Diagnose**:

```bash
hf-timestd service status | grep fusion
journalctl -u timestd-fusion.service -n 200
ipcs -m | awk '$1 ~ /4e5450/'
```

**Fix**:

```bash
sudo systemctl restart timestd-fusion   # ExecStartPre clears stale SHM
# Fusion then restarts chrony so it reattaches to fresh segments.
```

### 5.2 `raw_buffer` growing without bound

**Cause**: `timestd-prune.timer` disabled or QuotaManager misbehaving.

**Diagnose**:

```bash
systemctl list-timers 'timestd-prune*'
journalctl -u timestd-prune.service -n 50
du -sh /var/lib/timestd/raw_buffer/*/ | sort -h | tail
```

**Fix**:

```bash
sudo systemctl enable --now timestd-prune.timer
sudo systemctl start timestd-prune.service    # immediate sweep
```

### 5.3 HDF5 read errors in metrology

Post-v6.10 this should be rare — SWMR + `h5clear -s` on every
open-for-write handles unclean shutdowns.

**Diagnose**:

```bash
journalctl -u 'timestd-metrology@*' -p warning..err --since -1h | head -60
ls -lt /var/lib/timestd/phase2/<ch>/*.h5
```

**Fix** (only if a file is actually corrupt):

```bash
sudo -u timestd h5clear -s /var/lib/timestd/phase2/<ch>/<file>.h5
sudo systemctl restart timestd-metrology@<ch>.service
```

### 5.4 Fusion stuck in `ACQUIRING` or `CORRELATING`

**Causes**: no broadcasts detected (antenna/SNR), geographic prior
disabled in config, or not enough channels reporting for cluster lock.

**Diagnose**:

```bash
journalctl -u timestd-fusion.service --since -30min | grep -E "BOOTSTRAP|CLUSTER|TIER"
journalctl -u 'timestd-metrology@*' --since -30min | grep -c "tone detected"
```

**Fix**: verify channels are producing L1 records, confirm
`geographic_predictor` is enabled in `timestd-config.toml`, check
antenna health.

### 5.5 Web UI shows stale data

Post-v6.12 the "journalctl-out-of-sync" case is gone. Remaining causes:

1. Browser cache — hard-refresh.
2. FastAPI in-process cache — `sudo systemctl restart timestd-web-api`.
3. Handler crash — `journalctl -u timestd-web-api.service --since -1h`.

### 5.6 GRAPE upload stuck

**Causes**: PSWS SFTP credentials expired, PSWS host key rotated,
network outage.

**Diagnose**:

```bash
journalctl -u grape-daily.service -n 200 | grep -iE "ssh|sftp|upload|host key"
systemctl list-timers 'grape-daily*'
```

**Fix**: refresh PSWS credentials in `/etc/hf-timestd/grape.env` (or
equivalent); accept new host key if rotation was legitimate.

### 5.7 Radiod multicast silent

**Diagnose**:

```bash
ip maddr show | grep -A2 <iface>
sudo tcpdump -i <iface> -n udp -c 20
systemctl status 'radiod@*'
```

**Fix**: restart `radiod@<instance>.service`; verify IGMP querier on
the switch; check that `timestd-core-recorder`'s `EnvironmentFile=` has
the right multicast group.

---

## 6. Escalation — diagnostic bundle

When you need to hand off to a developer:

```bash
bundle=/tmp/hf-timestd-diag-$(date +%Y%m%d-%H%M%S)
mkdir -p "$bundle"

hf-timestd version   --json > "$bundle/version.json"
hf-timestd inventory --json > "$bundle/inventory.json"
hf-timestd validate  --json > "$bundle/validate.json" || true
hf-timestd service status   > "$bundle/service-status.txt"
hf-timestd profile show     > "$bundle/profile.txt"

journalctl -u 'timestd-*' --since -6h --no-pager > "$bundle/journal-timestd-6h.log"
journalctl -u 'radiod@*'  --since -6h --no-pager > "$bundle/journal-radiod-6h.log"
systemctl status 'timestd-*' --no-pager > "$bundle/systemctl-status.txt"
chronyc sources -v > "$bundle/chrony-sources.txt"
ipcs -m > "$bundle/ipcs-m.txt"
ls -lt /var/lib/timestd/raw_buffer/*/ 2>/dev/null | head -50 > "$bundle/raw-buffer-latest.txt"
ls -lt /var/lib/timestd/phase2/*/    2>/dev/null | head -50 > "$bundle/phase2-latest.txt"
df -h /var/lib/timestd /dev/shm > "$bundle/disk.txt"

tar -C /tmp -czf "${bundle}.tgz" "$(basename "$bundle")"
echo "Bundle: ${bundle}.tgz"
```

File an issue at <https://github.com/HamSCI/hf-timestd/issues> and
attach the tarball.

---

## 7. Quick command reference

```bash
# Status
hf-timestd service status
hf-timestd profile  show
hf-timestd validate
hf-timestd version  --json
hf-timestd inventory --json

# Logs (journald only)
journalctl -u 'timestd-*' -f
journalctl -u 'timestd-*' -p warning..err --since -1h
journalctl -u timestd-fusion.service --since today
journalctl -u 'timestd-metrology@*' -f
journalctl --disk-usage

# Service control
sudo systemctl restart timestd-fusion
sudo systemctl kill -s HUP timestd-fusion     # re-read log level
sudo hf-timestd profile set fusion

# Data freshness
ls -lt /var/lib/timestd/raw_buffer/*/ | head
ls -lt /var/lib/timestd/phase2/*/    | head
chronyc sources -v

# SHM + disk
ipcs -m | awk '$1 ~ /4e5450/'
df -h /var/lib/timestd /dev/shm
```

See also:
[INSTALLATION.md](../INSTALLATION.md),
[ARCHITECTURE.md](ARCHITECTURE.md),
[METROLOGY.md](METROLOGY.md),
[PIPELINE_VERIFICATION.md](PIPELINE_VERIFICATION.md).

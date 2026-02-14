# Production Deployment Correspondence Checklist

Purpose: ensure repository changes are consistently reflected in production (`/opt/hf-timestd` + `/etc`) and verified with objective gates.

This checklist is complementary to automation in `scripts/update-production.sh`.

---

## 1) What this is (and is not)

- **This is an ongoing method**, not a one-off incident note.
- It defines:
  1. Source-of-truth files in git
  2. How they are synced to production
  3. Verification gates required after each deploy
- It does **not** replace `scripts/update-production.sh`; it standardizes how to use it safely.

---

## 2) Source of truth map

### 2.1 Python runtime code

- Source: `src/hf_timestd/**`
- Production location: installed package in `/opt/hf-timestd/venv/.../site-packages/hf_timestd/`
- Sync mechanism: `scripts/update-production.sh` Step 1 (`pip install` non-editable, force reinstall)

### 2.2 Web API/UI assets

- Source: `web-api/**`
- Production location: `/opt/hf-timestd/web-api/**`
- Sync mechanism: `scripts/update-production.sh` Step 2b (`rsync`)

### 2.3 Ops scripts

- Source: `scripts/*.sh`, `scripts/*.py`
- Production location: `/opt/hf-timestd/scripts/**`
- Sync mechanism: `scripts/update-production.sh` Step 2

### 2.4 Systemd units

- Source: `systemd/*.service`, `systemd/*.timer`
- Production location: `/etc/systemd/system/*`
- Sync mechanism: `scripts/update-production.sh` Step 3 (`diff` + copy + `daemon-reload`)

### 2.5 Cron freshness monitor

- Source: `config/cron.d/timestd-freshness-monitor`
- Production location: `/etc/cron.d/timestd-freshness-monitor`
- Sync mechanism: `scripts/update-production.sh` Step 2d

### 2.6 Logrotate

- Source: `config/logrotate-timestd`
- Production location: `/etc/logrotate.d/hf-timestd`
- Sync mechanism: `scripts/update-production.sh` Step 2e

---

## 3) Required deploy path (every production change)

1. Commit and review changes in git.
2. Run:

```bash
sudo scripts/update-production.sh [--pull]
```

3. Confirm service restart status from script output.
4. Run verification gates in Section 4.

Do not use ad-hoc copy commands as primary deployment method except for emergency hotfix rollback.

---

## 4) Post-deploy verification gates (must pass)

### Gate A: Pipeline health

```bash
/opt/hf-timestd/scripts/verify_pipeline.sh
```

Expected: no FAIL.

### Gate B: Services active and stable

```bash
systemctl status timestd-metrology timestd-fusion timestd-physics timestd-web-api
```

Expected: active/running, no crash loop.

### Gate C: Freshness monitor active behavior

```bash
/opt/hf-timestd/scripts/check-freshness-alert.sh
```

Expected: exit 0 under normal healthy conditions.

### Gate D: Fusion output freshness

```bash
find /var/lib/timestd/phase2/fusion -name '*fusion_timing_*.h5' -type f -printf '%T@ %p\n' | sort -n | tail -1
```

Expected: latest file timestamp is recent (minutes, not hours).

### Gate E: Physics output freshness

```bash
find /var/lib/timestd/phase2/science/tec -name '*tec_*.h5' -type f -printf '%T@ %p\n' | sort -n | tail -1
```

Expected: latest file timestamp is recent.

---

## 5) Weekly correspondence audit (drift detection)

Run weekly on production host:

```bash
# Ensure deployed logrotate matches repo
sudo diff -u /home/mjh/git/hf-timestd/config/logrotate-timestd /etc/logrotate.d/hf-timestd

# Ensure deployed cron monitor matches repo
sudo diff -u /home/mjh/git/hf-timestd/config/cron.d/timestd-freshness-monitor /etc/cron.d/timestd-freshness-monitor

# Ensure deployed scripts were synced
sudo diff -u /home/mjh/git/hf-timestd/scripts/check-freshness-alert.sh /opt/hf-timestd/scripts/check-freshness-alert.sh
```

Expected: no diff or known intentional local override documented.

---

## 6) Rollback approach

If a deployment regresses production:

1. Restore known-good git commit.
2. Re-run:

```bash
sudo scripts/update-production.sh
```

3. Re-run all gates in Section 4.

Avoid partial/manual rollback of only one path unless incident response requires immediate triage.

---

## 7) Anti-patterns to avoid

- `pip install -e` in production
- Editing files directly in `/opt/hf-timestd` without back-porting to git
- Updating `/etc/systemd/system` or `/etc/logrotate.d` manually without matching repo change
- Declaring deploy complete without running verification gates

---

## 8) Definition of done for a production change

A production deployment is complete only when:

1. `update-production.sh` ran successfully
2. Verification gates (A-E) pass
3. Any intentional production-only deviation is documented in commit or ops note

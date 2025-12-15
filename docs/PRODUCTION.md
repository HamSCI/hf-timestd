# hf-timestd - Production Deployment Guide

**Author:** Michael James Hauan (AC0G)  
**Last Updated:** December 15, 2025

This guide covers deploying `hf-timestd` in production with systemd services for 24/7 operation.

**Scope note:** Phase 3 (decimation/products/uploads) is out of scope for this repo and handled by `grape-recorder`.

---

## Prerequisites

- `radiod` (ka9q-radio) installed and running
- Multicast networking working on your LAN
- A configured `timestd-config.toml`

---

## Install

```bash
git clone https://github.com/mijahauan/hf-timestd.git
cd hf-timestd

sudo ./scripts/install.sh --mode production --user $USER
sudo nano /etc/hf-timestd/timestd-config.toml
```

---

## Services

Continuous services:

- `timestd-core-recorder.service`
- `timestd-analytics.service`
- `timestd-web-ui.service`

Start/enable:

```bash
sudo systemctl start timestd-core-recorder timestd-analytics timestd-web-ui
sudo systemctl enable timestd-core-recorder timestd-analytics timestd-web-ui
```

Logs:

```bash
journalctl -u timestd-core-recorder -f
journalctl -u timestd-analytics -f
journalctl -u timestd-web-ui -f
```

---

## Paths

- Data root: `/var/lib/hf-timestd/`
- Logs: `/var/log/hf-timestd/`
- Config: `/etc/hf-timestd/`

---

## Operational notes

- Keep the host time synchronized (NTP/GPSDO as applicable).
- Monitor the Web UI and service logs for packet loss, gaps, and timing anomalies.

---

## Phase 3 / uploads

If you need 10 Hz products and PSWS/GRAPE uploads, deploy the separate `grape-recorder` project.

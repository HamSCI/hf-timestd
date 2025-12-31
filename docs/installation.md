# Installation Guide

This guide covers installing `hf-timestd` and its dependencies.

`hf-timestd` records and analyzes HF time standard stations (BPM, CHU, WWV, WWVH).

**Scope note:** `hf-timestd` does not perform decimation and does not use `digital_rf`. Phase 3 products and uploads are handled by the separate `grape-recorder` project.

---

## Prerequisites

- Linux host (Debian/Ubuntu class)
- Python 3.10+
- `radiod` (ka9q-radio) installed and running

---

## Install from source

```bash
git clone https://github.com/mijahauan/hf-timestd.git
cd hf-timestd

python3 -m venv venv
source venv/bin/activate
pip install .
```

---

## Web UI

The Web UI is now Python-based (FastAPI).

```bash
cd web-ui
./start_server.sh
```

Open:

- `http://localhost:3000`

---

## Configuration

Primary config:

- `config/timestd-config.toml`

Start in test mode first:

- `mode = "test"`

---

## Run (test scripts)

```bash
./scripts/timestd-all.sh -start
./scripts/timestd-all.sh -status
```

---

## Production install

See:

- `INSTALLATION.md`
- `docs/PRODUCTION.md`

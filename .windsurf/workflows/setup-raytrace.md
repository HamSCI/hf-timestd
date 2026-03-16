---
description: Set up PHaRLAP/pyLAP ray-tracing for hf-timestd physics overlay
---

## Prerequisites

PHaRLAP 4.7.4 and pyLAP must be sibling directories of `hf-timestd`:

```
~/Sync/GitHub/
  hf-timestd/
  pharlap_4.7.4/   ← download from DST Group website
  pylap/           ← https://github.com/[pyLAP repo]
```

Download PHaRLAP from: https://www.dst.defence.gov.au/partner-with-us/access-our-technology

### macOS (Apple Silicon / Intel)

Homebrew GCC with gfortran is required:

```bash
brew install gcc
```

### Linux

Intel Fortran 2020 redistributable is required. Download from Intel and set:
```bash
export LD_LIBRARY=/path/to/l_comp_lib_2020.4.304/linux/compiler/lib/intel64_lin
```

---

## Step 1 — Build pyLAP

```bash
export PHARLAP_HOME=/Users/mjh/Sync/GitHub/pharlap_4.7.4
export DIR_MODELS_REF_DAT=$PHARLAP_HOME/dat
cd /Users/mjh/Sync/GitHub/pylap
pip install -e . --no-build-isolation
```

Expected output — modules that will be built on PHaRLAP 4.7.4:
- `BUILD pylap.raytrace_2d` / `raytrace_2d_sp` / `raytrace_3d` / `raytrace_3d_sp`
- `BUILD pylap.abso_bg`, `dop_spread_eq`, `irreg_strength`, `nrlmsise00`
- `BUILD pylap.igrf2016`, `iri2016`
- `SKIP  pylap.igrf2007`, `igrf2011`, `iri2007`, `iri2012`  ← legacy libs absent in 4.7.4

---

## Step 2 — Set runtime environment

Add to your shell profile (`.zshrc` / `.bashrc`):

```bash
export PHARLAP_HOME=/Users/mjh/Sync/GitHub/pharlap_4.7.4
export DIR_MODELS_REF_DAT=$PHARLAP_HOME/dat
export PYLAP_MODULES=/Users/mjh/Sync/GitHub/pylap/modules
```

For the hf-timestd systemd services on the Linux receiver, add to
`/etc/default/timestd` (or the service `Environment=` directives):

```ini
PHARLAP_HOME=/opt/pharlap_4.7.4
DIR_MODELS_REF_DAT=/opt/pharlap_4.7.4/dat
PYLAP_MODULES=/opt/pylap/modules
```

---

## Step 3 — Verify

```bash
PHARLAP_HOME=/Users/mjh/Sync/GitHub/pharlap_4.7.4 \
DIR_MODELS_REF_DAT=/Users/mjh/Sync/GitHub/pharlap_4.7.4/dat \
PYLAP_MODULES=/Users/mjh/Sync/GitHub/pylap/modules \
python3.11 -c "
from hf_timestd.core.raytrace_engine import RaytraceEngine
e = RaytraceEngine.build(receiver_lat=40.68, receiver_lon=-105.04)
print('available:', e.is_available())
from datetime import datetime, timezone
r = e.compute_modes('WWV', 10.0, datetime.now(timezone.utc))
for m in r.modes:
    print(f'  {m.n_hops}F  delay={m.group_delay_ms:.2f} ms  elev={m.launch_elev_deg:.1f}°')
"
```

---

## Step 4 — Enable in pyproject.toml (optional)

Uncomment the `pylap` entry in the `[raytrace]` optional-dependency group:

```toml
raytrace = [
    "scipy>=1.10.0",
    "pylap @ file:../pylap",   # ← uncomment after building
]
```

Then install: `pip install -e ".[raytrace]"`

---

## Notes

- **Not on the real-time critical path.** The chrony feed continues if
  `PHARLAP_HOME` is unset or pyLAP fails to import — `RaytraceEngine`
  degrades to a geometric (vacuum speed-of-light) fallback automatically.
- **PHaRLAP licensing.** PHaRLAP is free for research use but must be
  downloaded individually from DST Group. Do not redistribute the binaries.
- **macOS C source patches.** pyLAP upstream targets Linux/GCC; the
  following source fixes were applied for macOS clang compatibility:
  - `raytrace_2d.c`: `verifyIonoGrid`, `buidlIonoStruct` — `void` → `int`
  - `raytrace_3d.c`: `verifyIono`, `buildIonoStruct` — `void` → `int`
  - `igrf2016.c`: `igrf2016_calc_` → `igrf2020_calc_` (PHaRLAP 4.7.4 rename)

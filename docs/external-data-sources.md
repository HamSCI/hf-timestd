# hf-timestd external data sources

hf-timestd's propagation / ray-trace fidelity depends on several external
space-weather and ionosphere feeds. This document records what each source
is, how it is fetched, its reliability characteristics, and the conclusions
of the 2026-06-13 audit.

Quick health check:

```bash
hf-timestd data sources            # cached snapshot
hf-timestd data sources --refresh  # live fetch
hf-timestd data sources --json
```

## Summary

| Source | What it provides | Fetcher | Cadence | Status |
|---|---|---|---|---|
| **IRI indices** (`apf107.dat`, `ig_rz.dat`) | F10.7, Ap, IG12, Rz12 for IRI-2020 / PHaRLAP | `scripts/update-iri-indices.sh` (timer) | weekly | Healthy |
| **NOAA SWPC** | Near-real-time F10.7 + Kp/Ap (current day) | `core/space_weather.py` (in-process) | 30 min | Healthy |
| **GFZ Potsdam** | Kp (fallback for SWPC) | `core/space_weather.py` | 30 min | Healthy |
| **CDDIS IONEX** | GPS global TEC maps (VTEC) | `scripts/ionex_integration.py` (timer) | daily | Healthy (needs Earthdata creds) |
| **GIRO / DIDBase** | Real-time ionosonde foF2 / hmF2 | `core/iono_data_service.py` (in-process) | 5 min | Repaired 2026-06-13; server intermittently overloaded |
| **WAM-IPE** | (intended) IPE ionosphere grid | `core/iono_data_service.py` | — | **Disabled** — no usable product published (see below) |

All in-process HTTP fetching goes through `core/net_fetch.py`, which provides
one shared `requests.Session` with bounded retry + exponential backoff,
per-request timeouts, and a descriptive User-Agent. CDDIS keeps its own
credentialed session (`cddis_auth.py`) for Earthdata Basic-Auth + redirects.

## Real-time ionosphere stack (priority order)

When the propagation model asks for ionospheric parameters at a point:

1. **GIRO ionosonde** measurement, if a station with recent autoscaled data
   is near the point (full weight ≤ ~555 km, zero beyond ~3330 km).
2. **IRI-2020** climatology (via `IonosphericModel`), driven by the weekly
   `apf107.dat` indices and the near-real-time F10.7 from SpaceWeatherService.
3. **Parametric** internal climatology, only if IRI is unavailable.

GIRO refinements are blended on top of the IRI (or parametric) base, so a
nearby sounding sharpens the climatology rather than replacing it wholesale.

## Source details

### IRI indices — `update-iri-indices.sh`
Refreshes `apf107.dat` (daily F10.7 + Ap) and `ig_rz.dat` (monthly IG12 +
Rz12) that IRI-2020 / PHaRLAP read at runtime. Primary source is the eCHAIM
mirror, fallback is irimodel.org; both validated by size + format. Weekly
cadence is fine for the smoothed indices but lags for the current day —
which is why SpaceWeatherService exists.

### NOAA SWPC + GFZ — `core/space_weather.py`
`SpaceWeatherService` fetches and caches the latest **F10.7** (SWPC
`products/summary/10cm-flux.json`, with the DSD text file as fallback) and
**Kp/Ap** (SWPC `products/noaa-planetary-k-index.json`, with GFZ
`kp.gfz.de` as Kp fallback). Values are range-validated and cached to
`iono_cache/space_weather.json`. The current F10.7 is injected into the
parametric ionosphere tier for any caller that doesn't supply one.

### CDDIS IONEX — `ionex_integration.py`
Downloads IGS Global Ionosphere Maps (Final, then Rapid, walking back up to
7 days) for GPS VTEC, via NASA Earthdata Login. **Optional enrichment**: if
`/etc/hf-timestd/earthdata-netrc` is not configured the daily timer skips
cleanly (see `docs/NASA_EARTHDATA_SETUP.md`).

### GIRO / DIDBase — `core/iono_data_service.py`
- **Station list**: parsed from the `DIDBFastStationList` HTML page (URSI
  code + lat/lon), with a bundled fallback table at
  `hf_timestd/data/giro_stations.tsv` and an on-disk cache, so GIRO keeps
  working when the page is down or its HTML changes.
- **Measurements**: the `DIDBGetValues` web service (`charName=foF2,hmF2,CS`,
  dates `YYYY/MM/DD HH:MM:SS`). Columns are parsed **by name** from the
  `#`-prefixed header and range-validated (foF2 0.5–30 MHz, hmF2 100–600 km).
- Only the **nearest ~12 ionosondes** to the operator are polled each cycle.
- The DIDBase server is frequently overloaded (HTTP 503 / Tomcat 404
  flapping); the retry/backoff session and graceful per-station degradation
  absorb this without losing the rest of the data.

### WAM-IPE — DISABLED
Audited 2026-06-13: the operational Whole-atmosphere Forecast System feeds —
both the public S3 PDS (`s3://noaa-nws-wam-ipe-pds/`, prefix `v1.2/wfs.*`)
and NOMADS (`.../wfs/prod`) — publish only the WAM **neutral** atmosphere
(variable `den`, kg m⁻³, on a fixed-height grid). They carry **no IPE
ionosphere product**: no electron density, NmF2, hmF2 or TEC. Neutral density
is not usable for HF ray-tracing, so the branch cannot supply the parameters
it was written for. It is disabled by default (`enable_wamipe=False`) and the
NetCDF parser now refuses to fabricate a grid from constant defaults (which
would otherwise have served a uniform fake grid tagged `source="wamipe"`).
The fetch/parse code is retained for the day an actual IPE 2D product is
published.

## Known follow-ups

- **IRI Fortran availability**: the `iri2020` Python package needs a Fortran
  compiler to build; if absent, `IonosphericModel` transparently falls back
  to its parametric tier (labeled `parametric`, not `iri`). Confirm `gfortran`
  is installed on the station (or that PHaRLAP is the intended IRI path).
- **apf107.dat current-day augmentation**: optionally appending today's
  observed F10.7/Ap from SWPC to `apf107.dat` so the IRI Fortran path also
  sees current conditions. Deferred — the in-process F10.7 injection already
  covers the parametric path, and writing the fixed-width file is risky.

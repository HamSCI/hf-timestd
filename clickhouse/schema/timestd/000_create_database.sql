-- hf-timestd: timestd database
-- Greenfield producer schema for per-cycle L2 detection events from
-- WWV/WWVH/CHU/BPM time-standard broadcasts.  L1 (raw correlator
-- output) stays in HDF5 — see hf-timestd's METROLOGY.md.  This DB
-- is the additive L2 staging tier the future hs-uploader will
-- ship to upstream destinations (PSWS, etc.).

CREATE DATABASE IF NOT EXISTS timestd;

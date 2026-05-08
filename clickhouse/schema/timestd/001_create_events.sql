-- hf-timestd: timestd.events — one row per L2 detection cycle.
--
-- Emitted by hf-timestd's fusion service (`multi_broadcast_fusion`)
-- after each fused result is written to HDF5.  L1 (raw correlator
-- output, ~kHz rate) is wrong shape for CH and stays in HDF5; L2
-- (per-minute fused detections, ~1 row/min/station) lands here.
--
-- Field layout combines:
--   * the plan's L2 inventory (time, station, frequency, snr, raw_toa,
--     toa_uncertainty, doppler, quality_flag, distance_km,
--     delay_plausible, processing_version) — see
--     /opt/git/sigmond/clickhouse/STATUS.md "Phase C"
--   * hf-timestd's L2TimingMeasurement record (clock_offset_ms,
--     expanded_uncertainty_ms, discrimination_method, propagation_mode,
--     n_hops, quality_grade) — see
--     hf-timestd/src/hf_timestd/models/measurement.py
--
-- ORDER BY tuple matches the natural query / dedup key:
-- "events from this station on this frequency at this time" — the
-- triple uniquely identifies one detection cycle.

CREATE TABLE IF NOT EXISTS timestd.events
(
    -- common header (CONTRACT v0.6 §17 column convention)
    time                       DateTime64(3, 'UTC') CODEC(Delta(8), ZSTD(1)),
    host_call                  LowCardinality(String) CODEC(LZ4),
    host_grid                  LowCardinality(String) CODEC(LZ4),
    radiod_id                  LowCardinality(String) CODEC(LZ4),
    instance                   LowCardinality(String) CODEC(LZ4),
    processing_version         LowCardinality(String) CODEC(LZ4),

    -- transmitter identity
    station                    LowCardinality(String) CODEC(LZ4),  -- WWV/WWVH/CHU/BPM
    frequency_khz              Int32                  CODEC(T64, ZSTD(1)),

    -- detection observables (always present)
    raw_toa_ms                 Float64                CODEC(Delta(4), ZSTD(3)),
    toa_uncertainty_ms         Float64                CODEC(Delta(4), ZSTD(3)),
    clock_offset_ms            Float64                CODEC(Delta(4), ZSTD(3)),
    expanded_uncertainty_ms    Nullable(Float64)      CODEC(Delta(4), ZSTD(3)),

    -- propagation context (nullable — not every cycle resolves these)
    snr_db                     Nullable(Float32)      CODEC(Delta(4), ZSTD(3)),
    doppler_hz                 Nullable(Float32)      CODEC(Delta(4), ZSTD(3)),
    distance_km                Nullable(Float32)      CODEC(Delta(4), ZSTD(3)),
    propagation_mode           LowCardinality(String) CODEC(LZ4),    -- "1F" / "2F" / "3F" / ""
    n_hops                     Nullable(UInt8)        CODEC(T64, ZSTD(1)),

    -- quality + provenance
    quality_flag               LowCardinality(String) CODEC(LZ4),    -- GOOD/MARGINAL/BAD/MISSING
    quality_grade              LowCardinality(String) CODEC(LZ4),    -- A/B/C/D
    discrimination_method      LowCardinality(String) CODEC(LZ4),    -- TONE/BCD/FUSION/...
    delay_plausible            UInt8                  CODEC(T64, ZSTD(1)),  -- 0/1

    ingested_at                DateTime DEFAULT now() CODEC(Delta(4), ZSTD(1))
)
ENGINE = ReplacingMergeTree()
PARTITION BY toYYYYMM(time)
ORDER BY (host_call, station, frequency_khz, time)
SETTINGS index_granularity = 8192;

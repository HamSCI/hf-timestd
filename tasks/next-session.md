# Next session — TSL3 as the highest-quality timing authority

User direction 2026-05-21: TSL3 is the HF-PPS-injection path that the
authority manager treats as T6 (top tier).  Phase 4 cleanup is done;
focus shifts to making sure TSL3 actually behaves at that quality
level.  Review current stability, variability, and overall quality;
address any problems.

## Baseline (snapshot 2026-05-21 16:14 UTC)

`chronyc sources -a`:
```
#* TSL3   stratum=0  reach=377  offset=+1432ns[+1091ns]  ±55us
```

`chronyc sourcestats TSL3`:
```
NP=10  NR=3  span=9  offset=+1520ns  std_dev=58ns
```

`authority_history.db` latest snapshot:
```
A1 / T6 active  sigma_ns=50000  t6_offset_ms=0.000238  t6_sigma_ms=0.05
```

So under normal conditions TSL3 is solidly the chrony primary with
sub-microsecond offset and ~60 ns short-term jitter — actually
*better* than the 145 ns std dev recorded at the original 2026-05-15
TSL3-LIVE checkpoint.  Authority pipeline pegs it as T6 with σ=50 μs.

## The signal that's worth chasing

`DIAG_chrony_stats` for `source_name='TSL3'` shows the source flipping
between selected (`*`) and unusable (`?`) every ~1-2 minutes:

```
2026-05-21T16:13:54Z | * | offset=0.226 us | reach=255
2026-05-21T16:12:46Z | * | offset=0.733 us | reach=255
2026-05-21T16:11:39Z | * | offset=0.292 us | reach=255
2026-05-21T16:10:29Z | ? | offset=4.660 us | reach=0
2026-05-21T16:09:22Z | * | offset=0.184 us | reach=255
2026-05-21T16:08:14Z | ? | offset=6.118 us | reach=0
2026-05-21T16:07:06Z | * | offset=-0.149 us | reach=255
2026-05-21T16:06:04Z | * | offset=-0.159 us | reach=255
2026-05-21T16:04:56Z | * | offset=0.439 us | reach=255
2026-05-21T16:03:48Z | * | offset=0.656 us | reach=255
```

The `?` rows have **reach=0** (chrony dropped 8 consecutive polls
worth of state) and offset ~5 μs — still small in absolute terms but
out-of-family compared to the surrounding 0.2-0.7 μs samples.  Two
`?` events in ~10 minutes.

Hypotheses worth checking:

1. **Producer pause.**  The HF-PPS feed into chrony's SHM segment may
   be intermittently late.  Find out:
   - Which process writes the SHM (sigmond? a specific
     hf-timestd subsystem?) and what its loop guarantees look like.
   - Is there a `[BREADCRUMB]` / scandir / reader-of-L1 path that
     could stall for >1 sample interval?
   - Is the SHM update happening from a thread that contends with
     a slow consumer?

2. **Chrony's own jitter / sample-aging threshold.**  chrony marks
   refclock sources `?` when samples age past tolerance.  Knob:
   `refclock SHM <n> dpoll 0 noselect refid TSL3 precision 1e-6
   delay 0.0` (or similar — confirm `/etc/chrony/chrony.conf`).
   If the SHM cadence is >1/s, the `dpoll`/`maxsamples` may need
   tuning.

3. **HF-PPS edge quality.**  The injected PPS edge has its own
   jitter signature — if the upstream HF detector occasionally
   reports an edge that's >σ_expected from the running median,
   chrony's `maxchange` or `maxoffset` clamps may be marking it
   unusable.  Look for clamp-log lines in the chrony log.

4. **t6_offset_ms vs chronyc Last sample.**  Authority records
   `t6_offset_ms=0.000238` (238 ns).  chronyc reports `+1432 ns`.
   These should agree within sampling slop.  If they don't, the
   T6 pipeline and chrony aren't seeing the same offsets — that
   would indicate a feeder bug.

## Where to start

1. **30-min historical scan** of `DIAG_chrony_stats` for TSL3:
   ```sql
   SELECT
     strftime('%H:%M', timestamp_utc) AS t,
     source_state, offset_us, std_dev_us, reach, n_samples
   FROM DIAG_chrony_stats
   WHERE source_name = 'TSL3'
     AND timestamp_utc > datetime('now', '-30 minutes')
   ORDER BY rowid;
   ```
   Look at the `?`-rate over a longer window.  Two-per-10-min may
   be normal, or it may be growing/correlating with something.

2. **`chronyc sourcestats TSL3` over time.**  std_dev=58ns now;
   has it been steady?  `chronyc sourcestats -a` history isn't
   logged, but `authority_history.db.t6_sigma_ms` is sampled
   per-cycle and can be queried as a time series.

3. **Locate the SHM writer.**  Probably one of:
   - hf-timestd `t6_probe` / `t6_timing_poll_loop` (in
     `core/core_recorder_v2.py`)
   - sigmond GPSDO governor
   - a refclock SHM driver elsewhere
   `grep -rn 'SHM\|shmctl\|ntpd\|shm_attach' src/` + `cat
   /etc/chrony/chrony.conf` will localize it fast.

4. **Cross-check the authority pipeline path.**  T6 sigma is set
   by who?  `grep -rn 't6_sigma_ms\|t6_offset_ms' src/`.  If the
   `?` events also show up in t6_sigma jumps, the bug is in the
   shared upstream.  If only chrony sees them, the bug is between
   the SHM writer and chrony.

## Related memory to read at start of session

- [[project_hf_timestd_costas_drift]] — TSL3 history: 6 fixes
  shipped to get to LIVE on 2026-05-15, last std dev 145 ns.
  Resolved root cause was a ka9q-python IQ-encoding mismatch.
- [[project_ka9q_python_iq_encoding_fix]] — the proximate fix.
- [[project_hf_timestd_authority_work]] — authority manager design.
  T6 is the top-tier witness; understanding the T6 pipeline is
  prerequisite for any TSL3 quality work.
- [[project_leo_bodnar_gpsdo_next]] — GpsdoProbe / authority.json
  integration; GPS PPS sits above HF PPS in the witness hierarchy
  but both feed the same authority manager.
- [[project_hf_timestd_wall_clock_wiring]] — Pattern B Layers 1-4
  shipped; AuthoritySnapshotStore + authority_history.db is the
  historical record for any time-series analysis.

## Out-of-scope (worth being aware of, not the focus)

- The `_malloc_trim` polish call in `multi_broadcast_fusion.py:202`
  — evaluate-or-delete after a few days of clean Phase-4-post RSS.
- `scripts/live_vtec.py` variable renames (hdf5_writer → vtec_writer).
- The SqliteDataProductWriter NaN+NOT-NULL gap (test currently
  skipped in `tests/unit/test_tid_l3_writer.py`).
- The `sys.path.insert(...)` lines in web-api modules that allowed
  the stale `/opt/hf-timestd/src/` snapshot to shadow the editable
  install — should be removed source-side.

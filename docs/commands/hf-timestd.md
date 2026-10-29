# hf-timestd command examples

> **Audience:** operator, contributor

These examples come from AC0G-B4, the project's test station, on 2026-10-29 at about 22:36 UTC. Each command only reads. The program also prints a log line such as `Logging configured at INFO level` on its error stream. The examples leave that line out.

## hf-timestd status

Use this first when you ask whether the timing pipeline works. It prints a one-word verdict and checks how fresh the fusion data is.

```
hf-timestd status
```

```
{
  "status": "WARN",
  "exit_code": 1,
  "calibration": null,
  "data_freshness": {
    "fusion_store": {
      "table": "L3_fusion_timing",
      "age_seconds": 0.3,
      "stale": false
    },
    "active_metrology_channels": 4,
    "total_metrology_channels": 6
  }
}
```

Look at `status` and `exit_code`. The command returns 0 for OK, 1 for WARN and 2 for CRIT. Run without `--calib-file`, it reports WARN with exit code 1 on a healthy station: it finds fusion data younger than 600 seconds and has no calibration file to judge. This capture shows that case, and `"calibration": null` marks it. With `--calib-file <path>`, the command reads that file instead. It reports OK with exit code 0 when the file says `usable` and holds an age under 300 seconds. It reports WARN when the file loads but does not qualify, and CRIT when the file is stale or missing.

Read `data_freshness` for the real health signal. A fusion age of a few seconds with `stale` false means the fusion service still writes results. A large age or `stale` true means the pipeline has stopped. The channel counts stand apart from the verdict. `active_metrology_channels` counts channels with data younger than 300 seconds, out of the channels the table holds. Here that gives 4 of 6. The counts never change `status` or `exit_code`. At night the high WWV channels often carry no signal, so a lower count there needs no alarm.

## hf-timestd quality

Use this to check whether the recorders lose any radio packets. A recorder that loses packets feeds gaps into every timing result.

```
hf-timestd quality
```

The real output lists six recorders. This example keeps the first one and the summary, and replaces the other five with `…` (trimmed).

```
{
  "schema_version": 1,
  "captured_at": 1793313364.7059128,
  "instance": "default",
  "client": "hf-timestd",
  "recorders": [
    {
      "description": "SHARED_2500",
      "frequency_hz": 2500000,
      "stream_state": "recording",
      "uptime_seconds": 34484.17,
      "packets_received_total": 5172621,
      "packets_lost_total": 0,
      "gap_events_total": 0,
      "completeness_pct": 100.0
    },
    …
  ],
  "summary": {
    "recorder_count": 6,
    "recorders_with_data": 6,
    "min_completeness_pct": 100.0,
    "total_packets_lost": 0,
    "max_packets_lost_rate": 0.0
  },
  "stale_seconds": 2.8,
  "snapshot_path": "/run/hf-timestd/quality.json"
}
```

The real output also carries ssrc, late, duplicate and resequenced counters, and rates for each recorder. They are trimmed here.

Look at the summary. Good means `recorders_with_data` equals `recorder_count`, `min_completeness_pct` sits at 100 and `total_packets_lost` is 0. A recorder whose `stream_state` is not `recording`, or any lost packets, marks a problem. A large `stale_seconds` means the snapshot no longer updates.

## hf-timestd service status

Use this to see which hf-timestd services should run and which ones do.

```
hf-timestd service status
```

```
Profile: full

  SERVICE                 CONFIG  STATE        SYSTEMD UNIT
  ────────────────────── ───────  ──────────── ──────────────────────────────────────
  chrony_monitor              on  active(waiting) timestd-chrony-monitor.timer
  core_recorder               on  active(running) timestd-core-recorder.service
  fusion                      on  active(running) timestd-fusion.service
  l2_calibration              on  active(running) timestd-l2-calibration.service
  metrology                   on  active(active) timestd-metrology.target
  pipeline_watchdog           on  active(waiting) timestd-pipeline-watchdog.timer
  prune                       on  active(waiting) timestd-prune.timer
  radiod_monitor              on  active(running) timestd-radiod-monitor.service
  vtec                        on  active(running) timestd-vtec.service
```

Compare the CONFIG and STATE columns. A service with CONFIG `on` should show `active`. Timers show `active(waiting)` between runs, which is normal. A service that shows `inactive` or `failed` while CONFIG says `on` needs attention.

## hf-timestd chain-delay show

Use this to see the stored delay of the T6 timing chain, and whether the judge uses it.

```
hf-timestd chain-delay show
```

The real output prints the record file, its history, and the judge's own copy. This example keeps the record and the judge's state, and trims the history and the duplicated judge fields with `…`.

```
record file /var/lib/timestd/state/t6_chain_delay.json:
{
  "schema": 1,
  "offset_ns": -17222404.47998047,
  "sigma_ns": 302205.7408655258,
  "spread_ns": 1287725.4009246826,
  "n": 717,
  "n_eff": 120.0866620146667,
  "span_s": 7205.199720880002,
  "learned_from_utc": "2026-10-27T21:30:13Z",
  "learned_to_utc": "2026-10-27T23:30:18Z",
  "lower_tier": "T4",
  "state": "confirmed",
  "candidate": null,
  "identity": {
    "t6_frequency_hz": 45375000.0,
    "t6_sample_rate": 96000.0,
    "low_edge_hz": -25000.0,
    "high_edge_hz": 25000.0
  },
  "history": […]
}
judge chain_delay block (/run/hf-timestd/offset_judge.json):
{
  "state": "confirmed",
  "record": {…},
  "candidate": null,
  "changed": null,
  "learning": null
}
```

Look at `state`. `confirmed` means a learned delay is in use. `seeded` means a starting guess is in use and no full learn has replaced it yet. The record and the judge's `state` should agree. A `candidate` that is not null waits for `hf-timestd chain-delay accept`. The offset here is about -17.2 ms, with a sigma of about 0.3 ms.

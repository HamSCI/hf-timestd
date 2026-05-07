# TSL Refclock Quality Comparison — Post Matched-Filter Deploy

**Host:** bee1
**Window:** 2026-05-06 17:00 UTC → 2026-05-07 00:49 UTC
**Sources:** `/var/log/chrony/statistics.log`, `/var/log/chrony/tracking.log`
**Compiled:** 2026-05-07

## Purpose

Quantitative validation of the BPSK PPS matched-filter calibrator deployed on
2026-05-06 (96 kHz / ±25 kHz; commits `bb6e52e` and `b9e643c`). This document
compares the four time references seen by chrony on bee1 — TSL1, TSL2, TSL3,
and the trusted local NTP server `192.168.1.80` (labeled `Time` in
`chronyc sources`) — and characterizes what the OS clock actually achieved.

## Window cutoff

The 17:00 UTC start excludes the chain_delay disambiguation transient that
followed the deploy. TSL3 hourly mean |offset| on 2026-05-06:

| Hour UTC | mean \|offset\| |
|----------|-----------------|
| 12       | 134 ms          |
| 13       | 453 µs          |
| 14       | 453 µs          |
| 15       | 94 µs           |
| 16       | 42 µs           |
| **17**   | **3.9 µs**      |
| 23       | 4.3 µs          |

17:00 UTC is the first hour at which |offset| collapsed to the steady-state
microsecond class.

## Per-source raw quality (`statistics.log`)

| Source                       | n      | mean σ    | min σ   | max σ   | mean \|off\| | offset range |
|------------------------------|--------|-----------|---------|---------|--------------|--------------|
| **TSL3** (matched-filter)    | 13,499 | **140 ns**| **1 ns**| 44 µs   | **3.95 µs**  | 124 µs       |
| TSL1 (legacy SHM)            |    697 | 11.0 µs   | 1.15 µs | 61.9 µs | 1.11 ms      | 1.82 ms      |
| TSL2 (legacy SHM)            |    697 | 15.4 µs   | 1.65 µs | 83.4 µs | 94 µs        | 674 µs       |
| Time (192.168.1.80, trusted) |  2,476 | 6.14 µs   | 311 ns  | 19.0 µs | 5.0 µs       | 159 µs       |

Ratios of mean σ:

- TSL3 is **~80× better than TSL1**
- TSL3 is **~110× better than TSL2**
- TSL3 is **~44× better than the local NTP server (Time)**

TSL3 min σ confirms the **1 ns chrony std-dev floor** claimed for the matched
filter.

## What chrony actually selected (`tracking.log`)

Each row in `tracking.log` records the source chrony was following at that
update — i.e., which reference was disciplining the system clock.

### Post-deploy stable window (2026-05-06 17:00 UTC → 2026-05-07 00:49 UTC)

| Selected reference        | n              | mean \|offset\| | mean offset sd | max max-error |
|---------------------------|----------------|------------------|----------------|---------------|
| **TSL3**                  | 13,496 (93.5%) | **124 ns**       | 6.74 µs        | 292 µs        |
| Time (192.168.1.80)       |    938 ( 6.5%) | 1.60 µs          | 2.28 µs        | 1.50 s\*      |
| TSL1, TSL2                | 0              | —                | —              | —             |

\* Single transient row during a brief TSL3 dropout. chrony's *bound* widened
to 1.5 s; the actual clock error was much smaller.

### May 7 only (most recent, fully settled)

| Selected reference  | n             | mean \|offset\| | max \|offset\| | max max-error |
|---------------------|---------------|------------------|------------------|---------------|
| **TSL3**            | 3,749 (99.4%) | **114 ns**       | 8.8 µs           | 131 µs        |
| Time (192.168.1.80) |    22 ( 0.6%) | 5.3 µs           | 14.5 µs          | 152 µs        |

By 2026-05-07, chrony picked TSL3 **99.4 %** of the time, with mean OS-clock-
vs-TSL3 offset of **114 ns**.

## Conclusions

1. **TSL3 dominates chrony.** Selected as the system reference 93.5 % of the
   post-deploy window, rising to 99.4 % once the chain_delay transient cleared.
2. **TSL1 and TSL2 are now diagnostic-only.** chrony rejected them in favor of
   TSL3 in every tracking update during this window. Their raw σ is ~80–110×
   worse than TSL3's, and TSL1's mean |offset| is ~280× worse.
3. **Time (192.168.1.80) is a healthy fallback.** ~6 µs class — when TSL3
   briefly drops out, the OS clock degrades from the 100 ns class to the µs
   class, never the ms class.
4. **The matched filter delivered the promised step-change.** The 1 ns σ floor
   is measured, not theoretical, and chrony's selection logic agrees with the
   raw quality numbers.

## Caveats

- **TSL3 mean |offset| on 2026-05-06 (full day) was 0.195 s** — driven entirely
  by the chain_delay disambiguation transient before 17:00 UTC. By the May 7
  window it was 4.3 µs.
- **The 1.50 s `max_maxerr` on Time(80)** is chrony's confidence bound during a
  TSL3 dropout, not actual clock error.
- **May 7 data ends at ~00:49 UTC** (log was open at fetch time). Confidence on
  May-7-only rows is lower than multi-day rows.
- The `Time` rows that show up in raw `statistics.log` greps (~3,294 hits) are
  rotation-header artifacts (`Date (UTC) Time IP Address ...`), not refclock
  samples. Filter with `NR>3` or test that column 4 is numeric.

## Reproduction

All numbers above can be reproduced from the bee1 chrony logs (sudo required):

```bash
START="2026-05-06 17:00:00"

# Per-source raw quality
sudo awk -v cut="$START" '
  NR>3 && $3 ~ /^(TSL1|TSL2|TSL3)$/ {
    if ($1" "$2 < cut) next;
    s=$3; n[s]++; sum[s]+=$4;
    if (!(s in mn) || $4<mn[s]) mn[s]=$4;
    if (!(s in mx) || $4>mx[s]) mx[s]=$4;
  }
  END { for (s in n) printf "%-5s n=%d mean_sd=%.3e min=%.3e max=%.3e\n",
                            s,n[s],sum[s]/n[s],mn[s],mx[s] }
' /var/log/chrony/statistics.log

# Selection share + system clock vs reference
sudo awk -v cut="$START" '
  NR>3 && ($1" "$2)>=cut && $3 ~ /^(TSL[123]|192\.168\.1\.80)$/ {
    s=$3; n[s]++; o=($7<0?-$7:$7); sum_o[s]+=o
  }
  END { for (s in n) printf "%-15s n=%d mean|off|=%.3e\n", s,n[s],sum_o[s]/n[s] }
' /var/log/chrony/tracking.log
```

## Related

- `docs/METROLOGY.md` — overall timing chain
- `docs/DUAL_CHRONY_FEED_ARCHITECTURE.md` — TSL1/TSL2/TSL3 design
- `docs/PIPELINE_VERIFICATION.md` — verification methodology
- Commits: `dbdfe58` (matched-filter), `bb6e52e` + `b9e643c` (chrony SHM
  ordering), `a9be7fd` (T4 disambiguation), `dfc1d0d` (SHM padding fix)

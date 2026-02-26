# NEVER CHANGE THE FOLLOWING PRIMARY INSTRUCTION!!!

**Primary Instruction:** In this context you will perform a critical review of the HF Time Standard (hf-timestd) project, either in its entirety or in a specific component, as specified by the user. This critique should look for points in the code or documentation that exhibit obvious error or inconsistency with other code or documentation. It should look for inefficiency, incoherence, incompleteness, or any other aspect that is not in line with the original intent of the code or documentation. It should also look for obsolete, deprecated, or "zombie" code that should be removed. Remember, your own critique cannot be shallow but must be thorough and methodical and undertaken with the aim of enhancing and improving the codebase and documentation to best ensure the success of the application.

Make your criticism from the perspective of 1) a user of the system, 2) a metrologist, 3) a ionospheric scientist, and 4) a software engineer. These perspectives can differ in their priorities and interests, and your critique should reflect this. For example, a user of the system will be most interested in the accuracy and reliability of the system, while a metrologist will be most interested in the precision and traceability of the system, while an ionospheric scientist will be most interested in the ionospheric conditions and their impact on the system, and a software engineer will be most interested in the code quality, maintainability, coverage and adequacy of testing, and the resilience and robustness of the system over time and in edge cases. Ultimately, however, a consensus of these perspectives should guide your critique in service of meeting the application's objectives.

# NEVER CHANGE THE PRECEEDING PRIMARY INSTRUCTION!!!

# The following secondary instruction and information will guide your critique in this particular session (the instructions below will vary from session to session)

---

## 📋 NEXT SESSION: CHU FSK DECODER — DEFINITIVE OPTIMIZATION

**Task:** Make the CHU FSK decoder reliably decode on all machines. Diagnose why it fails silently on two of three machines despite good CHU signals. Fix the decoder, not the symptoms. Produce working, tested code.

**What "working" means:**
1. Reliably decodes DUT1, TAI-UTC, year, and time on at least one CHU frequency per machine
2. Dashboard shows date+time (not time-only) for "Last Decode"
3. Decode success rate ≥50% of minutes when SNR permits (currently ~3/9 frames on the one machine where it works at all)

---

## System Context

- **Receiver:** GPSDO-locked RX888 SDR via KA9Q-radio, RTP-timestamped IQ at 24 kHz/channel
- **CHU Frequencies:** 3.330 MHz, 7.850 MHz, 14.670 MHz (Ottawa, Canada)
- **Machines:** 3 identical setups (B3-1, others) — decoder works intermittently on B3-1, fails on the other two
- **Location:** EM38 (~38.9°N, ~92.1°W, central Missouri)
- **Git:** `/home/mjh/git/hf-timestd/` | **Production:** `/opt/hf-timestd/` | **Data:** `/var/lib/timestd/`
- **Deploy:** `sudo scripts/update-production.sh [--pull]`

---

## CHU FSK Signal Specification (NRC)

CHU broadcasts a **Bell 103** compatible FSK time code during **seconds 31–39** of each minute:

| Parameter | Value |
|---|---|
| Mark frequency | 2225 Hz (logic 1) |
| Space frequency | 2025 Hz (logic 0) |
| Center frequency | 2125 Hz |
| Baud rate | 300 bps |
| Bit duration | 3.333 ms |
| Frame format | 1 start + 8 data + 1 parity (even) + 1 stop = 11 bits/byte |
| Bytes per second | 10 (5 data + 5 redundancy) |
| Timing per second | 0–10ms: 1000 Hz tick, 10–~133ms: mark sync, ~133–500ms: data, 500ms = precise boundary |

**Frame types:**
- **Frame A** (seconds 32–39): `6d dd hh mm ss` (BCD, nibble-swapped, repeated as bytes 5–9)
- **Frame B** (second 31): `xz yy yy tt aa` (DUT1, year, TAI-UTC, DST; bytes 5–9 = bitwise NOT of 0–4)

---

## Architecture Overview

```
CHUFSKListener (chu_fsk_listener.py)
  │
  ├── Creates USB-preset channels on radiod (12 kHz real audio)
  ├── CHUFSKChannel: 75s ring buffer per frequency, RTP-aligned
  ├── Health monitor thread per channel (10s check, 15s stale timeout)
  ├── _decode_loop: runs at ~2s past each minute boundary
  │     │
  │     ├── get_aligned_minute(minute_boundary) → 60s audio
  │     └── CHUFSKDecoder.decode_minute(audio, minute_boundary, is_audio=True)
  │           │
  │           ├── For each second 31–39:
  │           │   ├── detect_tick_onset() → tick timing (primary, ~0.05ms)
  │           │   ├── _fsk_demodulate_audio() → soft decisions (quadrature discriminator)
  │           │   ├── _find_first_start_bit() → synchronization
  │           │   ├── _extract_bits() → 110 bits
  │           │   ├── _bits_to_bytes() → 10 bytes (with parity check)
  │           │   └── _decode_frame_a() or _decode_frame_b()
  │           │
  │           └── _find_consensus_time() → majority vote across frames
  │
  ├── Writes JSON → /dev/shm/timestd/fsk_results/{channel}.json
  └── Writes HDF5 → phase2/{CHANNEL}/broadcast:fsk/

Web Dashboard (metrology.html)
  └── loadCHUFSK() → GET /api/metrology/chu-fsk/latest
        └── CHUFSKService (chu_fsk_service.py)
              ├── Primary: reads JSON from /dev/shm (last 5 min)
              └── Fallback: reads HDF5 (last 3 days)
```

---

## Known Bug 1: "Last Decode" Shows Time-Only (No Date)

**Root cause:** `metrology.html` line 1047 uses `formatTime(data.last_decode)` which calls `Date.toLocaleTimeString()` — time only, no date. When the last successful decode was days ago (from HDF5 fallback), the display shows just `17:23:00` with no indication it's stale.

**Files:**
- `web-api/static/js/common.js:51-60` — `formatTime()` = time only; `formatTimestamp()` = date+time
- `web-api/static/metrology.html:1047` — uses `formatTime` for "Last Decode"
- `web-api/static/metrology.html:955` — also uses `formatTime` in the warning banner

**Fix:** Replace `formatTime(data.last_decode)` with `formatTimestamp(data.last_decode)` on both lines 955 and 1047. Consider also adding a `timeAgo()` annotation (e.g., "2d 6h ago").

---

## Known Bug 2: Decoder Fails Silently on Other Machines

**Symptom:** On two of three machines, CHU signals are present and tone detection works (ticks detected, SNR adequate), but the FSK decoder produces `detected=false` every minute. No error in logs — just "frames=0/9" every cycle.

**Suspected failure modes (investigate all):**

### FM-1: Parity check is too strict (rejects ALL frames on marginal signals)

`_bits_to_bytes()` (line 392) rejects entire frames when parity errors exceed a threshold. On HF paths with moderate SNR, individual bit errors are common. The redundancy check (bytes 0–4 == 5–9 for Frame A) is already a strong integrity check — parity rejection before that discards frames that could have passed redundancy. **The current code at line 416 notes "log but don't reject" in the comment, but the actual behavior should be verified carefully.** If there's a code path that rejects on parity, it's too aggressive for HF.

### FM-2: Start bit search may fail at low SNR

`_find_first_start_bit()` has two strategies:
- **Frame A (secs 32–39):** Pattern-matches the 11-bit frame for expected byte `0x06`. Requires ≥9/11 bits matching. At low SNR this threshold may be too high.
- **Frame B (sec 31):** Edge detection with adaptive threshold based on `std * 0.3`. May fail with weak signals or DC offset.

Both search within a narrow window (50ms–200ms into the second). If the signal has a timing offset (e.g., from ionospheric delay), the start bit may be outside this window.

### FM-3: Consensus requires ≥3 Frame A decodes

`decode_minute()` line 971: consensus needs `len(frame_a_results) >= 3`. If only 1–2 frames decode (marginal signal), `detected` stays True but decoded_day/hour/minute aren't set via consensus. The fallback (line 1000) uses `max(set(...), key=count)` but logs a warning and doesn't validate time consistency. On other machines with weaker signals, this could produce unreliable results that get filtered out downstream.

### FM-4: AM demodulation may not apply (audio mode)

The listener passes `is_audio=True` because USB channels deliver real audio, not IQ. But `decode_second()` receives both `audio` and optionally `iq_samples`. When `is_audio=True`, `iq_samples` is `None` (line 930), so the code uses `_fsk_demodulate_audio()` (Hilbert → quadrature discriminator). This is correct, but the Hilbert transform on real audio can produce artifacts at signal edges. The IQ-direct path (`_fsk_demodulate_iq()`) is only used when the listener passes IQ — which it never does in the current architecture.

### FM-5: No SNR gating or diagnostic logging for "why no frames decoded"

When all 9 seconds produce `frame=None`, the only log is `frames=0/9, conf=0.00` — no per-second breakdown of _why_ each second failed. Add per-second diagnostic: was it start-bit search failure? Parity rejection? Redundancy mismatch? Byte decoding failure? This is essential for diagnosing cross-machine failures.

---

## Known Bug 3: Duplicate Decoder in advanced_signal_analysis.py

`advanced_signal_analysis.py` lines 1123–1353 contain a completely separate `decode_chu_fsk()` implementation with:
- A Python-loop Goertzel algorithm (extremely slow, ~300x slower than the production decoder)
- Wrong frame format (no nibble swap, wrong BCD parsing, wrong redundancy check)
- No parity checking, no consensus, no timing

This is dead code (never called in production) but could confuse future developers. It should be deleted.

---

## Key Files for This Session

| File | What to do |
|---|---|
| `src/hf_timestd/core/chu_fsk_decoder.py` | **PRIMARY TARGET.** Fix start-bit search robustness, review parity strictness, improve SNR tolerance, add per-second failure diagnostics |
| `src/hf_timestd/core/chu_fsk_listener.py` | Review decode loop timing, verify audio alignment, check health monitor behavior |
| `src/hf_timestd/core/advanced_signal_analysis.py:1123-1353` | **DELETE** the duplicate `decode_chu_fsk()` and related FSK methods |
| `web-api/static/metrology.html:955,1047` | Fix "Last Decode" to show date+time (`formatTimestamp`) |
| `web-api/services/chu_fsk_service.py` | Review HDF5 fallback logic, verify `last_decode` timestamp propagation |
| `web-api/static/js/common.js` | Reference only — `formatTime` vs `formatTimestamp` |
| `tests/test_chu_frame_slip.py` | **EXPAND** — add synthetic signal tests, SNR sweep, per-second failure mode tests |
| `src/hf_timestd/core/wwv_constants.py:329-343` | CHU FSK constants (reference) |

---

## Diagnostic Commands

```bash
# Check FSK listener status
cat /dev/shm/timestd/fsk_results/_status.json | python3 -m json.tool

# Check latest decode results per channel
for f in /dev/shm/timestd/fsk_results/CHU_*.json; do echo "=== $(basename $f) ==="; python3 -m json.tool "$f" 2>/dev/null | head -20; done

# Check FSK decoder logs (last 50 lines with FSK)
journalctl -u timestd-core-recorder --no-pager -n 200 | grep -i fsk | tail -50

# Check HDF5 decode history
python3 -c "
import h5py, sys
for ch in ['CHU_3330', 'CHU_7850', 'CHU_14670']:
    path = f'/var/lib/timestd/phase2/{ch}/broadcast:fsk/{ch}_chu_fsk_$(date -u +%Y%m%d).h5'
    try:
        with h5py.File(path, 'r', locking=False) as f:
            n = f['timestamp_utc'].shape[0]
            valid = sum(f['fsk_valid'][:])
            print(f'{ch}: {valid}/{n} valid decodes today')
    except: print(f'{ch}: no data')
"
```

---

## Success Criteria

1. **Dashboard fix deployed:** "Last Decode" shows `MM/DD/YYYY, HH:MM:SS` (using `formatTimestamp`)
2. **Per-second diagnostics:** Each FSK second logs reason for failure (start-bit miss / parity reject / redundancy fail / byte-count short)
3. **Decoder robustness:** On B3-1, decode success rate improves from 3/9 to ≥5/9 frames per minute on CHU_3330 (the best channel)
4. **Cross-machine fix:** At least one CHU channel decodes on each of the other two machines (verify via HDF5 history)
5. **Dead code removed:** `advanced_signal_analysis.py` duplicate FSK decoder deleted
6. **Tests expanded:** `test_chu_frame_slip.py` gains synthetic signal tests covering FM-1 through FM-5
7. **All changes committed and pushed**

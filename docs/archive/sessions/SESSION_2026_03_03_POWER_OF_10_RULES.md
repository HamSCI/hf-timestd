# Session: 2026-03-03 Power of 10 Rules Implementation

## Overview
Adapted and applied Gerard Holzmann's "Power of 10" rules for Python, specifically tailored to the `hf-timestd` real-time DSP and high-throughput I/O architecture. 

## Rule Adaptations & Implementations

### 1. Control Flow (`globals()` removal)
- **Problem:** `multi_broadcast_fusion.py` mutated global state (`last_chrony_d_clock`, `last_chrony_update_time`) inside the main loop, violating predictability.
- **Fix:** Removed all `globals()` checks. Initialized variables explicitly outside the main loop to preserve state between iterations.

### 2. Error Handling (No Silent Catch-All Exceptions)
- **Problem:** Widespread use of `except:` and `except Exception:` followed by `pass` or silent ignoring meant critical thread crashes or HDF5 locks went unlogged.
- **Fix:** Performed a global sweep over 17 core files (e.g., `audio_streamer.py`, `chu_fsk_decoder.py`, `multi_broadcast_fusion.py`). Replaced all bare exceptions with explicit `except Exception as e:` and added corresponding `logger.error` or `logger.debug` tracebacks. 

### 3. Explicit Interfaces (No `**kwargs` in core APIs)
- **Problem:** Core stream subscription APIs in `stream_api.py` relied on implicit `**kwargs`, breaking static analysis and IDE autocomplete.
- **Fix:** Replaced `**kwargs` with explicit typed parameters (`agc: bool`, `gain: float`, `destination: Optional[str]`, `description: str`) for `subscribe_iq`, `subscribe_usb`, `subscribe_am`, and `subscribe_batch`.

### 4. Resource Management (C-level Handles and HDF5)
- **Problem:** Python's Garbage Collector does not deterministically close `np.memmap` views or `h5py.File` objects.
- **Fix (binary_archive_writer):** Modified `np.memmap` usage. Read mapped data into a standard `numpy` array, then explicitly called `del mm` to force file descriptor release.
- **Fix (timing_validation_service):** Converted raw `h5py.File()` instantiation into a strict `with h5py.File(...) as f:` context manager.

### 5. Memory/State (DSP Zero-Allocation Hot Paths)
- **Problem:** `np.abs(iq_samples)` was allocating new ~5.7MB `float32` arrays during every process minute per channel. Over time, these allocations caused malloc arena fragmentation and runaway RSS memory bloat.
- **Fix (`metrology_engine.py` & `tick_matched_filter.py`):** 
  - Added pre-allocated persistent `_envelope_buffer` arrays during class initialization.
  - Rewrote AM demodulation to use the `out=` parameter: `np.abs(iq_samples, out=rf_envelope)`. 

## Artifacts Created
- `.windsurf/contracts/CODING_CONTRACT.md`

## Next Steps
- Gradual reduction of cyclomatic complexity (specifically in `MetrologyEngine.process_minute` which has a complexity of 86).
- Further integration of strict typing (`mypy`) into legacy metadata parsing dictionaries.

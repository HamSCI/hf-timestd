# The Power of 10: Python Edition

Adapting Gerard Holzmann's "Power of 10" rules for `hf-timestd`'s specific combination of real-time DSP, high-throughput I/O, and continuous execution. These rules account for the realities of Python's Garbage Collector (GC), `numpy` memory semantics, and `h5py` file locking.

| Goal | Proposed Rule for `hf-timestd` |
| :--- | :--- |
| **Control Flow** | **1.** No `exec`, `eval`, or `globals()` manipulation. |
| **Control Flow** | **2.** Strict cyclomatic complexity limits (max 3 levels of nesting). |
| **Interfaces** | **3.** No implicit `*args` or `**kwargs` in core APIs; explicit signatures only. |
| **Interfaces** | **4.** Strict type hinting, with `NDArray` exceptions isolated to DSP internals. |
| **Error Handling**| **5.** No bare `except:`. Thread-level broad catches must log `ERROR` tracebacks. |
| **State/Memory** | **6.** Pre-allocate and mutate `numpy` buffers in the hot path (zero-allocation DSP). |
| **State/Memory** | **7.** Immutable `dataclass` or `NamedTuple` for all metadata and measurements. |
| **Resources** | **8.** Strict context managers for all I/O, plus explicit release of `mmap`/C-level handles. |
| **Architecture** | **9.** Compute threads must never block on disk/network I/O. |
| **Environment** | **10.** Strict environment isolation (`venv`) with exact version pinning, no side-effect imports. |

## Detailed Guidelines

### 1. Invert Rule 8 for the DSP Data Plane (Pre-allocate, Don't Copy)
**Original:** Immutable Data Structures by Default.
**Adjustment:** Use strict immutability for *metadata and measurements* (e.g., `NamedTuple` for timing snapshots), but **mandate in-place mutation for high-throughput DSP arrays**. 
*   **Why:** Creating immutable copies of 60-second float64 IQ buffers (1.44M samples) repeatedly crosses glibc's `MMAP_THRESHOLD`. This causes malloc arena fragmentation that Python's allocator never returns to the OS, leading to runaway RSS memory leaks.
*   **Rule:** The hot path (e.g., `process_minute`, `_correlate_window`) must reuse pre-allocated `numpy` arrays using `out=` parameters (e.g., `np.multiply(a, b, out=c)`) to prevent GC pauses and memory fragmentation.

### 2. Expand Rule 4 to Ban Silent Error Swallowing
**Original:** No "Catch-All" Exceptions.
**Adjustment:** If a top-level thread boundary requires a broad exception catch to prevent a total service crash, it **must** log the full traceback at `ERROR` or `WARNING` level, never `DEBUG` or `INFO`.
*   **Why:** `hf-timestd` relies on continuous asynchronous services. If a broad exception handler catches an error at the `DEBUG` level while production runs at `INFO` level, the service could silently starve for days without visible errors.

### 3. Expand Rule 7 for C-level File Descriptors
**Original:** Explicit Resource Management (Context Managers).
**Adjustment:** Specifically mandate explicit closure and reference clearing for `h5py.File`, `np.memmap`, and file-backed numpy arrays.
*   **Why:** Python's GC does not deterministically clean up C-level file descriptors or memory-mapped pages. A `np.frombuffer()` that holds a reference to a decompressed memory view will leak the underlying file descriptor indefinitely. Explicit `del mm` or `.close()` is required.

### 4. Adjust Rule 2 for DSP/Numpy Typing
**Original:** Mandatory Type Hinting and Static Validation.
**Adjustment:** Enforce strict typing for business logic and control flow, but allow `numpy.typing.NDArray` without strict shape/dtype validation where static tools fail. 
*   **Why:** `mypy` and `pyright` struggle to accurately validate complex matrix broadcast shapes and `dtype` transitions in `scipy`/`numpy`. Static checks should not block deployment if the type ambiguity is isolated strictly inside a math-heavy DSP routine.

### 5. Add a Rule: No Blocking I/O in the Audio/DSP Thread
**New Rule:** Separation of Compute and I/O.
*   **Why:** The FFT and audio threads operate on strict real-time deadlines. Writing L2 metrology to HDF5 or performing database lookups must happen in separate background threads/processes. A 200ms disk wait will cause the ring buffer to drop incoming RTP packets.

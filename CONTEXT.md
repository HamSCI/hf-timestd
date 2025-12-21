# HF Time Standard Analysis (hf-timestd) - AI Context Document

**Author:** Michael James Hauan (AC0G)  
**Last Updated:** 2025-12-21  
**Version:** 6.0 (ManagedStream Refactor & Network Fixes)

---

## Project Scope

`hf-timestd` records and analyzes HF time standard stations (WWV, WWVH, CHU, BPM) to derive sub-millisecond timing products via ionospheric path analysis.

**Core Pipeline:**
1.  **Phase 1 (Ingest):** `core-recorder` (Python) -> `radiod` (Multicast RTP) -> `raw_buffer` (Binary IQ).
2.  **Phase 2 (Analytics):** `timestd-analytics` (Python) -> Reads `raw_buffer` -> Produces `D_clock`.
3.  **Phase 3 (Fusion):** `multi_broadcast_fusion` -> Fuses 17 broadcasts -> Feeds Chrony SHM.
4.  **Web UI:** `monitoring-server-v3.js` (Node) -> `summary.html` / `timing-dashboard-enhanced.html`.

---

## Architecture: The "ManagedStream" Paradigm (v6.0)

**CRITICAL:** As of v6.0, `core-recorder` **strictly** relies on the `ka9q-python` library's `ManagedStream` for all channel lifecycle management. Do NOT re-implement manual discovery or channel creation logic.

### 1. Channel Management
-   **Strategy:** "Frequency First" via `ManagedStream`.
-   **Mechanism:** `core-recorder` instantiates `ManagedStream(..., ssrc=None)`.
-   **Library Logic:** `ManagedStream` calls `ensure_channel(freq)`. If `radiod` has a channel on that frequency, it returns the existing SSRC. If not, it creates one.
-   **Result:** Prevents channel proliferation (duplicate channels) and race conditions.

### 2. Networking & Discovery
-   **Status Group:** `bee1-hf-status.local` (mDNS FQDN). **Hardcoded fallbacks (e.g., 239.192...) are REMOVED.**
-   **Discovery:** `discover_channels()` is used by both `core-recorder` (startup check) and `timestd-analytics` (SNR query).
-   **Reliability:** Explicit retry logic (3 attempts, 1s backoff, 2.5s timeout) is enforced in application code to handle startup races with `radiod`.

---

## Data Flow

```
[Antenna] -> [SDR] -> [radiod (C++)]
                          |
                          v (Multicast RTP: 239.116.198.49:5004)
                          |
[core-recorder (Python)] <+  <-- ManagedStream (Input)
       |
       +--> [raw_buffer (Phase 1)] --> {data_root}/raw_buffer/{YYYYMMDD}/{min}.bin
       |                               (Immutable, System Time)
       |
[timestd-analytics (Python)] <-- Reads raw_buffer (Input)
       |
       +--> [Analysis Engine]
       |         |
       |         +--> [tone_detector] -> [multi_station_detector]
       |         +--> [phase2 output] -> {data_root}/phase2/.../clock_offset/
       |
       +--> [Fusion Engine] -> [Chrony SHM] (System Clock Discipline)
```

---

## Next Session Goal: Restore Channel Audio Playback

**Objective:** The user wants to listen to the live or recorded audio of the channels via `summary.html`.

### Current State
-   **Web UI:** `summary.html` likely has a placeholder or broken player.
-   **Backend:** `monitoring-server-v3.js` serves API endpoints.
-   **Missing Piece:** We need to verify how audio is delivered.
    -   **Option A (Live):** WebSockets re-streaming RTP (via `core-recorder` or `radiod` direct?).
    -   **Option B (Recorded):** Serving `.bin` files converted to `.wav`/`.mp3` on the fly?
    -   **Legacy:** Previous versions might have had a simple "Listen" button.

### Strategy for Next Agent
1.  **Inspect `summary.html`:** Look for the "Audio" or "Listen" elements.
2.  **Inspect `monitoring-server-v3.js`:** Identify `/api/v1/audio` endpoints.
3.  **Determine Source:**
    -   If live: How do we bridge Multicast RTP to Web Browser (which doesn't support Multicast)? Likely need a WebSocket proxy in Node.js or Python.
    -   If recorded: Check if `raw_buffer` files can be transcoded quickly.

**Constraint:** Do NOT change the `core-recorder` channel management logic. It is fixed. Focus only on the plumbing to get audio bytes to the browser.

---

## Configuration & Environment

**Config:** `/etc/hf-timestd/timestd-config.toml`
-   **Status Address:** `bee1-hf-status.local` (Strict)
-   **RTP Dest:** `239.116.198.49:5004` (Derived from status group)

**Services:**
-   `timestd-core-recorder` (Phase 1)
-   `timestd-analytics` (Phase 2)
-   `timestd-web-ui` (Phase 3 UI)

**Useful Scripts:**
-   `scripts/list_radiod_channels.py`: Verifies `radiod` state.
-   `scripts/check_version.py`: Verifies source code version.


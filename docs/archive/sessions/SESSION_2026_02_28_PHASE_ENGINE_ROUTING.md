# Session: Phase Engine Support & FSK Decoupling
**Date:** 2026-02-28  
**Version Bump:** 6.8.0 -> 6.9.0

## Phase Engine Native Integration
`hf-timestd` now has first-class native support for taking in steered, beamformed streams from Phase Engine instead of directly attaching to an omnidirectional `radiod` listener.

When configuring the station with `scripts/setup-station.sh`, the user is prompted to select between `radiod` and `phase-engine`. 
Selecting `phase-engine` toggles `engine = "phase-engine"` in the TOML configuration.

### Channel Expansion
When `engine = "phase-engine"`, `CoreRecorderV2` dynamically unpacks `SHARED_` standard frequencies (2.5, 5, 10, 15 MHz) into three independent, targeted `StreamRecorderV2` channels (e.g., `WWV_10`, `WWVH_10`, `BPM_10`).
This automatically injects the correct `target=` query parameter to `ka9q-radio`, signaling the upstream Phase Engine to actively route and steer spatial beams for these individual stations onto identical frequencies without overlapping streams.

## CHU FSK USB Sidecar Removal
Previously, `hf-timestd` ran an independent `CHUFSKListener` instance that opened three concurrent USB streams alongside the archived IQ streams purely to decode the CHU FSK timing sequences.

This has been **completely stripped out** to simplify the architecture and reduce `radiod` multicast load.
Instead, `MetrologyEngine` now taps directly into the raw Phase 1 stored IQ buffers. It internally AM-demodulates and feeds the baseband data to the `CHUFSKDecoder` loop.

### Real-Time Dashboard Support
`MetrologyEngine` now handles updating the JSON state payloads for the web dashboard (`/dev/shm/timestd/fsk_results/*.json`) natively instead of relying on the decoupled listener process. `MetrologyService` successfully archives L2 HDF5 products natively.

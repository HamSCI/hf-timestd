# GRAPE Channel Setup Process

## Overview

This document describes the channel setup process from `grape-config.toml` definitions through to receiving validated RTP data. The process must handle multiple scenarios robustly with proper error checking.

## Configuration Source

### grape-config.toml Structure

```toml
[station]
id = "S000171"           # Used for deterministic multicast IP
instrument_id = "172"    # Used for deterministic multicast IP

[ka9q]
status_address = "bee1-hf-status.local"  # Primary radiod instance
auto_create_channels = true

[recorder.channel_defaults]
preset = "iq"
sample_rate = 20000
agc = 0
gain = 0
encoding = "float"       # REQUIRED: complex64 float IQ

[[recorder.channels]]
frequency_hz = 10000000
description = "WWV 10 MHz"
# Can override any default parameter
```

## Deterministic Multicast IP

GRAPE generates a unique multicast IP from `station.id` + `station.instrument_id`:

```python
def generate_grape_multicast_ip(station_id: str, instrument_id: str) -> str:
    key = f"GRAPE:{station_id}:{instrument_id}"
    hash_bytes = hashlib.sha256(key.encode()).digest()
    return f"239.{(hash_bytes[0] % 254) + 1}.{hash_bytes[1]}.{(hash_bytes[2] % 254) + 1}"
```

This ensures:
- Each GRAPE station has its own exclusive multicast stream
- No collision with other radiod clients
- Persistent across restarts

## Channel Setup Flow

### Phase 1: Discovery

```
┌─────────────────────────────────────────────────────────────────┐
│ 1. DISCOVER RADIOD INSTANCES                                    │
│    ka9q.discover_radiod_services() → list of {name, address}   │
│    - Multi-host aware: finds all radiod on network             │
│    - Returns status multicast addresses                         │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│ 2. SELECT TARGET RADIOD                                         │
│    - Match config's status_address to discovered services       │
│    - Validate radiod is reachable                               │
│    - Create RadiodControl connection                            │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│ 3. DISCOVER EXISTING CHANNELS                                   │
│    ka9q.discover_channels(status_address) → {ssrc: ChannelInfo} │
│    ChannelInfo provides:                                        │
│      - frequency, sample_rate, preset                           │
│      - multicast_address, port                                  │
│      - ssrc, snr, gps_time, rtp_timesnap                       │
└─────────────────────────────────────────────────────────────────┘
```

### Phase 2: Channel Matching & Validation

For each frequency in `grape-config.toml`:

```
┌─────────────────────────────────────────────────────────────────┐
│ 4. MATCH BY FREQUENCY                                           │
│    Search existing channels for frequency match (±1 Hz)         │
│                                                                 │
│    FOUND?                                                       │
│    ├── YES → Go to Step 5 (Validate Existing)                  │
│    └── NO  → Go to Step 6 (Create New)                         │
└─────────────────────────────────────────────────────────────────┘
```

### Phase 3: Validate Existing Channel

```
┌─────────────────────────────────────────────────────────────────┐
│ 5. VALIDATE EXISTING CHANNEL                                    │
│                                                                 │
│    5a. CHECK DESTINATION (Anti-Hijacking)                       │
│        existing.multicast_address == our_grape_multicast_ip?    │
│        ├── YES → This is OUR channel, proceed to 5b            │
│        └── NO  → This is SOMEONE ELSE's channel                │
│                  DO NOT MODIFY - create new channel instead     │
│                                                                 │
│    5b. CHECK PARAMETERS                                         │
│        - preset == "iq"?                                        │
│        - sample_rate == 20000?                                  │
│        - encoding == float? (check via payload inspection)      │
│                                                                 │
│        ALL MATCH?                                               │
│        ├── YES → Use existing SSRC, go to Step 7               │
│        └── NO  → Reconfigure via tune(), go to Step 7          │
└─────────────────────────────────────────────────────────────────┘
```

### Phase 4: Create New Channel

```
┌─────────────────────────────────────────────────────────────────┐
│ 6. CREATE NEW CHANNEL                                           │
│                                                                 │
│    RadiodControl.create_channel(                                │
│        frequency_hz=freq,                                       │
│        preset="iq",                                             │
│        sample_rate=20000,                                       │
│        destination=our_grape_multicast_ip,                      │
│        ssrc=None  # Auto-allocate                               │
│    )                                                            │
│                                                                 │
│    Then set encoding:                                           │
│    RadiodControl.set_output_encoding(ssrc, FLOAT_ENCODING)      │
│                                                                 │
│    Verify channel created:                                      │
│    RadiodControl.verify_channel(ssrc, frequency_hz)             │
└─────────────────────────────────────────────────────────────────┘
```

### Phase 5: Join and Validate Payload

```
┌─────────────────────────────────────────────────────────────────┐
│ 7. JOIN RTP STREAM                                              │
│                                                                 │
│    RTPReceiver.join_multicast(our_grape_multicast_ip, 5004)     │
│    Register callback for each SSRC                              │
│                                                                 │
│    7a. VALIDATE FIRST PACKET                                    │
│        - Parse RTP header                                       │
│        - Check payload size matches expected:                   │
│          samples_per_packet = sample_rate * blocktime_ms / 1000 │
│          payload_bytes = samples_per_packet * 8 (complex64)     │
│        - Verify payload is float (not int16):                   │
│          Check value range and byte pattern                     │
│                                                                 │
│    VALIDATION PASSED?                                           │
│    ├── YES → Start recording                                    │
│    └── NO  → Log error, attempt reconfiguration via tune()     │
└─────────────────────────────────────────────────────────────────┘
```

## Error Handling Scenarios

### Scenario 1: Frequency Exists with Different Destination
```
Existing: 10 MHz → 239.7.86.199 (someone else's stream)
Required: 10 MHz → 239.1.57.206 (our GRAPE stream)

Action: CREATE NEW channel at same frequency with our destination
        radiod supports multiple clients at same frequency
Result: Two channels at 10 MHz, different destinations
```

### Scenario 2: Our Channel Exists with Wrong Parameters
```
Existing: 10 MHz → 239.1.57.206, preset=am, rate=12000
Required: 10 MHz → 239.1.57.206, preset=iq, rate=20000

Action: RECONFIGURE via tune()
        RadiodControl.tune(ssrc, preset="iq", sample_rate=20000)
Result: Channel updated, same SSRC preserved
```

### Scenario 3: Payload Encoding Mismatch
```
Expected: complex64 float (8 bytes/sample)
Received: int16 (4 bytes/sample)

Detection: First packet payload size != expected
           OR value range check fails

Action: RadiodControl.set_output_encoding(ssrc, FLOAT_ENCODING)
        Wait for next packet, re-validate
```

### Scenario 4: Radiod Restart / Channel Lost
```
Symptom: No packets received for >30 seconds

Action: 
1. Check if channel still exists via discover_channels()
2. If missing: recreate channel
3. If exists but no data: check multicast routing
```

## Implementation Requirements

### ChannelManager Enhancements

```python
class ChannelManager:
    def ensure_channels_from_config(self, channels, defaults, destination):
        """
        Enhanced channel setup with anti-hijacking and validation.
        
        Returns:
            Dict[int, ChannelSetupResult] mapping freq_hz to result
        """
        
    def validate_channel_ownership(self, channel_info, our_destination):
        """
        Check if existing channel belongs to us (same destination).
        
        Returns:
            True if channel.multicast_address == our_destination
        """
        
    def reconfigure_channel(self, ssrc, preset, sample_rate, encoding):
        """
        Update existing channel parameters via tune().
        """
        
    def validate_payload_format(self, payload, expected_samples, expected_encoding):
        """
        Validate RTP payload matches expected format.
        
        Returns:
            (valid: bool, detected_encoding: str, error: Optional[str])
        """
```

### RTPReceiver Enhancements

```python
class RTPReceiver:
    def validate_first_packet(self, ssrc, payload, expected_config):
        """
        Validate first packet from channel matches expected format.
        
        Checks:
        - Payload size matches sample_rate * blocktime_ms / 1000 * bytes_per_sample
        - Encoding matches (float vs int16)
        
        Returns:
            ValidationResult with details
        """
```

## Encoding Detection

Since `ChannelInfo` doesn't expose encoding, detect from payload:

```python
def detect_encoding(payload: bytes, samples_per_packet: int) -> str:
    """
    Detect encoding from RTP payload.
    
    Float (complex64): 8 bytes/sample, values typically < 1.0
    Int16 (complex int16): 4 bytes/sample, values in [-32768, 32767]
    """
    bytes_per_sample_float = 8  # complex64
    bytes_per_sample_int16 = 4  # complex int16
    
    if len(payload) == samples_per_packet * bytes_per_sample_float:
        # Check if values look like floats (small magnitude)
        import struct
        samples = struct.unpack(f'<{samples_per_packet * 2}f', payload)
        max_val = max(abs(s) for s in samples)
        if max_val < 100:  # Float IQ typically normalized
            return "float"
    
    if len(payload) == samples_per_packet * bytes_per_sample_int16:
        return "int16"
    
    return "unknown"
```

## Digital RF Output

`hf-timestd` writes Phase 1 data as per-minute binary complex64 under `raw_buffer/`.
Digital RF output (`raw_archive/`) is not used in this repository.

## Summary

| Step | Action | Error Handling |
|------|--------|----------------|
| 1 | Discover radiod services | Fallback to config address |
| 2 | Discover existing channels | Retry with backoff |
| 3 | Match by frequency | Create if not found |
| 4 | Check destination ownership | Create new if not ours |
| 5 | Validate parameters | Reconfigure via tune() |
| 6 | Create/configure channel | Retry, log failure |
| 7 | Join multicast | Verify group membership |
| 8 | Validate first packet | Reconfigure encoding if wrong |
| 9 | Start recording | Monitor for gaps/loss |

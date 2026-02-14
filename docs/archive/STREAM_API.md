# Stream API Documentation

**Version:** 1.1.0  
**Date:** December 1, 2025

## Overview

The Stream API provides an SSRC-free interface for subscribing to radiod streams. Applications specify **what they want** (frequency, preset, sample rate) and the system handles SSRC allocation, stream sharing, and lifecycle management internally.

## Quick Start

```python
from signal_recorder import subscribe_stream

# Subscribe to a stream - no SSRC needed!
stream = subscribe_stream(
    radiod="radiod.local",
    frequency_hz=10.0e6,
    preset="iq",
    sample_rate=16000
)

# Use the stream
print(f"Frequency: {stream.frequency_mhz} MHz")
print(f"Receive on: {stream.multicast_address}:{stream.port}")

# Clean up when done
stream.release()
```

## Core Concepts

### StreamSpec - What Makes a Stream Unique

A stream is uniquely identified by its **content specification**, not by SSRC:

```python
StreamSpec = (frequency_hz, preset, sample_rate, agc, gain)
```

Two requests with identical `StreamSpec` share the same underlying radiod channel.

### StreamHandle - What You Receive

When you subscribe, you get a `StreamHandle` containing:

| Property | Description |
|----------|-------------|
| `frequency_hz` | Center frequency in Hz |
| `frequency_mhz` | Center frequency in MHz (convenience) |
| `preset` | Demodulation mode (iq, usb, am, etc.) |
| `sample_rate` | Output sample rate in Hz |
| `multicast_address` | Where to receive RTP packets |
| `port` | RTP port number |
| `agc` | Whether AGC is enabled |
| `gain` | Manual gain in dB |

The internal SSRC is available via `stream.ssrc` but applications should not depend on it.

### SSRC Allocation

SSRCs are allocated internally using a deterministic hash of the `StreamSpec`. This means:
- Same spec → same SSRC (across restarts, across managers)
- Different specs → different SSRCs (even at same frequency)
- No collisions between apps with different requirements

## API Reference

### Primary Functions

#### `subscribe_stream()`

Subscribe to a radiod stream.

```python
def subscribe_stream(
    radiod: str,
    frequency_hz: float,
    preset: str = "iq",
    sample_rate: int = 16000,
    agc: bool = False,
    gain: float = 0.0,
    destination: Optional[str] = None,
    description: str = ""
) -> StreamHandle
```

**Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `radiod` | str | required | mDNS name or address (e.g., "radiod.local") |
| `frequency_hz` | float | required | Center frequency in Hz |
| `preset` | str | "iq" | Demodulation mode: "iq", "usb", "lsb", "am", "fm", "cw" |
| `sample_rate` | int | 16000 | Output sample rate in Hz |
| `agc` | bool | False | Enable automatic gain control |
| `gain` | float | 0.0 | Manual gain in dB (when agc=False) |
| `destination` | str | None | Multicast destination (optional) |
| `description` | str | "" | Human-readable description for logging |

**Returns:** `StreamHandle`

**Example:**
```python
# WWV 10 MHz IQ for GRAPE
stream = subscribe_stream(
    radiod="bee1-hf.local",
    frequency_hz=10.0e6,
    preset="iq",
    sample_rate=16000
)

# WSPR 20m USB for decoding
stream = subscribe_stream(
    radiod="bee1-hf.local",
    frequency_hz=14.0956e6,
    preset="usb",
    sample_rate=12000
)
```

### Convenience Functions

#### `subscribe_iq()`

```python
stream = subscribe_iq(radiod, frequency_hz=10.0e6, sample_rate=16000)
```

#### `subscribe_usb()`

```python
stream = subscribe_usb(radiod, frequency_hz=14.0956e6, sample_rate=12000)
```

#### `subscribe_am()`

```python
stream = subscribe_am(radiod, frequency_hz=10.0e6, sample_rate=12000)
# AGC enabled by default for AM
```

### Batch Operations

#### `subscribe_batch()`

Create multiple streams with identical parameters (efficient for apps like GRAPE):

```python
streams = subscribe_batch(
    radiod="bee1-hf.local",
    frequencies=[2.5e6, 5.0e6, 10.0e6, 15.0e6, 20.0e6],
    preset="iq",
    sample_rate=16000,
    destination="239.1.2.101:5004"  # All on same data channel
)

for stream in streams:
    print(f"{stream.frequency_mhz} MHz → {stream.rtp_address}")
```

### Discovery Functions

#### `discover_streams()`

See all existing streams on a radiod instance:

```python
streams = discover_streams("radiod.local")
for s in streams:
    print(f"{s.spec.frequency_hz/1e6:.4f} MHz, {s.spec.preset}")
```

#### `find_stream()`

Find a specific compatible stream:

```python
existing = find_stream(
    radiod="radiod.local",
    frequency_hz=10.0e6,
    preset="iq",
    sample_rate=16000
)
if existing:
    print(f"Found: {existing}")
```

### Lifecycle Management

#### Context Manager

```python
with subscribe_stream(radiod, frequency_hz=10.0e6, preset="iq") as stream:
    # Use stream
    process_data(stream)
# Automatically released when context exits
```

#### Manual Release

```python
stream = subscribe_stream(radiod, frequency_hz=10.0e6, preset="iq")
try:
    process_data(stream)
finally:
    stream.release()
```

#### Global Cleanup

```python
from signal_recorder import close_all

# On application shutdown
close_all()  # Releases all managed streams
```

## Multi-Application Coordination

### Same Frequency, Different Modes

Two apps can use the same frequency with different parameters:

```python
# App 1: GRAPE wants IQ for Doppler analysis
grape_stream = subscribe_stream(
    radiod="radiod.local",
    frequency_hz=10.0e6,
    preset="iq",
    sample_rate=16000
)

# App 2: Audio monitor wants AM for listening
audio_stream = subscribe_stream(
    radiod="radiod.local",
    frequency_hz=10.0e6,
    preset="am",
    sample_rate=12000,
    agc=True
)

# These are DIFFERENT streams (different preset/rate)
# No collision, no interference
```

### Automatic Sharing

Two requests with identical parameters share the same stream:

```python
# Both get the same underlying radiod channel
stream1 = subscribe_stream(radiod, frequency_hz=10e6, preset="iq", sample_rate=16000)
stream2 = subscribe_stream(radiod, frequency_hz=10e6, preset="iq", sample_rate=16000)

assert stream1.multicast_address == stream2.multicast_address
assert stream1.port == stream2.port

# Reference counting ensures stream persists until all handles released
stream1.release()  # Stream still alive (stream2 holds reference)
stream2.release()  # Now stream can be cleaned up
```

### Data Channel Assignment

Direct streams to specific multicast groups for isolation:

```python
# GRAPE streams on dedicated data channel
grape_streams = subscribe_batch(
    radiod="radiod.local",
    frequencies=[2.5e6, 5.0e6, 10.0e6, 15.0e6],
    preset="iq",
    sample_rate=16000,
    destination="239.1.2.101:5004"  # GRAPE data channel
)

# WSPR streams on different data channel
wspr_streams = subscribe_batch(
    radiod="radiod.local",
    frequencies=[7.0386e6, 10.1387e6, 14.0956e6],
    preset="usb",
    sample_rate=12000,
    destination="239.1.2.102:5004"  # WSPR data channel
)
```

## Advanced Usage

### Direct StreamManager Access

For complex scenarios, use `StreamManager` directly:

```python
from signal_recorder import StreamManager

manager = StreamManager(
    radiod_address="radiod.local",
    default_destination="239.1.2.100:5004",
    auto_cleanup=True
)

# Subscribe
stream = manager.subscribe(frequency_hz=10.0e6, preset="iq", sample_rate=16000)

# List all managed streams
for handle in manager.list_managed():
    print(handle.info())

# Cleanup
manager.close()
```

### Integration with RecordingSession

The Stream API works alongside the existing recording infrastructure:

```python
from signal_recorder import subscribe_stream, RTPReceiver, RecordingSession

# Get stream info
stream = subscribe_stream(radiod, frequency_hz=10.0e6, preset="iq", sample_rate=16000)

# Create receiver for that multicast group
receiver = RTPReceiver(stream.multicast_address, stream.port)

# Create recording session (uses internal SSRC)
session = RecordingSession(
    config=SessionConfig(
        ssrc=stream.ssrc,  # Internal SSRC accessible if needed
        sample_rate=stream.sample_rate,
        segment_duration_sec=60.0
    ),
    writer=MyWriter()
)
```

## Presets Reference

| Preset | Description | Output Format | Typical Use |
|--------|-------------|---------------|-------------|
| `iq` | Raw I/Q (no demod) | Complex float32 | Recording, DSP |
| `usb` | Upper sideband | Real float32 | SSB, WSPR, FT8 |
| `lsb` | Lower sideband | Real float32 | SSB voice |
| `am` | Amplitude mod | Real float32 | Broadcast, WWV audio |
| `fm` | Frequency mod | Real float32 | VHF/UHF voice |
| `cw` | Morse code | Real float32 | CW reception |

## Error Handling

```python
try:
    stream = subscribe_stream(
        radiod="radiod.local",
        frequency_hz=10.0e6,
        preset="iq"
    )
except RuntimeError as e:
    print(f"Failed to create stream: {e}")
    # Possible causes:
    # - radiod not running
    # - Network unreachable
    # - Invalid parameters
```

## Migration from Manual SSRC

**Before (v1.0):**
```python
from ka9q import RadiodControl
from signal_recorder import RTPReceiver

control = RadiodControl("radiod.local")
control.create_channel(ssrc=20100, frequency_hz=10e6, preset="iq", sample_rate=16000)

receiver = RTPReceiver("239.192.152.141", 5004)
receiver.register_callback(20100, my_callback)
```

**After (v1.1):**
```python
from signal_recorder import subscribe_stream

stream = subscribe_stream(
    radiod="radiod.local",
    frequency_hz=10.0e6,
    preset="iq",
    sample_rate=16000
)
# SSRC handled internally, multicast info in stream handle
```

Both approaches continue to work - the Stream API is additive, not a breaking change.

# Integration with ka9q-radio (radiod)

This document explains how signal-recorder integrates with ka9q-radio's `radiod` and what each component is responsible for.

## Division of Responsibilities

### radiod Manages:
- **Hardware interface** - Talks to RX888/SDR hardware
- **RF tuning** - Sets LO frequencies, gains, filters
- **Multicast addressing** - Assigns multicast groups and ports
- **RTP streaming** - Generates RTP packets with audio data
- **Dynamic channel allocation** - Creates channels on demand
- **Status broadcasting** - Publishes channel metadata

### signal-recorder Manages:
- **Channel requirements** - What frequencies to record
- **Recording** - Captures RTP streams to disk
- **Processing** - Converts to GRAPE format
- **Upload** - Sends to HamSCI server
- **Scheduling** - When to process and upload

## What You Configure

### In radiod@.conf (Minimal)

You only need the **global** section for dynamic channel support:

```ini
[global]
hardware = rx888
status = your-status-address.local
data = your-data-address.local
mode = iq
samprate = 16000
```

**That's it!** No need to define individual channels.

### In signal-recorder config.toml

You define **what** you want to record:

```toml
[ka9q]
status_address = "your-status-address.local"
auto_create_channels = true

[[recorder.channels]]
ssrc = 10000000
frequency_hz = 10000000
description = "WWV 10 MHz"
```

**No multicast addresses or ports needed!** These are discovered automatically.

## How It Works

### 1. Startup Discovery

When signal-recorder starts:

```
signal-recorder → control → radiod
                    ↓
              "List all channels"
                    ↓
              radiod responds with:
              - SSRCs
              - Frequencies  
              - Multicast addresses ← radiod assigns these!
              - Ports
              - Sample rates
              - Presets
```

### 2. Channel Creation

If a channel doesn't exist:

```
signal-recorder → control → radiod
                    ↓
              "Create SSRC 10000000"
              "Set frequency 10 MHz"
              "Set preset iq"
                    ↓
              radiod creates channel:
              - Allocates multicast address ← radiod decides!
              - Assigns port
              - Starts streaming
                    ↓
              signal-recorder discovers:
              - Multicast address (e.g., 239.41.204.101)
              - Port (e.g., 5004)
```

### 3. Recording

```
radiod → RTP multicast → pcmrecord → WAV files
         239.x.x.x:5004    ↑
                           |
                    signal-recorder
                    (discovered address)
```

## Why This Design?

### Flexibility

radiod can change multicast addresses without breaking signal-recorder:
- Different radiod instances may use different addresses
- radiod can reassign addresses on restart
- No hardcoded assumptions

### Simplicity

User only specifies:
- **What** to record (frequencies)
- **Where** radiod is (status address)

Everything else is discovered automatically.

### Portability

Same signal-recorder config works with:
- Different radiod configurations
- Different hardware (RX888, Airspy, etc.)
- Different network setups

## Information Flow

```
User Config (config.toml)
    ↓
    What frequencies to record
    ↓
signal-recorder
    ↓
    Asks radiod: "What channels exist?"
    ↓
radiod
    ↓
    "Here are all channels with their multicast addresses"
    ↓
signal-recorder
    ↓
    "I need channel X, it doesn't exist"
    ↓
radiod
    ↓
    "OK, created channel X on multicast Y"
    ↓
signal-recorder
    ↓
    Starts pcmrecord on multicast Y
    ↓
Recording!
```

## Minimal radiod Configuration

For GRAPE recording, your radiod@.conf only needs:

```ini
[global]
hardware = rx888
status = bee1-hf-status.local
data = bee1-hf-data.local  
mode = iq
samprate = 16000
```

**No [channel] sections needed!** signal-recorder creates them dynamically.

## What radiod Decides

When creating a dynamic channel, radiod decides:

1. **Multicast address** - From available pool or configured default
2. **Port** - Typically 5004 for data
3. **SSRC** - Uses the one you specify
4. **RTP parameters** - Sequence numbers, timestamps, etc.

signal-recorder discovers all of this via the `control` utility.

## What You Decide

In signal-recorder config, you decide:

1. **SSRC** - Unique identifier (recommend: frequency in Hz)
2. **Frequency** - What to tune to
3. **Preset** - Demodulation mode (iq, usb, lsb, etc.)
4. **Sample rate** - Audio sample rate (optional, defaults from radiod)
5. **Description** - Human-readable name

## Example Workflow

### Initial Setup

1. Install and configure radiod with minimal config
2. Start radiod: `systemctl start radiod`
3. Create signal-recorder config with desired frequencies
4. Start signal-recorder: `signal-recorder daemon --config config.toml`

### What Happens

```
[radiod] Started, listening for commands on bee1-hf-status.local
[signal-recorder] Starting up
[signal-recorder] Discovering channels from bee1-hf-status.local
[signal-recorder] Found 0 channels
[signal-recorder] Need to create 9 channels
[signal-recorder] Creating SSRC 2500000 at 2.5 MHz
[radiod] Creating dynamic channel SSRC 2500000
[radiod] Assigned multicast 239.41.204.101:5004
[radiod] Tuning to 2.5 MHz, preset iq
[radiod] Streaming started
[signal-recorder] Channel verified: SSRC 2500000 at 239.41.204.101:5004
[signal-recorder] Creating SSRC 5000000 at 5.0 MHz
[radiod] Creating dynamic channel SSRC 5000000
[radiod] Assigned multicast 239.41.204.101:5004 (same group)
...
[signal-recorder] All 9 channels created and verified
[signal-recorder] Starting pcmrecord on 239.41.204.101
[pcmrecord] Recording 9 SSRCs to /mnt/grape-data
[signal-recorder] Recording started successfully
```

### Adding a New Frequency

1. Edit signal-recorder config, add new channel
2. Restart signal-recorder
3. It discovers the new channel is missing
4. Creates it via radiod
5. Starts recording

**No radiod restart needed!**

## Troubleshooting

### "Failed to create channel"

**Check radiod is running:**
```bash
systemctl status radiod
```

**Check radiod has dynamic channel support:**
```bash
# radiod@.conf should have:
[global]
data = ...
mode = ...
```

**Check you can run control:**
```bash
control bee1-hf-status.local
```

### "Multicast address not found"

This shouldn't happen with automatic discovery, but if it does:

**Check radiod is assigning addresses:**
```bash
control bee1-hf-status.local
# Look for "output channel" column
```

**Check network can receive multicast:**
```bash
sudo tcpdump -i any host 239.41.204.101
```

## Advanced: Multiple Data Streams

If your radiod is configured to use multiple multicast groups (e.g., for different bands), signal-recorder automatically handles this:

```
radiod assigns:
- 2.5 MHz → 239.41.204.101:5004
- 5.0 MHz → 239.41.204.101:5004
- 10.0 MHz → 239.41.204.101:5004
- 50.0 MHz → 239.41.204.102:5004 (different group)

signal-recorder discovers and records from both groups automatically.
```

## Summary

**You don't need to know or configure:**
- Multicast IP addresses
- Ports
- RTP parameters
- Network routing

**You only need to specify:**
- What frequencies to record
- Where radiod's status is

**radiod handles:**
- Hardware
- Streaming
- Multicast assignment

**signal-recorder handles:**
- Discovery
- Recording
- Processing
- Upload

This clean separation makes the system flexible, portable, and easy to configure!


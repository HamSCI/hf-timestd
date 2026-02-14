# Automatic Channel Creation

One of the key features of signal-recorder is **automatic channel creation** - you don't need to manually edit `radiod@.conf` files. The application can create and configure ka9q-radio channels on-demand based on your configuration file.

## How It Works

### Traditional Approach (Manual)

In the traditional workflow:
1. Edit `/etc/radio/radiod@yourdevice.conf`
2. Add channel definitions for each frequency
3. Restart radiod
4. Configure your recording application to use those channels

**Problems:**
- Two places to configure (radiod config + recorder config)
- Requires radiod restart to add channels
- Easy to get out of sync
- Hard to manage many channels

### Signal-Recorder Approach (Automatic)

With signal-recorder:
1. Define channels once in `config.toml`
2. Run signal-recorder
3. It automatically creates missing channels via ka9q-radio's control protocol

**Benefits:**
- Single source of truth (one config file)
- No radiod restart needed
- Dynamic channel management
- Portable configuration

## Configuration

### Enable Auto-Creation

In your config file:

```toml
[ka9q]
status_address = "bee1-hf-status.local"
auto_create_channels = true  # Enable automatic creation
data_address = "239.41.204.101"  # Multicast address for new channels
data_port = 5004
```

### Define Channels

Each channel needs:
- `ssrc` - Unique identifier (can match frequency)
- `frequency_hz` - Frequency in Hertz
- `preset` - Demodulation mode (iq, usb, lsb, am, fm, etc.)
- `sample_rate` - Sample rate in Hz
- `description` - Human-readable name

```toml
[[recorder.channels]]
ssrc = 10000000
frequency_hz = 10000000
preset = "iq"
sample_rate = 16000
description = "WWV 10 MHz"
processor = "grape"
enabled = true
```

## Startup Process

When signal-recorder starts:

1. **Discovery** - Queries ka9q-radio via `control` utility
   ```
   Found 46 existing channels
   ```

2. **Comparison** - Checks which configured channels exist
   ```
   Required channels: [2500000, 5000000, 10000000, ...]
   Existing channels: [60000, 136000, 474200, ...]
   Missing channels: [2500000, 5000000, 10000000, ...]
   ```

3. **Creation** - Creates missing channels
   ```
   Creating channel: SSRC=2500000, freq=2.500 MHz, preset=iq
   Creating channel: SSRC=5000000, freq=5.000 MHz, preset=iq
   ...
   ```

4. **Verification** - Confirms all channels exist
   ```
   Channel verified: SSRC=2500000, freq=2.500 MHz
   Channel verified: SSRC=5000000, freq=5.000 MHz
   ...
   ```

5. **Recording** - Starts recording from all channels

## Technical Details

### ka9q-radio Dynamic Channels

ka9q-radio supports dynamic channel creation when:
- `data` and `mode` settings are present in `[global]` section of radiod config
- The `control` utility can create channels at runtime

### Control Protocol

The `control` utility communicates with radiod via:
- **Status multicast channel** - Receives channel information
- **Command packets** - Sends configuration commands

Signal-recorder uses the same protocol to:
- List existing channels
- Create new channels
- Configure channel parameters

### Channel Creation Commands

To create a channel, signal-recorder:
1. Sends SSRC selection command
2. Sets frequency (`f` command)
3. Sets preset/mode (`m` command)
4. Sets sample rate if needed (`S` command)

This is equivalent to manually running:
```bash
control bee1-hf-status.local
# Type: 10000000 (SSRC)
# Type: f10000000 (frequency)
# Type: miq (preset)
# Type: q (quit)
```

## SSRC Selection

### Option 1: SSRC = Frequency (Recommended)

Use the frequency in Hz as the SSRC:
```toml
ssrc = 10000000  # 10 MHz
frequency_hz = 10000000
```

**Advantages:**
- Easy to remember
- Self-documenting
- Matches your setup's pattern

### Option 2: Arbitrary SSRC

Use any unique 32-bit number:
```toml
ssrc = 12345
frequency_hz = 10000000
```

**Advantages:**
- More flexibility
- Can have multiple channels on same frequency

## Presets

Common presets for GRAPE:

| Preset | Description | Use Case |
|--------|-------------|----------|
| `iq` | Complex I/Q sampling | WWV/CHU time signals (recommended) |
| `usb` | Upper sideband | Voice, digital modes |
| `lsb` | Lower sideband | Voice, digital modes |
| `am` | Amplitude modulation | AM broadcast |
| `fm` | Frequency modulation | FM broadcast, repeaters |

For GRAPE, always use `iq` preset:
- Captures full bandwidth around center frequency
- Preserves phase information
- Required for ionospheric analysis

## Sample Rates

Typical sample rates:
- **12000 Hz** - Voice modes (USB/LSB/AM)
- **16000 Hz** - IQ modes (recommended for GRAPE)
- **24000 Hz** - Wideband
- **48000 Hz** - Very wideband

For GRAPE, use **16000 Hz**:
- Adequate for WWV/CHU bandwidth
- Matches wsprdaemon's approach
- Good balance of quality vs. disk space

## Troubleshooting

### Channels Not Created

**Symptom:** Signal-recorder reports "Failed to create channel"

**Possible Causes:**
1. radiod not running
   ```bash
   systemctl status radiod
   ```

2. radiod not configured for dynamic channels
   - Check `/etc/radio/radiod@*.conf` has `data` and `mode` in `[global]`

3. `control` utility not found
   ```bash
   which control
   # Should show: /usr/local/bin/control
   ```

4. Wrong status address
   ```bash
   signal-recorder discover --radiod your-status-address.local
   ```

### Channels Created But Not Recording

**Symptom:** Channels exist but no files are created

**Possible Causes:**
1. Wrong multicast address
   - Check `data_address` matches radiod configuration

2. Multicast routing issue
   ```bash
   ip mroute show
   ```

3. pcmrecord not installed
   ```bash
   which pcmrecord
   ```

### Channels Disappear After radiod Restart

**Symptom:** Channels exist, then disappear

**Explanation:** Dynamic channels are not persistent - they're lost when radiod restarts.

**Solution:** Signal-recorder automatically recreates channels on startup. If radiod restarts while signal-recorder is running, restart signal-recorder too.

**Future Enhancement:** Add automatic detection of radiod restart and re-creation of channels.

## Manual Override

If you prefer manual channel management:

```toml
[ka9q]
status_address = "bee1-hf-status.local"
auto_create_channels = false  # Disable automatic creation
```

Then create channels manually:
1. Add to `/etc/radio/radiod@*.conf`
2. Restart radiod
3. Signal-recorder will use existing channels

## Best Practices

### 1. Use Descriptive Names

```toml
description = "WWV 10 MHz"  # Good
description = "Channel 5"   # Bad
```

### 2. Match SSRC to Frequency

```toml
ssrc = 10000000
frequency_hz = 10000000
```

### 3. Group Related Channels

```toml
# WWV channels
[[recorder.channels]]
ssrc = 2500000
...

[[recorder.channels]]
ssrc = 5000000
...

# CHU channels
[[recorder.channels]]
ssrc = 3330000
...
```

### 4. Disable Unused Channels

```toml
[[recorder.channels]]
ssrc = 25000000
frequency_hz = 25000000
enabled = false  # Temporarily disable
```

### 5. Document Your Configuration

```toml
# GRAPE WWV/CHU channels for ionospheric research
# All channels use IQ mode at 16 kHz for full bandwidth capture
[[recorder.channels]]
ssrc = 10000000
...
```

## Example: Adding a New Frequency

To add WWV 30 MHz (if it existed):

1. Add to config:
```toml
[[recorder.channels]]
ssrc = 30000000
frequency_hz = 30000000
preset = "iq"
sample_rate = 16000
description = "WWV 30 MHz"
processor = "grape"
enabled = true
```

2. Restart signal-recorder:
```bash
systemctl restart signal-recorder
```

3. Verify:
```bash
signal-recorder discover --radiod bee1-hf-status.local | grep 30000000
```

That's it! No radiod configuration needed.

## Comparison with wsprdaemon

| Feature | wsprdaemon | signal-recorder |
|---------|------------|-----------------|
| Channel definition | radiod@.conf | config.toml |
| Channel creation | Manual | Automatic |
| Configuration files | 2 (radiod + WD) | 1 (config.toml) |
| Restart required | Yes (radiod) | No |
| Dynamic changes | No | Yes |

## Future Enhancements

Planned improvements:
- **Auto-detection of radiod restart** - Recreate channels automatically
- **Channel templates** - Define channel groups (e.g., "all WWV frequencies")
- **Frequency ranges** - Specify "2-30 MHz every 5 MHz"
- **Conditional channels** - Create channels based on time/date
- **Channel health monitoring** - Detect and recreate failed channels

## Summary

Automatic channel creation makes signal-recorder much easier to use:
- **One configuration file** - Single source of truth
- **No radiod editing** - Works with default radiod setup
- **Dynamic management** - Add/remove channels without restart
- **Portable** - Same config works on any ka9q-radio system

This is a key advantage over traditional approaches and makes GRAPE recording much more accessible.


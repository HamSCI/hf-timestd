# Testing Channel Creation

This document explains how to test the dynamic channel creation feature of signal-recorder.

## Prerequisites

1. **radiod must be running** on your system
2. You must know the status multicast address (e.g., `239.251.200.193` or `bee1-hf-status.local`)
3. The radiod configuration must have `data=` set to enable dynamic channel creation

## Verifying radiod is Running

```bash
# Check if radiod is running
ps aux | grep radiod

# Check radiod status (if running as systemd service)
sudo systemctl status radiod@*
```

## Test Configuration

Create a test configuration file (e.g., `config/test-channel-creation.toml`):

```toml
[station]
callsign = "YOUR_CALLSIGN"
grid_square = "YOUR_GRID"

[ka9q]
# Use either IP address or hostname
status_address = "239.251.200.193"  # or "bee1-hf-status.local"
auto_create_channels = true

[recorder]
data_dir = "/tmp/test-recording"

# Test channel - choose a frequency that doesn't currently exist
[[recorder.channels]]
ssrc = 30000000
frequency_hz = 30000000
preset = "iq"
sample_rate = 16000
description = "Test channel 30 MHz"
enabled = true
```

## Running the Test

```bash
# Activate virtual environment
source venv/bin/activate

# Run channel creation
signal-recorder create-channels --config config/test-channel-creation.toml
```

## Verifying Success

### Method 1: Using the control utility

If you have ka9q-radio's `control` utility installed:

```bash
control -v 239.251.200.193
```

Look for your SSRC (30000000 in the example) in the output.

### Method 2: Using tcpdump

Monitor the multicast traffic to see if radiod responds:

```bash
# In one terminal, start monitoring
sudo tcpdump -i lo -n -v 'host 239.251.200.193'

# In another terminal, run the channel creation
signal-recorder create-channels --config config/test-channel-creation.toml
```

You should see:
1. Outgoing UDP packets from your machine to 239.251.200.193:5006 (control commands)
2. Incoming UDP packets from radiod with status updates

### Method 3: Check radiod logs

```bash
# If radiod is running as systemd service
sudo journalctl -u radiod@* -f

# Look for messages about new channels or SSRC creation
```

## Troubleshooting

### "radiod is not running"

**Problem**: No radiod process found on the system.

**Solution**: Start radiod:
```bash
sudo systemctl start radiod@your-config-name
```

### "Packets sent but channel not created"

**Problem**: Packets are being sent (visible in tcpdump) but radiod doesn't create the channel.

**Possible causes**:
1. **radiod config missing `data=` parameter**: Dynamic channel creation requires the `data=` option in radiod.conf
2. **Wrong multicast address**: Verify you're using the correct status address from radiod.conf
3. **Firewall blocking multicast**: Check iptables/firewall rules

**Solution**: Check your radiod configuration file (usually in `/etc/radio/`):
```ini
[global]
data = 239.251.200.0/24  # Required for dynamic channel creation
status = 239.251.200.193+5006
```

### "avahi-resolve timeout"

**Problem**: Using `.local` hostname but avahi-resolve times out.

**Solution**: Use the IP address directly instead:
```toml
[ka9q]
status_address = "239.251.200.193"  # Use IP instead of hostname
```

### "Channel verification failed"

**Problem**: Channel appears to be created but verification fails.

**Possible causes**:
1. **control utility not installed**: The verification uses ka9q-radio's `control` utility
2. **Timing issue**: Channel takes time to appear in status

**Solution**: 
- Install ka9q-radio tools
- Wait a few seconds and manually verify with `control -v <status_address>`

## Understanding the Output

### Successful creation:
```
2025-10-24 16:35:08 [INFO] Using direct IP address: 239.251.200.193
2025-10-24 16:35:08 [INFO] Connected to radiod at 239.251.200.193:5006 via loopback
2025-10-24 16:35:08 [INFO] Creating channel: SSRC=30000000, freq=30.000 MHz, preset=iq
2025-10-24 16:35:08 [INFO] Setting frequency for SSRC 30000000 to 30.000 MHz
2025-10-24 16:35:08 [INFO] Setting preset for SSRC 30000000 to iq
2025-10-24 16:35:08 [INFO] Setting sample rate for SSRC 30000000 to 16000 Hz
2025-10-24 16:35:08 [INFO] Channel 30000000 created and configured
2025-10-24 16:35:09 [INFO] ✓ Channel 30000000 verified
```

### Failed creation:
```
2025-10-24 16:35:08 [INFO] Creating channel: SSRC=30000000, freq=30.000 MHz, preset=iq
2025-10-24 16:35:08 [INFO] Channel 30000000 created and configured
2025-10-24 16:35:09 [WARNING] Channel 30000000 not found
2025-10-24 16:35:09 [WARNING] ✗ Channel 30000000 verification failed
```

If you see the "verification failed" message, use the manual verification methods above to check if the channel was actually created.

## Network Packet Analysis

To understand what's happening at the network level:

```bash
# Capture packets to a file
sudo tcpdump -i lo -n -w /tmp/radiod-control.pcap 'host 239.251.200.193'

# In another terminal, run channel creation
signal-recorder create-channels --config config/test-channel-creation.toml

# Stop tcpdump (Ctrl+C) and analyze
sudo tcpdump -r /tmp/radiod-control.pcap -n -v
```

You should see:
- **24-byte packet**: Initial command with frequency
- **18-byte packet**: Preset command
- **18-byte packet**: Sample rate command

All packets should be sent from 127.0.0.1 to 239.251.200.193:5006.

## Next Steps

Once channel creation is working:
1. Test recording from the created channel
2. Test the full GRAPE processing pipeline
3. Set up automated channel creation in daemon mode


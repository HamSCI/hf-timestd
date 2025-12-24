# u-blox ZED-F9P Configuration for TEC/Ionospheric Delay

## Overview

The **ZED-F9P** is a dual-frequency (L1/L5) GNSS receiver that can measure ionospheric delay, which is directly related to TEC.

**Key capability**: The ZED-F9P outputs **ionospheric delay** in the **UBX-NAV-SAT** message, which we can convert to TEC.

---

## What Data Contains TEC Information

### UBX-NAV-SAT Message

**Message**: `UBX-NAV-SAT` (Class 0x01, ID 0x35)

**Contains per-satellite**:

- `ionoDelay` - Ionospheric delay in meters (this is what we need!)
- `prRes` - Pseudorange residual
- `elev` - Elevation angle
- `azim` - Azimuth angle
- `prn` - Satellite PRN/ID

**Ionospheric delay → TEC conversion**:

```
TEC (TECU) = ionoDelay (meters) × f² / 40.3
```

Where f = GPS L1 frequency (1575.42 MHz)

---

## Configuration Steps

### Option 1: Using u-center (GUI)

1. **Connect ZED-F9P** to computer via USB
2. **Open u-center** software (free from u-blox)
3. **Enable UBX-NAV-SAT**:
   - View → Messages View
   - Navigate to UBX → NAV → SAT
   - Right-click → Enable Message
4. **Configure output rate**:
   - View → Configuration View
   - MSG (Messages)
   - Set UBX-NAV-SAT rate to 1 (every measurement epoch)
5. **Save configuration**:
   - CFG (Configuration) → Send
   - CFG → Save current configuration

### Option 2: Using UBX Protocol Commands (Programmatic)

**Enable UBX-NAV-SAT on UART1**:

```
UBX-CFG-MSG:
  Class: 0x06, ID: 0x01
  Payload:
    msgClass: 0x01 (NAV)
    msgID: 0x35 (SAT)
    rate[0]: 1  (UART1)
    rate[1]: 0  (UART2)
    rate[2]: 0  (USB)
    rate[3]: 0  (SPI)
```

**Hex command**:

```
B5 62 06 01 08 00 01 35 01 00 00 00 00 00 41 F1
```

Send this via your TCP connection or serial port.

### Option 3: Using pyubx2 Library (Python)

```python
from pyubx2 import UBXMessage

# Create CFG-MSG command to enable NAV-SAT
msg = UBXMessage('CFG', 'CFG-MSG', SET,
    msgClass=0x01,  # NAV
    msgID=0x35,     # SAT
    rateDDC=0,
    rateUART1=1,    # Enable on UART1
    rateUART2=0,
    rateUSB=0,
    rateSPI=0
)

# Send to receiver
serial_port.write(msg.serialize())
```

---

## Parsing UBX-NAV-SAT for TEC

### Message Structure

**UBX-NAV-SAT** (variable length):

- Header: 0xB5 0x62
- Class: 0x01
- ID: 0x35
- Length: variable (12 + 12*numSvs bytes)
- Payload:
  - iTOW (4 bytes) - GPS time of week
  - version (1 byte)
  - numSvs (1 byte) - Number of satellites
  - reserved (2 bytes)
  - For each satellite (12 bytes):
    - gnssId (1 byte) - GNSS type (0=GPS, 2=Galileo, etc.)
    - svId (1 byte) - Satellite ID
    - cno (1 byte) - Signal strength (dB-Hz)
    - elev (1 byte) - Elevation (degrees)
    - azim (2 bytes) - Azimuth (degrees)
    - prRes (2 bytes) - Pseudorange residual (0.1 m)
    - flags (4 bytes) - Status flags
    - **ionoDelay (2 bytes) - Ionospheric delay (0.01 m)** ← THIS!
- Checksum (2 bytes)

### Extract Ionospheric Delay

**Per satellite**:

```python
ionoDelay_meters = ionoDelay_raw * 0.01  # Convert from 0.01m units
```

**Convert to TEC**:

```python
# GPS L1 frequency
f_L1_Hz = 1575.42e6

# Convert ionospheric delay to TEC
TEC_tecu = (ionoDelay_meters * f_L1_Hz**2) / 40.3e16
```

**Average across satellites** (elevation > 30°):

```python
tec_values = []
for sat in satellites:
    if sat.elev > 30:  # Avoid low-elevation multipath
        tec = (sat.ionoDelay * 0.01 * f_L1_Hz**2) / 40.3e16
        tec_values.append(tec)

vtec_avg = sum(tec_values) / len(tec_values)
```

---

## Example: Current NMEA Stream

Your current stream shows **NMEA only**. To get UBX messages:

**Check timeserver configuration**:

- Is it outputting UBX protocol?
- Or only NMEA?

**If only NMEA**:

- Configure ZED-F9P to output UBX on same port
- Or use separate port (ZED-F9P has UART1 and UART2)

**Recommended**: Output both NMEA + UBX on same port

- NMEA for position/time (easy to parse)
- UBX for ionospheric delay (TEC data)

---

## Configuration File (u-center)

Save this as `zed-f9p-tec.txt` and load in u-center:

```
# Enable UBX-NAV-SAT
CFG-MSG - 01 35 - Rate: 1

# Set measurement rate to 1 Hz
CFG-RATE - measRate: 1000ms, navRate: 1, timeRef: GPS

# Enable both NMEA and UBX on UART1
CFG-PRT - PortID: UART1, Protocol: UBX+NMEA
```

---

## Quick Test

**Check if UBX is already enabled**:

```bash
# Look for UBX binary frames (start with 0xB5 0x62)
timeout 5 nc 192.168.0.202 2000 | xxd | grep "b562"
```

**If you see `b562`**: UBX is enabled! Just need to parse it.

**If not**: Need to configure ZED-F9P to output UBX.

---

## Next Steps

1. **Check current output**: Is UBX already in the stream?
2. **Configure if needed**: Enable UBX-NAV-SAT
3. **Parse UBX**: Extract ionospheric delay
4. **Convert to TEC**: Use formula above
5. **Integrate**: Feed into validation script

I'll create a UBX parser next!

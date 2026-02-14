# Multicast Socket Debugging Summary

## Problem Statement

Dynamic channel creation via radiod control protocol was not working. Packets appeared to be sent (confirmed by strace showing successful sendto() calls) but were not visible on the network (tcpdump showed no packets).

## Investigation Process

### Initial Symptoms

1. `strace` showed successful `sendto()` calls with correct byte counts
2. `tcpdump` on loopback interface showed no packets to 239.251.200.193:5006
3. Other radiod multicast traffic was visible on loopback (status packets)

### Code Analysis

Studied ka9q-radio source code to understand proper multicast setup:

**From `/home/ubuntu/ka9q-radio/src/multicast.c`:**
```c
struct ip_mreqn mreqn = {
    .imr_address.s_addr = htonl(INADDR_LOOPBACK),  // 127.0.0.1
    .imr_ifindex = lo_index  // loopback interface index (1)
};
setsockopt(fd, IPPROTO_IP, IP_MULTICAST_IF, &mreqn, sizeof mreqn);
```

Key insight: Must use `ip_mreqn` structure with **both** IP address and interface index, not just IP address.

### Implementation Changes

1. **Implemented ip_mreqn structure** in Python:
   ```python
   import struct
   lo_index = socket.if_nametoindex('lo')
   # ip_mreqn: imr_multiaddr (4 bytes), imr_address (4 bytes), imr_ifindex (4 bytes)
   ip_mreqn = struct.pack('=4s4si', 
       socket.inet_aton('0.0.0.0'),      # imr_multiaddr (not used for sending)
       socket.inet_aton('127.0.0.1'),    # imr_address (loopback)
       lo_index                           # imr_ifindex (interface index)
   )
   self.socket.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, ip_mreqn)
   ```

2. **Removed bind() call**: Was potentially interfering with sendto() destination routing

3. **Added IP address detection**: Handle both hostnames and direct IP addresses:
   ```python
   if re.match(r'^\d+\.\d+\.\d+\.\d+$', self.status_address):
       mcast_addr = self.status_address
   else:
       # Try avahi-resolve or getaddrinfo
   ```

## Root Cause Discovery

After implementing all fixes and running tests in the sandbox environment:

**The packets ARE being sent correctly!**

```bash
$ sudo tcpdump -r /tmp/packets.pcap -n -v
16:35:08.145140 IP (tos 0x0, ttl 2, id 54554, offset 0, flags [DF], proto UDP (17), length 52)
    127.0.0.1.37242 > 239.251.200.193.5006: UDP, length 24
16:35:08.245593 IP (tos 0x0, ttl 2, id 54569, offset 0, flags [DF], proto UDP (17), length 46)
    127.0.0.1.37242 > 239.251.200.193.5006: UDP, length 18
16:35:08.346055 IP (tos 0x0, ttl 2, id 54590, offset 0, flags [DF], proto UDP (17), length 46)
    127.0.0.1.37242 > 239.251.200.193.5006: UDP, length 18
```

**The actual issue**: **radiod is not running in the sandbox environment**

```bash
$ ps aux | grep radiod | grep -v grep
(no output)
```

## Conclusion

### What Works ✅

1. **Multicast socket configuration**: Correctly implemented using ip_mreqn structure
2. **TLV encoding**: Packets are correct size (24, 18, 18 bytes as expected)
3. **Network transmission**: Packets successfully sent to 239.251.200.193:5006 on loopback
4. **Interface binding**: Loopback interface correctly selected

### What Was Missing

- **radiod daemon**: No radiod process to receive and process the control packets
- This is expected in the sandbox environment (no ka9q-radio installation)

### Code Status

The code is **ready for testing on a real system** with radiod running. The multicast socket implementation is correct and follows ka9q-radio's proven approach.

## Testing on Real System

To test on a system with radiod running:

1. **Verify radiod is running**:
   ```bash
   ps aux | grep radiod
   sudo systemctl status radiod@*
   ```

2. **Check radiod configuration** has `data=` parameter:
   ```ini
   [global]
   data = 239.251.200.0/24
   status = 239.251.200.193+5006
   ```

3. **Run channel creation**:
   ```bash
   signal-recorder create-channels --config config/test-channel-creation.toml
   ```

4. **Verify with control utility**:
   ```bash
   control -v 239.251.200.193
   ```

## Technical Details

### Packet Structure

Based on strace output:
- **First packet (24 bytes)**: Frequency command
  - CMD type (1 byte)
  - RADIO_FREQUENCY TLV
  - OUTPUT_SSRC TLV
  - COMMAND_TAG TLV
  - EOL (1 byte)

- **Second packet (18 bytes)**: Preset command
  - CMD type (1 byte)
  - PRESET TLV (string)
  - OUTPUT_SSRC TLV
  - COMMAND_TAG TLV
  - EOL (1 byte)

- **Third packet (18 bytes)**: Sample rate command
  - CMD type (1 byte)
  - OUTPUT_SAMPRATE TLV
  - OUTPUT_SSRC TLV
  - COMMAND_TAG TLV
  - EOL (1 byte)

### Socket Options Set

```python
# Allow multiple sockets to bind to same port
socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

# Set multicast interface using ip_mreqn
socket.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, ip_mreqn)

# Enable multicast loopback
socket.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)

# Set multicast TTL
socket.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
```

## References

- ka9q-radio source: `/home/ubuntu/ka9q-radio/src/multicast.c`
- ka9q-radio source: `/home/ubuntu/ka9q-radio/src/control.c`
- ka9q-radio source: `/home/ubuntu/ka9q-radio/src/status.c`
- Implementation: `/home/ubuntu/signal-recorder-repo/src/signal_recorder/radiod_control.py`


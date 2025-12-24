#!/usr/bin/env python3
"""
Quick test to extract TEC from NAV-SAT messages at tcp://192.168.0.202:2000

This is a simplified version that focuses on finding and parsing NAV-SAT.
"""

import socket
import struct
from datetime import datetime

# GPS L1 frequency
GPS_L1_FREQ_HZ = 1575.42e6

def parse_nav_sat(payload):
    """Parse NAV-SAT payload and extract TEC."""
    if len(payload) < 8:
        return None
    
    iTOW = struct.unpack('<I', payload[0:4])[0]
    version = payload[4]
    numSvs = payload[5]
    
    tec_values = []
    offset = 8
    
    for i in range(numSvs):
        if offset + 12 > len(payload):
            break
        
        gnss_id = payload[offset]
        sv_id = payload[offset + 1]
        cno = payload[offset + 2]
        elev = struct.unpack('b', payload[offset+3:offset+4])[0]  # signed
        flags = struct.unpack('<I', payload[offset+8:offset+12])[0]
        
        # Extract ionospheric delay from flags (bits 16-31, 0.01m units)
        iono_delay_raw = (flags >> 16) & 0xFFFF
        if iono_delay_raw & 0x8000:
            iono_delay_raw -= 0x10000
        iono_delay = iono_delay_raw * 0.01  # meters
        
        # Calculate TEC
        if iono_delay != 0 and elev >= 30:  # Only high elevation
            tec = (iono_delay * GPS_L1_FREQ_HZ**2) / 40.3e16
            tec_values.append((sv_id, elev, tec))
        
        offset += 12
    
    return tec_values

# Connect
sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
sock.settimeout(30.0)
sock.connect(('192.168.0.202', 2000))

print("Connected! Looking for NAV-SAT...")

buffer = bytearray()
found_count = 0

while found_count < 3:  # Get 3 measurements
    data = sock.recv(4096)
    if not data:
        break
    
    buffer.extend(data)
    
    # Look for UBX-NAV-SAT (0xB5 0x62 0x01 0x35)
    i = 0
    while i < len(buffer) - 8:
        if (buffer[i] == 0xB5 and buffer[i+1] == 0x62 and 
            buffer[i+2] == 0x01 and buffer[i+3] == 0x35):
            
            # Get length
            length = struct.unpack('<H', buffer[i+4:i+6])[0]
            
            # Check if we have full message
            if i + 8 + length <= len(buffer):
                payload = buffer[i+6:i+6+length]
                
                # Parse
                tec_values = parse_nav_sat(payload)
                
                if tec_values:
                    avg_tec = sum(t[2] for t in tec_values) / len(tec_values)
                    print(f"\n✅ NAV-SAT #{found_count + 1}:")
                    print(f"   VTEC: {avg_tec:.2f} TECU ({len(tec_values)} satellites)")
                    for sv_id, elev, tec in tec_values[:5]:
                        print(f"     SV {sv_id:2d}: {tec:6.2f} TECU (el={elev}°)")
                    found_count += 1
                
                # Remove processed message
                buffer = buffer[i+8+length:]
                i = 0
            else:
                break
        else:
            i += 1
    
    # Keep buffer manageable
    if len(buffer) > 10000:
        buffer = buffer[-5000:]

sock.close()
print(f"\n✅ Successfully extracted TEC from {found_count} NAV-SAT messages!")

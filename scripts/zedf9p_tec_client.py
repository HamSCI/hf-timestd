#!/usr/bin/env python3
"""
u-blox UBX Protocol Parser for ZED-F9P TEC Extraction

Parses UBX-NAV-SAT messages to extract ionospheric delay and convert to TEC.

UBX Protocol:
- Binary format (not ASCII like NMEA)
- Frame: 0xB5 0x62 <class> <id> <length> <payload> <checksum>
- UBX-NAV-SAT: Class 0x01, ID 0x35

Ionospheric Delay → TEC Conversion:
    TEC (TECU) = ionoDelay (m) × f² / 40.3e16
    where f = GPS L1 frequency (1575.42 MHz)
"""

import struct
import socket
from typing import List, Optional, Dict
from dataclasses import dataclass
from datetime import datetime

# GPS L1 frequency for TEC calculation
GPS_L1_FREQ_HZ = 1575.42e6


@dataclass
class SatelliteData:
    """Data for one satellite from UBX-NAV-SAT."""
    gnss_id: int  # 0=GPS, 1=SBAS, 2=Galileo, 3=BeiDou, 5=QZSS, 6=GLONASS
    sv_id: int    # Satellite ID/PRN
    cno: int      # Signal strength (dB-Hz)
    elev: int     # Elevation (degrees, -90 to 90)
    azim: int     # Azimuth (degrees, 0 to 360)
    pr_res: float # Pseudorange residual (meters)
    iono_delay: float  # Ionospheric delay (meters)
    tec: float    # Calculated TEC (TECU)


@dataclass
class TECMeasurement:
    """Aggregated TEC measurement."""
    timestamp: datetime
    vtec_tecu: float
    num_satellites: int
    satellites: List[SatelliteData]


class UBXParser:
    """Parse u-blox UBX binary protocol."""
    
    # UBX frame markers
    SYNC_CHAR_1 = 0xB5
    SYNC_CHAR_2 = 0x62
    
    # Message classes
    CLASS_NAV = 0x01
    
    # Message IDs
    MSG_NAV_SAT = 0x35
    
    def __init__(self):
        self.buffer = bytearray()
    
    def add_data(self, data: bytes):
        """Add data to parse buffer."""
        self.buffer.extend(data)
    
    def parse_next_message(self) -> Optional[Dict]:
        """
        Parse next UBX message from buffer.
        
        Returns:
            Dict with message data, or None if no complete message
        """
        # Look for sync characters
        while len(self.buffer) >= 8:  # Minimum UBX frame size
            # Find sync pattern
            if self.buffer[0] != self.SYNC_CHAR_1 or self.buffer[1] != self.SYNC_CHAR_2:
                # Not a UBX frame, skip byte
                self.buffer.pop(0)
                continue
            
            # Read header
            msg_class = self.buffer[2]
            msg_id = self.buffer[3]
            length = struct.unpack('<H', self.buffer[4:6])[0]
            
            # Check if we have complete message
            total_length = 6 + length + 2  # header + payload + checksum
            if len(self.buffer) < total_length:
                # Wait for more data
                return None
            
            # Extract payload and checksum
            payload = self.buffer[6:6+length]
            checksum = self.buffer[6+length:6+length+2]
            
            # Verify checksum
            calc_ck_a, calc_ck_b = self._calculate_checksum(
                self.buffer[2:6+length]
            )
            
            if checksum[0] != calc_ck_a or checksum[1] != calc_ck_b:
                # Bad checksum, skip this frame
                self.buffer.pop(0)
                continue
            
            # Remove processed message from buffer
            self.buffer = self.buffer[total_length:]
            
            # Parse message
            if msg_class == self.CLASS_NAV and msg_id == self.MSG_NAV_SAT:
                return self._parse_nav_sat(payload)
            else:
                # Unknown message, skip
                continue
        
        return None
    
    def _calculate_checksum(self, data: bytes) -> tuple:
        """Calculate UBX checksum (Fletcher algorithm)."""
        ck_a = 0
        ck_b = 0
        for byte in data:
            ck_a = (ck_a + byte) & 0xFF
            ck_b = (ck_b + ck_a) & 0xFF
        return ck_a, ck_b
    
    def _parse_nav_sat(self, payload: bytes) -> Dict:
        """
        Parse UBX-NAV-SAT message.
        
        Payload structure:
        - iTOW (4 bytes) - GPS time of week (ms)
        - version (1 byte)
        - numSvs (1 byte) - Number of satellites
        - reserved (2 bytes)
        - Repeated for each satellite (12 bytes each):
          - gnssId (1 byte)
          - svId (1 byte)
          - cno (1 byte) - Signal strength
          - elev (1 byte) - Elevation (signed)
          - azim (2 bytes) - Azimuth
          - prRes (2 bytes) - Pseudorange residual (0.1 m)
          - flags (4 bytes)
        """
        if len(payload) < 8:
            return None
        
        # Parse header
        iTOW = struct.unpack('<I', payload[0:4])[0]
        version = payload[4]
        numSvs = payload[5]
        
        # Parse satellites
        satellites = []
        offset = 8
        
        for i in range(numSvs):
            if offset + 12 > len(payload):
                break
            
            gnss_id = payload[offset]
            sv_id = payload[offset + 1]
            cno = payload[offset + 2]
            elev = struct.unpack('b', payload[offset+3:offset+4])[0]  # signed
            azim = struct.unpack('<H', payload[offset+4:offset+6])[0]
            pr_res = struct.unpack('<h', payload[offset+6:offset+8])[0] * 0.1  # 0.1m units
            flags = struct.unpack('<I', payload[offset+8:offset+12])[0]
            
            # Extract ionospheric delay from flags
            # Bits 16-31 contain ionoDelay in 0.01m units (signed)
            iono_delay_raw = (flags >> 16) & 0xFFFF
            # Convert to signed 16-bit
            if iono_delay_raw & 0x8000:
                iono_delay_raw -= 0x10000
            iono_delay = iono_delay_raw * 0.01  # Convert to meters
            
            # Calculate TEC
            tec = (iono_delay * GPS_L1_FREQ_HZ**2) / 40.3e16
            
            satellites.append(SatelliteData(
                gnss_id=gnss_id,
                sv_id=sv_id,
                cno=cno,
                elev=elev,
                azim=azim,
                pr_res=pr_res,
                iono_delay=iono_delay,
                tec=tec
            ))
            
            offset += 12
        
        return {
            'iTOW': iTOW,
            'numSvs': numSvs,
            'satellites': satellites
        }


class ZEDF9PClient:
    """Client for ZED-F9P GPS receiver with TEC extraction."""
    
    def __init__(self, host: str = '192.168.0.202', port: int = 2000):
        self.host = host
        self.port = port
        self.socket = None
        self.ubx_parser = UBXParser()
    
    def connect(self):
        """Connect to GPS receiver."""
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.settimeout(10.0)
        self.socket.connect((self.host, self.port))
    
    def disconnect(self):
        """Disconnect from GPS receiver."""
        if self.socket:
            self.socket.close()
    
    def get_tec_measurement(self, min_elevation: int = 30) -> Optional[TECMeasurement]:
        """
        Get TEC measurement from UBX-NAV-SAT.
        
        Args:
            min_elevation: Minimum satellite elevation to include (degrees)
        
        Returns:
            TECMeasurement with averaged VTEC, or None if no data
        """
        # Read data until we get a NAV-SAT message
        for _ in range(100):  # Try up to 100 reads
            try:
                data = self.socket.recv(1024)
                if not data:
                    break
                
                self.ubx_parser.add_data(data)
                
                # Try to parse message
                msg = self.ubx_parser.parse_next_message()
                if msg and 'satellites' in msg:
                    # Filter by elevation and calculate average TEC
                    valid_sats = [
                        sat for sat in msg['satellites']
                        if sat.elev >= min_elevation and sat.iono_delay != 0
                    ]
                    
                    if not valid_sats:
                        continue
                    
                    # Average TEC across satellites
                    avg_tec = sum(sat.tec for sat in valid_sats) / len(valid_sats)
                    
                    return TECMeasurement(
                        timestamp=datetime.utcnow(),
                        vtec_tecu=avg_tec,
                        num_satellites=len(valid_sats),
                        satellites=valid_sats
                    )
            
            except socket.timeout:
                break
            except Exception as e:
                print(f"Error reading UBX data: {e}")
                break
        
        return None


def get_vtec_from_zedf9p(
    host: str = '192.168.0.202',
    port: int = 2000,
    min_elevation: int = 30
) -> Optional[float]:
    """
    Get current VTEC from ZED-F9P receiver.
    
    Args:
        host: Receiver IP address
        port: Receiver TCP port
        min_elevation: Minimum satellite elevation (degrees)
    
    Returns:
        VTEC in TECU, or None if not available
    """
    client = ZEDF9PClient(host, port)
    
    try:
        client.connect()
        measurement = client.get_tec_measurement(min_elevation)
        
        if measurement:
            return measurement.vtec_tecu
        return None
    
    finally:
        client.disconnect()


if __name__ == '__main__':
    import sys
    
    print("ZED-F9P TEC Extraction Test")
    print("=" * 50)
    
    client = ZEDF9PClient()
    
    try:
        print(f"Connecting to {client.host}:{client.port}...")
        client.connect()
        print("Connected!")
        
        print("\nWaiting for UBX-NAV-SAT message...")
        measurement = client.get_tec_measurement(min_elevation=30)
        
        if measurement:
            print(f"\n✅ TEC Measurement:")
            print(f"  VTEC: {measurement.vtec_tecu:.2f} TECU")
            print(f"  Satellites: {measurement.num_satellites}")
            print(f"  Time: {measurement.timestamp}")
            
            print(f"\n  Per-satellite TEC:")
            for sat in measurement.satellites[:5]:  # Show first 5
                gnss_name = {0: 'GPS', 2: 'Galileo', 3: 'BeiDou', 6: 'GLONASS'}.get(sat.gnss_id, 'Unknown')
                print(f"    {gnss_name} {sat.sv_id:2d}: {sat.tec:6.2f} TECU "
                      f"(el={sat.elev:2d}°, SNR={sat.cno:2d} dB-Hz)")
        else:
            print("\n❌ No UBX-NAV-SAT data received")
            print("\nPossible reasons:")
            print("  1. UBX protocol not enabled (only NMEA)")
            print("  2. UBX-NAV-SAT message not configured")
            print("  3. Need to configure ZED-F9P")
            print("\nSee docs/ZED_F9P_TEC_CONFIGURATION.md for setup instructions")
    
    except Exception as e:
        print(f"\n❌ Error: {e}")
        sys.exit(1)
    
    finally:
        client.disconnect()

import struct
import logging

logger = logging.getLogger(__name__)

class UBXParser:
    """
    Minimal UBX Parser for ZED-F9P VTEC application.
    Parses:
      - UBX-RXM-RAWX (0x02 0x15): Multi-GNSS Raw Measurements
      - UBX-NAV-SAT  (0x01 0x35): Satellite Information (Elevation/Azimuth)
    """
    
    PREAMBLE = b'\xB5\x62'
    
    def __init__(self):
        self.buffer = bytearray()
        
    def process_data(self, data: bytes):
        """
        Ingest data and yield parsed messages.
        Yields: (msg_class, msg_id, payload_dict)
        """
        self.buffer.extend(data)
        
        while len(self.buffer) >= 6: # Header (2) + Class (1) + ID (1) + Len (2)
            # Check Preamble
            if self.buffer[0:2] != self.PREAMBLE:
                # Scan for preamble
                try:
                    idx = self.buffer.index(self.PREAMBLE, 1)
                    del self.buffer[:idx]
                except ValueError:
                    # No preamble found, discard all except last byte (might be start of preamble)
                    del self.buffer[:-1]
                continue
                
            # Parse Header
            msg_class = self.buffer[2]
            msg_id = self.buffer[3]
            length = struct.unpack('<H', self.buffer[4:6])[0]
            
            total_len = 6 + length + 2 # Header + Payload + Checksum
            
            if len(self.buffer) < total_len:
                # Wait for more data
                break
                
            payload = self.buffer[6:6+length]
            checksum = self.buffer[6+length:total_len]
            
            # Verify Checksum (optional for performance, but recommended)
            if self._calc_checksum(self.buffer[2:6+length]) == checksum:
                parsed = self._parse_payload(msg_class, msg_id, payload)
                if parsed:
                    yield (msg_class, msg_id, parsed)
            else:
                logger.warning(f"UBX Checksum failed for Class {msg_class:02x} ID {msg_id:02x}")
            
            # Consume message
            del self.buffer[:total_len]

    def _calc_checksum(self, content):
        ck_a = 0
        ck_b = 0
        for byte in content:
            ck_a = (ck_a + byte) & 0xFF
            ck_b = (ck_b + ck_a) & 0xFF
        return bytes([ck_a, ck_b])

    def _parse_payload(self, msg_class, msg_id, payload):
        if msg_class == 0x02 and msg_id == 0x15: # RXM-RAWX
            return self._parse_rxm_rawx(payload)
        elif msg_class == 0x01 and msg_id == 0x35: # NAV-SAT
            return self._parse_nav_sat(payload)
        return None

    def _parse_rxm_rawx(self, payload):
        """
        Parses UBX-RXM-RAWX.
        Returns: {
           'rcvTow': float,
           'week': int,
           'measurements': [
              {'gnssId': int, 'svId': int, 'freqId': int, 'prMes': float, 'cpMes': float, 'doMes': float, ...}, ...
           ]
        }
        """
        if len(payload) < 16: return None
        
        rcvTow, week, leapS, numMeas, recStat = struct.unpack('<dHbBB', payload[0:13])
        # reserved 3 bytes
        
        measurements = []
        block_size = 32
        offset = 16
        
        for i in range(numMeas):
            if offset + block_size > len(payload): break
            
            block = payload[offset : offset + block_size]
            # UBX-RXM-RAWX repeated block (32 bytes per measurement):
            # prMes(R8), cpMes(R8), doMes(R4), gnssId(U1), svId(U1), sigId(U1), freqId(U1), 
            # locktime(U2), cno(U1), prStdev(X1), cpStdev(X1), doStdev(X1), trkStat(X1), reserved(U1)
            # Total: 8+8+4+1+1+1+1+2+1+1+1+1+1+1 = 32 bytes
            prMes, cpMes, doMes, gnssId, svId, sigId, freqId, locktime, cno, prStdev, cpStdev, doStdev, trkStat, reserved = \
                struct.unpack('<ddfBBBBHBBBBBB', block)
                
            measurements.append({
                'gnssId': gnssId, # 0=GPS, 2=Galileo, 3=Beidou, 6=GLONASS
                'svId': svId,
                'sigId': sigId, # 0=L1C/A, 3=L2CL ... varies by GNSS
                'prMes': prMes,
                'cpMes': cpMes,
                'doMes': doMes,
                'cno': cno,
                'locktime': locktime,
                'trkStat': trkStat
            })
            offset += block_size
            
        return {
            'rcvTow': rcvTow,
            'week': week,
            'measurements': measurements
        }

    def _parse_nav_sat(self, payload):
        """
        Parses UBX-NAV-SAT.
        Returns: {
           'iTOW': int,
           'sats': [
              {'gnssId': int, 'svId': int, 'cno': int, 'elev': int, 'azim': int, 'prRes': int}, ...
           ]
        }
        """
        if len(payload) < 8: return None
        
        iTOW, version, numSvs = struct.unpack('<IBB', payload[0:6])
        # reserved 2 bytes
        
        sats = []
        block_size = 12
        offset = 8
        
        for i in range(numSvs):
            if offset + block_size > len(payload): break
            
            block = payload[offset : offset + block_size]
            # gnssId(1), svId(1), cno(1), elev(1), azim(2), prRes(2), flags(4)
            gnssId, svId, cno, elevk, azim, prRes, flags = \
                struct.unpack('<BBBbhhi', block)
                
            # elev is int8 in degrees
            # azim is int16 in degrees
            
            sats.append({
                'gnssId': gnssId,
                'svId': svId,
                'elev': elevk,
                'azim': azim,
                'prRes': prRes, # 0.1 m
                'flags': flags
            })
            offset += block_size
            
        return {
            'iTOW': iTOW,
            'sats': sats
        }

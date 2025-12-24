#!/usr/bin/env python3
"""
GPS Timeserver TCP Client - Parse NMEA stream for VTEC data

Connects to GPS timeserver at 192.168.0.202:2000 and extracts:
- Position (lat/lon) from $GNGGA
- Time from $GNRMC
- VTEC from proprietary sentences (if available)

For ScintPI/GPS receivers that output TEC, common formats include:
- Proprietary NMEA sentences ($PSTI, $PGRM, etc.)
- JSON over TCP
- Custom binary format

This parser handles standard NMEA and can be extended for custom formats.
"""

import socket
import re
import logging
from datetime import datetime
from typing import Optional, Dict
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class GPSPosition:
    """GPS position and time."""
    timestamp: datetime
    latitude: float
    longitude: float
    altitude: float
    fix_quality: int
    num_satellites: int


@dataclass
class VTECMeasurement:
    """VTEC measurement from GPS."""
    timestamp: datetime
    vtec_tecu: float
    latitude: float
    longitude: float
    num_satellites: int


class NMEAParser:
    """Parse NMEA 0183 sentences."""
    
    @staticmethod
    def parse_gga(sentence: str) -> Optional[GPSPosition]:
        """
        Parse $GNGGA sentence for position.
        
        Format: $GNGGA,hhmmss.ss,llll.ll,a,yyyyy.yy,a,x,xx,x.x,x.x,M,x.x,M,x.x,xxxx*hh
        """
        parts = sentence.split(',')
        if len(parts) < 15:
            return None
        
        try:
            # Time
            time_str = parts[1]
            hour = int(time_str[0:2])
            minute = int(time_str[2:4])
            second = float(time_str[4:])
            
            # Position
            lat_str = parts[2]
            lat_deg = int(lat_str[0:2])
            lat_min = float(lat_str[2:])
            latitude = lat_deg + lat_min / 60.0
            if parts[3] == 'S':
                latitude = -latitude
            
            lon_str = parts[4]
            lon_deg = int(lon_str[0:3])
            lon_min = float(lon_str[3:])
            longitude = lon_deg + lon_min / 60.0
            if parts[5] == 'W':
                longitude = -longitude
            
            # Quality
            fix_quality = int(parts[6])
            num_satellites = int(parts[7])
            altitude = float(parts[9])
            
            # Create timestamp (use today's date + time from GPS)
            now = datetime.utcnow()
            timestamp = datetime(now.year, now.month, now.day, hour, minute, int(second))
            
            return GPSPosition(
                timestamp=timestamp,
                latitude=latitude,
                longitude=longitude,
                altitude=altitude,
                fix_quality=fix_quality,
                num_satellites=num_satellites
            )
        
        except (ValueError, IndexError) as e:
            logger.debug(f"Failed to parse GGA: {e}")
            return None
    
    @staticmethod
    def parse_rmc(sentence: str) -> Optional[datetime]:
        """
        Parse $GNRMC sentence for time and date.
        
        Format: $GNRMC,hhmmss.ss,A,llll.ll,a,yyyyy.yy,a,x.x,x.x,ddmmyy,x.x,a*hh
        """
        parts = sentence.split(',')
        if len(parts) < 10:
            return None
        
        try:
            time_str = parts[1]
            date_str = parts[9]
            
            hour = int(time_str[0:2])
            minute = int(time_str[2:4])
            second = float(time_str[4:])
            
            day = int(date_str[0:2])
            month = int(date_str[2:4])
            year = 2000 + int(date_str[4:6])
            
            return datetime(year, month, day, hour, minute, int(second))
        
        except (ValueError, IndexError) as e:
            logger.debug(f"Failed to parse RMC: {e}")
            return None


class GPSTimeserverClient:
    """
    TCP client for GPS timeserver.
    
    Connects to timeserver and parses NMEA stream to extract position and VTEC.
    """
    
    def __init__(self, host: str = '192.168.0.202', port: int = 2000):
        """
        Initialize client.
        
        Args:
            host: Timeserver IP address
            port: Timeserver TCP port
        """
        self.host = host
        self.port = port
        self.socket = None
        self.buffer = ""
        self.last_position = None
        self.last_vtec = None
    
    def connect(self):
        """Connect to timeserver."""
        try:
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.socket.settimeout(10.0)
            self.socket.connect((self.host, self.port))
            logger.info(f"Connected to GPS timeserver at {self.host}:{self.port}")
        except Exception as e:
            logger.error(f"Failed to connect to timeserver: {e}")
            raise
    
    def disconnect(self):
        """Disconnect from timeserver."""
        if self.socket:
            self.socket.close()
            self.socket = None
            logger.info("Disconnected from timeserver")
    
    def read_sentence(self) -> Optional[str]:
        """
        Read one NMEA sentence from stream.
        
        Returns:
            Complete NMEA sentence (without newline), or None if connection closed
        """
        while True:
            # Check if we have a complete sentence in buffer
            if '\n' in self.buffer:
                line, self.buffer = self.buffer.split('\n', 1)
                line = line.strip()
                if line.startswith('$') and '*' in line:
                    return line
            
            # Read more data
            try:
                data = self.socket.recv(1024)
                if not data:
                    return None  # Connection closed
                self.buffer += data.decode('ascii', errors='ignore')
            except socket.timeout:
                return None
            except Exception as e:
                logger.error(f"Error reading from socket: {e}")
                return None
    
    def get_current_position(self) -> Optional[GPSPosition]:
        """
        Get current GPS position.
        
        Reads sentences until we get a valid $GNGGA.
        """
        for _ in range(100):  # Read up to 100 sentences
            sentence = self.read_sentence()
            if not sentence:
                break
            
            if sentence.startswith('$GNGGA') or sentence.startswith('$GPGGA'):
                pos = NMEAParser.parse_gga(sentence)
                if pos:
                    self.last_position = pos
                    return pos
        
        return self.last_position
    
    def get_current_vtec(self) -> Optional[float]:
        """
        Get current VTEC measurement.
        
        NOTE: Standard NMEA does not include TEC. This method looks for:
        1. Proprietary sentences (e.g., $PSTI,TEC,...)
        2. Custom JSON messages
        3. Falls back to estimating from satellite data (future)
        
        Returns:
            VTEC in TECU, or None if not available
        """
        # Read sentences looking for TEC data
        for _ in range(100):
            sentence = self.read_sentence()
            if not sentence:
                break
            
            # Check for proprietary TEC sentences
            # Format varies by manufacturer:
            # - Septentrio: $PSTI,TEC,vtec,stec,...
            # - Trimble: $PGRM,TEC,...
            # - Custom: $PVTEC,timestamp,vtec,...
            
            if 'TEC' in sentence.upper():
                # Try to extract TEC value
                # This is a placeholder - actual format depends on your timeserver
                match = re.search(r'TEC[,:](\d+\.?\d*)', sentence, re.IGNORECASE)
                if match:
                    vtec = float(match.group(1))
                    logger.info(f"Extracted VTEC: {vtec} TECU from {sentence}")
                    return vtec
        
        # No TEC data found in standard NMEA
        logger.warning("No TEC data found in NMEA stream")
        logger.warning("Timeserver may need configuration to output TEC")
        return None
    
    def monitor(self, duration_seconds: int = 60):
        """
        Monitor stream and print all sentences for debugging.
        
        Args:
            duration_seconds: How long to monitor
        """
        import time
        start_time = time.time()
        
        print(f"Monitoring GPS timeserver for {duration_seconds} seconds...")
        print("Looking for TEC-related sentences...\n")
        
        while time.time() - start_time < duration_seconds:
            sentence = self.read_sentence()
            if sentence:
                print(sentence)
                
                # Highlight TEC-related sentences
                if 'TEC' in sentence.upper() or 'ION' in sentence.upper():
                    print(f"  ^^^ POTENTIAL TEC DATA ^^^")


def get_vtec_from_timeserver(
    host: str = '192.168.0.202',
    port: int = 2000,
    timeout: float = 10.0
) -> Optional[float]:
    """
    Convenience function to get current VTEC from timeserver.
    
    Args:
        host: Timeserver IP
        port: Timeserver port
        timeout: Connection timeout
    
    Returns:
        VTEC in TECU, or None if not available
    """
    client = GPSTimeserverClient(host, port)
    
    try:
        client.connect()
        vtec = client.get_current_vtec()
        return vtec
    except Exception as e:
        logger.error(f"Failed to get VTEC: {e}")
        return None
    finally:
        client.disconnect()


if __name__ == '__main__':
    import sys
    import argparse
    
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s'
    )
    
    parser = argparse.ArgumentParser(description='GPS Timeserver TCP Client')
    parser.add_argument('--host', default='192.168.0.202', help='Timeserver IP')
    parser.add_argument('--port', type=int, default=2000, help='Timeserver port')
    parser.add_argument('--monitor', action='store_true', help='Monitor stream for debugging')
    parser.add_argument('--duration', type=int, default=60, help='Monitor duration (seconds)')
    
    args = parser.parse_args()
    
    client = GPSTimeserverClient(args.host, args.port)
    
    try:
        client.connect()
        
        if args.monitor:
            # Monitor mode - print all sentences
            client.monitor(args.duration)
        else:
            # Normal mode - get position and VTEC
            print("Getting GPS position...")
            pos = client.get_current_position()
            if pos:
                print(f"Position: {pos.latitude:.6f}°N, {pos.longitude:.6f}°W")
                print(f"Altitude: {pos.altitude:.1f}m")
                print(f"Satellites: {pos.num_satellites}")
                print(f"Time: {pos.timestamp}")
            else:
                print("Failed to get position")
            
            print("\nGetting VTEC...")
            vtec = client.get_current_vtec()
            if vtec is not None:
                print(f"VTEC: {vtec:.2f} TECU")
            else:
                print("VTEC not available")
                print("\nTo see raw stream, run with --monitor flag:")
                print(f"  python {sys.argv[0]} --monitor --duration 30")
    
    finally:
        client.disconnect()

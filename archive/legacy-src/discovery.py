"""
Stream discovery module for ka9q-radio

Discovers available streams via Avahi/mDNS and decodes status metadata
to automatically determine SSRCs, sample rates, and other parameters.
"""

import socket
import struct
import subprocess
import logging
from typing import Dict, List, Tuple, Optional
from dataclasses import dataclass
from enum import IntEnum

logger = logging.getLogger(__name__)


class StatusType(IntEnum):
    """Status metadata types from ka9q-radio status.h"""
    EOL = 0
    COMMAND_TAG = 1
    CMD_CNT = 2
    GPS_TIME = 3
    DESCRIPTION = 4
    STATUS_DEST_SOCKET = 5
    
    OUTPUT_DATA_SOURCE_SOCKET = 12
    OUTPUT_DATA_DEST_SOCKET = 13
    OUTPUT_SSRC = 14
    OUTPUT_TTL = 15
    OUTPUT_SAMPRATE = 16
    OUTPUT_METADATA_PACKETS = 17
    OUTPUT_DATA_PACKETS = 18
    OUTPUT_ERRORS = 19
    
    RADIO_FREQUENCY = 25
    
    OUTPUT_CHANNELS = 37
    
    OUTPUT_ENCODING = 67
    RTP_PT = 69


class Encoding(IntEnum):
    """Output encoding types"""
    S16BE = 0
    S16LE = 1
    F32LE = 2
    F16LE = 3
    OPUS = 10


@dataclass
class StreamMetadata:
    """Metadata for a discovered stream"""
    ssrc: int
    frequency: float  # Hz
    sample_rate: int  # Hz
    channels: int  # 1 or 2
    encoding: Encoding
    description: str = ""
    multicast_address: str = ""
    port: int = 0


def resolve_mdns_name(name: str, timeout: int = 5) -> Tuple[str, int]:
    """
    Resolve mDNS service name to multicast address and port using avahi-browse
    
    Args:
        name: Service name (e.g., "wwv-iq.local")
        timeout: Timeout in seconds
        
    Returns:
        Tuple of (multicast_address, port)
        
    Raises:
        ValueError: If service not found
    """
    logger.info(f"Resolving mDNS name: {name}")
    
    # Remove .local suffix if present for avahi-browse
    service_name = name.replace('.local', '')
    
    try:
        # Use avahi-browse to discover RTP services
        cmd = ["avahi-browse", "-r", "-p", "-t", "_rtp._udp"]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        
        for line in result.stdout.split('\n'):
            if service_name in line and line.startswith('='):
                # Parse avahi-browse output format:
                # =;interface;protocol;name;type;domain;hostname;address;port;txt...
                parts = line.split(';')
                if len(parts) >= 9:
                    address = parts[7]
                    port = int(parts[8])
                    logger.info(f"Resolved {name} → {address}:{port}")
                    return (address, port)
        
        # Fallback: try standard getaddrinfo
        logger.warning(f"avahi-browse didn't find {name}, trying getaddrinfo")
        import socket
        result = socket.getaddrinfo(name, None, socket.AF_INET, socket.SOCK_DGRAM)
        if result:
            address = result[0][4][0]
            # Default RTP port
            port = 5004
            logger.info(f"Resolved {name} → {address}:{port} (via getaddrinfo)")
            return (address, port)
            
    except subprocess.TimeoutExpired:
        logger.error(f"Timeout resolving {name}")
    except Exception as e:
        logger.error(f"Error resolving {name}: {e}")
    
    raise ValueError(f"Could not resolve mDNS name: {name}")


def decode_status_metadata(packet: bytes) -> Dict:
    """
    Decode ka9q-radio status metadata packet using TLV format
    
    Args:
        packet: Raw status packet bytes
        
    Returns:
        Dictionary of decoded metadata fields
    """
    if len(packet) < 2 or packet[0] != 0:  # Must be STATUS packet type
        logger.debug(f"Invalid packet: len={len(packet)}, type={packet[0] if packet else 'empty'}")
        return {}
    
    metadata = {}
    offset = 1  # Skip packet type byte
    logger.debug(f"Decoding STATUS packet, length={len(packet)}")
    
    while offset < len(packet):
        if offset + 2 > len(packet):
            break
        
        tag = packet[offset]
        length = packet[offset + 1]
        offset += 2
        
        logger.debug(f"TLV: tag={tag}, length={length}")
        
        if tag == StatusType.EOL:
            break
        
        if offset + length > len(packet):
            logger.warning(f"Truncated TLV field: tag={tag}, length={length}")
            break
        
        value_bytes = packet[offset:offset + length]
        offset += length
        
        try:
            # Decode based on tag type
            if tag == StatusType.OUTPUT_SSRC and length == 4:
                metadata['ssrc'] = struct.unpack('>I', value_bytes)[0]
                logger.debug(f"Found SSRC: 0x{metadata['ssrc']:08x}")
            elif tag == StatusType.OUTPUT_SAMPRATE and length == 4:
                metadata['sample_rate'] = struct.unpack('>I', value_bytes)[0]
            elif tag == StatusType.RADIO_FREQUENCY and length == 8:
                metadata['frequency'] = struct.unpack('>d', value_bytes)[0]
            elif tag == StatusType.OUTPUT_CHANNELS and length == 2:
                metadata['channels'] = struct.unpack('>H', value_bytes)[0]
            elif tag == StatusType.OUTPUT_ENCODING and length == 1:
                metadata['encoding'] = Encoding(struct.unpack('>B', value_bytes)[0])
            elif tag == StatusType.DESCRIPTION:
                metadata['description'] = value_bytes.decode('utf-8', errors='ignore').strip('\x00')
            elif tag == StatusType.OUTPUT_DATA_DEST_SOCKET:
                # Decode sockaddr structure
                if length >= 8:  # IPv4 sockaddr_in
                    family, port = struct.unpack('>HH', value_bytes[0:4])
                    addr_bytes = value_bytes[4:8]
                    addr = socket.inet_ntoa(addr_bytes)
                    metadata['data_address'] = addr
                    metadata['data_port'] = port
        except Exception as e:
            logger.debug(f"Error decoding tag {tag}: {e}")
            continue
    
    return metadata


class StreamDiscovery:
    """Discover and track streams from a ka9q-radio service"""
    
    def __init__(self, stream_name: str, status_stream: str = None, status_port: int = None):
        """
        Initialize stream discovery
        
        Args:
            stream_name: mDNS service name for data (e.g., "wwv-iq.local")
            status_stream: Optional separate mDNS name for status (e.g., "hf-status.local")
            status_port: Optional explicit port for status metadata (default: use resolved port)
        """
        self.stream_name = stream_name
        self.status_stream = status_stream or stream_name
        self.status_port_override = status_port
        self.data_address = None
        self.data_port = None
        self.status_address = None
        self.status_port = None
        self.streams: Dict[int, StreamMetadata] = {}  # ssrc -> metadata
        
    def resolve(self) -> Tuple[str, int]:
        """
        Resolve mDNS name to multicast address
        
        Returns:
            Tuple of (address, port)
        """
        self.data_address, self.data_port = resolve_mdns_name(self.stream_name)
        
        # Resolve status stream (may be same as data stream or separate)
        if self.status_stream != self.stream_name:
            self.status_address, status_port = resolve_mdns_name(self.status_stream)
        else:
            self.status_address = self.data_address
            status_port = self.data_port
        
        # Use explicit port override if provided, otherwise use resolved port
        self.status_port = self.status_port_override if self.status_port_override else status_port
        
        return (self.data_address, self.data_port)
    
    def discover_streams(self, timeout: float = 5.0) -> Dict[int, StreamMetadata]:
        """
        Listen to status stream and discover all active SSRCs
        
        Args:
            timeout: How long to listen for status packets (seconds)
            
        Returns:
            Dictionary mapping SSRC to StreamMetadata
        """
        if not self.data_address:
            self.resolve()
        
        logger.info(f"Discovering streams from {self.stream_name} (status: {self.status_address}:{self.status_port})")
        
        # Create UDP socket for status stream
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        
        # Enable multicast loopback (needed when radiod is on same machine)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
        
        try:
            # Bind first, then join multicast group
            sock.bind(('', self.status_port))
            
            # Join multicast group - try loopback first (for local radiod), then any interface
            try:
                mreq = struct.pack("4s4s", socket.inet_aton(self.status_address), socket.inet_aton('127.0.0.1'))
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
                logger.debug(f"Joined multicast group {self.status_address} on loopback interface")
            except OSError as e:
                logger.debug(f"Could not join on loopback: {e}, trying INADDR_ANY")
                mreq = struct.pack("4sl", socket.inet_aton(self.status_address), socket.INADDR_ANY)
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
                logger.debug(f"Joined multicast group {self.status_address} on all interfaces")
            
            sock.settimeout(timeout)
            
            # Listen for status packets
            packets_received = 0
            logger.debug(f"Listening for status packets (timeout={timeout}s)...")
            while True:
                try:
                    data, addr = sock.recvfrom(8192)
                    packets_received += 1
                    logger.debug(f"Received packet {packets_received} from {addr}, length={len(data)}, type={data[0] if data else 'empty'}")
                    
                    if data[0] == 0:  # STATUS packet type
                        metadata_dict = decode_status_metadata(data)
                        logger.debug(f"Decoded metadata: {metadata_dict}")
                        
                        if 'ssrc' in metadata_dict:
                            ssrc = metadata_dict['ssrc']
                            
                            # Create or update StreamMetadata
                            if ssrc not in self.streams:
                                self.streams[ssrc] = StreamMetadata(
                                    ssrc=ssrc,
                                    frequency=metadata_dict.get('frequency', 0.0),
                                    sample_rate=metadata_dict.get('sample_rate', 0),
                                    channels=metadata_dict.get('channels', 2),
                                    encoding=metadata_dict.get('encoding', Encoding.F32LE),
                                    description=metadata_dict.get('description', ''),
                                    multicast_address=self.data_address,
                                    port=self.data_port
                                )
                                
                                logger.info(
                                    f"Discovered stream: SSRC=0x{ssrc:08x}, "
                                    f"freq={self.streams[ssrc].frequency/1e6:.3f} MHz, "
                                    f"rate={self.streams[ssrc].sample_rate} Hz, "
                                    f"ch={self.streams[ssrc].channels}, "
                                    f"enc={self.streams[ssrc].encoding.name}"
                                )
                            else:
                                # Update existing metadata
                                for key, value in metadata_dict.items():
                                    if key != 'ssrc' and value:
                                        setattr(self.streams[ssrc], key, value)
                
                except socket.timeout:
                    logger.debug(f"Timeout after receiving {packets_received} packets")
                    break
                except Exception as e:
                    logger.error(f"Error receiving status packet: {e}")
                    import traceback
                    logger.error(traceback.format_exc())
                    break
        
        finally:
            sock.close()
        
        logger.info(f"Discovery complete: found {len(self.streams)} streams ({packets_received} packets received)")
        return self.streams
    
    def get_stream_by_frequency(self, target_freq: float, tolerance: float = 1000) -> Optional[StreamMetadata]:
        """
        Find stream matching a specific frequency
        
        Args:
            target_freq: Target frequency in Hz
            tolerance: Frequency tolerance in Hz
            
        Returns:
            StreamMetadata if found, None otherwise
        """
        for ssrc, metadata in self.streams.items():
            if abs(metadata.frequency - target_freq) < tolerance:
                return metadata
        return None


class StreamManager:
    """Manage discovery and monitoring of multiple ka9q-radio streams"""
    
    def __init__(self, config: Dict):
        """
        Initialize stream manager
        
        Args:
            config: Configuration dictionary
        """
        self.config = config
        self.discoveries: Dict[str, StreamDiscovery] = {}
        self.frequency_to_ssrc: Dict[float, int] = {}
        self.ssrc_to_metadata: Dict[int, StreamMetadata] = {}
        
    def discover_all(self) -> Dict[int, StreamMetadata]:
        """
        Discover all configured streams
        
        Returns:
            Dictionary mapping SSRC to StreamMetadata for all discovered streams
        """
        logger.info("Starting stream discovery for all configured streams")
        
        for stream_config in self.config.get('streams', []):
            stream_name = stream_config['stream_name']
            
            # Create discovery instance
            discovery = StreamDiscovery(stream_name)
            self.discoveries[stream_name] = discovery
            
            # Discover streams
            try:
                streams = discovery.discover_streams(timeout=5.0)
                
                # Map frequencies to SSRCs
                for freq in stream_config.get('frequencies', []):
                    metadata = discovery.get_stream_by_frequency(freq)
                    if metadata:
                        self.frequency_to_ssrc[freq] = metadata.ssrc
                        self.ssrc_to_metadata[metadata.ssrc] = metadata
                        logger.info(f"Mapped {freq/1e6:.3f} MHz → SSRC 0x{metadata.ssrc:08x}")
                    else:
                        logger.warning(f"No stream found for {freq/1e6:.3f} MHz in {stream_name}")
            
            except Exception as e:
                logger.error(f"Error discovering streams from {stream_name}: {e}")
        
        logger.info(f"Discovery complete: {len(self.ssrc_to_metadata)} streams mapped")
        return self.ssrc_to_metadata
    
    def get_metadata_for_frequency(self, frequency: float) -> Optional[StreamMetadata]:
        """Get stream metadata for a specific frequency"""
        ssrc = self.frequency_to_ssrc.get(frequency)
        if ssrc:
            return self.ssrc_to_metadata.get(ssrc)
        return None


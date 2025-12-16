#!/usr/bin/env python3
"""
Generic RTP Receiver with ka9q-python Integration

Receives RTP packets from multicast and demultiplexes by SSRC.
Uses ka9q-python for RTP parsing and timing (GPS_TIME/RTP_TIMESNAP).

This module provides efficient multi-SSRC demultiplexing on a single
socket, while leveraging ka9q-python's timing capabilities.
"""

import socket
import struct
import threading
import logging
from typing import Dict, Optional, Callable

# Use ka9q-python for RTP parsing and timing
from ka9q import parse_rtp_header, RTPHeader, rtp_to_wallclock, ChannelInfo

logger = logging.getLogger(__name__)


# Re-export RTPHeader from ka9q for backward compatibility
# RTPHeader is now imported from ka9q above


class RTPReceiver:
    """
    Receives RTP packets from multicast and demultiplexes by SSRC.
    
    Uses ka9q-python for RTP parsing and timing. Callbacks receive:
    - RTPHeader (from ka9q)
    - payload bytes
    - wallclock time (from ka9q's rtp_to_wallclock, if timing info available)
    
    Example:
        receiver = RTPReceiver('239.192.152.141', port=5004)
        receiver.register_callback(ssrc=10000000, callback=my_handler)
        receiver.start()
        # Callback signature: callback(header: RTPHeader, payload: bytes, wallclock: Optional[float])
    """
    
    # Expected bytes per sample for different encodings
    BYTES_PER_SAMPLE_FLOAT = 8   # complex64 (2 x float32)
    BYTES_PER_SAMPLE_INT16 = 4   # complex int16 (2 x int16)
    
    def __init__(self, multicast_address: str, port: int = 5004):
        """
        Initialize RTP receiver.
        
        Args:
            multicast_address: Multicast group address to join
            port: RTP port (default 5004)
        """
        self.multicast_address = multicast_address
        self.port = port
        self.running = False
        self.socket = None
        self.thread = None
        self.callbacks: Dict[int, Callable] = {}  # ssrc -> callback
        self.channel_info: Dict[int, ChannelInfo] = {}  # ssrc -> ChannelInfo for timing
        self.expected_config: Dict[int, dict] = {}  # ssrc -> expected config for validation
        self.validated_ssrcs: set = set()  # SSRCs that passed payload validation
        
    def register_callback(self, ssrc: int, callback: Callable, 
                          channel_info: Optional[ChannelInfo] = None,
                          expected_sample_rate: int = 20000,
                          expected_encoding: str = 'float',
                          blocktime_ms: float = 20.0):
        """
        Register callback for specific SSRC with expected configuration.
        
        Args:
            ssrc: RTP SSRC identifier
            callback: Function to call with (RTPHeader, payload_bytes, wallclock_time)
                     wallclock_time is None if timing info not available
            channel_info: Optional ChannelInfo with timing data (gps_time, rtp_timesnap)
                         If provided, enables wallclock time calculation
            expected_sample_rate: Expected sample rate in Hz (for payload validation)
            expected_encoding: Expected encoding ('float' or 'int16')
            blocktime_ms: Expected blocktime in ms (determines samples per packet)
        """
        self.callbacks[ssrc] = callback
        if channel_info:
            self.channel_info[ssrc] = channel_info
        
        # Store expected config for payload validation
        samples_per_packet = int(expected_sample_rate * blocktime_ms / 1000)
        if expected_encoding == 'float':
            expected_payload_bytes = samples_per_packet * self.BYTES_PER_SAMPLE_FLOAT
        else:
            expected_payload_bytes = samples_per_packet * self.BYTES_PER_SAMPLE_INT16
        
        self.expected_config[ssrc] = {
            'sample_rate': expected_sample_rate,
            'encoding': expected_encoding,
            'blocktime_ms': blocktime_ms,
            'samples_per_packet': samples_per_packet,
            'expected_payload_bytes': expected_payload_bytes
        }
        
        logger.info(f"Registered callback for SSRC {ssrc} "
                   f"(timing: {'enabled' if channel_info else 'disabled'}, "
                   f"expect {samples_per_packet} samples, {expected_payload_bytes} bytes/pkt)")
        
    def unregister_callback(self, ssrc: int):
        """
        Unregister callback for specific SSRC.
        
        Args:
            ssrc: RTP SSRC identifier to remove
        """
        if ssrc in self.callbacks:
            del self.callbacks[ssrc]
        if ssrc in self.channel_info:
            del self.channel_info[ssrc]
        if ssrc in self.expected_config:
            del self.expected_config[ssrc]
        if ssrc in self.validated_ssrcs:
            self.validated_ssrcs.discard(ssrc)
        logger.info(f"Unregistered callback for SSRC {ssrc}")
    
    def validate_payload(self, ssrc: int, payload: bytes) -> tuple:
        """
        Validate RTP payload matches expected encoding.
        
        Note: radiod uses variable packet sizes (typically 9ms, not fixed 20ms),
        so we validate encoding type rather than exact byte count.
        
        Args:
            ssrc: Channel SSRC
            payload: RTP payload bytes
            
        Returns:
            (valid: bool, detected_encoding: str, error: Optional[str])
        """
        if ssrc not in self.expected_config:
            return (True, 'unknown', None)  # No validation configured
        
        config = self.expected_config[ssrc]
        expected_encoding = config['encoding']
        actual_bytes = len(payload)
        
        # Detect encoding from payload size divisibility and value inspection
        # Float (complex64): 8 bytes per sample, values typically < 1.0
        # Int16 (complex int16): 4 bytes per sample, values in [-32768, 32767]
        
        if actual_bytes % self.BYTES_PER_SAMPLE_FLOAT == 0:
            # Could be float - verify by checking value range
            import struct
            samples = actual_bytes // self.BYTES_PER_SAMPLE_FLOAT
            if samples > 0:
                # Check first few samples
                try:
                    floats = struct.unpack(f'<{min(8, samples * 2)}f', payload[:min(32, actual_bytes)])
                    max_val = max(abs(f) for f in floats)
                    # Float IQ is typically normalized, values < 10
                    if max_val < 100:
                        detected_encoding = 'float'
                        detected_samples = samples
                    else:
                        # Large values suggest int16 misinterpreted as float
                        detected_encoding = 'int16'
                        detected_samples = actual_bytes // self.BYTES_PER_SAMPLE_INT16
                except struct.error:
                    detected_encoding = 'unknown'
                    detected_samples = 0
            else:
                detected_encoding = 'unknown'
                detected_samples = 0
        elif actual_bytes % self.BYTES_PER_SAMPLE_INT16 == 0:
            detected_encoding = 'int16'
            detected_samples = actual_bytes // self.BYTES_PER_SAMPLE_INT16
        else:
            return (False, 'unknown', 
                   f"Payload size {actual_bytes} not divisible by sample size")
        
        # Check if encoding matches expected
        if detected_encoding != expected_encoding:
            return (False, detected_encoding,
                   f"Encoding mismatch: expected {expected_encoding}, got {detected_encoding}")
        
        return (True, detected_encoding, None)
    
    def update_channel_info(self, ssrc: int, channel_info: ChannelInfo):
        """
        Update timing info for a channel (call when status stream updates).
        
        Args:
            ssrc: RTP SSRC identifier
            channel_info: Updated ChannelInfo with fresh gps_time/rtp_timesnap
        """
        if ssrc in self.callbacks:
            self.channel_info[ssrc] = channel_info
            logger.debug(f"Updated timing info for SSRC {ssrc}")
        
    def start(self):
        """Start receiving RTP packets"""
        if self.running:
            logger.warning("RTP receiver already running")
            return
            
        # Create UDP socket
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        
        # Request large receive buffer to prevent packet loss under load
        # 25MB buffer for 9 channels @ 20kHz = ~5 seconds of data
        try:
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 26214400)
            actual_size = self.socket.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)
            logger.info(f"UDP receive buffer: requested 25MB, got {actual_size // 1024 // 1024}MB")
        except Exception as e:
            logger.warning(f"Could not set UDP buffer size: {e}")
        
        # Bind to port
        self.socket.bind(('', self.port))
        
        # Join multicast group on all interfaces (INADDR_ANY)
        # Note: radiod may send via network interface, not loopback
        mreq = struct.pack("4sl",
                          socket.inet_aton(self.multicast_address),
                          socket.INADDR_ANY)
        self.socket.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        logger.info(f"Joined multicast {self.multicast_address} on all interfaces")
        
        # Start receiver thread
        self.running = True
        self.thread = threading.Thread(target=self._receive_loop, daemon=True)
        self.thread.start()
        logger.info(f"RTP receiver started on {self.multicast_address}:{self.port}")
        
    def stop(self):
        """Stop receiving"""
        self.running = False
        if self.thread:
            self.thread.join(timeout=2)
        if self.socket:
            self.socket.close()
        logger.info("RTP receiver stopped")
        
    def _receive_loop(self):
        """Main packet reception loop"""
        packet_count = 0
        ssrc_seen = set()
        
        while self.running:
            try:
                data, addr = self.socket.recvfrom(8192)
                packet_count += 1
                
                # Parse RTP header using ka9q-python
                header = parse_rtp_header(data)
                if not header:
                    continue
                
                # Log first packet from each SSRC for diagnostics (DEBUG level)
                if header.ssrc not in ssrc_seen:
                    ssrc_seen.add(header.ssrc)
                    has_callback = header.ssrc in self.callbacks
                    has_timing = header.ssrc in self.channel_info
                    logger.debug(f"First packet from SSRC {header.ssrc}: "
                                f"seq={header.sequence}, ts={header.timestamp}, "
                                f"payload={len(data)-12} bytes, "
                                f"callback={'YES' if has_callback else 'NO'}, "
                                f"timing={'YES' if has_timing else 'NO'}")
                    if not has_callback:
                        logger.debug(f"No callback registered for SSRC {header.ssrc}. "
                                    f"Registered SSRCs: {list(self.callbacks.keys())}")
                    
                    # Validate first packet payload for registered SSRCs
                    if has_callback and header.ssrc not in self.validated_ssrcs:
                        # Extract payload for validation (need to calculate offset first)
                        val_offset = 12 + (header.csrc_count * 4)
                        if header.extension and len(data) >= val_offset + 4:
                            ext_header = struct.unpack('>HH', data[val_offset:val_offset+4])
                            val_offset += 4 + (ext_header[1] * 4)
                        if val_offset < len(data):
                            val_payload = data[val_offset:]
                            valid, detected, error = self.validate_payload(header.ssrc, val_payload)
                            if valid:
                                self.validated_ssrcs.add(header.ssrc)
                                logger.info(f"✓ SSRC {header.ssrc} payload validated: "
                                           f"{len(val_payload)} bytes, encoding={detected}")
                            else:
                                logger.warning(f"⚠ SSRC {header.ssrc} payload validation failed: {error}")
                                # Still process packets but log the mismatch
                
                # Log periodic stats every 10000 packets (reduced verbosity)
                if packet_count % 10000 == 0:
                    logger.info(f"RTP receiver: {packet_count} packets, "
                               f"{len(ssrc_seen)} SSRCs ({len(self.callbacks)} registered)")
                    
                # Extract payload - account for CSRC list and extension headers
                payload_offset = 12 + (header.csrc_count * 4)  # Base header + CSRC list
                
                # Handle extension header if present
                if header.extension:
                    if len(data) >= payload_offset + 4:
                        # Extension header: 2 bytes profile + 2 bytes length (in 32-bit words)
                        ext_header = struct.unpack('>HH', data[payload_offset:payload_offset+4])
                        ext_length_words = ext_header[1]
                        payload_offset += 4 + (ext_length_words * 4)
                
                if payload_offset >= len(data):
                    logger.warning(f"Invalid RTP packet: payload_offset={payload_offset}, len={len(data)}")
                    continue
                
                payload = data[payload_offset:]
                
                # Calculate wallclock time if timing info available
                wallclock = None
                channel_info = self.channel_info.get(header.ssrc)
                if channel_info:
                    wallclock = rtp_to_wallclock(header.timestamp, channel_info)
                
                # Dispatch to appropriate callback with timing
                callback = self.callbacks.get(header.ssrc)
                if callback:
                    callback(header, payload, wallclock)
            
            except Exception as e:
                if self.running:
                    logger.error(f"Error receiving RTP packet: {e}")
                    
    # Note: _parse_rtp_header removed - now using ka9q.parse_rtp_header()

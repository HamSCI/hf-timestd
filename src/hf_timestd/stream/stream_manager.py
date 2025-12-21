#!/usr/bin/env python3
"""
Stream Manager - Internal stream lifecycle management

Manages the mapping between StreamSpecs and radiod channels.
Handles SSRC allocation internally - applications never see SSRCs.
"""

import logging
import threading
import time
from typing import Dict, List, Optional, Set
from dataclasses import dataclass

from ka9q import discover_channels, RadiodControl, ChannelInfo

from .stream_spec import StreamSpec, StreamRequest
from .stream_handle import StreamHandle, StreamInfo

logger = logging.getLogger(__name__)


@dataclass
class ManagedStream:
    """Internal record of a stream we're managing"""
    spec: StreamSpec
    ssrc: int
    multicast_address: str
    port: int
    ref_count: int = 0
    created_by_us: bool = True  # False if we found it via discovery


class StreamManager:
    """
    Manages streams on a radiod instance.
    
    This class:
    - Discovers existing streams
    - Allocates SSRCs internally (apps don't see them)
    - Creates new streams when needed
    - Shares streams when specs match
    - Tracks reference counts for cleanup
    
    Thread-safe for concurrent access.
    
    Example:
        manager = StreamManager("radiod.local")
        
        # Subscribe to a stream (SSRC hidden)
        handle = manager.subscribe(
            frequency_hz=10.0e6,
            preset="iq",
            sample_rate=16000
        )
        
        # Use the stream
        print(f"Receiving on {handle.multicast_address}:{handle.port}")
        
        # Release when done
        handle.release()
    """
    
    def __init__(
        self,
        radiod_address: str,
        default_destination: Optional[str] = None,
        auto_cleanup: bool = True
    ):
        """
        Initialize stream manager.
        
        Args:
            radiod_address: mDNS name or address of radiod status stream
            default_destination: Default multicast destination for new streams
            auto_cleanup: If True, remove streams when ref_count hits 0
        """
        self.radiod_address = radiod_address
        self.default_destination = default_destination
        self.auto_cleanup = auto_cleanup
        
        # Internal state
        self._lock = threading.Lock()
        self._streams: Dict[StreamSpec, ManagedStream] = {}
        self._ssrc_to_spec: Dict[int, StreamSpec] = {}
        self._used_ssrcs: Set[int] = set()
        
        # Control connection (lazy init)
        self._control: Optional[RadiodControl] = None
        
        logger.info(f"StreamManager initialized for {radiod_address}")
    
    # === Public API ===
    
    def subscribe(
        self,
        frequency_hz: float,
        preset: str = "iq",
        sample_rate: int = 16000,
        agc: bool = False,
        gain: float = 0.0,
        destination: Optional[str] = None,
        description: str = ""
    ) -> StreamHandle:
        """
        Subscribe to a stream.
        
        If an identical stream already exists, returns a handle to it.
        Otherwise, creates a new stream in radiod.
        
        SSRC is allocated internally - callers never need to know it.
        
        Args:
            frequency_hz: Center frequency in Hz
            preset: Demodulation mode ("iq", "usb", "lsb", "am", "fm", "cw")
            sample_rate: Output sample rate in Hz
            agc: Enable automatic gain control
            gain: Manual gain in dB (used when agc=False)
            destination: Multicast destination (default: radiod's default)
            description: Human-readable description for logging
            
        Returns:
            StreamHandle for receiving the stream
            
        Raises:
            RuntimeError: If stream creation fails
        """
        spec = StreamSpec(
            frequency_hz=frequency_hz,
            preset=preset,
            sample_rate=sample_rate,
            agc=agc,
            gain=gain
        )
        
        request = StreamRequest(
            spec=spec,
            destination=destination or self.default_destination,
            description=description or str(spec)
        )
        
        return self._subscribe(request)
    
    def subscribe_request(self, request: StreamRequest) -> StreamHandle:
        """
        Subscribe using a StreamRequest object.
        
        Args:
            request: StreamRequest with spec and preferences
            
        Returns:
            StreamHandle for receiving the stream
        """
        return self._subscribe(request)
    
    def discover(self) -> List[StreamInfo]:
        """
        Discover all existing streams on radiod.
        
        Returns:
            List of StreamInfo for each discovered stream
        """
        try:
            channels = discover_channels(self.radiod_address)
            
            with self._lock:
                # Update our knowledge of used SSRCs
                for ssrc in channels.keys():
                    self._used_ssrcs.add(ssrc)
            
            return [StreamInfo.from_channel_info(info) for info in channels.values()]
            
        except Exception as e:
            logger.error(f"Discovery failed: {e}")
            return []
    
    def find_compatible(
        self,
        frequency_hz: float,
        preset: str,
        sample_rate: int,
        frequency_tolerance_hz: float = 100.0
    ) -> Optional[StreamInfo]:
        """
        Find an existing stream that matches the given parameters.
        
        Args:
            frequency_hz: Desired frequency
            preset: Desired preset
            sample_rate: Desired sample rate
            frequency_tolerance_hz: How close frequency must match
            
        Returns:
            StreamInfo if compatible stream found, None otherwise
        """
        target = StreamSpec(
            frequency_hz=frequency_hz,
            preset=preset,
            sample_rate=sample_rate
        )
        
        for info in self.discover():
            if info.spec.matches(target, frequency_tolerance_hz):
                return info
        
        return None
    
    def list_managed(self) -> List[StreamHandle]:
        """
        List all streams we're currently managing.
        
        Returns:
            List of StreamHandles for managed streams
        """
        with self._lock:
            handles = []
            for spec, managed in self._streams.items():
                handle = StreamHandle(
                    spec=spec,
                    multicast_address=managed.multicast_address,
                    port=managed.port,
                    _ssrc=managed.ssrc,
                    _manager=self,
                    _ref_count=managed.ref_count
                )
                handles.append(handle)
            return handles
    
    def close(self):
        """Close the manager and release resources."""
        if self._control:
            self._control.close()
            self._control = None
        logger.info(f"StreamManager closed for {self.radiod_address}")
    
    # === Internal Methods ===
    
    def _subscribe(self, request: StreamRequest) -> StreamHandle:
        """Internal subscribe implementation."""
        spec = request.spec
        
        with self._lock:
            # Check if we already have this stream
            if spec in self._streams:
                managed = self._streams[spec]
                managed.ref_count += 1
                logger.info(f"Reusing existing stream: {spec} (refs={managed.ref_count})")
                
                return StreamHandle(
                    spec=spec,
                    multicast_address=managed.multicast_address,
                    port=managed.port,
                    _ssrc=managed.ssrc,
                    _manager=self,
                    _ref_count=managed.ref_count
                )
        
        # Need to create new stream - check if compatible one exists in radiod
        existing = self._find_existing_in_radiod(spec)
        if existing:
            return self._adopt_existing(spec, existing, request)
        
        # Create new stream
        return self._create_new(request)
    
    def _find_existing_in_radiod(self, spec: StreamSpec) -> Optional[ChannelInfo]:
        """Check radiod for an existing compatible stream."""
        try:
            channels = discover_channels(self.radiod_address)
            
            for ssrc, info in channels.items():
                existing_spec = StreamSpec(
                    frequency_hz=info.frequency,
                    preset=info.preset,
                    sample_rate=info.sample_rate
                )
                if existing_spec.matches(spec):
                    logger.debug(f"Found existing stream in radiod: SSRC {ssrc}")
                    return info
            
            return None
            
        except Exception as e:
            logger.warning(f"Failed to check radiod for existing streams: {e}")
            return None
    
    def _adopt_existing(
        self,
        spec: StreamSpec,
        channel_info: ChannelInfo,
        request: StreamRequest
    ) -> StreamHandle:
        """Adopt an existing stream from radiod."""
        with self._lock:
            ssrc = channel_info.ssrc
            
            managed = ManagedStream(
                spec=spec,
                ssrc=ssrc,
                multicast_address=channel_info.multicast_address,
                port=channel_info.port,
                ref_count=1,
                created_by_us=False
            )
            
            self._streams[spec] = managed
            self._ssrc_to_spec[ssrc] = spec
            self._used_ssrcs.add(ssrc)
            
            logger.info(f"Adopted existing stream: {spec} (SSRC {ssrc})")
            
            return StreamHandle(
                spec=spec,
                multicast_address=managed.multicast_address,
                port=managed.port,
                _ssrc=ssrc,
                _manager=self,
                _ref_count=1
            )
    
    def _create_new(self, request: StreamRequest) -> StreamHandle:
        """Create a new stream in radiod."""
        spec = request.spec
        
        # Ensure control connection
        if not self._control:
            self._control = RadiodControl(self.radiod_address)
        
        # Create or reuse channel via ka9q ensure_channel
        try:
            logger.info(f"Ensuring stream: {spec} → {request.destination or 'radiod default'}")
            channel_info = self._control.ensure_channel(
                frequency_hz=spec.frequency_hz,
                preset=spec.preset,
                sample_rate=spec.sample_rate,
                agc_enable=1 if spec.agc else 0,
                gain=spec.gain,
                destination=request.destination
            )
            
            with self._lock:
                managed = ManagedStream(
                    spec=spec,
                    ssrc=channel_info.ssrc,
                    multicast_address=channel_info.multicast_address,
                    port=channel_info.port,
                    ref_count=1,
                    created_by_us=True
                )
                
                self._streams[spec] = managed
                self._ssrc_to_spec[channel_info.ssrc] = spec
                self._used_ssrcs.add(channel_info.ssrc)
                
                logger.info(
                    f"Provisioned stream: {spec} "
                    f"(SSRC {channel_info.ssrc}) → {channel_info.multicast_address}:{channel_info.port}"
                )
                
                return StreamHandle(
                    spec=spec,
                    multicast_address=channel_info.multicast_address,
                    port=channel_info.port,
                    _ssrc=channel_info.ssrc,
                    _manager=self,
                    _ref_count=1
                )
                
        except Exception as e:
            raise RuntimeError(f"Failed to create stream: {e}") from e
    
    def _release_handle(self, handle: StreamHandle):
        """Release a handle (called by StreamHandle.release())."""
        spec = handle.spec
        
        with self._lock:
            if spec not in self._streams:
                logger.warning(f"Release called for unknown stream: {spec}")
                return
            
            managed = self._streams[spec]
            managed.ref_count -= 1
            
            logger.debug(f"Released handle: {spec} (refs={managed.ref_count})")
            
            if managed.ref_count <= 0 and self.auto_cleanup and managed.created_by_us:
                self._remove_stream(spec)
    
    def _remove_stream(self, spec: StreamSpec):
        """Remove a stream from radiod (must hold lock)."""
        if spec not in self._streams:
            return
        
        managed = self._streams[spec]
        ssrc = managed.ssrc
        
        try:
            if self._control:
                self._control.remove_channel(ssrc)
                logger.info(f"Removed stream: {spec}")
        except Exception as e:
            logger.warning(f"Failed to remove stream from radiod: {e}")
        
        del self._streams[spec]
        self._ssrc_to_spec.pop(ssrc, None)
        self._used_ssrcs.discard(ssrc)
    
    def __enter__(self):
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False

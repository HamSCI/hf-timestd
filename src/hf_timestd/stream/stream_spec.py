#!/usr/bin/env python3
"""
Stream Specification - Content-based stream identity

A stream is uniquely identified by its content specification, not by SSRC.
Two requests with identical StreamSpec share the same underlying stream.
"""

from dataclasses import dataclass
from typing import Optional


def _edges_match(a, b) -> bool:
    """Do two specs ask for the same filter?

    Shared by __eq__ and is_compatible_with so the two can never drift into
    disagreeing about whether a stream may be reused -- a disagreement worse
    than either blind spot alone.  None means "whatever radiod defaults to",
    which is a DIFFERENT ask from any stated edge, never a wildcard that
    matches one.  Stated edges use the same 1 Hz tolerance as frequency.
    """
    for x, y in ((a.low_edge, b.low_edge), (a.high_edge, b.high_edge)):
        if x is None or y is None:
            if x is not None or y is not None:
                return False
        elif abs(x - y) >= 1.0:
            return False
    return True


@dataclass(frozen=True)
class StreamSpec:
    """
    Defines what makes a unique stream (content specification).
    
    This is what applications care about - the SSRC is just an internal
    index that radiod uses to track streams.
    
    Two StreamSpecs are equal if all parameters match (within tolerance
    for frequency). This enables automatic stream sharing.
    
    Attributes:
        frequency_hz: Center frequency in Hz
        preset: Demodulation mode ("iq", "usb", "lsb", "am", "fm", "cw")
        sample_rate: Output sample rate in Hz
        agc: Automatic gain control (True=on, False=off)
        gain: Manual gain in dB (used when agc=False)
        low_edge: Filter low edge in Hz, or None for radiod's default
        high_edge: Filter high edge in Hz, or None for radiod's default
    """
    frequency_hz: float
    preset: str
    sample_rate: int
    agc: bool = False
    gain: float = 0.0
    # APPENDED for positional compatibility, and part of IDENTITY.
    #
    # The preset records what a caller ASKED FOR; the filters are what
    # the channel actually does.  Phil Karn (ka9q-radio), 2026-09-21:
    # "you send PRESET USB followed by retuning the filters to -3000,
    # -50, you actually get the lower sideband.  So now the preset
    # actively lies about the channel."  core_recorder_v2 passes
    # per-channel edges on creation, so two specs differing only in
    # bandwidth hashed the same and compared equal -- and sharing keys
    # on that, so a second requester was handed the first one's stream
    # at a bandwidth it never asked for.  The 2026-09-20 T6 A/B built
    # exactly such a pair and measured them 1.31x apart.
    low_edge: Optional[float] = None
    high_edge: Optional[float] = None
    
    def __hash__(self):
        # Round frequency to nearest Hz for hashing
        # This ensures near-identical frequencies hash the same
        return hash((
            round(self.frequency_hz),
            self.preset.lower(),
            self.sample_rate,
            self.agc,
            round(self.gain, 1),
            # None stays distinct from any number: "whatever radiod
            # defaults to" is a different ask from a stated edge.
            None if self.low_edge is None else round(self.low_edge),
            None if self.high_edge is None else round(self.high_edge),
        ))
    
    def __eq__(self, other):
        if not isinstance(other, StreamSpec):
            return False
        # Frequency tolerance of 1 Hz
        freq_match = abs(self.frequency_hz - other.frequency_hz) < 1.0
        return (
            freq_match and
            self.preset.lower() == other.preset.lower() and
            self.sample_rate == other.sample_rate and
            self.agc == other.agc and
            abs(self.gain - other.gain) < 0.1 and
            _edges_match(self, other)
        )
    
    def __str__(self):
        agc_str = "AGC" if self.agc else f"{self.gain:.0f}dB"
        return f"{self.frequency_hz/1e6:.4f}MHz/{self.preset}/{self.sample_rate}Hz/{agc_str}"
    
    def __repr__(self):
        return (f"StreamSpec(frequency_hz={self.frequency_hz}, preset='{self.preset}', "
                f"sample_rate={self.sample_rate}, agc={self.agc}, gain={self.gain})")
    
    @property
    def frequency_mhz(self) -> float:
        """Frequency in MHz for convenience"""
        return self.frequency_hz / 1e6
    
    @property
    def frequency_khz(self) -> float:
        """Frequency in kHz for convenience"""
        return self.frequency_hz / 1e3
    
    def matches(self, other: 'StreamSpec', frequency_tolerance_hz: float = 1.0) -> bool:
        """
        Check if another StreamSpec is compatible (could share stream).
        
        Args:
            other: StreamSpec to compare
            frequency_tolerance_hz: How close frequencies must be
            
        Returns:
            True if specs are compatible
        """
        freq_match = abs(self.frequency_hz - other.frequency_hz) < frequency_tolerance_hz
        return (
            freq_match and
            self.preset.lower() == other.preset.lower() and
            self.sample_rate == other.sample_rate and
            self.agc == other.agc and
            abs(self.gain - other.gain) < 0.1 and
            _edges_match(self, other)
        )


@dataclass
class StreamRequest:
    """
    A request for a stream, including optional destination preferences.
    
    This wraps StreamSpec with additional parameters that don't affect
    stream identity but influence how the stream is set up.
    
    Attributes:
        spec: The content specification (what makes the stream unique)
        destination: Preferred multicast destination (address or address:port)
        description: Human-readable description for logging
    """
    spec: StreamSpec
    destination: Optional[str] = None
    description: str = ""
    
    @classmethod
    def create(
        cls,
        frequency_hz: float,
        preset: str = "iq",
        sample_rate: int = 16000,
        agc: bool = False,
        gain: float = 0.0,
        destination: Optional[str] = None,
        description: str = ""
    ) -> 'StreamRequest':
        """
        Convenience factory to create a StreamRequest.
        
        Args:
            frequency_hz: Center frequency in Hz
            preset: Demodulation mode
            sample_rate: Output sample rate in Hz
            agc: Enable AGC
            gain: Manual gain in dB
            destination: Preferred multicast destination
            description: Human-readable description
            
        Returns:
            StreamRequest instance
        """
        spec = StreamSpec(
            frequency_hz=frequency_hz,
            preset=preset,
            sample_rate=sample_rate,
            agc=agc,
            gain=gain
        )
        return cls(spec=spec, destination=destination, description=description)
    
    def __str__(self):
        dest = f" → {self.destination}" if self.destination else ""
        desc = f" ({self.description})" if self.description else ""
        return f"{self.spec}{dest}{desc}"

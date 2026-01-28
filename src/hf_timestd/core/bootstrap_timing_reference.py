"""
Bootstrap Timing Reference DTO

This module defines the data transfer object (DTO) for passing timing reference
information from the bootstrap service to the metrology service.

ARCHITECTURE (2026-01-28):
==========================
The bootstrap service establishes a mapping between RTP timestamps (GPSDO-governed)
and UTC(NIST) (derived from HF tone arrivals). This mapping is passed to the
metrology service via a well-defined DTO.

KEY CONCEPTS:
- RTP timestamps are the "steel ruler" - governed by GPSDO, monotonic, jitter-free
- UTC(NIST) is derived PURELY from HF tone arrivals
- NTP is NOT used in the timing pipeline - only as a hint for minute identification

BOOTSTRAP SEQUENCE:
1. GPSDO provides RTP sample counts (no time basis, just counts)
2. Bootstrap finds tone clusters, validates 1,440,000 sample recurrence
3. Offset = earliest_tone_rtp - geometric_delay (RTP at minute boundary)
4. We now have RELATIVE time - "minute N" from first detection
5. BCD/FSK decoding gives INITIAL ESTIMATE of which absolute minute
6. Initial Unix time is an ESTIMATE requiring further refinement:
   - Ionospheric delay is variable (not just geometric)
   - Multipath effects
   - Station-specific biases
7. Ongoing refinement: continuously compare tone arrivals to refine offset

THE DTO CONTAINS:
1. reference_rtp: RTP timestamp at a minute boundary (from tone detection)
2. minute_offset: Which minute relative to bootstrap start (0, 1, 2, ...)
3. decoded_minute: Absolute minute from BCD/FSK (None until decoded)
4. decoded_hour: Absolute hour from BCD/FSK (None until decoded)
5. reference_utc: ESTIMATED UTC (refined over time, None until BCD/FSK)
6. offset_uncertainty_ms: Current uncertainty in the offset estimate

CONVERSION (after BCD/FSK, with ongoing refinement):
  UTC(NIST) = reference_utc + (RTP - reference_rtp) / sample_rate
  (reference_utc is continuously refined based on tone arrivals)

USAGE IN METROLOGY SERVICE:
---------------------------
If time_confirmed (BCD/FSK decoded):
  - Use reference_rtp and reference_utc for RTP-to-UTC conversion
  - reference_utc is an estimate that improves over time
If not time_confirmed (pattern confirmed but not absolute time):
  - Can compute relative timing errors between tones
  - Cannot compute absolute UTC until BCD/FSK confirms
"""

import json
import logging
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Default state file location
DEFAULT_STATE_FILE = Path('/var/lib/timestd/state/bootstrap_timing_reference.json')


@dataclass
class BootstrapTimingReference:
    """
    Timing reference from bootstrap to metrology.
    
    This DTO provides the RTP-to-UTC mapping derived purely from tone arrivals.
    
    Attributes:
        locked: Whether bootstrap has achieved pattern lock
        lock_tier: Lock quality (NONE, PROVISIONAL, REFINED)
        
        reference_rtp: RTP timestamp at a minute boundary (from tone detection)
        sample_rate: Sample rate in Hz (for RTP-to-seconds conversion)
        
        # Relative timing (always available after PROVISIONAL lock)
        minute_offset: Which minute relative to bootstrap start (0, 1, 2, ...)
        
        # Absolute timing (only after BCD/FSK confirmation)
        decoded_hour: Hour from BCD/FSK decode (0-23), None if not confirmed
        decoded_minute: Minute from BCD/FSK decode (0-59), None if not confirmed
        time_confirmed: Whether BCD/FSK has confirmed absolute time
        
        # For backward compatibility (computed from decoded time if available)
        reference_utc: UTC timestamp of reference_rtp (None until time confirmed)
        
        uncertainty_ms: Estimated uncertainty in the reference (1-sigma)
        lock_time: ISO timestamp when lock was achieved
        
    Conversion (after time confirmation):
        UTC(NIST) = reference_utc + (RTP - reference_rtp) / sample_rate
    """
    # Lock status
    locked: bool = False
    lock_tier: str = 'NONE'  # NONE, PROVISIONAL, REFINED
    
    # The RTP reference point (from tone detection)
    reference_rtp: Optional[int] = None
    
    # Conversion parameter
    sample_rate: int = 24000
    
    # Relative timing (minute offset from bootstrap start)
    minute_offset: int = 0
    
    # Absolute timing from BCD/FSK decode
    decoded_hour: Optional[int] = None
    decoded_minute: Optional[int] = None
    time_confirmed: bool = False
    
    # UTC reference (computed from decoded time, for backward compatibility)
    reference_utc: Optional[float] = None
    
    # Quality indicators
    uncertainty_ms: Optional[float] = None
    lock_time: Optional[str] = None  # ISO timestamp
    
    def is_valid(self) -> bool:
        """Check if the reference is valid for relative timing."""
        return (
            self.locked and
            self.reference_rtp is not None and
            self.sample_rate > 0
        )
    
    def is_time_confirmed(self) -> bool:
        """Check if absolute time has been confirmed via BCD/FSK."""
        return (
            self.is_valid() and
            self.time_confirmed and
            self.decoded_hour is not None and
            self.decoded_minute is not None
        )
    
    def rtp_to_utc(self, rtp_timestamp: int) -> Optional[float]:
        """
        Convert an RTP timestamp to UTC(NIST).
        
        Requires time_confirmed=True (BCD/FSK decode completed).
        
        Args:
            rtp_timestamp: RTP timestamp to convert
            
        Returns:
            UTC(NIST) timestamp, or None if time not confirmed
        """
        if not self.is_time_confirmed() or self.reference_utc is None:
            return None
        return self.reference_utc + (rtp_timestamp - self.reference_rtp) / self.sample_rate
    
    def rtp_to_relative_seconds(self, rtp_timestamp: int) -> Optional[float]:
        """
        Convert an RTP timestamp to seconds relative to reference.
        
        Available even before BCD/FSK confirmation.
        
        Args:
            rtp_timestamp: RTP timestamp to convert
            
        Returns:
            Seconds from reference_rtp, or None if not valid
        """
        if not self.is_valid():
            return None
        return (rtp_timestamp - self.reference_rtp) / self.sample_rate
    
    def to_json(self) -> str:
        """Serialize to JSON."""
        return json.dumps(asdict(self), indent=2)
    
    @classmethod
    def from_json(cls, json_str: str) -> 'BootstrapTimingReference':
        """Deserialize from JSON."""
        data = json.loads(json_str)
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
    
    @classmethod
    def from_file(cls, path: Path = DEFAULT_STATE_FILE) -> Optional['BootstrapTimingReference']:
        """Load reference from file."""
        try:
            if path.exists():
                with open(path, 'r') as f:
                    return cls.from_json(f.read())
        except Exception as e:
            logger.warning(f"Failed to read bootstrap timing reference: {e}")
        return None
    
    def to_file(self, path: Path = DEFAULT_STATE_FILE) -> bool:
        """Write reference to file."""
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # Write atomically via temp file
            temp_path = path.with_suffix('.tmp')
            with open(temp_path, 'w') as f:
                f.write(self.to_json())
            temp_path.rename(path)
            return True
        except Exception as e:
            logger.error(f"Failed to write bootstrap timing reference: {e}")
            return False


class BootstrapTimingReferenceWriter:
    """
    Writer for bootstrap timing reference.
    
    Used by core_recorder to publish the timing reference for metrology service.
    """
    
    def __init__(self, state_file: Path = DEFAULT_STATE_FILE):
        self.state_file = state_file
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
    
    def write(
        self,
        reference_rtp: int,
        lock_tier: str = 'PROVISIONAL',
        uncertainty_ms: float = 5.0,
        sample_rate: int = 24000,
        minute_offset: int = 0,
        decoded_hour: Optional[int] = None,
        decoded_minute: Optional[int] = None,
        reference_utc: Optional[float] = None
    ) -> bool:
        """
        Write a timing reference.
        
        Args:
            reference_rtp: RTP timestamp at a minute boundary (from tone detection)
            lock_tier: Lock quality (PROVISIONAL or REFINED)
            uncertainty_ms: Estimated uncertainty
            sample_rate: Sample rate in Hz
            minute_offset: Which minute relative to bootstrap start
            decoded_hour: Hour from BCD/FSK decode (None if not confirmed)
            decoded_minute: Minute from BCD/FSK decode (None if not confirmed)
            reference_utc: Estimated UTC (None until BCD/FSK confirms)
            
        Returns:
            True if write succeeded
        """
        time_confirmed = decoded_hour is not None and decoded_minute is not None
        
        ref = BootstrapTimingReference(
            locked=True,
            lock_tier=lock_tier,
            reference_rtp=reference_rtp,
            sample_rate=sample_rate,
            minute_offset=minute_offset,
            decoded_hour=decoded_hour,
            decoded_minute=decoded_minute,
            time_confirmed=time_confirmed,
            reference_utc=reference_utc,
            uncertainty_ms=uncertainty_ms,
            lock_time=datetime.now(timezone.utc).isoformat()
        )
        
        success = ref.to_file(self.state_file)
        if success:
            if time_confirmed:
                logger.info(
                    f"[BOOTSTRAP_REF] Wrote timing reference: "
                    f"RTP={reference_rtp} @ UTC={reference_utc:.3f} "
                    f"(tier={lock_tier}, time_confirmed, ±{uncertainty_ms:.1f}ms)"
                )
            else:
                logger.info(
                    f"[BOOTSTRAP_REF] Wrote timing reference: "
                    f"RTP={reference_rtp}, minute_offset={minute_offset} "
                    f"(tier={lock_tier}, awaiting BCD/FSK, ±{uncertainty_ms:.1f}ms)"
                )
        return success
    
    def clear(self) -> bool:
        """Clear the timing reference (bootstrap lost lock)."""
        ref = BootstrapTimingReference(locked=False, lock_tier='NONE')
        return ref.to_file(self.state_file)

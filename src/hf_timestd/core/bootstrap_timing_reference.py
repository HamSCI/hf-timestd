"""
Bootstrap Timing Reference DTO

This module defines the data transfer object (DTO) for passing timing reference
information from the bootstrap service to the metrology service.

ARCHITECTURE (2026-01-27):
==========================
The bootstrap service establishes a mapping between RTP timestamps (GPSDO-governed)
and UTC(NIST) (derived from HF tone arrivals). This mapping is passed to the
metrology service via a well-defined DTO.

KEY CONCEPTS:
- RTP timestamps are the "steel ruler" - governed by GPSDO, monotonic, jitter-free
- UTC(NIST) is derived from HF tone arrivals, NOT from NTP
- NTP is used only as a hint for minute identification, not as ground truth

THE DTO CONTAINS:
1. A consistent (RTP, UTC) reference pair:
   - reference_rtp: RTP timestamp at a known minute boundary
   - reference_utc: UTC(NIST) timestamp of that same minute boundary
   
2. Conversion formula:
   UTC(NIST) = reference_utc + (RTP - reference_rtp) / sample_rate

3. Quality indicators:
   - lock_tier: PROVISIONAL or REFINED
   - uncertainty_ms: Estimated uncertainty in the reference

USAGE IN METROLOGY SERVICE:
---------------------------
Given a buffer with:
  - start_rtp_timestamp: RTP when buffer started
  - start_system_time: NTP-derived time when buffer started (for fallback only)

The metrology service converts RTP to UTC(NIST):
  buffer_utc = reference_utc + (start_rtp_timestamp - reference_rtp) / sample_rate

This buffer_utc is then used as the timing reference for tone detection.
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
    
    This DTO provides everything needed to convert RTP timestamps to UTC(NIST).
    
    Attributes:
        locked: Whether bootstrap has achieved lock
        lock_tier: Lock quality (NONE, PROVISIONAL, REFINED)
        
        reference_rtp: RTP timestamp at a known minute boundary
        reference_utc: UTC(NIST) timestamp of that minute boundary
        sample_rate: Sample rate in Hz (for RTP-to-seconds conversion)
        
        uncertainty_ms: Estimated uncertainty in the reference (1-sigma)
        lock_time: ISO timestamp when lock was achieved
        
    Conversion:
        UTC(NIST) = reference_utc + (RTP - reference_rtp) / sample_rate
    """
    # Lock status
    locked: bool = False
    lock_tier: str = 'NONE'  # NONE, PROVISIONAL, REFINED
    
    # The reference pair: (RTP, UTC) at the same instant
    # This is the core of the handoff - a consistent mapping point
    reference_rtp: Optional[int] = None
    reference_utc: Optional[float] = None
    
    # Conversion parameter
    sample_rate: int = 24000
    
    # Quality indicators
    uncertainty_ms: Optional[float] = None
    lock_time: Optional[str] = None  # ISO timestamp
    
    def is_valid(self) -> bool:
        """Check if the reference is valid for use."""
        return (
            self.locked and
            self.reference_rtp is not None and
            self.reference_utc is not None and
            self.sample_rate > 0
        )
    
    def rtp_to_utc(self, rtp_timestamp: int) -> Optional[float]:
        """
        Convert an RTP timestamp to UTC(NIST).
        
        Args:
            rtp_timestamp: RTP timestamp to convert
            
        Returns:
            UTC(NIST) timestamp, or None if reference is not valid
        """
        if not self.is_valid():
            return None
        return self.reference_utc + (rtp_timestamp - self.reference_rtp) / self.sample_rate
    
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
        reference_utc: float,
        lock_tier: str = 'PROVISIONAL',
        uncertainty_ms: float = 5.0,
        sample_rate: int = 24000
    ) -> bool:
        """
        Write a timing reference.
        
        Args:
            reference_rtp: RTP timestamp at a known minute boundary
            reference_utc: UTC(NIST) timestamp of that minute boundary
            lock_tier: Lock quality (PROVISIONAL or REFINED)
            uncertainty_ms: Estimated uncertainty
            sample_rate: Sample rate in Hz
            
        Returns:
            True if write succeeded
        """
        ref = BootstrapTimingReference(
            locked=True,
            lock_tier=lock_tier,
            reference_rtp=reference_rtp,
            reference_utc=reference_utc,
            sample_rate=sample_rate,
            uncertainty_ms=uncertainty_ms,
            lock_time=datetime.now(timezone.utc).isoformat()
        )
        
        success = ref.to_file(self.state_file)
        if success:
            logger.info(
                f"[BOOTSTRAP_REF] Wrote timing reference: "
                f"RTP={reference_rtp} @ UTC={reference_utc:.3f} "
                f"(tier={lock_tier}, ±{uncertainty_ms:.1f}ms)"
            )
        return success
    
    def clear(self) -> bool:
        """Clear the timing reference (bootstrap lost lock)."""
        ref = BootstrapTimingReference(locked=False, lock_tier='NONE')
        return ref.to_file(self.state_file)

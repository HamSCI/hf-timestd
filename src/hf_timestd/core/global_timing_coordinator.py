"""
Global Timing Coordinator

Aggregates tone detections from all channels and runs the GlobalDifferentialSolver
to produce a single, verified UTC(NIST) back-calculation.

Architecture:
- Each analytics service writes detections to: {data_root}/shared/detections/{minute}.json
- This coordinator polls the shared directory and runs global solve
- Results written to: {data_root}/shared/global_timing.json

The shared detection file format:
{
    "minute_utc": "2025-12-03T12:00:00Z",
    "minute_boundary_rtp": 123456789,
    "sample_rate": 20000,
    "detections": [
        {
            "channel": "WWV 10 MHz",
            "station": "WWV",
            "frequency_mhz": 10.0,
            "arrival_rtp": 123456900,
            "snr_db": 15.2,
            "timing_ms": 5.55,
            "timestamp": "2025-12-03T12:00:05.123Z"
        },
        ...
    ]
}
"""

import json
import logging
import time
import math
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, List, Optional
from dataclasses import dataclass, asdict

from .differential_time_solver import GlobalDifferentialSolver, GlobalSolveResult
from .transmission_time_solver import grid_to_latlon

logger = logging.getLogger(__name__)


@dataclass
class ChannelDetection:
    """Detection from a single channel."""
    channel: str
    station: str
    frequency_mhz: float
    arrival_rtp: int
    snr_db: float
    timing_ms: float
    timestamp: str


@dataclass  
class GlobalTimingResult:
    """Result from global timing solve."""
    minute_utc: str
    clock_error_ms: float
    uncertainty_ms: float
    confidence: float
    quality_grade: str
    verified: bool
    n_channels: int
    n_pairs: int
    pair_consistency_ms: float
    mode_assignments: List[Dict]
    last_updated: str


class GlobalTimingCoordinator:
    """
    Coordinates timing across all channels using shared detection files.
    """
    
    def __init__(
        self,
        shared_dir: Path,
        grid_square: str,
        sample_rate: int = 20000,
        min_channels: int = 2,
        max_age_seconds: int = 120
    ):
        """
        Initialize coordinator.
        
        Args:
            shared_dir: Directory for shared detection files
            grid_square: Receiver location (e.g., "EM38ww")
            sample_rate: Audio sample rate
            min_channels: Minimum channels required for global solve
            max_age_seconds: Max age of detections to consider
        """
        self.shared_dir = Path(shared_dir)
        self.detections_dir = self.shared_dir / "detections"
        self.results_file = self.shared_dir / "global_timing.json"
        
        self.sample_rate = sample_rate
        self.min_channels = min_channels
        self.max_age_seconds = max_age_seconds
        
        # Create directories
        self.detections_dir.mkdir(parents=True, exist_ok=True)
        
        # Initialize solver
        lat, lon = grid_to_latlon(grid_square)
        self.solver = GlobalDifferentialSolver(lat, lon)
        
        logger.info(f"GlobalTimingCoordinator initialized")
        logger.info(f"  Shared dir: {self.shared_dir}")
        logger.info(f"  Grid: {grid_square} → ({lat:.2f}, {lon:.2f})")
        logger.info(f"  Min channels: {min_channels}")
    
    def write_detection(
        self,
        minute_utc: datetime,
        channel: str,
        station: str,
        frequency_mhz: float,
        timing_error_ms: float,
        snr_db: float = 0.0
    ) -> None:
        """
        Write a detection from one channel to the shared file.
        
        Called by each analytics service when it detects a tone.
        
        Args:
            minute_utc: UTC minute of detection
            channel: Channel name (e.g., "WWV 10 MHz")
            station: Station identifier ("WWV", "WWVH", "CHU")
            frequency_mhz: Signal frequency
            timing_error_ms: Timing error from detector (arrival - expected)
            snr_db: Signal-to-noise ratio
        """
        minute_str = minute_utc.strftime("%Y%m%d_%H%M")
        detection_file = self.detections_dir / f"{minute_str}.json"
        
        detection = {
            "channel": channel,
            "station": station,
            "frequency_mhz": frequency_mhz,
            "timing_error_ms": timing_error_ms,
            "snr_db": snr_db,
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
        
        # Read existing or create new
        if detection_file.exists():
            try:
                with open(detection_file) as f:
                    data = json.load(f)
            except (json.JSONDecodeError, IOError):
                data = None
        else:
            data = None
        
        if data is None:
            data = {
                "minute_utc": minute_utc.isoformat(),
                "detections": []
            }
        
        # Update or add detection for this channel
        existing_idx = None
        for i, d in enumerate(data["detections"]):
            if d["channel"] == channel:
                existing_idx = i
                break
        
        if existing_idx is not None:
            data["detections"][existing_idx] = detection
        else:
            data["detections"].append(detection)
        
        # Write atomically
        temp_file = detection_file.with_suffix('.tmp')
        with open(temp_file, 'w') as f:
            json.dump(data, f, indent=2)
        temp_file.rename(detection_file)
        
        logger.debug(f"Wrote detection: {channel} {station} {frequency_mhz}MHz → {timing_error_ms:.2f}ms")
    
    def solve_minute(self, minute_utc: datetime) -> Optional[GlobalTimingResult]:
        """
        Run global solve for a specific minute.
        
        Returns None if insufficient data.
        """
        minute_str = minute_utc.strftime("%Y%m%d_%H%M")
        detection_file = self.detections_dir / f"{minute_str}.json"
        
        if not detection_file.exists():
            return None
        
        try:
            with open(detection_file) as f:
                data = json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            logger.warning(f"Failed to read {detection_file}: {e}")
            return None
        
        detections = data.get("detections", [])
        
        if len(detections) < self.min_channels:
            logger.debug(f"Only {len(detections)} channels, need {self.min_channels}")
            return None
        
        # Build observations for solver
        # timing_error_ms = arrival - expected, so we use it directly as timing
        # We fake RTP values: arrival_rtp = timing_error_ms * sample_rate / 1000
        # with minute_boundary_rtp = 0
        observations = []
        for det in detections:
            timing_ms = det.get("timing_error_ms", det.get("timing_ms", 0))
            arrival_rtp = int(timing_ms * self.sample_rate / 1000)
            observations.append({
                "station": det["station"],
                "frequency_mhz": det["frequency_mhz"],
                "arrival_rtp": arrival_rtp
            })
        
        # Run global solve with fake minute_boundary_rtp=0
        result = self.solver.solve_global(
            observations=observations,
            minute_boundary_rtp=0,
            sample_rate=self.sample_rate
        )
        
        if result.confidence < 0.1:
            logger.warning(f"Global solve confidence too low: {result.confidence:.0%}")
            return None
        
        global_result = GlobalTimingResult(
            minute_utc=minute_utc.isoformat(),
            clock_error_ms=result.clock_error_ms,
            uncertainty_ms=result.uncertainty_ms,
            confidence=result.confidence,
            quality_grade=result.quality_grade,
            verified=result.verified,
            n_channels=result.n_observations,
            n_pairs=result.n_pairs,
            pair_consistency_ms=result.pair_consistency_ms,
            mode_assignments=result.mode_assignments,
            last_updated=datetime.now(timezone.utc).isoformat()
        )
        
        logger.info(
            f"🌐 GLOBAL SOLVE: {result.n_observations} channels, "
            f"clock_error={result.clock_error_ms:+.3f}ms, "
            f"confidence={result.confidence:.0%}, grade={result.quality_grade}"
        )
        
        return global_result
    
    def solve_and_save(self, minute_utc: datetime) -> Optional[GlobalTimingResult]:
        """
        Run global solve and save result to shared file.
        """
        result = self.solve_minute(minute_utc)
        
        if result is None:
            return None
        
        # Read existing results or create new
        if self.results_file.exists():
            try:
                with open(self.results_file) as f:
                    results_data = json.load(f)
            except (json.JSONDecodeError, IOError):
                results_data = {"results": [], "latest": None}
        else:
            results_data = {"results": [], "latest": None}
        
        # Add new result (keep last 60 minutes)
        result_dict = asdict(result)
        results_data["results"].append(result_dict)
        results_data["results"] = results_data["results"][-60:]
        results_data["latest"] = result_dict
        
        # Write atomically
        temp_file = self.results_file.with_suffix('.tmp')
        with open(temp_file, 'w') as f:
            json.dump(results_data, f, indent=2)
        temp_file.rename(self.results_file)
        
        return result
    
    def get_latest_result(self) -> Optional[GlobalTimingResult]:
        """Get the most recent global timing result."""
        if not self.results_file.exists():
            return None
        
        try:
            with open(self.results_file) as f:
                data = json.load(f)
            latest = data.get("latest")
            if latest:
                return GlobalTimingResult(**latest)
        except (json.JSONDecodeError, IOError, TypeError):
            pass
        
        return None
    
    def cleanup_old_files(self, max_age_hours: int = 24) -> int:
        """Remove detection files older than max_age_hours."""
        cutoff = time.time() - (max_age_hours * 3600)
        removed = 0
        
        for f in self.detections_dir.glob("*.json"):
            if f.stat().st_mtime < cutoff:
                f.unlink()
                removed += 1
        
        if removed > 0:
            logger.info(f"Cleaned up {removed} old detection files")
        
        return removed


def create_coordinator(data_root: Path, grid_square: str) -> GlobalTimingCoordinator:
    """Factory function to create a coordinator."""
    shared_dir = Path(data_root) / "shared"
    return GlobalTimingCoordinator(
        shared_dir=shared_dir,
        grid_square=grid_square
    )

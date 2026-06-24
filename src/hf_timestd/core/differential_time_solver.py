#!/usr/bin/env python3
"""
Differential Time Solver - Clock-Error-Immune UTC(NIST) Back-Calculation

This module implements the CORRECT approach to HF time transfer by using
DIFFERENTIAL measurements between WWV and WWVH that cancel out local clock error.

The Key Insight:
----------------
For a single receiver observing both WWV and WWVH simultaneously:

    timing_error_wwv  = T_prop_wwv  + clock_error
    timing_error_wwvh = T_prop_wwvh + clock_error

Subtracting:
    Δ_observed = timing_error_wwv - timing_error_wwvh
               = T_prop_wwv - T_prop_wwvh   ← Clock error CANCELS!

This differential is IMMUNE to local clock error and can be computed purely
from RTP sample positions (no UTC mapping required).

Mode Identification Strategy:
----------------------------
1. Calculate expected differential for each (mode_wwv, mode_wwvh) pair
2. Match Δ_observed to expected differentials
3. Select mode pair with best match
4. Use identified modes to compute individual T_prop values
5. Derive clock_error = timing_error_wwv - T_prop_wwv (or equivalently from WWVH)
6. This clock_error IS the UTC(NIST) offset we seek!

Inter-Frequency Consistency:
----------------------------
Different frequencies may use different modes, but:
- All should yield the SAME clock_error
- If they don't, we can detect mode misidentification
- Weighted average with outlier rejection gives robust estimate

CHU Support:
-----------
CHU (Ottawa, 3330/7850/14670 kHz) is included for:
- Single-station mode identification
- WWV-CHU or WWVH-CHU differential (cross-frequency)

Global Multi-Channel Solving:
----------------------------
With N detections from any stations/frequencies, we have N*(N-1)/2 differential
pairs. ALL pairs must be consistent with the SAME clock_error. This over-constrains
the problem and enables robust mode identification.

Example with 4 WWV frequencies + 3 CHU frequencies = 7 detections → 21 pairs!

Usage:
------
    solver = DifferentialTimeSolver(receiver_lat=39.0, receiver_lon=-94.5)
    
    result = solver.solve_differential(
        wwv_arrival_rtp=12345000,
        wwvh_arrival_rtp=12346000,  
        sample_rate=20000,
        frequency_mhz=10.0,
        delay_spread_ms=0.5,
        doppler_std_hz=0.1
    )
    
    print(f"WWV Mode: {result.wwv_mode}")
    print(f"WWVH Mode: {result.wwvh_mode}")
    print(f"Clock Error: {result.clock_error_ms:.3f} ms")
    print(f"Confidence: {result.confidence:.1%}")
"""

import math
import logging
from typing import Optional, List, Dict, Tuple
from dataclasses import dataclass, field
from enum import Enum

from hamsci_dsp.geometry import great_circle_km

# Issue 4.1 Fix (2025-12-07): Import coordinates from single source of truth
from .wwv_constants import STATION_LOCATIONS

logger = logging.getLogger(__name__)

# Physical constants
SPEED_OF_LIGHT_KM_S = 299792.458
EARTH_RADIUS_KM = 6371.0

# Ionospheric layer heights
E_LAYER_HEIGHT_KM = 110.0
F2_LAYER_HEIGHT_KM = 300.0
F2_NIGHT_HEIGHT_KM = 350.0

# Station locations - Issue 4.1 Fix: Now imported from wwv_constants.py
STATIONS = STATION_LOCATIONS  # NIST/NRC verified coordinates


class PropagationMode(Enum):
    """Discrete propagation modes"""
    ONE_HOP_E = "1E"
    ONE_HOP_F = "1F"
    TWO_HOP_F = "2F"
    THREE_HOP_F = "3F"
    UNKNOWN = "UNK"


@dataclass
class ModePairCandidate:
    """A candidate (WWV mode, WWVH mode) pair"""
    wwv_mode: PropagationMode
    wwvh_mode: PropagationMode
    wwv_delay_ms: float
    wwvh_delay_ms: float
    differential_ms: float  # wwv - wwvh
    plausibility: float  # 0-1


@dataclass
class DifferentialResult:
    """Result of differential time solving"""
    # Identified modes
    wwv_mode: PropagationMode
    wwvh_mode: PropagationMode
    wwv_n_hops: int
    wwvh_n_hops: int
    
    # Propagation delays
    wwv_delay_ms: float
    wwvh_delay_ms: float
    differential_delay_ms: float  # Observed
    expected_differential_ms: float  # From mode pair
    
    # The prize: clock error (UTC offset)
    clock_error_ms: float  # = timing_error - T_prop
    clock_error_verified: bool  # WWV and WWVH agree?
    
    # Confidence metrics
    confidence: float
    differential_residual_ms: float  # |observed - expected|
    wwv_wwvh_agreement_ms: float  # Cross-check error
    mode_separation_ms: float  # Gap to next-best pair
    
    # Diagnostics
    candidates_evaluated: int
    ambiguous: bool


@dataclass
class MultiFrequencyResult:
    """Combined result from multiple frequencies"""
    # Best estimate
    clock_error_ms: float
    uncertainty_ms: float
    
    # Quality
    confidence: float
    consistency: float  # How well frequencies agree
    n_frequencies: int
    
    # Per-frequency results
    frequency_results: List[Dict]
    
    # Verification
    verified: bool
    quality_grade: str  # A/B/C/D


class DifferentialTimeSolver:
    """
    Solve for UTC(NIST) using differential WWV/WWVH measurements.
    
    This approach is IMMUNE to local clock error because we measure
    the DIFFERENCE in arrival times, not absolute times.
    """
    
    def __init__(
        self,
        receiver_lat: float,
        receiver_lon: float,
        f_layer_height_km: float = F2_LAYER_HEIGHT_KM
    ):
        """
        Initialize with receiver location.
        
        Args:
            receiver_lat: Receiver latitude (degrees)
            receiver_lon: Receiver longitude (degrees) 
            f_layer_height_km: Assumed F-layer height
        """
        self.receiver_lat = receiver_lat
        self.receiver_lon = receiver_lon
        self.f_layer_height_km = f_layer_height_km
        
        # Pre-calculate distances
        self.distances = {}
        for station, loc in STATIONS.items():
            self.distances[station] = self._great_circle_distance(
                receiver_lat, receiver_lon, loc['lat'], loc['lon']
            )
            logger.info(f"Distance to {station}: {self.distances[station]:.1f} km")
        
        # Pre-calculate mode delays for each station
        self.mode_delays = {}
        for station in STATIONS:
            self.mode_delays[station] = self._calculate_all_modes(station)
            
        # Expected differential for geographic predictor comparison
        self.expected_differential_ms = (
            self._calculate_mode_delay(self.distances['WWV'], 1, self.f_layer_height_km) -
            self._calculate_mode_delay(self.distances['WWVH'], 1, self.f_layer_height_km)
        )
        logger.info(f"Expected 1F differential (WWV-WWVH): {self.expected_differential_ms:.2f} ms")
    
    def _great_circle_distance(
        self, lat1: float, lon1: float, lat2: float, lon2: float
    ) -> float:
        """Delegates to hamsci_dsp.geometry.great_circle_km (geodesic WGS-84)."""
        return great_circle_km(lat1, lon1, lat2, lon2)
    
    def _calculate_mode_delay(
        self,
        ground_distance_km: float,
        n_hops: int,
        layer_height_km: float
    ) -> float:
        """Calculate propagation delay for N-hop mode."""
        if n_hops == 0:
            path_length = ground_distance_km
        else:
            hop_distance = ground_distance_km / n_hops
            half_hop = hop_distance / 2
            slant_range = math.sqrt(half_hop ** 2 + layer_height_km ** 2)
            path_length = 2 * slant_range * n_hops
        
        delay_ms = (path_length / SPEED_OF_LIGHT_KM_S) * 1000
        return delay_ms
    
    def _calculate_all_modes(self, station: str) -> Dict[PropagationMode, Dict]:
        """Calculate delays for all modes to a station."""
        distance = self.distances[station]
        modes = {}
        
        # E-layer (only viable for shorter paths, <2500 km)
        if distance < 2500:
            delay = self._calculate_mode_delay(distance, 1, E_LAYER_HEIGHT_KM)
            modes[PropagationMode.ONE_HOP_E] = {
                'delay_ms': delay,
                'n_hops': 1,
                'layer': 'E',
                'plausibility': 1.0 if distance < 1500 else 0.7
            }
        
        # F-layer modes with distance-based plausibility
        # Key insight: Use the minimum sensible hop count for the distance
        # Single hop up to ~3000 km, 2-hop for 2000-6000 km, 3-hop for >4500 km
        
        # 1-hop F: Most plausible for short-medium paths
        if distance <= 4000:
            delay = self._calculate_mode_delay(distance, 1, self.f_layer_height_km)
            # Plausibility decreases as distance approaches max single-hop range
            if distance < 2000:
                plausibility = 1.0  # Very likely
            elif distance < 3000:
                plausibility = 0.9
            elif distance < 3500:
                plausibility = 0.7
            else:
                plausibility = 0.5  # Marginal
            modes[PropagationMode.ONE_HOP_F] = {
                'delay_ms': delay,
                'n_hops': 1,
                'layer': 'F',
                'plausibility': plausibility
            }
        
        # 2-hop F: Plausible for medium-long paths
        if 2000 <= distance <= 8000:
            delay = self._calculate_mode_delay(distance, 2, self.f_layer_height_km)
            if distance < 3000:
                plausibility = 0.3  # Unlikely for short paths
            elif distance < 4500:
                plausibility = 0.7
            elif distance < 6000:
                plausibility = 1.0  # Sweet spot
            else:
                plausibility = 0.8
            modes[PropagationMode.TWO_HOP_F] = {
                'delay_ms': delay,
                'n_hops': 2,
                'layer': 'F',
                'plausibility': plausibility
            }
        
        # 3-hop F: Only for long paths
        if distance >= 4500:
            delay = self._calculate_mode_delay(distance, 3, self.f_layer_height_km)
            if distance < 5500:
                plausibility = 0.4  # Marginal
            elif distance < 7000:
                plausibility = 0.7
            else:
                plausibility = 1.0  # Likely for very long paths
            modes[PropagationMode.THREE_HOP_F] = {
                'delay_ms': delay,
                'n_hops': 3,
                'layer': 'F',
                'plausibility': plausibility
            }
        
        return modes
    
    def solve_differential(
        self,
        wwv_arrival_rtp: int,
        wwvh_arrival_rtp: int,
        sample_rate: int,
        frequency_mhz: float,
        delay_spread_ms: float = 0.5,
        doppler_std_hz: float = 0.1
    ) -> DifferentialResult:
        """
        Solve for UTC offset using differential WWV/WWVH arrival times.
        
        The key innovation: we use (wwv_arrival - wwvh_arrival) which is
        INDEPENDENT of local clock error!
        
        Args:
            wwv_arrival_rtp: RTP sample index of WWV tone detection
            wwvh_arrival_rtp: RTP sample index of WWVH tone detection
            sample_rate: Audio sample rate (Hz)
            frequency_mhz: Signal frequency
            delay_spread_ms: Observed multipath spread (quality indicator)
            doppler_std_hz: Doppler stability (quality indicator)
            
        Returns:
            DifferentialResult with identified modes and clock error
        """
        # Calculate observed differential (in ms)
        # CRITICAL: This is clock-error-free!
        differential_samples = wwv_arrival_rtp - wwvh_arrival_rtp
        observed_differential_ms = (differential_samples / sample_rate) * 1000
        
        logger.info(f"Observed differential (WWV-WWVH): {observed_differential_ms:.3f} ms")
        
        # Generate all (WWV mode, WWVH mode) pair candidates
        candidates = self._generate_mode_pairs()
        
        if not candidates:
            return self._no_solution()
        
        # Score each candidate pair
        scored = []
        for cand in candidates:
            residual = abs(observed_differential_ms - cand.differential_ms)
            
            # Base score from residual match
            if residual > 2.0:
                score = 0.1
            elif residual > 1.0:
                score = 0.4
            elif residual > 0.5:
                score = 0.7
            else:
                score = 1.0
            
            # Plausibility factor
            score *= cand.plausibility
            
            # Penalty for high delay spread (suggests we might be seeing wrong peak)
            if delay_spread_ms > 1.0:
                score *= 0.7
            
            # Penalty for high Doppler (unstable path)
            if doppler_std_hz > 0.5:
                score *= 0.8
            
            scored.append((score, residual, cand))
        
        # Sort by score (best first)
        scored.sort(key=lambda x: x[0], reverse=True)
        
        best_score, best_residual, best_cand = scored[0]
        
        # Check for ambiguity
        ambiguous = False
        mode_separation_ms = 10.0
        if len(scored) > 1:
            second_score, second_residual, second_cand = scored[1]
            mode_separation_ms = abs(best_cand.differential_ms - second_cand.differential_ms)
            if mode_separation_ms < 0.5:
                ambiguous = True
        
        # Calculate clock error from BOTH stations (should agree)
        # clock_error = observed_timing - T_prop
        # But we only have differential... need reference
        
        # We can derive clock error if we know the RTP timestamp of the second boundary.
        # For now, we output the differential result which is the first step.
        # The actual clock error calculation needs an anchor point.
        
        # Calculate confidence
        confidence = best_score * min(1.0, mode_separation_ms / 0.5)
        if ambiguous:
            confidence *= 0.5  # Heavy penalty for ambiguity
        
        return DifferentialResult(
            wwv_mode=best_cand.wwv_mode,
            wwvh_mode=best_cand.wwvh_mode,
            wwv_n_hops=self.mode_delays['WWV'].get(best_cand.wwv_mode, {}).get('n_hops', 0),
            wwvh_n_hops=self.mode_delays['WWVH'].get(best_cand.wwvh_mode, {}).get('n_hops', 0),
            wwv_delay_ms=best_cand.wwv_delay_ms,
            wwvh_delay_ms=best_cand.wwvh_delay_ms,
            differential_delay_ms=observed_differential_ms,
            expected_differential_ms=best_cand.differential_ms,
            clock_error_ms=0.0,  # Needs anchor - see solve_with_anchor()
            clock_error_verified=False,
            confidence=confidence,
            differential_residual_ms=best_residual,
            wwv_wwvh_agreement_ms=0.0,  # Needs anchor
            mode_separation_ms=mode_separation_ms,
            candidates_evaluated=len(candidates),
            ambiguous=ambiguous
        )
    
    def solve_with_anchor(
        self,
        wwv_arrival_rtp: int,
        wwvh_arrival_rtp: int,
        minute_boundary_rtp: int,
        sample_rate: int,
        frequency_mhz: float,
        delay_spread_ms: float = 0.5,
        doppler_std_hz: float = 0.1
    ) -> DifferentialResult:
        """
        Full solution using differential mode identification + anchor.
        
        Strategy:
        1. Use differential to identify modes (clock-error-immune)
        2. Use identified mode to compute T_prop for one station
        3. Derive clock_error = (arrival - boundary) - T_prop
        4. Cross-validate with other station
        
        Args:
            wwv_arrival_rtp: RTP of WWV tone arrival
            wwvh_arrival_rtp: RTP of WWVH tone arrival
            minute_boundary_rtp: RTP timestamp of expected minute boundary
                                (from RTP counter, NOT from wall clock)
            sample_rate: Audio sample rate
            frequency_mhz: Signal frequency
            delay_spread_ms: Multipath indicator
            doppler_std_hz: Path stability indicator
            
        Returns:
            DifferentialResult with verified clock error
        """
        # Step 1: Differential mode identification
        diff_result = self.solve_differential(
            wwv_arrival_rtp, wwvh_arrival_rtp,
            sample_rate, frequency_mhz,
            delay_spread_ms, doppler_std_hz
        )
        
        if diff_result.confidence < 0.1:
            return diff_result
        
        # Step 2: Calculate observed timing for each station (from RTP anchor)
        wwv_timing_samples = wwv_arrival_rtp - minute_boundary_rtp
        wwvh_timing_samples = wwvh_arrival_rtp - minute_boundary_rtp
        
        wwv_timing_ms = (wwv_timing_samples / sample_rate) * 1000
        wwvh_timing_ms = (wwvh_timing_samples / sample_rate) * 1000
        
        # Step 3: Derive clock error from each station
        # clock_error = observed_timing - T_prop
        wwv_clock_error = wwv_timing_ms - diff_result.wwv_delay_ms
        wwvh_clock_error = wwvh_timing_ms - diff_result.wwvh_delay_ms
        
        # Step 4: Cross-validate
        agreement_ms = abs(wwv_clock_error - wwvh_clock_error)
        
        # If WWV and WWVH give different clock errors, mode ID was wrong
        if agreement_ms < 1.0:
            verified = True
            # Average the two estimates
            clock_error_ms = (wwv_clock_error + wwvh_clock_error) / 2
            # Boost confidence
            confidence = min(1.0, diff_result.confidence * 1.3)
        elif agreement_ms < 2.0:
            verified = False
            clock_error_ms = wwv_clock_error  # Trust WWV (continental)
            confidence = diff_result.confidence * 0.7
        else:
            verified = False
            clock_error_ms = wwv_clock_error
            confidence = diff_result.confidence * 0.3
            logger.warning(
                f"WWV/WWVH clock error mismatch: WWV={wwv_clock_error:.2f}ms, "
                f"WWVH={wwvh_clock_error:.2f}ms, agreement={agreement_ms:.2f}ms"
            )
        
        # Update result
        return DifferentialResult(
            wwv_mode=diff_result.wwv_mode,
            wwvh_mode=diff_result.wwvh_mode,
            wwv_n_hops=diff_result.wwv_n_hops,
            wwvh_n_hops=diff_result.wwvh_n_hops,
            wwv_delay_ms=diff_result.wwv_delay_ms,
            wwvh_delay_ms=diff_result.wwvh_delay_ms,
            differential_delay_ms=diff_result.differential_delay_ms,
            expected_differential_ms=diff_result.expected_differential_ms,
            clock_error_ms=clock_error_ms,
            clock_error_verified=verified,
            confidence=confidence,
            differential_residual_ms=diff_result.differential_residual_ms,
            wwv_wwvh_agreement_ms=agreement_ms,
            mode_separation_ms=diff_result.mode_separation_ms,
            candidates_evaluated=diff_result.candidates_evaluated,
            ambiguous=diff_result.ambiguous
        )
    
    def solve_single_station(
        self,
        station: str,
        arrival_rtp: int,
        minute_boundary_rtp: int,
        sample_rate: int,
        frequency_mhz: float,
        delay_spread_ms: float = 0.5,
        doppler_std_hz: float = 0.1
    ) -> Dict:
        """
        Single-station solve for CHU or when only one station detected.
        
        WARNING: This is contaminated by clock error and cannot be verified!
        Use differential methods when possible.
        
        Args:
            station: 'WWV', 'WWVH', or 'CHU'
            arrival_rtp: RTP sample index of tone arrival
            minute_boundary_rtp: RTP at expected second boundary
            sample_rate: Audio sample rate
            frequency_mhz: Signal frequency
            delay_spread_ms: Multipath indicator
            doppler_std_hz: Path stability
            
        Returns:
            Dict with mode identification and unverified clock error
        """
        if station not in self.mode_delays:
            logger.warning(f"Unknown station: {station}")
            return {'confidence': 0, 'verified': False}
        
        # Calculate observed timing
        timing_samples = arrival_rtp - minute_boundary_rtp
        timing_ms = (timing_samples / sample_rate) * 1000
        
        # Score each mode candidate
        modes = self.mode_delays[station]
        scored = []
        
        for mode, info in modes.items():
            residual = abs(timing_ms - info['delay_ms'])
            
            # Base score from residual
            if residual > 2.0:
                score = 0.1
            elif residual > 1.0:
                score = 0.4
            elif residual > 0.5:
                score = 0.7
            else:
                score = 1.0
            
            # Apply plausibility
            score *= info.get('plausibility', 0.5)
            
            # Multipath penalty for single-hop
            if delay_spread_ms > 1.0 and info['n_hops'] == 1:
                score *= 0.7
            
            scored.append((score, residual, mode, info))
        
        if not scored:
            return {'confidence': 0, 'verified': False}
        
        # Best mode
        scored.sort(key=lambda x: x[0], reverse=True)
        best_score, best_residual, best_mode, best_info = scored[0]
        
        # Mode separation for confidence
        mode_sep = 10.0
        if len(scored) > 1:
            mode_sep = abs(best_info['delay_ms'] - scored[1][3]['delay_ms'])
        
        # Clock error (UNVERIFIABLE)
        clock_error_ms = timing_ms - best_info['delay_ms']
        
        # Confidence capped since single-station can't verify
        confidence = min(0.3, best_score * min(1.0, mode_sep / 0.5))
        
        return {
            'station': station,
            'mode': best_mode.value,
            'n_hops': best_info['n_hops'],
            'delay_ms': best_info['delay_ms'],
            'clock_error_ms': clock_error_ms,
            'confidence': confidence,
            'verified': False,  # Single-station cannot verify!
            'warning': 'Single-station result is contaminated by clock error'
        }
    
    def _generate_mode_pairs_generic(
        self, 
        station_a: str, 
        station_b: str
    ) -> List[ModePairCandidate]:
        """Generate all valid mode combinations for any two stations."""
        candidates = []
        
        if station_a not in self.mode_delays or station_b not in self.mode_delays:
            return candidates
        
        for mode_a, info_a in self.mode_delays[station_a].items():
            for mode_b, info_b in self.mode_delays[station_b].items():
                delay_a = info_a['delay_ms']
                delay_b = info_b['delay_ms']
                differential = delay_a - delay_b
                
                # Individual plausibilities
                plaus_a = info_a.get('plausibility', 0.5)
                plaus_b = info_b.get('plausibility', 0.5)
                
                # Hop agreement bonus
                hop_diff = abs(info_a['n_hops'] - info_b['n_hops'])
                hop_agreement = 1.0 if hop_diff == 0 else (0.9 if hop_diff == 1 else 0.7)
                
                plausibility = plaus_a * plaus_b * hop_agreement
                
                candidates.append(ModePairCandidate(
                    wwv_mode=mode_a,  # Reusing field names for compatibility
                    wwvh_mode=mode_b,
                    wwv_delay_ms=delay_a,
                    wwvh_delay_ms=delay_b,
                    differential_ms=differential,
                    plausibility=plausibility
                ))
        
        return candidates
    
    def solve_station_pair(
        self,
        station_a: str,
        station_b: str,
        arrival_a_rtp: int,
        arrival_b_rtp: int,
        minute_boundary_rtp: int,
        sample_rate: int,
        frequency_mhz: float = 10.0,
        delay_spread_ms: float = 0.5,
        doppler_std_hz: float = 0.1
    ) -> Dict:
        """
        Differential solve for ANY two stations (WWV-WWVH, WWV-CHU, or WWVH-CHU).
        
        This is the clock-error-immune approach that works for any station pair
        as long as they transmit at the same UTC second boundary.
        
        Args:
            station_a: First station ('WWV', 'WWVH', or 'CHU')
            station_b: Second station ('WWV', 'WWVH', or 'CHU')
            arrival_a_rtp: RTP sample of station A tone arrival
            arrival_b_rtp: RTP sample of station B tone arrival
            minute_boundary_rtp: RTP at expected second boundary
            sample_rate: Audio sample rate
            frequency_mhz: Signal frequency (for logging)
            delay_spread_ms: Multipath indicator
            doppler_std_hz: Path stability
            
        Returns:
            Dict with mode identification and verified clock error
        """
        # Calculate observed differential (clock-error-free!)
        differential_samples = arrival_a_rtp - arrival_b_rtp
        observed_differential_ms = (differential_samples / sample_rate) * 1000
        
        logger.info(
            f"Differential solve {station_a}-{station_b}: Δ={observed_differential_ms:+.3f}ms"
        )
        
        # Generate mode pairs for this station combination
        candidates = self._generate_mode_pairs_generic(station_a, station_b)
        
        if not candidates:
            return {'confidence': 0, 'verified': False, 'error': 'No valid mode pairs'}
        
        # Score candidates
        scored = []
        for cand in candidates:
            residual = abs(observed_differential_ms - cand.differential_ms)
            
            if residual > 2.0:
                score = 0.1
            elif residual > 1.0:
                score = 0.4
            elif residual > 0.5:
                score = 0.7
            else:
                score = 1.0
            
            score *= cand.plausibility
            if delay_spread_ms > 1.0:
                score *= 0.7
            
            scored.append((score, residual, cand))
        
        scored.sort(key=lambda x: x[0], reverse=True)
        best_score, best_residual, best_cand = scored[0]
        
        # Mode separation
        mode_sep = 10.0
        if len(scored) > 1:
            mode_sep = abs(best_cand.differential_ms - scored[1][2].differential_ms)
        
        # Calculate clock error from each station
        timing_a_samples = arrival_a_rtp - minute_boundary_rtp
        timing_b_samples = arrival_b_rtp - minute_boundary_rtp
        timing_a_ms = (timing_a_samples / sample_rate) * 1000
        timing_b_ms = (timing_b_samples / sample_rate) * 1000
        
        clock_error_a = timing_a_ms - best_cand.wwv_delay_ms
        clock_error_b = timing_b_ms - best_cand.wwvh_delay_ms
        
        # Cross-validate
        agreement_ms = abs(clock_error_a - clock_error_b)
        
        if agreement_ms < 1.0:
            verified = True
            clock_error_ms = (clock_error_a + clock_error_b) / 2
            confidence = min(1.0, best_score * min(1.0, mode_sep / 0.5) * 1.3)
        elif agreement_ms < 2.0:
            verified = False
            clock_error_ms = clock_error_a
            confidence = best_score * min(1.0, mode_sep / 0.5) * 0.7
        else:
            verified = False
            clock_error_ms = clock_error_a
            confidence = best_score * min(1.0, mode_sep / 0.5) * 0.3
            logger.warning(
                f"{station_a}/{station_b} mismatch: {clock_error_a:.2f}ms vs {clock_error_b:.2f}ms"
            )
        
        return {
            'station_a': station_a,
            'station_b': station_b,
            'mode_a': best_cand.wwv_mode.value,
            'mode_b': best_cand.wwvh_mode.value,
            'delay_a_ms': best_cand.wwv_delay_ms,
            'delay_b_ms': best_cand.wwvh_delay_ms,
            'differential_observed_ms': observed_differential_ms,
            'differential_expected_ms': best_cand.differential_ms,
            'clock_error_ms': clock_error_ms,
            'agreement_ms': agreement_ms,
            'verified': verified,
            'confidence': confidence,
            'mode_separation_ms': mode_sep
        }
    
    def _generate_mode_pairs(self) -> List[ModePairCandidate]:
        """Generate all valid (WWV mode, WWVH mode) combinations."""
        candidates = []
        
        for wwv_mode, wwv_info in self.mode_delays['WWV'].items():
            for wwvh_mode, wwvh_info in self.mode_delays['WWVH'].items():
                wwv_delay = wwv_info['delay_ms']
                wwvh_delay = wwvh_info['delay_ms']
                differential = wwv_delay - wwvh_delay
                
                # Individual mode plausibilities (distance-based)
                wwv_plausibility = wwv_info.get('plausibility', 0.5)
                wwvh_plausibility = wwvh_info.get('plausibility', 0.5)
                
                # Combined plausibility: product of individual + hop-count agreement
                hop_diff = abs(wwv_info['n_hops'] - wwvh_info['n_hops'])
                if hop_diff == 0:
                    hop_agreement = 1.0
                elif hop_diff == 1:
                    hop_agreement = 0.9
                else:
                    hop_agreement = 0.7
                
                # Final plausibility combines all factors
                plausibility = wwv_plausibility * wwvh_plausibility * hop_agreement
                
                candidates.append(ModePairCandidate(
                    wwv_mode=wwv_mode,
                    wwvh_mode=wwvh_mode,
                    wwv_delay_ms=wwv_delay,
                    wwvh_delay_ms=wwvh_delay,
                    differential_ms=differential,
                    plausibility=plausibility
                ))
        
        return candidates
    
    def _no_solution(self) -> DifferentialResult:
        """Return empty result when no solution possible."""
        return DifferentialResult(
            wwv_mode=PropagationMode.UNKNOWN,
            wwvh_mode=PropagationMode.UNKNOWN,
            wwv_n_hops=0,
            wwvh_n_hops=0,
            wwv_delay_ms=0,
            wwvh_delay_ms=0,
            differential_delay_ms=0,
            expected_differential_ms=0,
            clock_error_ms=0,
            clock_error_verified=False,
            confidence=0,
            differential_residual_ms=float('inf'),
            wwv_wwvh_agreement_ms=float('inf'),
            mode_separation_ms=0,
            candidates_evaluated=0,
            ambiguous=True
        )
    
    def solve_multi_frequency(
        self,
        observations: List[Dict],
        minute_boundary_rtp: int,
        sample_rate: int
    ) -> MultiFrequencyResult:
        """
        Solve using observations from multiple frequencies.
        
        All frequencies should yield the SAME clock error (it's a receiver property).
        If they don't agree, we have mode misidentification somewhere.
        
        Args:
            observations: List of dicts with:
                - frequency_mhz
                - wwv_arrival_rtp
                - wwvh_arrival_rtp
                - delay_spread_ms (optional)
                - doppler_std_hz (optional)
            minute_boundary_rtp: RTP at expected second
            sample_rate: Audio sample rate
            
        Returns:
            MultiFrequencyResult with combined clock error estimate
        """
        if not observations:
            return self._no_multi_solution()
        
        # Solve each frequency
        results = []
        for obs in observations:
            if 'wwv_arrival_rtp' not in obs or 'wwvh_arrival_rtp' not in obs:
                continue
                
            result = self.solve_with_anchor(
                wwv_arrival_rtp=obs['wwv_arrival_rtp'],
                wwvh_arrival_rtp=obs['wwvh_arrival_rtp'],
                minute_boundary_rtp=minute_boundary_rtp,
                sample_rate=sample_rate,
                frequency_mhz=obs.get('frequency_mhz', 10.0),
                delay_spread_ms=obs.get('delay_spread_ms', 0.5),
                doppler_std_hz=obs.get('doppler_std_hz', 0.1)
            )
            
            results.append({
                'frequency_mhz': obs.get('frequency_mhz', 10.0),
                'clock_error_ms': result.clock_error_ms,
                'confidence': result.confidence,
                'wwv_mode': result.wwv_mode.value,
                'wwvh_mode': result.wwvh_mode.value,
                'verified': result.clock_error_verified,
                'agreement_ms': result.wwv_wwvh_agreement_ms
            })
        
        if not results:
            return self._no_multi_solution()
        
        # Filter to confident results
        confident = [r for r in results if r['confidence'] > 0.3]
        
        if not confident:
            # Use best single result
            best = max(results, key=lambda r: r['confidence'])
            return MultiFrequencyResult(
                clock_error_ms=best['clock_error_ms'],
                uncertainty_ms=5.0,
                confidence=best['confidence'] * 0.5,
                consistency=0.0,
                n_frequencies=1,
                frequency_results=results,
                verified=False,
                quality_grade='D'
            )
        
        # Weighted average
        weights = [r['confidence'] for r in confident]
        clock_errors = [r['clock_error_ms'] for r in confident]
        
        total_weight = sum(weights)
        combined_error = sum(e * w for e, w in zip(clock_errors, weights)) / total_weight
        
        # Check consistency
        spread = max(clock_errors) - min(clock_errors)
        consistency = max(0.0, 1.0 - spread / 3.0)
        
        # Uncertainty from spread
        if len(clock_errors) > 1:
            import numpy as np
            uncertainty = np.std(clock_errors) / math.sqrt(len(clock_errors))
        else:
            uncertainty = 2.0
        
        # Combined confidence
        avg_conf = sum(r['confidence'] for r in confident) / len(confident)
        combined_conf = min(1.0, avg_conf * consistency * 1.2)
        
        # Quality grade
        if uncertainty < 0.5 and consistency > 0.8:
            grade = 'A'
        elif uncertainty < 1.5 and consistency > 0.6:
            grade = 'B'
        elif uncertainty < 3.0:
            grade = 'C'
        else:
            grade = 'D'
        
        verified = uncertainty < 2.0 and consistency > 0.5 and len(confident) >= 2
        
        return MultiFrequencyResult(
            clock_error_ms=combined_error,
            uncertainty_ms=uncertainty,
            confidence=combined_conf,
            consistency=consistency,
            n_frequencies=len(confident),
            frequency_results=results,
            verified=verified,
            quality_grade=grade
        )
    
    def _no_multi_solution(self) -> MultiFrequencyResult:
        """Return empty multi-frequency result."""
        return MultiFrequencyResult(
            clock_error_ms=0,
            uncertainty_ms=float('inf'),
            confidence=0,
            consistency=0,
            n_frequencies=0,
            frequency_results=[],
            verified=False,
            quality_grade='D'
        )


@dataclass
class GlobalSolveResult:
    """Result from solving with ALL observations globally."""
    clock_error_ms: float
    uncertainty_ms: float
    confidence: float
    
    # Per-observation mode assignments
    mode_assignments: List[Dict]  # station, freq, mode, delay_ms
    
    # Verification
    n_observations: int
    n_pairs: int
    pair_consistency_ms: float  # RMS of pairwise residuals
    verified: bool
    quality_grade: str
    
    # Debug
    best_score: float
    candidates_evaluated: int


class GlobalDifferentialSolver:
    """
    Solve for clock error using ALL observations from ANY stations/frequencies.
    
    This is the correct approach that uses pairwise differentials to eliminate
    clock error from the constraint equations. With N observations, we have
    N*(N-1)/2 differential constraints that must all be consistent.
    """
    
    def __init__(self, receiver_lat: float, receiver_lon: float, f_layer_height_km: float = 300.0):
        """Initialize with receiver location."""
        self.receiver_lat = receiver_lat
        self.receiver_lon = receiver_lon
        self.f_layer_height_km = f_layer_height_km
        
        # Calculate distance to each station
        self.distances = {}
        for station, loc in STATIONS.items():
            self.distances[station] = self._great_circle_distance(
                receiver_lat, receiver_lon, loc['lat'], loc['lon']
            )
        
        # Pre-compute mode delays for each station
        self.mode_delays = {}
        for station in STATIONS:
            self.mode_delays[station] = self._compute_modes(station)
        
        logger.info(f"GlobalDifferentialSolver initialized for ({receiver_lat:.2f}, {receiver_lon:.2f})")
        for station, dist in self.distances.items():
            modes = list(self.mode_delays[station].keys())
            logger.info(f"  {station}: {dist:.0f} km, modes: {[m.value for m in modes]}")
    
    def _great_circle_distance(self, lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        """Delegates to hamsci_dsp.geometry.great_circle_km (geodesic WGS-84)."""
        return great_circle_km(lat1, lon1, lat2, lon2)
    
    def _compute_modes(self, station: str) -> Dict[PropagationMode, Dict]:
        """Compute plausible modes for a station based on distance."""
        dist_km = self.distances[station]
        modes = {}
        
        # Ground wave (< 200 km)
        if dist_km < 200:
            delay = dist_km / SPEED_OF_LIGHT_KM_S * 1000
            modes[PropagationMode.ONE_HOP_E] = {
                'delay_ms': delay, 'n_hops': 1, 'plausibility': 1.0
            }
        
        # 1-hop E-layer (110 km)
        if dist_km < 2000:
            half_angle = dist_km / (2 * EARTH_RADIUS_KM)
            slant = 2 * math.sqrt((EARTH_RADIUS_KM + E_LAYER_HEIGHT_KM)**2 - 
                                  (EARTH_RADIUS_KM * math.cos(half_angle))**2)
            delay = slant / SPEED_OF_LIGHT_KM_S * 1000
            plausibility = 1.0 if dist_km < 1500 else 0.7
            modes[PropagationMode.ONE_HOP_E] = {
                'delay_ms': delay, 'n_hops': 1, 'plausibility': plausibility
            }
        
        # 1-hop F-layer (300 km)
        if dist_km < 4000:
            half_angle = dist_km / (2 * EARTH_RADIUS_KM)
            slant = 2 * math.sqrt((EARTH_RADIUS_KM + self.f_layer_height_km)**2 - 
                                  (EARTH_RADIUS_KM * math.cos(half_angle))**2)
            delay = slant / SPEED_OF_LIGHT_KM_S * 1000
            plausibility = 1.0 if dist_km < 3000 else 0.8
            modes[PropagationMode.ONE_HOP_F] = {
                'delay_ms': delay, 'n_hops': 1, 'plausibility': plausibility
            }
        
        # 2-hop F-layer
        if dist_km > 1500 and dist_km < 8000:
            hop_dist = dist_km / 2
            half_angle = hop_dist / (2 * EARTH_RADIUS_KM)
            slant = 2 * math.sqrt((EARTH_RADIUS_KM + self.f_layer_height_km)**2 - 
                                  (EARTH_RADIUS_KM * math.cos(half_angle))**2)
            delay = 2 * slant / SPEED_OF_LIGHT_KM_S * 1000
            plausibility = 0.8 if dist_km > 3000 else 0.5
            modes[PropagationMode.TWO_HOP_F] = {
                'delay_ms': delay, 'n_hops': 2, 'plausibility': plausibility
            }
        
        # 3-hop F-layer
        if dist_km > 4000:
            hop_dist = dist_km / 3
            half_angle = hop_dist / (2 * EARTH_RADIUS_KM)
            slant = 2 * math.sqrt((EARTH_RADIUS_KM + self.f_layer_height_km)**2 - 
                                  (EARTH_RADIUS_KM * math.cos(half_angle))**2)
            delay = 3 * slant / SPEED_OF_LIGHT_KM_S * 1000
            plausibility = 0.7
            modes[PropagationMode.THREE_HOP_F] = {
                'delay_ms': delay, 'n_hops': 3, 'plausibility': plausibility
            }
        
        return modes
    
    def solve_global(
        self,
        observations: List[Dict],
        minute_boundary_rtp: int,
        sample_rate: int
    ) -> GlobalSolveResult:
        """
        Solve for clock error using ALL observations simultaneously.
        
        Args:
            observations: List of dicts with:
                - station: 'WWV', 'WWVH', or 'CHU'
                - frequency_mhz: float
                - arrival_rtp: int (RTP sample of tone arrival)
            minute_boundary_rtp: RTP at expected UTC second boundary
            sample_rate: Audio sample rate
            
        Returns:
            GlobalSolveResult with clock error and mode assignments
        """
        if len(observations) < 2:
            logger.warning("Need at least 2 observations for differential solving")
            return self._no_global_solution()
        
        # Convert arrivals to timing in ms
        obs_timing = []
        for obs in observations:
            timing_samples = obs['arrival_rtp'] - minute_boundary_rtp
            timing_ms = (timing_samples / sample_rate) * 1000
            obs_timing.append({
                'station': obs['station'],
                'frequency_mhz': obs['frequency_mhz'],
                'timing_ms': timing_ms,
                'modes': self.mode_delays.get(obs['station'], {})
            })
        
        # Generate all pairwise differentials (observed)
        n = len(obs_timing)
        pairs = []
        for i in range(n):
            for j in range(i + 1, n):
                diff_observed = obs_timing[i]['timing_ms'] - obs_timing[j]['timing_ms']
                pairs.append({
                    'i': i, 'j': j,
                    'diff_observed': diff_observed
                })
        
        logger.info(f"Global solve: {n} observations → {len(pairs)} differential pairs")
        
        # Generate all mode assignment combinations
        from itertools import product
        
        mode_options = []
        for obs in obs_timing:
            modes = list(obs['modes'].keys())
            if not modes:
                modes = [PropagationMode.UNKNOWN]
            mode_options.append(modes)
        
        # Evaluate each combination
        best_score = -1
        best_assignment = None
        best_residual_rms = float('inf')
        candidates_evaluated = 0
        
        for assignment in product(*mode_options):
            candidates_evaluated += 1
            
            # Get expected delays for this assignment
            delays = []
            plausibility = 1.0
            for idx, mode in enumerate(assignment):
                obs = obs_timing[idx]
                if mode in obs['modes']:
                    info = obs['modes'][mode]
                    delays.append(info['delay_ms'])
                    plausibility *= info.get('plausibility', 0.5)
                else:
                    delays.append(0)
                    plausibility *= 0.1
            
            # Calculate expected differentials and compare to observed
            # If modes are correct, residuals should be ~0 (clock error cancels!)
            residuals = []
            for pair in pairs:
                diff_expected = delays[pair['i']] - delays[pair['j']]
                residual = pair['diff_observed'] - diff_expected
                residuals.append(residual)
            
            # Residuals should all be near zero for correct mode assignment
            # (differential eliminates clock error)
            rms_residual = math.sqrt(sum(r**2 for r in residuals) / len(residuals))
            
            # Score: differential fit is DOMINANT, plausibility is minor tie-breaker
            # Perfect fit (< 0.05ms): likely correct mode
            # Good fit (< 0.2ms): probably correct
            # Poor fit (> 0.5ms): likely wrong mode
            if rms_residual < 0.05:
                fit_score = 1.0
            elif rms_residual < 0.2:
                fit_score = 0.9 - rms_residual
            elif rms_residual < 0.5:
                fit_score = 0.5 - rms_residual * 0.5
            else:
                fit_score = 0.1 / (1 + rms_residual * 2)
            
            # Combined: fit is 90% of score, plausibility is 10% tie-breaker
            score = 0.9 * fit_score + 0.1 * plausibility
            
            if score > best_score:
                best_score = score
                best_assignment = assignment
                best_residual_rms = rms_residual
                best_delays = delays
        
        if best_assignment is None:
            return self._no_global_solution()
        
        # Calculate clock error from absolute timing (now that we know modes)
        # clock_error = timing - prop_delay for each observation
        clock_errors = []
        for idx, mode in enumerate(best_assignment):
            obs = obs_timing[idx]
            if mode in obs['modes']:
                delay = obs['modes'][mode]['delay_ms']
                clock_error = obs['timing_ms'] - delay
                clock_errors.append(clock_error)
        
        # All should agree - average them
        best_clock_error = sum(clock_errors) / len(clock_errors) if clock_errors else 0
        
        # Build mode assignments result
        mode_assignments = []
        for idx, mode in enumerate(best_assignment):
            obs = obs_timing[idx]
            info = obs['modes'].get(mode, {'delay_ms': 0, 'n_hops': 0})
            mode_assignments.append({
                'station': obs['station'],
                'frequency_mhz': obs['frequency_mhz'],
                'mode': mode.value,
                'n_hops': info.get('n_hops', 0),
                'delay_ms': info.get('delay_ms', 0),
                'timing_ms': obs['timing_ms']
            })
        
        # Calculate confidence
        # High confidence if: low residual RMS, multiple observations, high plausibility
        if best_residual_rms < 0.5:
            consistency_score = 1.0
        elif best_residual_rms < 1.0:
            consistency_score = 0.8
        elif best_residual_rms < 2.0:
            consistency_score = 0.5
        else:
            consistency_score = 0.2
        
        multi_obs_bonus = min(2.0, 1.0 + 0.1 * n)
        confidence = min(1.0, best_score * consistency_score * multi_obs_bonus / 2)
        
        # Uncertainty from residual RMS
        uncertainty = best_residual_rms / math.sqrt(len(pairs))
        
        # Quality grade
        if uncertainty < 0.5 and consistency_score > 0.8 and n >= 3:
            grade = 'A'
        elif uncertainty < 1.0 and consistency_score > 0.5 and n >= 2:
            grade = 'B'
        elif uncertainty < 2.0:
            grade = 'C'
        else:
            grade = 'D'
        
        verified = consistency_score > 0.5 and n >= 2
        
        logger.info(
            f"Global solve result: clock_error={best_clock_error:+.3f}ms, "
            f"uncertainty={uncertainty:.3f}ms, confidence={confidence:.0%}, "
            f"grade={grade}, verified={verified}"
        )
        
        return GlobalSolveResult(
            clock_error_ms=best_clock_error,
            uncertainty_ms=uncertainty,
            confidence=confidence,
            mode_assignments=mode_assignments,
            n_observations=n,
            n_pairs=len(pairs),
            pair_consistency_ms=best_residual_rms,
            verified=verified,
            quality_grade=grade,
            best_score=best_score,
            candidates_evaluated=candidates_evaluated
        )
    
    def _no_global_solution(self) -> GlobalSolveResult:
        """Return empty result."""
        return GlobalSolveResult(
            clock_error_ms=0,
            uncertainty_ms=float('inf'),
            confidence=0,
            mode_assignments=[],
            n_observations=0,
            n_pairs=0,
            pair_consistency_ms=float('inf'),
            verified=False,
            quality_grade='D',
            best_score=0,
            candidates_evaluated=0
        )

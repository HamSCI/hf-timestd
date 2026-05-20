#!/usr/bin/env python3
"""
Metrology Engine: Pure DSP Time-of-Arrival Measurement
======================================================
Part of the "Metrology First, Physics Second" architecture.

Responsibility:
1. "The Instrument": Measure what happened (Timestamp, Frequency, Power).
2. "The Facts": Report Raw Time of Arrival (TOA).
3. "No Interpretation": Do NOT attempt to calculate d_clock or propagation delay.
   (Except for basic speed-of-light sanity checks).

Inputs:
- Raw IQ buffer (complex64)
- System Time
- RTP Timestamp

Outputs:
- List[L1MetrologyMeasurement]
"""

import numpy as np
import logging
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional, Dict, List, Tuple, Any
import threading
import json
import math

# Imports
from hf_timestd.models import (
    L1MetrologyMeasurement,
    QualityFlag,
    StationID
)
from hf_timestd.core.wwvh_discrimination import WWVHDiscriminator
from hf_timestd.core.tone_detector import MultiStationToneDetector
try:
    from hf_timestd.core.arrival_pattern_matrix import ArrivalPatternMatrix as ArrivalPatternMatrix
    _ARRIVAL_MATRIX_AVAILABLE = True
except Exception:
    ArrivalPatternMatrix = None  # type: ignore[assignment,misc]
    _ARRIVAL_MATRIX_AVAILABLE = False
from hf_timestd.core.tick_matched_filter import TickMatchedFilter, StationType
from hf_timestd.core.decoder_config import get_decoder_config, DecoderConfig, DecoderComparisonTracker
from hf_timestd.core.tick_edge_detector import TickEdgeDetector
from hf_timestd.core.hop_geometry import (
    hop_geometry,
    n_hops_for_distance,
)
from hf_timestd.core.snr import peak_snr_db_envelope
from hf_timestd.core.fusion_timing_state import FusionTimingState, LockTier
from hf_timestd.core.bootstrap_state import BootstrapStateWriter
from hf_timestd.core.timing_consistency_validator import TimingConsistencyValidator
# We keep discriminators as they are signal analysis, not physics modeling.

logger = logging.getLogger(__name__)

# Constants (Same as Phase 2)
EXPECTED_DTYPE = np.complex64
SAMPLE_RATE_FULL = 24000
MAX_EXPECTED_AMPLITUDE = 1.0
AMPLITUDE_WARNING_THRESHOLD = 10.0
SPEED_OF_LIGHT_KM_MS = 299.792458
# Convert SPEED_OF_LIGHT_KM_MS to the seconds-based units the hop-geometry
# helpers use.  Keeping both forms here avoids a unit-conversion bug at the
# call site.
SPEED_OF_LIGHT_KM_S = SPEED_OF_LIGHT_KM_MS * 1000.0  # 299792.458
# F-layer reference height used by the vacuum/no-model fallback (M-M5).
# Matches `propagation_engine.F2_LAYER_HEIGHT_KM`.
_FALLBACK_F2_HEIGHT_KM = 300.0
# Nominal slant-TEC per hop for the 40.3/f² ionospheric delay term
# (matches `propagation_engine.NOMINAL_SLANT_TEC_PER_HOP_TECU`).
_FALLBACK_SLANT_TEC_PER_HOP_TECU = 30.0
# 40.3·10¹⁶ / c (m/s → km/s, TECU → 10¹⁶ el/m²), giving group delay in ms.
# Same value as `propagation_engine.IONO_DELAY_CONSTANT_MS`.
_IONO_DELAY_CONSTANT_MS = 40.3 / SPEED_OF_LIGHT_KM_S * 1e16 / 1e12


def _great_circle_km(lat1_deg: float, lon1_deg: float,
                     lat2_deg: float, lon2_deg: float) -> float:
    """Spherical-Earth great-circle distance (km).  Pulled out so the
    vacuum-fallback helper below stays a pure function of its inputs."""
    from .wwv_constants import EARTH_RADIUS_KM
    dlat = math.radians(lat2_deg - lat1_deg)
    dlon = math.radians(lon2_deg - lon1_deg)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1_deg))
         * math.cos(math.radians(lat2_deg))
         * math.sin(dlon / 2) ** 2)
    c = 2 * math.asin(math.sqrt(a))
    return EARTH_RADIUS_KM * c


def _vacuum_hop_fallback_delay(dist_km: float, frequency_hz: float) -> Tuple[float, float]:
    """Geometric + climatological-iono propagation delay (M-M5).

    Returns ``(expected_delay_ms, uncertainty_1sigma_ms)`` for a great-
    circle distance ``dist_km`` at carrier ``frequency_hz``.  Uses the
    shared spherical-Earth hop model (S2) for the geometric slant and a
    40.3/f² group-delay term against a nominal slant TEC per hop for
    the dispersive ionospheric contribution.  Uncertainty is the per-hop
    geometric uncertainty plus the (uncertain) climatological iono term.

    Replaces the previous ``light_time × 1.15`` heuristic, which had no
    physical basis and was frequency-blind.
    """
    if dist_km <= 0:
        return 0.0, 15.0

    hops = n_hops_for_distance(dist_km, _FALLBACK_F2_HEIGHT_KM)
    geom = hop_geometry(dist_km, _FALLBACK_F2_HEIGHT_KM, hops)
    geometric_delay_ms = geom.path_length_km / SPEED_OF_LIGHT_KM_S * 1000.0

    f_mhz = frequency_hz / 1e6
    if f_mhz > 0:
        iono_delay_ms = (
            _IONO_DELAY_CONSTANT_MS
            * _FALLBACK_SLANT_TEC_PER_HOP_TECU * hops
            / (f_mhz ** 2)
        )
    else:
        iono_delay_ms = 0.0

    total_delay_ms = geometric_delay_ms + iono_delay_ms
    # Carry the climatological iono term as its own uncertainty — TEC
    # routinely varies by its own magnitude across the day.
    uncertainty_ms = 3.0 * hops + iono_delay_ms
    return total_delay_ms, uncertainty_ms


class MetrologyEngine:
    """
    Metrology Engine: Pure DSP processing for Time-of-Arrival.
    Orchestrates Tone Detection and Channel Characterization.
    
    Two operating modes:
    - RTP Mode: Timing is authoritative (GPSDO + GPS+PPS). We KNOW when second 0 is.
                No searching needed - directly measure signals at known times.
    - Fusion Mode: Timing from NTP (uncertain). Bootstrap to find UTC offset first,
                   then operate like RTP mode.
    """
    
    def __init__(
        self,
        raw_buffer_dir: Path,
        output_dir: Path,
        channel_name: str,
        frequency_hz: float,
        receiver_grid: str,
        sample_rate: int = SAMPLE_RATE_FULL,
        precise_lat: Optional[float] = None,
        precise_lon: Optional[float] = None,
        is_rtp_authority: bool = True,  # Default to RTP mode
        enable_physics_products: bool = True,  # False = timing-only, skip secondary-arrival search
        enable_coarse_time: bool = True,
        coarse_time_path: Optional[Path] = None,
    ):
        self.raw_buffer_dir = Path(raw_buffer_dir)
        self.output_dir = Path(output_dir)
        self.channel_name = channel_name
        self.frequency_hz = frequency_hz
        self.frequency_mhz = frequency_hz / 1e6
        self.receiver_grid = receiver_grid
        self.sample_rate = sample_rate
        self.precise_lat = precise_lat
        self.precise_lon = precise_lon
        self.is_rtp_authority = is_rtp_authority
        self.enable_physics_products = enable_physics_products

        # Pre-allocated buffers for zero-allocation DSP
        self._max_samples = 65 * self.sample_rate
        self._envelope_buffer = np.empty(self._max_samples, dtype=np.float32)
        self.is_chu_channel = 'CHU' in channel_name.upper()

        # CHU FSK coarse-time producer for the authority manager's
        # bootstrap coordinator (METROLOGY.md §4.5). Only CHU channels
        # carry the BCD/FSK burst, so non-CHU instances never publish.
        # Best-effort init: a missing /run/hf-timestd directory (e.g.
        # unit-test runs without the service unit's RuntimeDirectory=)
        # logs a warning and leaves the writer None.
        self._coarse_time_writer = None
        if self.is_chu_channel and enable_coarse_time:
            try:
                from hf_timestd.core.coarse_time_writer import CoarseTimeWriter
                if coarse_time_path is not None:
                    self._coarse_time_writer = CoarseTimeWriter(path=coarse_time_path)
                else:
                    self._coarse_time_writer = CoarseTimeWriter()
                logger.info(
                    f"{channel_name}: coarse-time writer enabled "
                    f"({self._coarse_time_writer.path})"
                )
            except Exception as e:
                logger.warning(f"{channel_name}: coarse-time writer disabled: {e}")
        
        # Initialize sub-components
        self._init_components()
        
        # Initialize Arrival Pattern Matrix for physics-based validation
        self._init_arrival_matrix()
        
        # Initialize Timing Consistency Validator for multi-constraint validation
        self._init_timing_validator()
        
        # State
        self._lock = threading.Lock()
        self.minutes_processed = 0
        
        # Detection gap tracking: last physics-validated detection time per station.
        # Used to emit WARNING when a station goes dark for >5 minutes.
        # §3.4 Low: the `_last_*` attributes below are per-channel state
        # owned by exactly one writer thread -- the one driving
        # ``process_minute`` -- and one reader (the metrology service's
        # post-processing pass that runs synchronously after the same
        # writer's call returns).  No re-entrancy or cross-thread
        # mutation should reach these.  If a future change introduces a
        # second writer, this contract needs an explicit lock; today
        # the single-owner discipline is enforced by the caller.
        self._last_validated_detection: Dict[str, float] = {}  # station -> unix time
        self._gap_warning_emitted: Dict[str, float] = {}  # station -> last warning time
        self._DETECTION_GAP_THRESHOLD_S = 300.0  # 5 minutes
        self._GAP_WARNING_INTERVAL_S = 300.0  # Don't spam: one warning per 5 min
        
        # Edge detection results (per-second onset timing)
        self._last_edge_results: Dict[str, Any] = {}
        
        # CHU FSK cross-validation state (populated by decode_minute results)
        # These fields enable four downstream integrations:
        #   1. Frame A UTC sanity check (detect broken RTP timing chain)
        #   2. TAI-UTC leap second watch (detect upcoming leap seconds)
        #   3. DUT1 for UT1 recovery (correct solar zenith in propagation model)
        #   4. BER-based confidence weighting (degrade CHU weight during fading)
        self._fsk_last_tai_utc: Optional[int] = None        # Last decoded TAI-UTC
        self._fsk_last_dut1: Optional[float] = None          # Last decoded DUT1 (seconds)
        self._fsk_tai_utc_changed: bool = False               # True when leap second detected
        self._fsk_utc_mismatch_count: int = 0                 # Consecutive UTC mismatches
        
        # NOTE (§3.4 Low): a `bpm_calibration` dict + `_load_calibration`
        # / `_save_calibration` JSON round-trip lived here.  The dict was
        # initialised, optionally loaded from disk, and the saver was
        # never called -- the value was also never *read* anywhere
        # downstream.  Removed; if a future BPM offset calibration is
        # added it should land on a dedicated dataclass with explicit
        # consumers, not a free-floating dict.
        
        # Fusion mode timing state (only used when is_rtp_authority=False)
        # This replaces the separate BootstrapService
        self.fusion_state: Optional[FusionTimingState] = None
        self._bootstrap_state_writer: Optional[BootstrapStateWriter] = None
        if not self.is_rtp_authority:
            self.fusion_state = FusionTimingState(sample_rate=self.sample_rate)
            self._bootstrap_state_writer = BootstrapStateWriter()
            logger.info(f"{channel_name}: Fusion mode - timing lock required before narrow search")
        
        logger.info(
            f"MetrologyEngine initialized for {channel_name} "
            f"({self.frequency_mhz} MHz), mode={'RTP' if is_rtp_authority else 'FUSION'}")

    def _init_components(self):
        """Initialize discriminators and detectors."""
        try:
            # 1. Tone Detector
            self.tone_detector = MultiStationToneDetector(
                channel_name=self.channel_name,
                sample_rate=self.sample_rate
            )
            
            # 2. WWV/WWVH Discriminator (includes BCD and Doppler)
            self.discriminator = WWVHDiscriminator(
                channel_name=self.channel_name,
                receiver_grid=self.receiver_grid,
                sample_rate=self.sample_rate
            )
            self.discriminator.frequency_mhz = self.frequency_mhz
            
            # 3. BPM Discriminator
            bpm_active_hours = set(range(24))
            if abs(self.frequency_mhz - 2.5) < 0.1:
                bpm_active_hours = {0} | set(range(8, 24))
            elif abs(self.frequency_mhz - 15.0) < 0.1:
                bpm_active_hours = set(range(1, 9))

            from hf_timestd.core.bpm_discriminator import BPMDiscriminator
            self.bpm_discriminator = BPMDiscriminator(
                receiver_lat=self.precise_lat,
                receiver_lon=self.precise_lon,
                channel_name=self.channel_name,
                active_hours=bpm_active_hours
            )

            # 4. Multi-Station Detector (Used for cross-freq guidance logic)
            # Note: We are using it for DSP purposes (signal presence), not physics solving.
            from hf_timestd.core.multi_station_detector import MultiStationDetector
            self.multi_station_detector = MultiStationDetector(
                receiver_lat=self.precise_lat,
                receiver_lon=self.precise_lon,
                sample_rate=self.sample_rate
            )
            
            # 5. Correlator Bank (Optional, if coords available)
            if self.precise_lat is not None and self.precise_lon is not None:
                from hf_timestd.core.correlator_bank import CorrelatorBank
                self.correlator_bank = CorrelatorBank(
                    receiver_lat=self.precise_lat,
                    receiver_lon=self.precise_lon,
                    sample_rate=self.sample_rate,
                    calibrated=False
                )
            else:
                self.correlator_bank = None
                
            # 6. CHU FSK Decoder
            if 'CHU' in self.channel_name.upper():
                from hf_timestd.core.chu_fsk_decoder import CHUFSKDecoder
                self.chu_fsk_decoder = CHUFSKDecoder(
                    sample_rate=self.sample_rate,
                    channel_name=self.channel_name
                )
            
            # 7. Tick Matched Filters for per-second timing (55+ estimates/minute)
            self.tick_filters: Dict[StationType, TickMatchedFilter] = {}
            self._init_tick_filters()
            
            # 8. Tick Edge Detector for per-second onset timing (57 edges/minute)
            # Detects the onset step of each tick via differential envelope,
            # overcoming the intermod and low-processing-gain problems that
            # prevented use of 5ms WWV/WWVH ticks in the matched filter.
            self.edge_detector = TickEdgeDetector(sample_rate=self.sample_rate)
            
            # 9. Decoder config and A/B comparison tracker
            self.decoder_config = get_decoder_config()
            self.pll_decoders = {}  # PLL flywheel decoders for A/B comparison
            if self.decoder_config.enable_ab_comparison:
                self.comparison_tracker = DecoderComparisonTracker(self.decoder_config)
                # Initialize PLL decoders for each station type
                from hf_timestd.core.tick_pll_decoder import TickPLLDecoder
                for station_type in self.tick_filters.keys():
                    self.pll_decoders[station_type] = TickPLLDecoder(
                        sample_rate=self.sample_rate,
                        station_type=station_type.value,
                        window_ms=self.decoder_config.pll_window_ms,
                        alpha=self.decoder_config.pll_alpha,
                        max_missed=self.decoder_config.pll_max_missed
                    )
                logger.info(f"{self.channel_name}: A/B comparison enabled - MF + PLL decoders running")
            else:
                self.comparison_tracker = None
                
        except ImportError as e:
            logger.error(f"Failed to initialize Metrology components: {e}")
            raise

    def _init_arrival_matrix(self):
        """
        Initialize the Arrival Pattern Matrix for physics-based validation.
        
        The matrix provides expected arrival times based on:
        - Geography (receiver and station locations)
        - Frequency (affects ionospheric reflection height)
        - UTC time (affects ionospheric conditions via IRI-2020)
        
        This replaces historical calibration with physics-based predictions.
        """
        self.arrival_matrix = None
        
        if self.precise_lat is not None and self.precise_lon is not None:
            try:
                self.arrival_matrix = ArrivalPatternMatrix(
                    receiver_lat=self.precise_lat,
                    receiver_lon=self.precise_lon,
                    sample_rate=self.sample_rate,
                    enable_iri=True  # Use IRI-2020 if available
                )
                logger.info(f"ArrivalPatternMatrix initialized for {self.channel_name}")
            except Exception as e:
                logger.warning(f"Could not initialize ArrivalPatternMatrix: {e}")
                self.arrival_matrix = None
        else:
            logger.info(f"ArrivalPatternMatrix not initialized (no precise coordinates)")

    def _init_timing_validator(self):
        """
        Initialize the Timing Consistency Validator for multi-constraint validation.
        
        The validator exploits multiple timing constraints:
        - Intra-minute: arrival sequence, cross-station consistency, cross-frequency TEC
        - Inter-minute: sample interval stability, arrival time stability
        
        This provides additional validation beyond the physics-based arrival matrix.
        """
        self.timing_validator = None
        
        if self.precise_lat is not None and self.precise_lon is not None:
            try:
                self.timing_validator = TimingConsistencyValidator(
                    receiver_lat=self.precise_lat,
                    receiver_lon=self.precise_lon,
                    sample_rate=self.sample_rate,
                    history_minutes=60  # Track 1 hour of history
                )
                
                # Wire up TEC feedback: validator -> arrival matrix
                # When validator computes TEC, it feeds back to refine arrival predictions
                if self.arrival_matrix is not None:
                    self.timing_validator.set_tec_callback(self.arrival_matrix.update_measured_tec)
                    logger.info(f"TEC feedback enabled: validator -> arrival matrix")
                
                logger.info(f"TimingConsistencyValidator initialized for {self.channel_name}")
            except Exception as e:
                logger.warning(f"Could not initialize TimingConsistencyValidator: {e}")
                self.timing_validator = None
        else:
            logger.debug(f"TimingConsistencyValidator not initialized (no precise coordinates)")

    def _init_tick_filters(self):
        """
        Initialize per-second tick matched filters based on channel type.
        
        Creates filters for stations that can be received on this channel:
        - SHARED channels: WWV, WWVH, BPM
        - WWV-only channels (20, 25 MHz): WWV only
        - CHU channels: CHU only
        """
        channel_upper = self.channel_name.upper()
        
        if 'CHU' in channel_upper:
            # CHU-only channels (3.33, 7.85, 14.67 MHz)
            self.tick_filters[StationType.CHU] = TickMatchedFilter(
                station=StationType.CHU,
                sample_rate=self.sample_rate
            )
            logger.info(f"{self.channel_name}: CHU tick filter initialized (58 ticks/min)")
            
        elif 'WWV_20' in channel_upper or 'WWV_25' in channel_upper:
            # WWV-only channels (20, 25 MHz)
            self.tick_filters[StationType.WWV] = TickMatchedFilter(
                station=StationType.WWV,
                sample_rate=self.sample_rate
            )
            logger.info(f"{self.channel_name}: WWV tick filter initialized (57 ticks/min)")
            
        elif 'SHARED' in channel_upper:
            # Shared channels (2.5, 5, 10, 15 MHz) - WWV, WWVH, BPM all possible
            self.tick_filters[StationType.WWV] = TickMatchedFilter(
                station=StationType.WWV,
                sample_rate=self.sample_rate
            )
            self.tick_filters[StationType.WWVH] = TickMatchedFilter(
                station=StationType.WWVH,
                sample_rate=self.sample_rate
            )
            self.tick_filters[StationType.BPM] = TickMatchedFilter(
                station=StationType.BPM,
                sample_rate=self.sample_rate
            )
            logger.info(f"{self.channel_name}: WWV/WWVH/BPM tick filters initialized (57+57+59 ticks/min)")

    def _validate_input(self, iq_samples: np.ndarray) -> Tuple[np.ndarray, Dict[str, Any]]:
        """Validate and normalize input samples."""
        # Same logic as Phase2TemporalEngine
        metrics = {'amplitude_warning': False}
        if iq_samples.dtype != EXPECTED_DTYPE:
            iq_samples = iq_samples.astype(EXPECTED_DTYPE)
        
        max_amp = float(np.max(np.abs(iq_samples)))
        if max_amp > AMPLITUDE_WARNING_THRESHOLD:
            logger.warning(f"High amplitude: {max_amp}")
            metrics['amplitude_warning'] = True
            
        if max_amp > MAX_EXPECTED_AMPLITUDE:
            iq_samples = iq_samples / max_amp
            
        return iq_samples, metrics

    def _predict_geometric_delay(self, station: str, utc_time: Optional[float] = None) -> Tuple[float, float, float]:
        """
        Calculate expected propagation delay using physics-based models.
        
        Priority:
        1. ArrivalPatternMatrix (uses HFPropagationModel internally — multi-mode,
           frequency-dependent ionospheric delay, adaptive uncertainty)
        2. HFPropagationModel directly (if matrix not available)
        3. Simple light-speed calculation with ionospheric overhead (last resort)
        
        Returns: (expected_delay_ms, distance_km, uncertainty_1sigma_ms)
        
        Side effect: populates self._last_prediction_meta with model metadata
        for traceability (data_source, model_confidence, propagation_mode).
        """
        from datetime import datetime, timezone
        if utc_time is not None:
            dt = datetime.fromtimestamp(utc_time, tz=timezone.utc)
        else:
            dt = datetime.now(timezone.utc)
        
        # Try ArrivalPatternMatrix first (physics-based, may use HFPropagationModel)
        if self.arrival_matrix is not None:
            try:
                arrival = self.arrival_matrix.get_expected_arrivals(dt).get_arrival(
                    station, self.frequency_mhz
                )
                if arrival is not None:
                    self._last_prediction_meta = {
                        'data_source': getattr(arrival, 'data_source', 'matrix'),
                        'model_confidence': getattr(arrival, 'model_confidence', 0.0),
                        'propagation_mode': getattr(arrival, 'propagation_mode', '1F'),
                    }
                    return (
                        arrival.expected_delay_ms,
                        arrival.great_circle_km,
                        arrival.uncertainty_3sigma_ms / 3.0  # Return 1-sigma
                    )
            except Exception as e:
                logger.debug(f"ArrivalPatternMatrix lookup failed: {e}")
        
        # Try HFPropagationModel directly (cached instance)
        if self.precise_lat is not None and self.precise_lon is not None:
            try:
                if not hasattr(self, '_prop_model_fallback') or self._prop_model_fallback is None:
                    from .propagation_model import HFPropagationModel
                    self._prop_model_fallback = HFPropagationModel(
                        receiver_lat=self.precise_lat,
                        receiver_lon=self.precise_lon,
                        enable_realtime=True
                    )
                prediction = self._prop_model_fallback.predict(station, self.frequency_mhz, dt)
                if prediction.primary_delay_ms > 0:
                    self._last_prediction_meta = {
                        'data_source': prediction.data_source,
                        'model_confidence': prediction.model_confidence,
                        'propagation_mode': prediction.primary_mode,
                    }
                    return (
                        prediction.primary_delay_ms,
                        prediction.distance_km,
                        prediction.primary_uncertainty_1sigma_ms  # explicit 1-sigma (P-H13)
                    )
            except Exception as e:
                logger.debug(f"HFPropagationModel fallback failed: {e}")
        
        # Last resort: 1-hop slant-range geometric fallback (M-M5).
        #
        # The previous "light_time × 1.15" heuristic fabricated a 15 %
        # propagation overhead that has no physical basis — both the
        # geometric slant and the ionospheric delay depend on path length
        # and (for the iono term) on frequency, neither of which the
        # ×1.15 multiplier captures. A 500 km path overstated delay by
        # ~1.5 ms; a 5000 km path understated it by ~5 ms; and the
        # frequency-blind iono part scaled wrong by ~25× across the
        # 2.5–25 MHz broadcast bands.
        #
        # Use the shared spherical-Earth hop model and the standard
        # 40.3/f² group-delay term against a climatological slant TEC
        # per hop — same recipe as `propagation_engine._estimate_geometric`
        # (P-M19), so the fallback agrees with the primary path when
        # both run on the same geometry.
        from .wwv_constants import STATION_LOCATIONS
        STATIONS = {k: {'lat': v['lat'], 'lon': v['lon']} for k, v in STATION_LOCATIONS.items()}

        if station not in STATIONS or self.precise_lat is None or self.precise_lon is None:
            return 0.0, 0.0, 500.0  # Blind fallback

        st = STATIONS[station]
        dist_km = _great_circle_km(
            self.precise_lat, self.precise_lon, st['lat'], st['lon']
        )
        expected_delay_ms, uncertainty_ms = _vacuum_hop_fallback_delay(
            dist_km, self.frequency_hz
        )

        self._last_prediction_meta = {
            'data_source': 'vacuum_fallback',
            'model_confidence': 0.0,
            'propagation_mode': 'vacuum',
        }
        return expected_delay_ms, dist_km, uncertainty_ms

    @staticmethod
    def _get_tone_duration(station_name: str, sec_in_minute: int, minute_in_hour: int = 0) -> float:
        """
        Return the correct tone duration (seconds) for a given station and second.
        
        This ensures the matched filter template matches the actual signal duration,
        maximizing processing gain.  Key durations:
        
        WWV/WWVH (shared channels):
            Second 0:  0.800s  (minute marker — PRIMARY timing anchor)
            Others:    0.0     (5ms ticks DROPPED: ±50ms jitter, confounded
                                by 2nd harmonics of 500/600 Hz tones)
            
        CHU (unique channels):
            Second 0:  0.500s  (minute marker — PRIMARY timing anchor)
            Seconds 1-28, 30, 40-49: 0.300s  (300ms tones — excellent)
            Seconds 31-39: 0.0  (FSK seconds — no tone)
            Seconds 50-59: 0.0  (voice seconds — no tone)
            
        BPM:
            Second 0:  0.300s  (minute marker)
            UT1 minutes (25-29, 55-59): 0.100s  (100ms ticks — usable)
            UTC minutes: 0.0   (10ms ticks DROPPED: same jitter problem)
        """
        if station_name in ('WWV', 'WWVH'):
            if sec_in_minute == 0:
                return 0.800  # Minute marker — PRIMARY timing anchor
            else:
                return 0.0    # Drop 5ms ticks: ±50ms jitter, confounded by
                              # 2nd harmonics of 500/600 Hz tones on shared channels
        
        elif station_name == 'CHU':
            if sec_in_minute == 0:
                return 0.500  # Minute marker — PRIMARY timing anchor
            elif sec_in_minute in range(31, 40):
                return 0.0    # FSK seconds — no tone to correlate
            elif sec_in_minute in range(50, 60):
                return 0.0    # Voice seconds — no tone to correlate
            else:
                return 0.300  # 300ms tones — excellent timing source
        
        elif station_name == 'BPM':
            if sec_in_minute == 0:
                return 0.300  # Minute marker
            elif minute_in_hour in (25, 26, 27, 28, 29, 55, 56, 57, 58, 59):
                return 0.100  # 100ms UT1 ticks — usable
            else:
                return 0.0    # Drop 10ms UTC ticks: same jitter problem as WWV 5ms
        
        else:
            return 0.0  # Unknown station — skip

    def _check_signal_presence(self, iq_samples: np.ndarray) -> bool:
        """Check whether any tick-frequency energy is present in this buffer.
        
        Examines a 1-second slice from mid-buffer at the primary tick
        frequencies (1000 Hz for WWV/BPM, 1200 Hz for WWVH).  If no
        energy is found, the minute is likely a station ID / silent
        period and tick phase extraction would correlate pure noise.
        
        Returns:
            True if signal energy detected at any tick frequency.
        """
        try:
            from scipy.signal import butter, sosfiltfilt
            
            # Sample 1 second from the middle of the buffer
            mid = len(iq_samples) // 2
            half_sec = self.sample_rate // 2
            start = max(0, mid - half_sec)
            end = min(len(iq_samples), mid + half_sec)
            chunk = iq_samples[start:end]
            
            if len(chunk) < self.sample_rate // 4:
                return False
            
            # AM demodulate: tick frequencies (1000/1200 Hz) are modulation
            # tones that exist in the envelope, not in the baseband IQ.
            envelope = np.abs(chunk)
            
            # Check each tick frequency used on this channel
            channel_upper = self.channel_name.upper()
            if 'CHU' in channel_upper:
                freqs = [1000]
            elif 'SHARED' in channel_upper:
                freqs = [1000, 1200]  # WWV + WWVH
            else:
                freqs = [1000]
            
            nyquist = self.sample_rate / 2
            noise_power = float(np.mean(envelope**2))
            if noise_power <= 0:
                return False
            
            for freq in freqs:
                bw = 100.0  # ±100 Hz
                low, high = freq - bw, freq + bw
                if low <= 0 or high >= nyquist:
                    continue
                sos = butter(4, [low, high], btype='band', fs=self.sample_rate, output='sos')
                filtered = sosfiltfilt(sos, envelope)
                band_power = float(np.mean(filtered**2))
                
                # Band power relative to total power — if the tone is present,
                # the 200 Hz band should contain a meaningful fraction of energy.
                # Threshold: -80 dB absolute or 10× above expected noise floor
                # in a 200 Hz band (noise_power * 200/nyquist).
                expected_noise_in_band = noise_power * (200.0 / nyquist)
                if expected_noise_in_band > 0 and band_power > 3.0 * expected_noise_in_band:
                    return True
            
            return False
        except Exception as e:
            logger.warning(f"Signal presence check failed: {e}")
            return True  # Fail open — run tick filter if check fails

    def _find_all_correlation_peaks(
        self,
        correlation: np.ndarray,
        dominant_peak_idx: int,
        noise_envelope: np.ndarray,
        n_template: int,
        start_sample: int,
        min_corr_snr_db: float = 7.42,  # S4-finish: bumped 1.42 dB to match
                                        # the median→σ shift; preserves the
                                        # historical 6 dB-in-peak/median gate.
        max_peaks: int = 6,
        mainlobe_samples: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """
        Find all significant peaks in the correlation envelope.

        Each peak represents a distinct propagation path (e.g. 2F2, 3F2, 4F2
        arriving at different delays).  Peaks are separated by at least
        ``mainlobe_samples`` so a single arrival's autocorrelation lobe is
        not re-detected as multiple peaks.

        Args:
            correlation:       Full correlation envelope (already computed).
            dominant_peak_idx: Index of the already-identified dominant peak.
            noise_envelope:    1-D noise-region slice of ``correlation`` —
                               a Rayleigh envelope.  σ̂ is recovered via
                               :func:`core.snr.peak_snr_db_envelope` (S4-finish)
                               so this site reports SNR consistently with
                               the rest of the codebase.
            n_template:        Template length in samples.
            start_sample:      Offset of measurement_region from audio_signal start.
            min_corr_snr_db:   Minimum corr SNR for a secondary peak to be recorded.
            max_peaks:         Maximum number of peaks to return (including dominant).
            mainlobe_samples:  Half-width (in samples) of the suppression
                               region around each found peak.  Defaults to
                               ``n_template // 2`` — the half-amplitude
                               width of the autocorrelation main lobe for
                               a windowed sinusoidal template (M-M8).

                               The previous default of ``n_template``
                               (full main-lobe radius) erased every
                               multipath arrival closer than the template
                               duration — for the 800 ms minute marker,
                               that meant 1–800 ms multipath was
                               unreportable.  ``n_template // 2`` is the
                               Rayleigh-resolution criterion; closer
                               multipath fundamentally requires CLEAN
                               deconvolution (see
                               :meth:`TickEdgeDetector._clean_deconvolve`),
                               which this plain peak finder does not do.

        Returns:
            List of dicts, each with keys:
                peak_rank        int   0 = dominant, 1 = next strongest, ...
                peak_idx         int   index into correlation array
                arrival_sample   int   index into audio_signal
                corr_snr_db      float correlation SNR of this peak
                peak_value       float raw correlation value
        """
        if len(correlation) == 0 or len(noise_envelope) == 0:
            return []

        if mainlobe_samples is None:
            mainlobe_samples = max(1, n_template // 2)

        peaks: List[Dict[str, Any]] = []
        suppressed = np.zeros(len(correlation), dtype=bool)

        for rank in range(max_peaks):
            search = np.where(suppressed, 0.0, correlation)
            if search.max() <= 0:
                break

            idx = int(np.argmax(search))
            val = float(correlation[idx])
            snr_db = peak_snr_db_envelope(val, noise_envelope)

            if not np.isfinite(snr_db) or snr_db < min_corr_snr_db:
                break

            peaks.append({
                'peak_rank': rank,
                'peak_idx': idx,
                'arrival_sample': start_sample + idx,
                'corr_snr_db': float(snr_db),
                'peak_value': val,
            })

            lo = max(0, idx - mainlobe_samples)
            hi = min(len(suppressed), idx + mainlobe_samples + 1)
            suppressed[lo:hi] = True

        return peaks

    def _measure_tone_at_known_time(
        self,
        audio_signal: np.ndarray,
        expected_delay_ms: float,
        tone_freq_hz: float,
        tone_duration_sec: float,
        station_name: str,
        search_window_ms: Optional[float] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Measure a tone at a KNOWN position in the buffer.
        
        expected_delay_ms is the expected arrival time in milliseconds from
        buffer sample 0. This can be anywhere in the buffer — the caller
        (process_minute) uses BufferTiming to compute the correct position.
        
        Returns arrival_ms relative to buffer sample 0 (not minute boundary).
        The caller converts to minute-boundary-relative using BufferTiming.
        
        ALWAYS returns a measurement dict (never None) so that rejected
        attempts are recorded for threshold calibration.  The 'detected'
        flag indicates whether the measurement passed all quality gates.
        'rejection_reason' explains why it was rejected (None if accepted).
        
        Args:
            audio_signal: AM-demodulated audio (magnitude - mean)
            expected_delay_ms: Expected arrival time from buffer start (ms)
            tone_freq_hz: Tone frequency (1000 or 1200 Hz)
            tone_duration_sec: Expected tone duration (0.8s WWV, 0.5s CHU)
            station_name: Station identifier for logging
            search_window_ms: Search window half-width in ms from physics model.
                If None, uses legacy fixed window based on tone duration.
            
        Returns:
            Dict with measurement results.  'detected' is True only if all
            quality gates passed.  Always contains at least station, frequency,
            expected_delay_ms, and whatever metrics could be computed.
        """
        # Base result returned on early exits when no correlation is possible
        base_result = {
            'station': station_name,
            'frequency_hz': tone_freq_hz,
            'expected_delay_ms': expected_delay_ms,
            'arrival_ms': expected_delay_ms,
            'timing_error_ms': 0.0,
            'snr_db': -99.0,
            'corr_snr_db': -99.0,
            'tone_power': 0.0,
            'peak_correlation': 0.0,
            'detected': False,
            'rejection_reason': None,
        }
        from scipy import signal as scipy_signal
        from scipy.fft import rfft, rfftfreq
        
        expected_sample = int(expected_delay_ms * self.sample_rate / 1000)
        
        # Measurement window must be large enough for the template + search margin
        # AND enough extra for a clean noise floor estimate in the correlation output.
        # mode='valid' correlation output length = len(region) - len(template) + 1.
        # We want at least 2× template length of correlation output so there's
        # ample noise-only region on both sides of the peak for SNR estimation.
        # For 800ms template: need ±(0.8 + 0.8 + 0.5) = ±2.1s → 50k region → 31k corr samples.
        search_margin_sec = 0.5
        noise_margin_sec = tone_duration_sec  # extra room for noise floor estimation
        window_sec = max(0.4, tone_duration_sec + noise_margin_sec + search_margin_sec)
        window_samples = int(window_sec * self.sample_rate)
        start_sample = max(0, expected_sample - window_samples)
        end_sample = min(len(audio_signal), expected_sample + window_samples)
        
        if end_sample <= start_sample:
            base_result['rejection_reason'] = 'window_invalid'
            return base_result
            
        measurement_region = audio_signal[start_sample:end_sample]
        
        # Bandpass filter the measurement region to isolate the tone frequency.
        # On shared channels, competing stations (WWV 1000Hz vs WWVH 1200Hz)
        # and broadband noise corrupt the correlation, especially for long
        # templates (800ms) where out-of-band energy accumulates.
        # ±50 Hz bandwidth is narrow enough to reject the competing tone
        # (200 Hz away) while wide enough to preserve the tone onset/offset.
        nyquist = self.sample_rate / 2
        bw = 50.0  # ±50 Hz
        low_hz = max(1.0, tone_freq_hz - bw)
        high_hz = min(nyquist - 1.0, tone_freq_hz + bw)
        if high_hz > low_hz and len(measurement_region) > 100:
            sos = scipy_signal.butter(4, [low_hz, high_hz], btype='band',
                                      fs=self.sample_rate, output='sos')
            measurement_region = scipy_signal.sosfiltfilt(sos, measurement_region)
        
        n_template = int(tone_duration_sec * self.sample_rate)
        t = np.arange(n_template) / self.sample_rate
        
        # Quadrature templates for phase-invariant detection
        window = scipy_signal.windows.tukey(n_template, alpha=0.1)
        template_sin = np.sin(2 * np.pi * tone_freq_hz * t) * window
        template_cos = np.cos(2 * np.pi * tone_freq_hz * t) * window
        
        # Normalize to unit energy
        template_sin /= np.linalg.norm(template_sin)
        template_cos /= np.linalg.norm(template_cos)
        
        # Correlate with full-duration template
        corr_sin = scipy_signal.correlate(measurement_region, template_sin, mode='valid')
        corr_cos = scipy_signal.correlate(measurement_region, template_cos, mode='valid')
        correlation = np.sqrt(corr_sin**2 + corr_cos**2)
        
        if len(correlation) == 0:
            base_result['rejection_reason'] = 'correlation_empty'
            return base_result
        
        # Search window: prefer physics-model-derived adaptive window.
        # Falls back to legacy fixed window if no model is available.
        #
        # The adaptive window from the physics model directly improves
        # weak-signal sensitivity by reducing the number of independent
        # noise samples in the search region.  See
        # docs/design/UNIFIED_MEASUREMENT_PATH.md for the full analysis.
        #
        # Legacy fallback rationale (kept for backward compatibility):
        #   5ms tick:    ±50ms  (ionospheric variation ~30ms)
        #   100ms tone:  ±100ms
        #   300ms+ tone: ±100ms (physics-constrained)
        #   800ms marker: ±100ms (physics-constrained)
        if search_window_ms is not None:
            SEARCH_WINDOW_MS = max(5.0, min(200.0, search_window_ms))
        else:
            SEARCH_WINDOW_MS = max(50.0, min(100.0, tone_duration_sec * 625))
        
        # expected_corr_idx is relative to measurement_region (which starts at start_sample)
        expected_corr_idx = expected_sample - start_sample
        window_samples = int(SEARCH_WINDOW_MS * self.sample_rate / 1000)
        
        search_start = max(0, expected_corr_idx - window_samples)
        search_end = min(len(correlation), expected_corr_idx + window_samples)
        
        if search_end <= search_start:
            logger.debug(f"{station_name}: Search window invalid - search_start={search_start}, search_end={search_end}, corr_len={len(correlation)}")
            base_result['rejection_reason'] = 'search_window_invalid'
            return base_result
        
        # Find peak within constrained window
        search_region = correlation[search_start:search_end]
        local_peak_idx = np.argmax(search_region)
        peak_idx = search_start + local_peak_idx
        peak_val = correlation[peak_idx]
        
        # VALIDATION: Reject if peak is at edge of search window (likely noise/flat correlation)
        # A real tone should produce a clear peak away from the edges
        edge_margin = min(50, len(search_region) // 10)  # At least 50 samples or 10% from edge
        if local_peak_idx < edge_margin or local_peak_idx > len(search_region) - edge_margin:
            # Check if correlation is essentially flat (noise)
            corr_range = np.max(search_region) - np.min(search_region)
            corr_mean = np.mean(search_region)
            if corr_mean > 0 and corr_range / corr_mean < 0.5:  # Less than 50% variation = flat
                logger.debug(f"{station_name}: Correlation flat/noisy - peak at edge "
                            f"(local_peak={local_peak_idx}, range/mean={corr_range/corr_mean:.2f})")
                base_result['rejection_reason'] = 'correlation_flat'
                base_result['peak_correlation'] = float(peak_val)
                base_result['corr_snr_db'] = 0.0
                return base_result
        
        # Step 3: VALIDATE correlation quality
        # Estimate noise floor from correlation values well away from the peak.
        # For long templates the signal can fill most of the correlation output,
        # so exclude a region proportional to the template length to avoid
        # contaminating the noise estimate with signal energy.
        #
        # Use full template length as exclusion (not half) — the correlation
        # plateau from a real signal extends ±template_length around the peak.
        exclusion = max(100, n_template)
        noise_region = np.concatenate([
            correlation[:max(0, peak_idx - exclusion)],
            correlation[min(len(correlation), peak_idx + exclusion):]
        ])
        
        # S4-finish: use the canonical Rayleigh-envelope SNR helper so
        # this site agrees with `tick_edge_detector` and
        # `tick_matched_filter._correlate_tick_iq` (both migrated under
        # the M-M1/M-M3 cluster).  The previous ``20·log10(peak/median)``
        # absorbed the 1.1774× median-to-σ factor implicitly and reported
        # an SNR ~1.4 dB lower than the canonical definition; the
        # `MIN_CORR_SNR_DB = 8.0` gate below was tuned against that
        # offset and now sees the canonical value.
        if len(noise_region) > 10:
            snr_noise_region = noise_region
        else:
            # Not enough noise-only samples — fall back to the lower
            # half of the full correlation, which is dominated by noise
            # for any real peak.
            if len(correlation) > 0:
                cutoff = np.percentile(correlation, 50)
                snr_noise_region = correlation[correlation <= cutoff]
                if len(snr_noise_region) == 0:
                    snr_noise_region = correlation
            else:
                snr_noise_region = np.asarray([1.0])

        corr_snr_db = peak_snr_db_envelope(float(peak_val), snr_noise_region)
        if not np.isfinite(corr_snr_db):
            corr_snr_db = 0.0
        # `noise_floor` is retained for diagnostic logging/threshold display below.
        noise_floor = float(np.median(snr_noise_region)) if len(snr_noise_region) > 0 else 1.0
        
        # Fixed correlation SNR threshold for all tone durations.
        # Templates are normalized to unit energy, so peak height does NOT
        # scale with duration.  The old duration-scaled threshold (8 + 10*log10(dur/0.1))
        # was killing the 800ms minute marker (required 17 dB, measured 2.6 dB)
        # because the noise floor was contaminated by the signal itself.
        #
        # S4-finish recalibration: the historical 8.0 dB gate was in
        # ``peak/median(env)`` units; the migration to the canonical
        # ``peak/σ̂`` definition (σ̂ = median/√(2 ln 2)) shifts the
        # reported value up by 20·log10(1.1774) ≈ 1.42 dB.  Bumping the
        # gate by the same 1.42 dB keeps the underlying false-rejection
        # rate identical to pre-migration behaviour.
        MIN_CORR_SNR_DB = 9.42
        if corr_snr_db < MIN_CORR_SNR_DB:
            logger.info(f"{station_name}: Correlation too weak "
                        f"(corr_SNR={corr_snr_db:.1f}dB < {MIN_CORR_SNR_DB:.1f}dB, expected={expected_delay_ms:.1f}ms, "
                        f"peak_idx={peak_idx}, peak={peak_val:.4f}, noise={noise_floor:.4f})")
            # Still compute arrival so the rejection is a complete record
            arrival_sample_rej = start_sample + peak_idx
            raw_arrival_ms_rej = arrival_sample_rej * 1000 / self.sample_rate
            base_result['rejection_reason'] = 'corr_snr_low'
            base_result['corr_snr_db'] = float(corr_snr_db)
            base_result['snr_db'] = float(corr_snr_db)
            base_result['peak_correlation'] = float(peak_val)
            base_result['arrival_ms'] = float(raw_arrival_ms_rej)
            base_result['timing_error_ms'] = float(raw_arrival_ms_rej - expected_delay_ms)
            base_result['corr_snr_threshold_db'] = float(MIN_CORR_SNR_DB)
            return base_result
        
        # Cross-frequency discrimination gate (WWV 1000Hz vs WWVH 1200Hz).
        # A 5ms template has 33% cross-response between 1000↔1200 Hz, so a strong
        # WWV tick produces a correlation peak on the WWVH template (and vice versa).
        # Fix: correlate the same region at the competing frequency and reject if
        # the claimed frequency doesn't dominate.
        CROSS_FREQ_PAIRS = {1000: 1200, 1200: 1000}  # WWV↔WWVH
        cross_freq = CROSS_FREQ_PAIRS.get(int(tone_freq_hz))
        if cross_freq is not None:
            # Build cross-frequency template (same duration, different freq)
            cross_sin = np.sin(2 * np.pi * cross_freq * t) * window
            cross_cos = np.cos(2 * np.pi * cross_freq * t) * window
            cross_sin /= np.linalg.norm(cross_sin)
            cross_cos /= np.linalg.norm(cross_cos)
            
            # Correlate at the same peak location
            cross_corr_sin = scipy_signal.correlate(measurement_region, cross_sin, mode='valid')
            cross_corr_cos = scipy_signal.correlate(measurement_region, cross_cos, mode='valid')
            cross_env = np.sqrt(cross_corr_sin**2 + cross_corr_cos**2)
            
            # Compare at the same peak index
            if peak_idx < len(cross_env):
                cross_peak = cross_env[peak_idx]
                if cross_peak > 0:
                    freq_advantage_db = 20 * np.log10(peak_val / cross_peak)
                else:
                    freq_advantage_db = 40.0
                
                # Require claimed frequency to be at least 3 dB stronger than cross-freq.
                # Clean single-frequency signal: ~10 dB advantage.
                # Cross-talk from other station: ~0 dB or negative.
                MIN_FREQ_ADVANTAGE_DB = 3.0
                if freq_advantage_db < MIN_FREQ_ADVANTAGE_DB:
                    arrival_sample_rej = start_sample + peak_idx
                    raw_arrival_ms_rej = arrival_sample_rej * 1000 / self.sample_rate
                    logger.debug(f"{station_name} @ {tone_freq_hz}Hz: REJECTED cross-talk "
                                f"(advantage={freq_advantage_db:+.1f}dB < {MIN_FREQ_ADVANTAGE_DB}dB, "
                                f"peak={peak_val:.4f}, cross={cross_peak:.4f})")
                    return {
                        'station': station_name,
                        'frequency_hz': tone_freq_hz,
                        'arrival_ms': float(raw_arrival_ms_rej),
                        'expected_delay_ms': expected_delay_ms,
                        'timing_error_ms': float(raw_arrival_ms_rej - expected_delay_ms),
                        'snr_db': float(corr_snr_db),
                        'corr_snr_db': float(corr_snr_db),
                        'tone_power': 0.0,
                        'peak_correlation': float(peak_val),
                        'detected': False,
                        'rejection_reason': 'cross_freq',
                    }
        
        # Step 2: Measure tone SNR at the DETECTED peak location (not expected location)
        # This handles buffer alignment issues where tone arrives later than expected
        tone_start = max(0, peak_idx)
        tone_end = min(len(measurement_region), tone_start + int(tone_duration_sec * self.sample_rate))
        
        if tone_end - tone_start >= int(0.1 * self.sample_rate):
            tone_segment = measurement_region[tone_start:tone_end]
            windowed = tone_segment * scipy_signal.windows.hann(len(tone_segment))
            fft_result = rfft(windowed)
            freqs = rfftfreq(len(windowed), 1/self.sample_rate)
            
            freq_idx = np.argmin(np.abs(freqs - tone_freq_hz))
            tone_power = np.abs(fft_result[freq_idx])**2
            
            noise_bins = np.concatenate([
                np.arange(max(0, freq_idx - 50), max(0, freq_idx - 10)),
                np.arange(min(len(fft_result), freq_idx + 10), min(len(fft_result), freq_idx + 50))
            ])
            if len(noise_bins) > 5:
                noise_power = np.mean(np.abs(fft_result[noise_bins.astype(int)])**2)
            else:
                noise_power = np.mean(np.abs(fft_result)**2)
            
            tone_snr_db = 10 * np.log10(tone_power / noise_power) if noise_power > 0 else 0.0
        else:
            tone_snr_db = corr_snr_db  # Fallback to correlation SNR
            tone_power = peak_val
        
        # Sub-sample interpolation
        sub_sample_offset = 0.0
        if 0 < peak_idx < len(correlation) - 1:
            y_m1 = correlation[peak_idx - 1]
            y_0 = correlation[peak_idx]
            y_p1 = correlation[peak_idx + 1]
            denom = y_m1 - 2*y_0 + y_p1
            if abs(denom) > 1e-10:
                sub_sample_offset = 0.5 * (y_m1 - y_p1) / denom
                sub_sample_offset = max(-0.5, min(0.5, sub_sample_offset))
        
        precise_peak_idx = peak_idx + sub_sample_offset
        
        # Leading-edge back-calculation for long tones (minute markers and 300ms+ ticks).
        #
        # Signal model determines where the correlation peak lands relative to tone onset:
        #
        #   WWV/WWVH/BPM: The AM envelope of a continuous carrier is nearly flat DC.
        #     The minute marker appears as a rectangular ON/OFF pulse in the envelope.
        #     Correlating a sinusoidal template against a rectangular pulse: the peak
        #     lands at the CENTRE of the pulse (half_template after onset).
        #     → Subtract half_template to recover the leading edge.
        #
        #   CHU: Transmits AM-compatible USB (carrier + upper sideband).  The 1000 Hz
        #     tone is amplitude modulation on the carrier.  The AM envelope after mean
        #     subtraction is a GATED SINUSOID (not a rectangular pulse).  Correlating
        #     a sinusoidal template against a gated sinusoid: the peak lands at the
        #     ONSET of the tone (0 ms offset from leading edge).
        #     → No correction needed; applying -half_template gives the -74ms bias.
        #
        # Verified by simulation: gated sinusoid → peak at onset regardless of phase.
        half_template_samples = n_template / 2.0
        if tone_duration_sec >= 0.3 and station_name != 'CHU':
            leading_edge_idx = precise_peak_idx - half_template_samples
            precise_peak_idx = leading_edge_idx
            logger.debug(f"{station_name}: Leading edge correction applied "
                        f"(-{half_template_samples/self.sample_rate*1000:.1f}ms for {tone_duration_sec*1000:.0f}ms tone)")
        elif station_name == 'CHU':
            logger.debug(f"CHU: No leading-edge correction (gated sinusoid, peak=onset)")
        
        # Convert to arrival time (ms from minute boundary)
        # For mode='valid', peak_idx=0 means template starts at sample 0 of measurement_region
        # The tone ONSET is at the start of the template alignment
        arrival_sample = start_sample + precise_peak_idx
        raw_arrival_ms = arrival_sample * 1000 / self.sample_rate
        
        # Timing is measured from RTP timestamp (sample 0 = minute boundary)
        # Timing error = measured_arrival - expected_propagation_delay
        timing_error_ms = raw_arrival_ms - expected_delay_ms
        
        # PROPAGATION BOUNDS VALIDATION (2026-02-05, updated 2026-02-09)
        # Validate that the measured arrival time is within tolerance of expected.
        # expected_delay_ms already includes tx_offset (e.g., 1000ms for CHU second 1).
        # RTP timestamps are authoritative (no wall-clock calibration bias).
        # Allow ±500ms to accommodate multi-hop ionospheric paths on lower
        # frequencies.  The physics validation downstream (arrival matrix with
        # ±50ms window) is the real quality gate.  This gate only prevents
        # obviously wrong detections (e.g., locking onto an adjacent second).
        ARRIVAL_TOLERANCE_MS = 500.0
        
        if abs(timing_error_ms) > ARRIVAL_TOLERANCE_MS:
            logger.info(f"{station_name} @ {tone_freq_hz}Hz: REJECTED - arrival={raw_arrival_ms:.2f}ms "
                       f"error={timing_error_ms:+.1f}ms exceeds ±{ARRIVAL_TOLERANCE_MS:.0f}ms "
                       f"(expected={expected_delay_ms:.1f}ms, corr_SNR={corr_snr_db:.1f}dB)")
            return {
                'station': station_name,
                'frequency_hz': tone_freq_hz,
                'arrival_ms': raw_arrival_ms,
                'expected_delay_ms': expected_delay_ms,
                'timing_error_ms': timing_error_ms,
                'snr_db': tone_snr_db,
                'corr_snr_db': float(corr_snr_db),
                'tone_power': tone_power,
                'peak_correlation': float(peak_val),
                'detected': False,
                'rejection_reason': 'arrival_tolerance',
            }
        
        # BPM-specific: Require higher SNR due to shorter template (more false positives)
        if station_name == 'BPM':
            MIN_BPM_SNR_DB = 12.0
            if tone_snr_db < MIN_BPM_SNR_DB:
                logger.info(f"{station_name} @ {tone_freq_hz}Hz: REJECTED - SNR={tone_snr_db:.1f}dB "
                           f"< {MIN_BPM_SNR_DB}dB minimum for BPM")
                return {
                    'station': station_name,
                    'frequency_hz': tone_freq_hz,
                    'arrival_ms': raw_arrival_ms,
                    'expected_delay_ms': expected_delay_ms,
                    'timing_error_ms': timing_error_ms,
                    'snr_db': tone_snr_db,
                    'corr_snr_db': float(corr_snr_db),
                    'tone_power': tone_power,
                    'peak_correlation': float(peak_val),
                    'detected': False,
                    'rejection_reason': 'bpm_snr_low',
                }
        
        logger.info(f"{station_name} @ {tone_freq_hz}Hz: DETECTED arrival={raw_arrival_ms:.2f}ms "
                   f"(expected={expected_delay_ms:.1f}ms), error={timing_error_ms:+.2f}ms, "
                   f"corr_SNR={corr_snr_db:.1f}dB")
        
        # Multi-path arrival search: find all significant peaks in the full
        # correlation output.  Each peak above the SNR threshold and separated
        # by at least one template length represents a distinct propagation path
        # (e.g. 2F2, 3F2, 4F2 arriving at different delays).  The dominant peak
        # (rank 0) is the one already identified above; secondary peaks are
        # additional arrivals recorded for ionospheric science.
        if self.enable_physics_products:
            all_arrivals = self._find_all_correlation_peaks(
                correlation=correlation,
                dominant_peak_idx=peak_idx,
                noise_envelope=snr_noise_region,
                n_template=n_template,
                start_sample=start_sample,
            )
        else:
            all_arrivals = []
        # Annotate each arrival with its timing relative to minute boundary
        for arr in all_arrivals:
            arr_ms = arr['arrival_sample'] * 1000.0 / self.sample_rate
            arr['arrival_ms'] = arr_ms
            arr['timing_error_ms'] = arr_ms - expected_delay_ms
        if len(all_arrivals) > 1:
            logger.info(f"{station_name} @ {tone_freq_hz}Hz: {len(all_arrivals)} arrivals "
                       f"(multi-path): " +
                       ", ".join(f"rank{a['peak_rank']}={a['timing_error_ms']:+.1f}ms "
                                 f"({a['corr_snr_db']:.1f}dB)"
                                 for a in all_arrivals))

        # Include model metadata for traceability (M4)
        meta = getattr(self, '_last_prediction_meta', {})
        return {
            'station': station_name,
            'frequency_hz': tone_freq_hz,
            'arrival_ms': raw_arrival_ms,  # Arrival relative to minute boundary
            'expected_delay_ms': expected_delay_ms,
            'timing_error_ms': timing_error_ms,
            'snr_db': tone_snr_db,
            'corr_snr_db': float(corr_snr_db),
            'tone_power': tone_power,
            'peak_correlation': float(peak_val),
            'detected': True,
            'rejection_reason': None,
            'model_data_source': meta.get('data_source', ''),
            'model_confidence': meta.get('model_confidence', 0.0),
            'propagation_mode': meta.get('propagation_mode', ''),
            'all_arrivals': all_arrivals,  # All detected propagation paths
        }

    def process_minute(
        self,
        iq_samples: np.ndarray,
        system_time: float,
        rtp_timestamp: int,
        buffer_timing=None
    ) -> List[L1MetrologyMeasurement]:
        """
        Process minute: Tone Detection + Channel Char -> L1 Measurements.
        
        Unified detection path: both RTP and Fusion modes use the same
        per-second correlator when BufferTiming is available.  In Fusion
        mode, UTC estimate uncertainty from FusionTimingState is added
        to the physics model uncertainty (quadrature).  Post-detection,
        RTP mode logs GPS+PPS residuals; Fusion mode feeds the Kalman
        filter and chrony SHM.
        
        See docs/design/UNIFIED_MEASUREMENT_PATH.md for full design.
        
        Args:
            iq_samples: Raw IQ buffer (complex64)
            system_time: UTC timestamp (from metadata, may be inaccurate)
            rtp_timestamp: RTP counter at buffer start
            buffer_timing: BufferTiming object mapping samples to UTC.
                          If provided, overrides system_time for all timing.
        """
        # Derive the minute boundary from the authoritative timing source
        # (M-M6).  `system_time` is the writer's start-of-buffer wall-clock
        # estimate from its OWN (possibly stale) GPS/RTP mapping; if a
        # radiod restart left that mapping seconds-wrong, every
        # `process_minute` call inherited that drift and tone-schedule
        # decisions — including BPM UT1-vs-UTC second classification —
        # would slip.  When buffer_timing is present, use the RTP-anchored
        # sample0 UTC instead.
        if buffer_timing is not None:
            buffer_anchor_utc = buffer_timing.sample_to_utc(0)
        else:
            buffer_anchor_utc = system_time
        minute_boundary = round(buffer_anchor_utc / 60) * 60
        minute_number = int((minute_boundary // 60) % 60)

        iq_samples, _ = self._validate_input(iq_samples)
        
        # Buffer mid-time for timestamp calculations
        if buffer_timing is not None:
            buffer_mid_time = buffer_timing.sample_to_utc(len(iq_samples) / 2)
        else:
            buffer_mid_time = system_time + len(iq_samples) / self.sample_rate / 2
        
        # Expand pre-allocated buffer if needed
        n_samples = len(iq_samples)
        if n_samples > self._max_samples:
            self._max_samples = n_samples + 5 * self.sample_rate
            self._envelope_buffer = np.empty(self._max_samples, dtype=np.float32)
            
        # === Step 0: Carrier SNR Check ===
        # Don't attempt detection if carrier is too weak.
        envelope = self._envelope_buffer[:n_samples]
        np.abs(iq_samples, out=envelope)
        carrier_amplitude = np.mean(envelope)
        mad = np.median(np.abs(envelope - np.median(envelope)))
        noise_std = 1.4826 * mad
        
        if noise_std > 0 and carrier_amplitude > 0:
            carrier_snr_db = 20 * np.log10(carrier_amplitude / noise_std)
        else:
            carrier_snr_db = -100.0
        
        # Log carrier SNR but don't gate on it — the matched filter can detect
        # signals well below the carrier noise floor.  That's its whole purpose.
        if carrier_snr_db < 2.0:
            logger.info(f"{self.channel_name}: Carrier SNR very low "
                       f"({carrier_snr_db:.1f}dB) — matched filter may still detect")
        
        # Demodulation:
        # All stations use AM envelope (|IQ| - DC) for TIMING correlation.
        # The leading-edge back-calculation in _measure_tone_at_known_time
        # assumes the correlation peak lands at the tone center, which is only
        # true for AM envelope detection.
        #
        # CHU transmits USB with preserved carrier, but for timing purposes
        # the AM envelope (|IQ| - DC) is correct: the 1000 Hz tone keying
        # appears as amplitude modulation and the envelope peak is at the
        # tone center regardless of carrier phase.  Using np.real(IQ) instead
        # produces a carrier-phase-dependent peak offset that caused the
        # observed -77 ms systematic on all CHU channels.
        #
        # The raw IQ (iq_samples) is still passed to the edge detector for
        # carrier phase / Doppler extraction, which correctly uses IQ mixing.
        audio_signal = envelope - np.mean(envelope)
        
        # Compute expected delays and uncertainties for all stations using physics model
        expected_delays_by_station = {}
        expected_uncertainty_by_station = {}
        for station in ['WWV', 'WWVH', 'CHU', 'BPM']:
            expected_delay_ms, dist_km, uncertainty_ms = self._predict_geometric_delay(
                station, system_time
            )
            if expected_delay_ms > 0:
                expected_delays_by_station[station] = expected_delay_ms
                expected_uncertainty_by_station[station] = uncertainty_ms
        
        # edge_results is populated in the RTP branch (Step 1 edge ensemble)
        # and consumed later in Step 2D (tick phase extraction) regardless of mode.
        edge_results = {}
        
        # === UNIFIED DETECTION PATH ===
        # Both RTP and Fusion modes use the same per-second correlator when
        # BufferTiming is available.  This ensures identical detection
        # algorithms, enabling RTP mode to validate Fusion-mode metrology.
        # See docs/design/UNIFIED_MEASUREMENT_PATH.md for design rationale.
        #
        # The only mode-dependent behavior:
        #   - Fusion mode adds UTC estimate uncertainty to the search window
        #   - Post-detection: RTP logs GPS+PPS residual; Fusion feeds Kalman
        
        mode_label = "RTP" if self.is_rtp_authority else "Fusion"
        
        # In Fusion mode, add UTC estimate uncertainty from FusionTimingState
        # to the per-station physics model uncertainty (quadrature sum).
        # In RTP mode, GPS+PPS gives ~50 µs — negligible.
        utc_unc_ms = 0.0
        if not self.is_rtp_authority and self.fusion_state is not None:
            utc_unc_ms = self.fusion_state.get_search_window_ms() / 3.0  # Convert 3σ to 1σ
            logger.info(f"{self.channel_name}: {mode_label} mode: "
                       f"UTC uncertainty ±{utc_unc_ms:.0f}ms (1σ), "
                       f"lock_tier={self.fusion_state.lock_tier.name}")
        
        # Define station templates based on channel type.
        # Tone frequency is per-station; duration is per-second (set in loop).
        channel_upper = self.channel_name.upper()
        if 'CHU' in channel_upper:
            station_tone_freqs = [('CHU', 1000)]
        elif 'WWV_20' in channel_upper or 'WWV_25' in channel_upper:
            station_tone_freqs = [('WWV', 1000)]
        else:
            # SHARED channels: WWV and WWVH only.
            # BPM is EXCLUDED: it uses the same 1000 Hz tone as WWV, so
            # the matched filter cannot distinguish them.  The fig12
            # correlation heatmap shows r=0.91 between "BPM" and WWV
            # Doppler at 10 MHz — confirming that "BPM" detections on
            # shared frequencies are misattributed WWV signals.
            # BPM discrimination would require tick-duration measurement
            # (10ms BPM vs 5ms WWV) which is below our time resolution.
            station_tone_freqs = [
                ('WWV', 1000),
                ('WWVH', 1200),
            ]
        
        measurements = []
        all_attempts = []
        # §3.4 Low: the previous gate compared against 'metadata_fallback'
        # — a source value that buffer_timing.resolve_buffer_timing no
        # longer (and per the current source: comment, ever) produces.  The
        # check was therefore a no-op (always True when buffer_timing was
        # not None).  Compare against the actual non-authoritative value,
        # 'no_timing', so a sentinel BufferTiming correctly skips this
        # branch.
        use_per_second_correlator = (
            buffer_timing is not None and buffer_timing.source != 'no_timing'
        )
        
        if use_per_second_correlator:
            # === Per-Second Correlator (primary path, both modes) ===
            # BufferTiming maps samples↔UTC.  Find which UTC seconds fall
            # within this buffer and measure tones there.
            logger.debug(f"{self.channel_name}: {mode_label} mode - "
                        f"per-second correlator with BufferTiming")
            n_samples = len(audio_signal)
            buf_start_utc = buffer_timing.sample0_utc
            buf_end_utc = buffer_timing.sample_to_utc(n_samples)
            
            for station_name, tone_freq in station_tone_freqs:
                prop_delay_ms = expected_delays_by_station.get(station_name, 20.0)
                prop_delay_sec = prop_delay_ms / 1000.0
                
                # Use the longest possible tone (minute marker) for margin calc
                max_tone_duration = 1.0  # 1s covers all minute markers
                margin_sec = max_tone_duration + 0.5
                
                # Find UTC seconds whose tone arrival falls in the buffer.
                # A tick transmitted at UTC second T arrives at T + prop_delay.
                # We need samples from T + prop_delay through T + prop_delay + margin.
                first_utc_sec = int(buf_start_utc) - 1
                last_utc_sec = int(buf_end_utc) + 1
                
                measurable = []
                for utc_sec in range(first_utc_sec, last_utc_sec + 1):
                    sec_in_minute = utc_sec % 60
                    # Skip silent seconds
                    if station_name == 'CHU' and sec_in_minute == 29:
                        continue
                    if station_name in ('WWV', 'WWVH') and sec_in_minute in (29, 59):
                        continue
                    
                    # CHU regular-second 300ms tones start ~74ms after the
                    # UTC second boundary + propagation delay.
                    #
                    # Evidence chain (definitive):
                    # 1. Direct AM envelope measurement (3ms energy windows,
                    #    BP 950-1050Hz): 1000Hz pip onset at +68-80ms from
                    #    utc_sec (= +62-74ms from utc_sec + prop_delay).
                    # 2. Same measurement for 2225Hz FSK mark tone (seconds
                    #    31-39, NRC spec: T+10ms): onset at +87ms from
                    #    utc_sec (= +71ms from expected T+prop+10ms).
                    # 3. Both 1000Hz and 2225Hz are delayed by ~74ms through
                    #    the IDENTICAL receiver pipeline. WWV shows 0ms
                    #    offset through the same pipeline. The delay is
                    #    CHU-specific and in the transmitted signal.
                    # 4. Root cause: CHU uses H3E (USB + full carrier). The
                    #    transmitter's analog sideband filter introduces a
                    #    group delay of ~74ms on all audio content. NRC's
                    #    ≤1μs spec refers to the atomic clock accuracy, not
                    #    the audio onset relative to the second marker.
                    # 5. FSK stop-bit (T+500ms, phase transition) gives
                    #    timing_offset=+6ms → CHU clock offset is +6ms.
                    #    Using 0.074 gives timing_error ≈ +6ms, consistent.
                    # Second 0 (minute marker, 500ms) starts at 0ms.
                    chu_tx_onset_sec = 0.0
                    if station_name == 'CHU' and sec_in_minute != 0:
                        chu_tx_onset_sec = 0.074

                    tone_arrival_utc = utc_sec + prop_delay_sec + chu_tx_onset_sec
                    tone_end_utc = tone_arrival_utc + margin_sec
                    
                    onset_sample = buffer_timing.utc_to_sample(tone_arrival_utc)
                    end_sample = buffer_timing.utc_to_sample(tone_end_utc)
                    
                    if onset_sample >= 0 and end_sample < n_samples:
                        measurable.append((utc_sec, onset_sample))
                
                if not measurable:
                    logger.debug(f"{self.channel_name}: No {station_name} tones in buffer "
                                f"(buf UTC {buf_start_utc:.1f}–{buf_end_utc:.1f})")
                    continue
                
                # Prioritize: minute marker (sec 0) first, then other seconds.
                # Sort so second 0 comes first for maximum detection probability.
                measurable.sort(key=lambda x: (x[0] % 60 != 0, x[0]))
                
                # Try up to 15 seconds per station (was 5)
                for utc_sec, onset_sample in measurable[:15]:
                    sec_in_minute = utc_sec % 60
                    tone_duration = self._get_tone_duration(
                        station_name, sec_in_minute, minute_number
                    )
                    if tone_duration <= 0:
                        continue  # Silent second — no tone to detect
                    
                    expected_ms_from_buf_start = onset_sample * 1000 / self.sample_rate
                    
                    # Adaptive search window: physics model 1σ + UTC uncertainty
                    # (quadrature), then take 3σ.
                    station_unc_1sigma = expected_uncertainty_by_station.get(station_name)
                    if station_unc_1sigma is not None:
                        combined_1sigma = math.sqrt(station_unc_1sigma**2 + utc_unc_ms**2)
                        adaptive_window = combined_1sigma * 3.0
                    else:
                        adaptive_window = None
                    
                    result = self._measure_tone_at_known_time(
                        audio_signal=audio_signal,
                        expected_delay_ms=expected_ms_from_buf_start,
                        tone_freq_hz=tone_freq,
                        tone_duration_sec=tone_duration,
                        station_name=station_name,
                        search_window_ms=adaptive_window
                    )
                    
                    # Record every attempt for diagnostic summary
                    result['utc_second'] = utc_sec
                    result['tone_duration_sec'] = tone_duration
                    all_attempts.append(result)
                    
                    if result.get('detected'):
                        # arrival_ms is from buffer start.  Convert to UTC.
                        arrival_utc = buffer_timing.sample_to_utc(
                            result['arrival_ms'] * self.sample_rate / 1000
                        )
                        # Expected arrival UTC includes CHU tx_onset offset
                        # so timing_error_ms reflects the true clock offset.
                        chu_tx = 0.074 if (station_name == 'CHU' and utc_sec % 60 != 0) else 0.0
                        expected_utc = utc_sec + prop_delay_sec + chu_tx
                        result['timing_error_ms'] = (arrival_utc - expected_utc) * 1000
                        result['arrival_utc'] = arrival_utc
                        measurements.append(result)
            
            # Per-minute diagnostic: what did we attempt, what passed, what failed and why?
            if all_attempts:
                n_detected = sum(1 for a in all_attempts if a.get('detected'))
                n_rejected = len(all_attempts) - n_detected
                # Count rejection reasons
                reasons = {}
                rejected_snrs = []
                for a in all_attempts:
                    reason = a.get('rejection_reason')
                    if reason:
                        reasons[reason] = reasons.get(reason, 0) + 1
                        if a.get('corr_snr_db', -99) > -99:
                            rejected_snrs.append(a['corr_snr_db'])
                
                reason_str = ', '.join(f"{r}={c}" for r, c in sorted(reasons.items()))
                snr_str = ''
                if rejected_snrs:
                    snr_str = f", rejected SNRs: {min(rejected_snrs):.1f}–{max(rejected_snrs):.1f}dB"
                
                logger.info(f"{self.channel_name}: {mode_label} attempts={len(all_attempts)} "
                           f"detected={n_detected} rejected={n_rejected} "
                           f"[{reason_str}]{snr_str}")
                
                if measurements:
                    secs = [m['utc_second'] % 60 for m in measurements]
                    logger.info(f"{self.channel_name}: {mode_label} detected at seconds {secs}")
            
            # === Per-Second Edge Detection (both modes) ===
            # Run differential edge detector on all per-second ticks.
            # This provides up to 57 independent timing measurements per
            # minute from the tick onset edges, even when the minute marker
            # correlation fails (low SNR, fading, etc.).
            #
            # The edge ensemble augments timing for stations that had NO
            # successful minute marker correlation this minute.
            #
            # BPM is included here (but NOT in the per-second correlator)
            # because the edge detector uses tick-duration-specific templates
            # (10ms BPM vs 5ms WWV) which provide real discrimination.
            # BPM edge results feed the physics pipeline (Doppler, dTEC,
            # carrier phase) for transpolar ionospheric analysis, but do NOT
            # create synthetic timing measurements.
            is_dedicated = ('WWV_20' in channel_upper or 'WWV_25' in channel_upper)
            stations_with_corr = {m['station'] for m in measurements}
            edge_results = {}
            
            # Build edge station list: start from correlator list, add BPM
            # on shared frequencies during its broadcast hours.
            edge_station_freqs = list(station_tone_freqs)
            if ('BPM', 1000) not in edge_station_freqs:
                current_utc_hour = int(buf_start_utc // 3600) % 24
                if (hasattr(self, 'bpm_discriminator')
                        and current_utc_hour in self.bpm_discriminator.active_hours):
                    edge_station_freqs.append(('BPM', 1000))
            
            for station_name, tone_freq in edge_station_freqs:
                prop_delay_ms = expected_delays_by_station.get(station_name, 20.0)
                prop_delay_sec = prop_delay_ms / 1000.0
                
                try:
                    edge_result = self.edge_detector.detect_edges(
                        audio_signal=audio_signal,
                        station=station_name,
                        minute_number=minute_number,
                        buffer_timing=buffer_timing,
                        expected_delay_sec=prop_delay_sec,
                        is_dedicated_channel=is_dedicated,
                        iq_samples=iq_samples,
                    )
                except Exception as e:
                    logger.warning(f"{self.channel_name}: Edge detection failed for "
                                f"{station_name}: {e}")
                    edge_result = None
                
                if edge_result is not None:
                    edge_results[station_name] = edge_result
                    
                    # If this station had NO correlation detection but the
                    # edge ensemble has sufficient confidence, create a
                    # synthetic measurement from the ensemble.
                    # BPM is excluded from timing recovery: transpolar path
                    # is too variable, and on shared frequencies the 10ms
                    # template may still correlate with WWV's 5ms ticks.
                    # BPM edge results still feed the physics pipeline
                    # (tick_phase → Doppler → dTEC) via edge_results dict.
                    if (station_name != 'BPM'
                            and station_name not in stations_with_corr
                            and edge_result.confidence >= 0.3
                            and edge_result.ensemble_n_edges >= 5):
                        
                        # The ensemble timing_error is relative to expected
                        # propagation delay.  Convert to arrival_ms from
                        # buffer start, matching the correlation output
                        # format.  Use the buffer midpoint as the reference
                        # tick second, *without* truncating to a whole
                        # second — the prior `int(mid_utc)` (M-M7) discarded
                        # up to 0.5 s of fractional offset, so the recorded
                        # `arrival_ms` and `timing_error_ms` disagreed on
                        # the on-time marker by ±0.5 s.  `utc_second` is
                        # still an integer label for the tick (closest
                        # second, via round, not floor).
                        mid_utc = (buf_start_utc + buf_end_utc) / 2.0
                        utc_second = int(round(mid_utc))
                        synth_arrival_utc = (
                            mid_utc
                            + prop_delay_sec
                            + edge_result.ensemble_timing_error_ms / 1000.0
                        )
                        synth_arrival_sample = buffer_timing.utc_to_sample(synth_arrival_utc)
                        synth_arrival_ms = synth_arrival_sample * 1000 / self.sample_rate
                        
                        synth_measurement = {
                            'station': station_name,
                            'frequency_hz': tone_freq,
                            'arrival_ms': synth_arrival_ms,
                            'expected_delay_ms': prop_delay_ms,
                            'timing_error_ms': edge_result.ensemble_timing_error_ms,
                            'snr_db': edge_result.mean_edge_snr_db,
                            'corr_snr_db': edge_result.mean_edge_snr_db,
                            'tone_power': 0.0,
                            'peak_correlation': 0.0,
                            'detected': True,
                            'rejection_reason': None,
                            'utc_second': utc_second,
                            'tone_duration_sec': 0.005,
                            'arrival_utc': synth_arrival_utc,
                            'detection_method': 'edge_ensemble',
                            'edge_n': edge_result.ensemble_n_edges,
                            'edge_uncertainty_ms': edge_result.ensemble_uncertainty_ms,
                            'edge_confidence': edge_result.confidence,
                        }
                        measurements.append(synth_measurement)
                        logger.info(
                            f"{self.channel_name}: {station_name} EDGE ENSEMBLE "
                            f"recovery: {edge_result.ensemble_n_edges} edges, "
                            f"timing={edge_result.ensemble_timing_error_ms:+.3f}"
                            f"±{edge_result.ensemble_uncertainty_ms:.3f}ms, "
                            f"conf={edge_result.confidence:.2f}")
                    
                    elif station_name in stations_with_corr and edge_result.ensemble_n_edges >= 5:
                        # Station already has correlation detection.
                        # Log the edge ensemble as a cross-check.
                        corr_err = [m['timing_error_ms'] for m in measurements 
                                   if m['station'] == station_name]
                        if corr_err:
                            delta = edge_result.ensemble_timing_error_ms - corr_err[0]
                            logger.info(
                                f"{self.channel_name}: {station_name} edge cross-check: "
                                f"corr={corr_err[0]:+.3f}ms, "
                                f"edge={edge_result.ensemble_timing_error_ms:+.3f}ms, "
                                f"Δ={delta:+.3f}ms "
                                f"({edge_result.ensemble_n_edges} edges)")
            
            # Store edge results for caller to retrieve
            self._last_edge_results = edge_results
            
        elif not self.is_rtp_authority:
            # === Fusion fallback: tone_detector when BufferTiming unavailable ===
            # Without BufferTiming we can't do per-second correlation.
            # Fall back to the legacy MultiStationToneDetector.
            logger.debug(f"{self.channel_name}: Fusion mode - tone_detector fallback "
                        f"(no BufferTiming)")
            
            # Use adaptive search window based on physics model + UTC uncertainty
            max_uncertainty_ms = 15.0
            for station, delay in expected_delays_by_station.items():
                _, _, unc = self._predict_geometric_delay(station, system_time)
                max_uncertainty_ms = max(max_uncertainty_ms, unc)
            
            adaptive_window_ms = min(200.0, max(50.0, max_uncertainty_ms * 3))
            
            if self.fusion_state is not None:
                adaptive_window_ms = self.fusion_state.get_search_window_ms()
            
            logger.info(f"{self.channel_name}: Fusion fallback search: "
                       f"expected_delays={expected_delays_by_station}, "
                       f"window=±{adaptive_window_ms:.0f}ms, "
                       f"lock_tier={self.fusion_state.lock_tier.name if self.fusion_state else 'N/A'}")
            
            detections = self.tone_detector.process_samples(
                timestamp=buffer_mid_time,
                samples=iq_samples,
                rtp_timestamp=rtp_timestamp,
                original_sample_rate=self.sample_rate,
                buffer_rtp_start=rtp_timestamp,
                search_window_ms=adaptive_window_ms,
                expected_delays_by_station=expected_delays_by_station
            )
            
            if not detections:
                logger.debug(f"{self.channel_name}: No detections for minute {minute_boundary}")
                return []
            
            station_names = [det.station.value for det in detections]
            logger.info(f"{self.channel_name}: Fusion fallback detected "
                       f"{len(detections)} station(s): {station_names}")
            # Skip the measurement→detection conversion below; tone_detector
            # already returns ToneDetectionResult objects.  Jump to Step 2.
            # (detections variable is already set)
            
        else:
            # RTP mode without BufferTiming — fall back to legacy method.
            # Without BufferTiming we don't know which second we're at,
            # so use a conservative 20ms template as before.
            for station_name, tone_freq in station_tone_freqs:
                prop_delay = expected_delays_by_station.get(station_name, 20.0)
                station_unc_1sigma = expected_uncertainty_by_station.get(station_name)
                adaptive_window = station_unc_1sigma * 3.0 if station_unc_1sigma else None
                result = self._measure_tone_at_known_time(
                    audio_signal=audio_signal,
                    expected_delay_ms=prop_delay,
                    tone_freq_hz=tone_freq,
                    tone_duration_sec=0.02,
                    station_name=station_name,
                    search_window_ms=adaptive_window
                )
                if result and result.get('detected'):
                    measurements.append(result)
        
        # === Convert measurements to ToneDetectionResult ===
        # The per-second correlator produces dicts; downstream needs ToneDetectionResult.
        # The Fusion fallback path already has ToneDetectionResult objects.
        if use_per_second_correlator or self.is_rtp_authority:
            if not measurements:
                logger.debug(f"{self.channel_name}: No signals detected at expected times")
                return []
            
            # Select best measurement per station for timing use.
            # Strategy: robust median consistency filter across per-second
            # measurements, then highest-SNR from the consistent set.
            # This rejects false peaks (multipath, fading) that would win
            # a naive highest-SNR selection.
            from collections import defaultdict
            by_station = defaultdict(list)
            for m in measurements:
                by_station[m['station']].append(m)
            
            best_per_station = {}
            for stn, stn_measurements in by_station.items():
                errs = np.array([m['timing_error_ms'] for m in stn_measurements])
                if len(errs) >= 3:
                    med = np.median(errs)
                    mad = np.median(np.abs(errs - med))
                    sigma = max(mad * 1.4826, 15.0)  # MAD->std, floor 15ms
                    threshold = max(30.0, 2.5 * sigma)
                    consistent = [m for m in stn_measurements
                                  if abs(m['timing_error_ms'] - med) <= threshold]
                    n_rejected = len(stn_measurements) - len(consistent)
                    if n_rejected:
                        logger.debug(f"{self.channel_name}: {stn} consistency filter "
                                     f"rejected {n_rejected}/{len(stn_measurements)} "
                                     f"outliers (median={med:+.1f}ms, σ={sigma:.1f}ms)")
                    pool = consistent if consistent else stn_measurements
                else:
                    pool = stn_measurements
                best = max(pool, key=lambda m: m['snr_db'])
                best_per_station[stn] = best
            
            best_keys = set()
            for m in best_per_station.values():
                best_keys.add((m['station'], m.get('utc_second', 0)))
            
            # Convert to ToneDetectionResult format for downstream
            from ..interfaces.data_models import ToneDetectionResult, StationType
            detections = []
            for m in measurements:
                station_type = StationType[m['station']] if m['station'] in StationType.__members__ else StationType.UNKNOWN
                is_best = (m['station'], m.get('utc_second', 0)) in best_keys
                
                if buffer_timing is not None and 'arrival_utc' in m:
                    timestamp_utc_val = m['arrival_utc']
                else:
                    timestamp_utc_val = system_time + m['arrival_ms'] / 1000.0
                sample_pos = int(m['arrival_ms'] * self.sample_rate / 1000)
                
                det = ToneDetectionResult(
                    station=station_type,
                    frequency_hz=m['frequency_hz'],
                    duration_sec=m.get('tone_duration_sec', 0.02),
                    timestamp_utc=timestamp_utc_val,
                    timing_error_ms=m['timing_error_ms'],
                    snr_db=m['snr_db'],
                    confidence=max(0.0, min(1.0, m['snr_db'] / 20.0)),
                    use_for_time_snap=is_best,
                    correlation_peak=m.get('correlation_peak', 0.0),
                    noise_floor=0.0,
                    tone_power_db=m['snr_db'],
                    sample_position_original=sample_pos,
                    original_sample_rate=self.sample_rate
                )
                detections.append(det)
            
            n_best = len(best_per_station)
            station_names = [m['station'] for m in measurements]
            logger.info(f"{self.channel_name}: {mode_label} mode measured "
                       f"{len(detections)} signal(s) "
                       f"({n_best} best for timing): {station_names}")
             
        # === Step 2: Channel Characterization ===
        # We need this for Station ID and Metrics
        # Re-use Phase 2 logic style but inline or simplified?
        # Actually Phase 2 logic handles BCD, Doppler, etc.
        # We can instantiate a 'TimeSnapResult' dummy if we want to reuse existing methods,
        # or just call discriminators directly.
        # Calling discriminator methods directly is cleaner.
        
        # 2A. BCD (if applicable)
        bcd_metrics = {}
        if self.frequency_mhz in (2.5, 5.0, 10.0, 15.0):
             bcd_res = self.discriminator.detect_bcd_discrimination(
                 iq_samples, self.sample_rate, system_time, self.frequency_mhz
             )
             if bcd_res and bcd_res[0]:
                 bcd_metrics['wwv_amp'] = bcd_res[0]
                 bcd_metrics['wwvh_amp'] = bcd_res[1]
                 
        # 2B. Doppler
        doppler_metrics = {}
        doppler_info = self.discriminator.estimate_doppler_shift_from_ticks(
            iq_samples, self.sample_rate
        )
        if doppler_info:
            doppler_metrics = doppler_info
            
        # 2C. CHU FSK Time Code Decoding
        # Decode directly from IQ buffer using AM demod + FSK discriminator.
        # The IQ decimation filter attenuates FSK tones (2025/2225 Hz), but the
        # AM-demodulated audio path recovers them via the envelope detector.
        chu_metrics = {}
        if self.is_chu_channel:
            if hasattr(self, 'chu_fsk_decoder'):
                chu_metrics = self._decode_fsk_from_iq(iq_samples, minute_boundary)
            if chu_metrics:
                self._cross_validate_fsk(chu_metrics, minute_boundary)
        
        # === Step 2D: Per-Second Tick Phase Extraction (deferred physics) ===
        # The tick filter extracts carrier phase from per-second ticks for
        # ionospheric analysis (Doppler, TEC, scintillation). It does NOT
        # contribute to timing — _measure_tone_at_known_time() handles that
        # via the arrival pattern matrix with proper buffer timing.
        #
        # Signal presence gating: use edge_results from Step 1 (already ran).
        # The old _check_signal_presence() band-energy test always fails for
        # WWV/WWVH 5ms ticks (0.5% duty cycle → band power ≈ noise floor).
        # Edge ensemble detection of ≥5 ticks is a reliable signal indicator.
        tick_results = {}
        comparison_records = []  # A/B comparison records for HDF5 persistence
        signal_present = (
            bool(edge_results)
            or self._check_signal_presence(iq_samples)
        )
        
        if signal_present and self.tick_filters:
            logger.debug(f"{self.channel_name}: Running tick phase extraction for "
                        f"{len(self.tick_filters)} stations (physics, not timing)")
            for station_type, tick_filter in self.tick_filters.items():
                try:
                    tick_analysis = tick_filter.process_minute(
                        iq_samples, minute_number,
                        buffer_timing=buffer_timing,
                        minute_boundary=minute_boundary
                    )
                    if tick_analysis and tick_analysis.valid_windows > 0:
                        tick_results[station_type.value] = tick_analysis
                        logger.debug(f"{self.channel_name}: {station_type.value} tick phase: "
                                    f"{tick_analysis.valid_windows}/{tick_analysis.total_windows} windows, "
                                    f"tick_std={tick_analysis.tick_std_offset_ms:.1f}ms")
                        
                        # A/B Comparison: Only valid for WWV/WWVH (continuous 1000/1200 Hz tones)
                        # CHU uses FSK, BPM has different tone pattern — PLL is meaningless for those
                        #
                        # MF baseline uses the EDGE ENSEMBLE (robust median of ~57 per-second
                        # tick front-edge detections) rather than TickMatchedFilter.d_clock_ms,
                        # which reports correlation peak position within the ±100ms search
                        # window — not a valid timing residual.
                        station_name = station_type.value
                        edge_result = edge_results.get(station_name)
                        if (self.comparison_tracker and station_type in self.pll_decoders
                                and station_name in ('WWV', 'WWVH')
                                and edge_result is not None
                                and edge_result.ensemble_n_edges >= 5):
                            try:
                                pll_decoder = self.pll_decoders[station_type]
                                pll_result = pll_decoder.process_minute(
                                    iq_samples, minute_number,
                                    buffer_timing=buffer_timing,
                                    minute_boundary=minute_boundary
                                )
                                
                                # MF side: edge ensemble (per-second tick front-edge timing)
                                mf_d_clock = edge_result.ensemble_timing_error_ms
                                mf_std = edge_result.ensemble_uncertainty_ms
                                mf_ticks = edge_result.ensemble_n_edges
                                
                                # PLL side: continuous carrier phase tracking
                                pll_ticks = pll_result.n_ticks_detected if pll_result else 0
                                
                                # Feed comparison into tracker
                                comparison = self.comparison_tracker.add_comparison(
                                    timestamp=system_time,
                                    mf_d_clock=mf_d_clock,
                                    pll_d_clock=pll_result.d_clock_ms if pll_result else None,
                                    mf_n_ticks=mf_ticks,
                                    pll_n_ticks=pll_ticks
                                )
                                
                                # Build comparison record for HDF5 persistence
                                comparison_records.append({
                                    'station': station_name,
                                    'frequency_mhz': self.frequency_mhz,
                                    'mf_d_clock_ms': mf_d_clock,
                                    'pll_d_clock_ms': pll_result.d_clock_ms if pll_result else None,
                                    'delta_ms': comparison.get('delta_ms'),
                                    'mf_timing_offset_ms': mf_d_clock,
                                    'pll_timing_offset_ms': pll_result.mean_timing_offset_ms if pll_result else None,
                                    'mf_std_ms': mf_std,
                                    'pll_std_ms': pll_result.std_timing_offset_ms if pll_result else None,
                                    'mf_n_ticks': mf_ticks,
                                    'pll_n_ticks': pll_ticks,
                                    'pll_lock_quality': pll_result.lock_quality if pll_result else 0.0,
                                    'pll_lock_duration_sec': None,
                                    'winner': comparison.get('winner', 'NONE'),
                                    'winner_confidence': comparison.get('winner_confidence', 0.0),
                                    'gps_reference': comparison.get('gps_reference'),
                                    'mf_gps_error_ms': comparison.get('mf_gps_error_ms'),
                                    'pll_gps_error_ms': comparison.get('pll_gps_error_ms'),
                                    'quality': 'GOOD' if (mf_ticks > 0 and pll_ticks > 0) else 'PARTIAL',
                                })
                                
                                logger.debug(f"{self.channel_name}: A/B comparison {station_name} - "
                                            f"Edge: {mf_d_clock:+.3f}±{mf_std:.3f}ms ({mf_ticks} edges), "
                                            f"PLL: {pll_ticks} ticks, "
                                            f"winner: {comparison.get('winner', 'NONE')}")
                            except Exception as e:
                                logger.warning(f"{self.channel_name}: PLL comparison failed: {e}")
                except Exception as e:
                    logger.warning(f"{self.channel_name}: {station_type.value} tick extraction failed: {e}")
            
            # Periodically update comparison metrics for API exposure (every 10 minutes)
            if self.comparison_tracker and self.minutes_processed % 10 == 0:
                self.decoder_config.update_comparison_metrics(self.comparison_tracker)
                logger.debug(f"{self.channel_name}: Updated comparison metrics for API")
        elif not signal_present:
            logger.info(f"{self.channel_name}: No signal at tick frequency — "
                       f"skipping tick phase extraction (silent minute?)")
                 
        # === Step 3: Package into L1MetrologyMeasurement ===
        # Validate each detection against the ArrivalPatternMatrix.
        # Only the best detection per station (use_for_time_snap=True) creates
        # an L1 timing measurement and feeds the fusion state.  All detections
        # contribute SNR data points to the HDF5 for dashboard plotting.
        
        # Compute per-station multipath spread from edge detection (Step 5).
        # Two indicators:
        #   1. CLEAN delay spread: max delay_offset_ms across resolved components
        #   2. Per-second timing spread: ensemble_uncertainty_ms when it exceeds
        #      the noise floor (~0.5ms for 24kHz sample rate)
        # Take the larger of the two as the multipath-induced timing ambiguity.
        multipath_spread_by_station = {}
        for stn, er in edge_results.items():
            clean_spread_ms = 0.0
            for tick in er.edges:
                if tick.clean_arrivals and len(tick.clean_arrivals) >= 2:
                    max_offset = max(abs(c.delay_offset_ms) for c in tick.clean_arrivals)
                    clean_spread_ms = max(clean_spread_ms, max_offset)
            
            # Per-second spread above noise floor (~0.5ms at 24kHz)
            timing_spread_ms = max(0.0, er.ensemble_uncertainty_ms - 0.5) if er.ensemble_n_edges >= 5 else 0.0
            
            spread = max(clean_spread_ms, timing_spread_ms)
            if spread > 0.0:
                multipath_spread_by_station[stn] = spread
                logger.info(f"{self.channel_name}: {stn} multipath spread: "
                           f"{spread:.2f}ms (CLEAN={clean_spread_ms:.2f}ms, "
                           f"timing={timing_spread_ms:.2f}ms)")
        
        results = []
        for det in detections:
            # Map station name to Enum
            try:
                station_id_enum = StationID[det.station.value]
            except KeyError:
                station_id_enum = StationID.UNKNOWN

            # Physics-based validation using ArrivalPatternMatrix
            geo_delay, dist_km, uncertainty_ms = self._predict_geometric_delay(
                det.station.value, system_time
            )
            
            # Validate detection against physics model
            physics_valid = True
            physics_confidence = 1.0
            validation_reason = "no_matrix"
            
            if self.arrival_matrix is not None:
                # arrival_utc IS the ToA (from RTP timestamp of the tone sample).
                # timing_error_ms = (arrival_utc - expected_utc) * 1000, already
                # computed from the RTP timestamp.  Just check if it's within
                # the arrival matrix's uncertainty window.
                #
                # NOTE (2026-02-12): Analysis of detection_attempts shows that
                # ~80% of WWV/WWVH "detections" that pass the corr_snr gate
                # have timing errors uniformly distributed across ±500ms —
                # these are FALSE POSITIVES (noise correlation peaks, not real
                # arrivals).  Only ~10% have |err| < 15ms (real 1F arrivals).
                # This physics gate is ESSENTIAL for rejecting false positives.
                # The root cause is the matched filter SNR calculation not
                # discriminating real signals from noise for long (800ms) templates.
                matrix = self.arrival_matrix.get_expected_arrivals(
                    datetime.fromtimestamp(system_time, tz=timezone.utc)
                )
                arrival_info = matrix.get_arrival(det.station.value, self.frequency_mhz)
                
                if arrival_info is not None:
                    window_ms = arrival_info.uncertainty_3sigma_ms
                    timing_err = det.timing_error_ms
                    sigma_ms = window_ms / 3.0
                    deviation_sigma = abs(timing_err) / sigma_ms if sigma_ms > 0 else float('inf')
                    
                    # Gate → Weight: physics model informs confidence, not a binary gate.
                    # Detections within 1σ: full confidence.
                    # Detections 1σ–5σ: degraded confidence (Gaussian tail).
                    # Detections >5σ: still rejected — at this distance from the
                    # model window the detection is almost certainly a false positive
                    # (noise correlation peak), not a real arrival with model error.
                    # The 5σ hard cutoff preserves false-positive suppression for
                    # WWV/WWVH shared frequencies while allowing model-error-affected
                    # real detections (e.g. CHU systematic offset) through.
                    HARD_REJECT_SIGMA = 5.0
                    if deviation_sigma > HARD_REJECT_SIGMA:
                        physics_valid = False
                        physics_confidence = 0.0
                        validation_reason = (f"Hard reject: {deviation_sigma:.1f}σ > {HARD_REJECT_SIGMA:.0f}σ "
                                           f"(likely false positive, not model error)")
                        logger.info(f"{self.channel_name}: Physics REJECTED: "
                                   f"{det.station.value} timing_err={timing_err:+.1f}ms - "
                                   f"{validation_reason}")
                        continue  # Skip — almost certainly noise, not a real arrival
                    else:
                        physics_valid = True
                        # Gaussian-like confidence decay: 1.0 at 0σ, ~0.6 at 1σ, ~0.1 at 3σ, ~0.01 at 5σ
                        deviation_factor = math.exp(-0.5 * (deviation_sigma ** 2) / (3.0 ** 2))
                        snr_factor = 1.0 / (1.0 + math.exp(-(det.snr_db - 10.0) / 5.0))
                        physics_confidence = deviation_factor * snr_factor
                        if deviation_sigma > 1.0:
                            validation_reason = (f"timing_err={timing_err:+.1f}ms "
                                               f"({deviation_sigma:.1f}σ, degraded confidence={physics_confidence:.2f})")
                            logger.info(f"{self.channel_name}: Physics MARGINAL: "
                                       f"{det.station.value} {validation_reason}")
                        else:
                            validation_reason = (f"timing_err={timing_err:+.1f}ms "
                                               f"({deviation_sigma:.1f}σ)")
                            logger.info(f"{self.channel_name}: Physics VALIDATED: "
                                       f"{det.station.value} {validation_reason}")
                        
                        # Feed validated detection to adaptive window tracker.
                        # Weight the effective SNR by physics_confidence so that
                        # marginal detections have less influence on window narrowing.
                        effective_snr = det.snr_db * physics_confidence
                        station_mp_spread = multipath_spread_by_station.get(
                            det.station.value, 0.0)
                        self.arrival_matrix.record_detection(
                            station=det.station.value,
                            frequency_mhz=self.frequency_mhz,
                            detected_ms=det.timing_error_ms + arrival_info.expected_delay_ms,
                            expected_ms=arrival_info.expected_delay_ms,
                            snr_db=effective_snr,
                            multipath_spread_ms=station_mp_spread
                        )
                        
                        # Multipath degrades timing confidence: the earliest
                        # arrival is correct but the correlator may lock onto
                        # a later mode.  Reduce confidence proportionally.
                        if station_mp_spread > 0:
                            mp_penalty = 1.0 / (1.0 + station_mp_spread / 3.0)
                            physics_confidence *= mp_penalty
            
            # Construct L1 measurement (only for validated detections)
            meas = L1MetrologyMeasurement(
                timestamp_utc=datetime.fromtimestamp(buffer_mid_time, tz=timezone.utc).isoformat(),
                minute_boundary_utc=minute_boundary,
                rtp_timestamp=rtp_timestamp,
                station_id=station_id_enum,
                frequency_mhz=self.frequency_mhz,
                
                raw_toa_ms=det.timing_error_ms,
                tone_detected=True,
                
                snr_db=det.snr_db,
                doppler_hz=doppler_metrics.get(f"{det.station.value.lower()}_doppler_hz"),
                
                identification_method="tone_frequency",
                identification_confidence=det.confidence * physics_confidence,
                
                distance_km=dist_km,
                light_travel_time_ms=geo_delay,
                
                quality_flag=QualityFlag.GOOD if (det.confidence > 0.5 and physics_valid) else QualityFlag.MARGINAL
            )
            results.append(meas)
            
            # Feed ONLY the best detection per station to FusionTimingState.
            # Multiple timing measurements from the same station would confuse
            # the Kalman filter with correlated noise.
            # Gate → likelihood: use physics_confidence threshold (0.1) instead
            # of binary physics_valid.  Marginal detections feed the Kalman
            # filter with low weight rather than being excluded entirely.
            if self.fusion_state is not None and physics_confidence > 0.1 and det.use_for_time_snap:
                lock_status = self.fusion_state.add_detection(
                    station=det.station.value,
                    timing_error_ms=det.timing_error_ms,
                    frequency_mhz=self.frequency_mhz,
                    snr_db=det.snr_db,
                    confidence=det.confidence * physics_confidence,
                    system_time=system_time
                )
                if lock_status:
                    logger.info(f"{self.channel_name}: {lock_status}")
                    self._write_bootstrap_state_on_lock()
            
        # Safeguard 2: Record misses for stations with no validated detection.
        # This feeds the consecutive miss counter in BroadcastWindowState,
        # which forces the search window back to initial width after
        # MISS_RESET_THRESHOLD consecutive misses (prevents FM2 lock-up).
        if self.arrival_matrix is not None:
            validated_stations = {
                meas.station_id.value if hasattr(meas.station_id, 'value') else str(meas.station_id)
                for meas in results
            }
            for (station, freq) in list(self.arrival_matrix._broadcast_windows.keys()):
                if freq == self.frequency_mhz and station not in validated_stations:
                    self.arrival_matrix.record_miss(station, freq)
        
        with self._lock:
            self.minutes_processed += 1
        
        # Store FSK data for caller to retrieve
        self._last_chu_fsk_data = chu_metrics if chu_metrics else None
        
        # Store tick analysis results for caller to retrieve
        self._last_tick_results = tick_results if tick_results else None
        
        # Store decoder comparison data for HDF5 persistence
        self._decoder_comparison_data = comparison_records if comparison_records else None
        
        # Store ALL measurement attempts (detected + rejected) for threshold calibration.
        # This is the evidence that keeps us honest: by recording what we reject and why,
        # we can later ask whether our thresholds are correctly calibrated.
        self._last_rtp_attempts = all_attempts if all_attempts else None
        
        # === Step 4: Multi-Constraint Timing Validation ===
        # Validate detections using all known timing constraints:
        # - Arrival sequence (stations at different distances)
        # - Cross-station consistency (all transmit at UTC second 0)
        # - Sample interval stability (1,440,000 samples between minutes)
        # - Arrival time stability (consistent offsets across minutes)
        if self.timing_validator is not None and results:
            validation_detections = [
                {
                    'station': meas.station_id.value if hasattr(meas.station_id, 'value') else str(meas.station_id),
                    'frequency_mhz': meas.frequency_mhz,
                    'arrival_ms': meas.raw_toa_ms,
                    'snr_db': meas.snr_db
                }
                for meas in results
            ]
            
            validation_result = self.timing_validator.validate_minute(
                minute_boundary=minute_boundary,
                detections=validation_detections,
                rtp_timestamp=rtp_timestamp
            )
            
            # Log validation summary
            self.timing_validator.log_validation_summary(validation_result)
            
            # Update history for inter-minute tracking
            self.timing_validator.update_history(minute_boundary, validation_detections)
            
            # Store validation result for caller to retrieve
            self._last_validation_result = validation_result
            
            # Log stability metrics periodically (every 10 minutes)
            if self.minutes_processed % 10 == 0:
                stability = self.timing_validator.get_stability_metrics()
                if stability.n_minutes >= 5:
                    logger.info(f"{self.channel_name}: Stability metrics (n={stability.n_minutes}):")
                    for station, std in stability.arrival_std_ms.items():
                        mean = stability.arrival_mean_ms.get(station, 0)
                        logger.info(f"  {station}: arrival={mean:.1f}±{std:.1f}ms")
                    if stability.sample_interval_std > 0:
                        logger.info(f"  Sample interval: {stability.sample_interval_mean:.0f}±{stability.sample_interval_std:.1f}")
            
        # === Detection Gap Alerting ===
        # Track last physics-validated detection per station.
        # Emit WARNING when a station goes dark for >5 minutes.
        now = system_time
        validated_stations = set()
        for meas in results:
            stn = meas.station_id.value if hasattr(meas.station_id, 'value') else str(meas.station_id)
            self._last_validated_detection[stn] = now
            validated_stations.add(stn)
        
        # Check all stations we expect on this channel for gaps.
        # Derive from channel name (works in both RTP and fusion modes).
        channel_upper = self.channel_name.upper()
        if 'CHU' in channel_upper:
            expected_stations = ['CHU']
        elif 'WWV_20' in channel_upper or 'WWV_25' in channel_upper:
            expected_stations = ['WWV']
        else:
            expected_stations = ['WWV', 'WWVH', 'BPM']
        for stn in expected_stations:
            last_det = self._last_validated_detection.get(stn)
            if last_det is None:
                # Never detected — only warn after we've processed enough minutes
                if self.minutes_processed >= 5:
                    last_warn = self._gap_warning_emitted.get(stn, 0)
                    if now - last_warn >= self._GAP_WARNING_INTERVAL_S:
                        logger.warning(f"{self.channel_name}: {stn} NEVER DETECTED "
                                      f"after {self.minutes_processed} minutes")
                        self._gap_warning_emitted[stn] = now
            else:
                gap_s = now - last_det
                if gap_s >= self._DETECTION_GAP_THRESHOLD_S:
                    last_warn = self._gap_warning_emitted.get(stn, 0)
                    if now - last_warn >= self._GAP_WARNING_INTERVAL_S:
                        gap_min = gap_s / 60.0
                        logger.warning(f"{self.channel_name}: {stn} DETECTION GAP "
                                      f"{gap_min:.1f}min (last validated {gap_min:.0f}min ago)")
                        self._gap_warning_emitted[stn] = now
        
        return results

    def _write_bootstrap_state_on_lock(self):
        """Write bootstrap state file when FusionTimingState achieves lock.
        
        This unblocks the fusion service which waits for bootstrap_state.json
        before starting its main loop. Called on PROVISIONAL and REFINED lock
        transitions.
        """
        if self._bootstrap_state_writer is None or self.fusion_state is None:
            return
        
        if not self.fusion_state.is_locked:
            return
        
        try:
            offset_stats = self.fusion_state._compute_offset_stats()
            lock_tier = self.fusion_state.lock_tier.name
            d_clock_ms = offset_stats.get('median_ms', 0.0)
            uncertainty_ms = offset_stats.get('std_ms', 50.0)
            
            self._bootstrap_state_writer.write_locked(
                lock_tier=lock_tier,
                d_clock_ms=d_clock_ms,
                uncertainty_ms=uncertainty_ms,
                sample_rate=self.sample_rate
            )
            logger.info(
                f"{self.channel_name}: Bootstrap state written: {lock_tier}, "
                f"D_clock={d_clock_ms:+.1f}ms ± {uncertainty_ms:.1f}ms"
            )
        except Exception as e:
            logger.error(f"{self.channel_name}: Failed to write bootstrap state: {e}")

    def _cross_validate_fsk(self, chu_metrics: dict, minute_boundary: int) -> None:
        """Cross-validate CHU FSK decode against other metrology functions.
        
        Implements four integrations:
        
        1. **Frame A UTC sanity check**: Compare FSK-decoded minute against
           RTP-derived minute_boundary. A mismatch indicates the RTP timing
           chain (GPS → radiod → RTP counter → UTC) may be broken. This is
           the only independent UTC source in the system.
        
        2. **TAI-UTC leap second watch**: Track TAI-UTC value across minutes.
           When it changes (e.g. 37→38), a leap second insertion is imminent.
           Sets _fsk_tai_utc_changed flag so fusion can hold off the Kalman
           filter during the transition.
        
        3. **DUT1 tracking**: Store latest DUT1 (UT1-UTC) for use by the
           propagation model's solar zenith calculation. UT1 = UTC + DUT1
           gives the correct Earth rotation angle for ionospheric modeling.
        
        4. **BER confidence**: Degrade chu_metrics['fsk_confidence'] based on
           frame decode rate (frames_decoded/9). Minutes with heavy fading
           (few frames decoded) get lower confidence in fusion weighting.
        """
        from datetime import datetime, timezone
        
        # === 1. Frame A UTC Sanity Check ===
        decoded_minute = chu_metrics.get('decoded_minute')
        if decoded_minute is not None:
            expected_minute = int((minute_boundary // 60) % 60)
            if decoded_minute != expected_minute:
                self._fsk_utc_mismatch_count += 1
                if self._fsk_utc_mismatch_count >= 3:
                    logger.error(
                        f"{self.channel_name}: FSK UTC MISMATCH x{self._fsk_utc_mismatch_count}: "
                        f"CHU says :{decoded_minute:02d} but RTP says :{expected_minute:02d} — "
                        f"RTP timing chain may be broken!")
                else:
                    logger.warning(
                        f"{self.channel_name}: FSK UTC mismatch: "
                        f"CHU=:{decoded_minute:02d} vs RTP=:{expected_minute:02d} "
                        f"(count={self._fsk_utc_mismatch_count})")
            else:
                if self._fsk_utc_mismatch_count > 0:
                    logger.info(f"{self.channel_name}: FSK UTC sanity check OK "
                               f"(cleared {self._fsk_utc_mismatch_count} prior mismatches)")
                self._fsk_utc_mismatch_count = 0
        
        # === 2. TAI-UTC Leap Second Watch ===
        tai_utc = chu_metrics.get('tai_utc')
        if tai_utc is not None and isinstance(tai_utc, int) and tai_utc > 0:
            if self._fsk_last_tai_utc is not None and tai_utc != self._fsk_last_tai_utc:
                logger.warning(
                    f"{self.channel_name}: *** TAI-UTC CHANGED: {self._fsk_last_tai_utc} → {tai_utc} *** "
                    f"Leap second {'insertion' if tai_utc > self._fsk_last_tai_utc else 'deletion'} detected!")
                self._fsk_tai_utc_changed = True
            elif self._fsk_last_tai_utc is not None:
                self._fsk_tai_utc_changed = False
            self._fsk_last_tai_utc = tai_utc
        
        # === 3. DUT1 Tracking ===
        dut1 = chu_metrics.get('dut1_seconds')
        if dut1 is not None:
            if self._fsk_last_dut1 is not None and abs(dut1 - self._fsk_last_dut1) > 0.05:
                logger.info(f"{self.channel_name}: DUT1 changed: {self._fsk_last_dut1:+.1f}s → {dut1:+.1f}s")
            self._fsk_last_dut1 = dut1
        
        # === 4. BER-Based Confidence Adjustment ===
        frames_decoded = chu_metrics.get('fsk_frames_decoded', 0)
        if frames_decoded > 0:
            # Scale confidence by decode rate: 9/9 → 1.0, 2/9 → 0.22
            decode_rate = frames_decoded / 9.0
            raw_confidence = chu_metrics.get('fsk_confidence', 0.5)
            adjusted_confidence = raw_confidence * decode_rate
            chu_metrics['fsk_confidence'] = adjusted_confidence
            chu_metrics['fsk_decode_rate'] = decode_rate
            if decode_rate < 0.5:
                logger.debug(f"{self.channel_name}: FSK confidence degraded: "
                            f"{raw_confidence:.2f} → {adjusted_confidence:.2f} "
                            f"(decode_rate={decode_rate:.2f})")


    def _write_fsk_result(self, metrics: dict):
        """Write CHU FSK result to shared JSON for real-time dashboard."""
        from pathlib import Path
        import time
        try:
            fsk_dir = Path('/dev/shm/timestd/fsk_results')
            fsk_dir.mkdir(parents=True, exist_ok=True)
            fsk_path = fsk_dir / f'{self.channel_name}.json'
            
            # Map metrics to what the dashboard expects
            data = {
                'written_at': time.time(),
                'detected': metrics.get('fsk_valid', False),
                'frames_decoded': metrics.get('fsk_frames_decoded', 0),
                'decode_confidence': metrics.get('fsk_confidence', 0.0),
                'decoded_day': metrics.get('decoded_day'),
                'decoded_hour': metrics.get('decoded_hour'),
                'decoded_minute': metrics.get('decoded_minute'),
                'dut1_seconds': metrics.get('dut1_seconds'),
                'tai_utc': metrics.get('tai_utc'),
                'year': metrics.get('year'),
                'timing_offset_ms': metrics.get('timing_offset_ms'),
            }
            with open(fsk_path, 'w') as f:
                json.dump(data, f)
        except Exception as e:
            logger.warning(f"{self.channel_name}: Failed to write FSK result JSON: {e}")

    def _decode_fsk_from_iq(self, iq_samples: np.ndarray, minute_boundary: int) -> dict:
        """Decode CHU FSK directly from IQ buffer (live IQ-tapped path)."""
        try:
            result = self.chu_fsk_decoder.decode_minute(
                iq_samples, float(minute_boundary), is_audio=False
            )
            if not result.detected:
                logger.debug(f"{self.channel_name}: IQ-direct FSK: not detected")
                return {}
            # Publish minute-level UTC for the authority manager's bootstrap
            # coordinator (METROLOGY.md §4.5). Best-effort — any failure is
            # logged inside the helper and does not affect the decode path.
            self._publish_coarse_time(result)
            chu_metrics = {
                'fsk_valid': True,
                'fsk_frames_decoded': result.frames_decoded,
                'fsk_confidence': result.decode_confidence,
                'source': 'iq_direct',
            }
            if result.decoded_day is not None:
                chu_metrics['decoded_day'] = result.decoded_day
                chu_metrics['decoded_hour'] = result.decoded_hour
                chu_metrics['decoded_minute'] = result.decoded_minute
            if result.dut1_seconds is not None:
                chu_metrics['dut1_seconds'] = result.dut1_seconds
            if result.tai_utc is not None:
                chu_metrics['tai_utc'] = result.tai_utc
            if result.year is not None:
                chu_metrics['year'] = result.year
            if result.timing_offset_ms is not None:
                chu_metrics['timing_offset_ms'] = result.timing_offset_ms
            logger.info(f"{self.channel_name}: CHU FSK from IQ-direct - "
                       f"frames={result.frames_decoded}/9, "
                       f"DUT1={result.dut1_seconds}s, "
                       f"TAI-UTC={result.tai_utc}s")
            self._write_fsk_result(chu_metrics)
            return chu_metrics
        except Exception as e:
            logger.warning(f"{self.channel_name}: IQ-direct FSK decode failed: {e}")
            return {}

    def _station_from_channel_name(self) -> str:
        """Helper to guess station from name."""
        if 'CHU' in self.channel_name.upper(): return 'CHU'
        if 'WWVH' in self.channel_name.upper(): return 'WWVH'
        if 'WWV' in self.channel_name.upper(): return 'WWV'
        return 'UNKNOWN'

    def _publish_coarse_time(self, result) -> None:
        """Translate a successful CHU FSK decode into a coarse_time.json
        record for the authority manager's bootstrap coordinator.

        Precision is minute-level — Frame A of the FSK burst carries
        (day_of_year, hour, minute) but the decode window does not tell
        us which second inside the minute we are observing at write time.
        `max_error_sec=60` reflects that; the bootstrap coordinator's
        threshold must be > 60 s for the comparison to be meaningful
        (METROLOGY.md §4.5 sets the default to 90 s).

        Year resolution comes from Frame B (result.year). If Frame B
        was not decoded this minute, we fall back to the current
        system-clock year — still independent of the second-level clock
        error that bootstrap exists to correct.
        """
        if self._coarse_time_writer is None:
            return
        try:
            day = result.decoded_day
            hour = result.decoded_hour
            minute = result.decoded_minute
            year = getattr(result, 'year', None)
            if day is None or hour is None or minute is None:
                return

            from datetime import timedelta as _td
            if year is None:
                year = datetime.now(timezone.utc).year
            coarse_utc = datetime(
                int(year), 1, 1, 0, 0, 0, tzinfo=timezone.utc,
            ) + _td(days=int(day) - 1, hours=int(hour), minutes=int(minute))

            self._coarse_time_writer.publish(
                source="FSK",
                station=self._station_from_channel_name(),
                coarse_utc=coarse_utc,
                max_error_sec=60.0,
            )
        except Exception as e:
            logger.warning(f"{self.channel_name}: coarse_time publish failed: {e}")


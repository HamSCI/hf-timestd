#!/usr/bin/env python3
"""
WWV/WWVH/CHU Shared Constants - Central Reference for Phase 2 Analytics

================================================================================
PURPOSE
================================================================================
Single source of truth for all timing constants, broadcast schedules, station
locations, and detection thresholds used across Phase 2 analytics modules.

Centralizing these values ensures consistency and makes it easy to update
parameters based on empirical observations or specification changes.

================================================================================
STATION SPECIFICATIONS
================================================================================
WWV - NIST Radio Station, Fort Collins, Colorado, USA
    Coordinates: 40.6775°N, 105.0472°W
    Frequencies: 2.5, 5, 10, 15, 20, 25 MHz
    Timing Tone: 1000 Hz, 800ms duration at second 0
    Power: 2.5 kW (2.5, 20, 25 MHz), 10 kW (5, 10, 15 MHz)

WWVH - NIST Radio Station, Kekaha, Kauai, Hawaii, USA
    Coordinates: 21.9886°N, 159.7639°W
    Frequencies: 2.5, 5, 10, 15 MHz
    Timing Tone: 1200 Hz, 800ms duration at second 0
    Power: 10 kW (all frequencies)

CHU - NRC Radio Station, Ottawa, Ontario, Canada
    Coordinates: 45.2925°N, 75.7542°W
    Frequencies: 3.33, 7.85, 14.67 MHz
    Timing Tone: 1000 Hz, 500ms duration (1000ms at hour)
    Special: FSK time code at seconds 31-39 (Bell 103 AFSK)

BPM - NTSC, Pucheng County, Shaanxi, China
    Coordinates: 34.9489°N, 109.5430°E
    Frequencies: 2.5, 5, 10, 15 MHz
    Timing Tone: 1000 Hz ticks (10ms UTC, 100ms UT1)


================================================================================
SHARED vs UNIQUE FREQUENCIES
================================================================================
SHARED (require discrimination):
    2.5 MHz  - WWV + WWVH + BPM
    5 MHz    - WWV + WWVH + BPM
    10 MHz   - WWV + WWVH + BPM
    15 MHz   - WWV + WWVH + BPM


UNIQUE (no discrimination needed):
    20 MHz   - WWV only
    25 MHz   - WWV only
    3.33 MHz - CHU only
    7.85 MHz - CHU only
    14.67 MHz - CHU only

================================================================================
GROUND TRUTH MINUTES
================================================================================
During certain minutes, only ONE station broadcasts 500/600 Hz tones:

WWV-ONLY (4 minutes/hour):
    Minute 1:  WWV=600 Hz, WWVH=440 Hz (WWVH 440 Hz ground truth)
    Minute 16: WWV=500 Hz, WWVH=silent
    Minute 17: WWV=600 Hz, WWVH=silent
    Minute 19: WWV=600 Hz, WWVH=silent

WWVH-ONLY (10 minutes/hour):
    Minute 2:  WWV=440 Hz, WWVH=600 Hz (WWV 440 Hz ground truth)
    Minutes 43-51: WWV=silent, WWVH=500/600 Hz alternating

TEST SIGNAL MINUTES (exclusive broadcast):
    Minute 8:  WWV only (WWVH silent)
    Minute 44: WWVH only (WWV silent)

Total: 14 ground truth minutes per hour!

================================================================================
PROPAGATION PHYSICS
================================================================================
Constants for ionospheric propagation modeling:

SPEED OF LIGHT: 299,792.458 km/s
EARTH RADIUS:   6,371 km (mean)

IONOSPHERIC LAYERS:
    E-layer:  110 km altitude (daytime only, MUF ~4 MHz)
    F1-layer: 200 km altitude (daytime, merges with F2 at night)
    F2-layer: 300 km altitude (primary HF reflection layer)

IONOSPHERIC DELAY:
    Group delay ≈ 40.3 × TEC / f² (seconds)
    Typical: 0.1-0.5 ms per hop

PLAUSIBLE PROPAGATION DELAY RANGES (continental US):
    WWV:  2-35 ms  (Fort Collins relatively close)
    WWVH: 12-60 ms (Hawaii much farther)
    CHU:  3-40 ms  (Ottawa intermediate)

================================================================================
REFERENCES
================================================================================
- NIST Special Publication 432, "NIST Time and Frequency Services" (2012)
- NIST Special Publication 250-67, "NIST Time and Frequency Radio Stations"
- NRC CHU Technical Specifications
- BPM (Time Service) Wikipedia / NTSC Publications
- ITU-R P.531-14, "Ionospheric propagation data and prediction methods"

================================================================================
REVISION HISTORY
================================================================================
2025-12-07: Added comprehensive documentation
2025-11-15: Added CHU FSK parameters
2025-10-20: Initial constants extracted from analysis modules
"""

from typing import Dict, Set, Optional

# =============================================================================
# SAMPLE RATE CONSTANTS
# =============================================================================

SAMPLE_RATE_FULL = 20000  # Hz - Full rate from radiod RTP stream
SAMPLE_RATE_LEGACY = 16000  # Hz - Legacy 16 kHz mode (deprecated)

# =============================================================================
# STATION BROADCAST SCHEDULES
# =============================================================================

# Minutes where only one station broadcasts 500/600 Hz tones (ground truth)
# These provide unambiguous station identification
WWV_ONLY_TONE_MINUTES: Set[int] = {1, 16, 17, 19}
WWVH_ONLY_TONE_MINUTES: Set[int] = {2, 43, 44, 45, 46, 47, 48, 49, 50, 51}

# 440 Hz tone schedule (for station discrimination)
# Minute 1: WWVH broadcasts 440 Hz
# Minute 2: WWV broadcasts 440 Hz
MINUTE_440HZ_WWVH = 1
MINUTE_440HZ_WWV = 2

# Test signal minutes (scientific modulation test)
# Note: Test signal is IDENTICAL for WWV/WWVH - discrimination from schedule
WWV_TEST_SIGNAL_MINUTE = 8
WWVH_TEST_SIGNAL_MINUTE = 44

# =============================================================================
# 500/600 Hz TONE SCHEDULE (Complete 60-minute cycle)
# =============================================================================

# Per-minute schedule: {minute: {'WWV': freq_or_None, 'WWVH': freq_or_None}}
TONE_SCHEDULE_500_600: Dict[int, Dict[str, Optional[int]]] = {
    0: {'WWV': None, 'WWVH': None},
    1: {'WWV': 600, 'WWVH': 440},   # 440 Hz ground truth minutes
    2: {'WWV': 440, 'WWVH': 600},
    3: {'WWV': 600, 'WWVH': 500},
    4: {'WWV': 500, 'WWVH': 600},
    5: {'WWV': 600, 'WWVH': 500},
    6: {'WWV': 500, 'WWVH': 600},
    7: {'WWV': 600, 'WWVH': 500},
    8: {'WWV': None, 'WWVH': None},   # Test signal minute (WWV)
    9: {'WWV': None, 'WWVH': None},
    10: {'WWV': None, 'WWVH': None},
    11: {'WWV': 600, 'WWVH': 500},
    12: {'WWV': 500, 'WWVH': 600},
    13: {'WWV': 600, 'WWVH': 500},
    14: {'WWV': None, 'WWVH': None},
    15: {'WWV': None, 'WWVH': None},
    16: {'WWV': 500, 'WWVH': None},   # WWV-only
    17: {'WWV': 600, 'WWVH': None},   # WWV-only
    18: {'WWV': None, 'WWVH': None},
    19: {'WWV': 600, 'WWVH': None},   # WWV-only
    20: {'WWV': 500, 'WWVH': 600},
    21: {'WWV': 600, 'WWVH': 500},
    22: {'WWV': 500, 'WWVH': 600},
    23: {'WWV': 600, 'WWVH': 500},
    24: {'WWV': 500, 'WWVH': 600},
    25: {'WWV': 600, 'WWVH': 500},
    26: {'WWV': 500, 'WWVH': 600},
    27: {'WWV': 600, 'WWVH': 500},
    28: {'WWV': 500, 'WWVH': 600},
    29: {'WWV': None, 'WWVH': None},
    30: {'WWV': None, 'WWVH': None},
    31: {'WWV': 600, 'WWVH': 500},
    32: {'WWV': 500, 'WWVH': 600},
    33: {'WWV': 600, 'WWVH': 500},
    34: {'WWV': 500, 'WWVH': 600},
    35: {'WWV': 600, 'WWVH': 500},
    36: {'WWV': 500, 'WWVH': 600},
    37: {'WWV': 600, 'WWVH': 500},
    38: {'WWV': 500, 'WWVH': 600},
    39: {'WWV': 600, 'WWVH': 500},
    40: {'WWV': 500, 'WWVH': 600},
    41: {'WWV': 600, 'WWVH': 500},
    42: {'WWV': 500, 'WWVH': 600},
    43: {'WWV': None, 'WWVH': 500},   # WWVH-only
    44: {'WWV': None, 'WWVH': 600},   # WWVH-only (+ test signal)
    45: {'WWV': None, 'WWVH': 500},   # WWVH-only
    46: {'WWV': None, 'WWVH': 600},   # WWVH-only
    47: {'WWV': None, 'WWVH': 500},   # WWVH-only
    48: {'WWV': None, 'WWVH': 600},   # WWVH-only
    49: {'WWV': None, 'WWVH': 500},   # WWVH-only
    50: {'WWV': None, 'WWVH': 600},   # WWVH-only
    51: {'WWV': None, 'WWVH': 500},   # WWVH-only
    52: {'WWV': 500, 'WWVH': 600},
    53: {'WWV': 600, 'WWVH': 500},
    54: {'WWV': 500, 'WWVH': 600},
    55: {'WWV': 600, 'WWVH': 500},
    56: {'WWV': 500, 'WWVH': 600},
    57: {'WWV': 600, 'WWVH': 500},
    58: {'WWV': 500, 'WWVH': 600},
    59: {'WWV': None, 'WWVH': None}
}

# =============================================================================
# STANDARD CHANNELS LIST
# =============================================================================
# Systematic list of all monitored channels
STANDARD_CHANNELS = [
    'WWV 2.5 MHz', 'WWV 5 MHz', 'WWV 10 MHz', 'WWV 15 MHz', 
    'WWV 20 MHz', 'WWV 25 MHz',
    'WWVH 2.5 MHz', 'WWVH 5 MHz', 'WWVH 10 MHz', 'WWVH 15 MHz',
    'CHU 3.33 MHz', 'CHU 7.85 MHz', 'CHU 14.67 MHz',
    'BPM 2.5 MHz', 'BPM 5 MHz', 'BPM 10 MHz', 'BPM 15 MHz'
]

# =============================================================================
# STATION LOCATIONS (for propagation delay calculations)
# =============================================================================
# IMPORTANT: These are the AUTHORITATIVE coordinates. All other modules MUST
# import from this file. Do NOT define station coordinates elsewhere!
#
# Issue 4.1 Fix (2025-12-07): Coordinates were inconsistent across 6 files,
# with differences up to 0.008° (~700 meters). This caused ~2-3 μs timing error.
# Now consolidated to single source of truth with NIST-verified coordinates.
#
# VERIFICATION SOURCES:
# - WWV: NIST website (https://www.nist.gov/pml/time-and-frequency-division/
#        time-distribution/radio-station-wwv) states: 40° 40' 50.5" N, 105° 02' 26.6" W
# - WWVH: NIST website (https://www.nist.gov/pml/time-and-frequency-division/
#         time-distribution/radio-station-wwvh) states: 21° 59' 14" N, 159° 45' 49" W
# - CHU: NRC Canada (https://nrc.canada.ca/en/certifications-evaluations-standards/
#        canadas-official-time/nrc-shortwave-station-chu) states: 45° 17' 43" N, 75° 45' 16" W
# =============================================================================

# WWV - Fort Collins, Colorado, USA
# NIST official: 40° 40' 50.5" N, 105° 02' 26.6" W
# Decimal: 40 + 40/60 + 50.5/3600 = 40.68069, 105 + 2/60 + 26.6/3600 = 105.04072
WWV_LAT = 40.6807
WWV_LON = -105.0407

# WWVH - Kekaha, Kauai, Hawaii, USA  
# NIST official: 21° 59' 14" N, 159° 45' 49" W
# Decimal: 21 + 59/60 + 14/3600 = 21.98722, 159 + 45/60 + 49/3600 = 159.76361
WWVH_LAT = 21.9872
WWVH_LON = -159.7636

# CHU - Ottawa, Ontario, Canada
# NRC official: 45° 17' 43" N, 75° 45' 16" W
# Decimal: 45 + 17/60 + 43/3600 = 45.29528, 75 + 45/60 + 16/3600 = 75.75444
CHU_LAT = 45.2953
CHU_LON = -75.7544

# BPM - Pucheng County, Shaanxi, China
# 34°56′55.96″N 109°32′34.93″E
# Decimal: 34 + 56/60 + 55.96/3600 = 34.94887...
#          109 + 32/60 + 34.93/3600 = 109.54303...
BPM_LAT = 34.9489
BPM_LON = 109.5430

# Convenience dictionary for programmatic access
STATION_LOCATIONS = {
    'WWV': {'lat': WWV_LAT, 'lon': WWV_LON, 'name': 'Fort Collins, CO, USA'},
    'WWVH': {'lat': WWVH_LAT, 'lon': WWVH_LON, 'name': 'Kekaha, Kauai, HI, USA'},
    'CHU': {'lat': CHU_LAT, 'lon': CHU_LON, 'name': 'Ottawa, ON, Canada'},
    'BPM': {'lat': BPM_LAT, 'lon': BPM_LON, 'name': 'Pucheng, Shaanxi, China'},
}

# =============================================================================
# TONE FREQUENCIES (Hz)
# =============================================================================

# Fundamental timing markers
WWV_TICK_FREQ = 1000   # Hz - WWV uses 1000 Hz tick
WWVH_TICK_FREQ = 1200  # Hz - WWVH uses 1200 Hz tick
CHU_TICK_FREQ = 1000   # Hz - CHU uses 1000 Hz tick
BPM_TICK_FREQ = 1000   # Hz - BPM uses 1000 Hz tick

# =============================================================================
# CHU TIMING STRUCTURE (Reference: NRC CHU Technical Specifications)
# =============================================================================

# CHU 1000 Hz tone pattern per minute:
# - Second 00: 0.5s tone (minute marker) - 1.0s at top of hour
# - Seconds 01-08: 0.3s tones (or DUT1 split tones for positive DUT1)
# - Seconds 09-16: 0.3s tones (or DUT1 split tones for negative DUT1)
# - Seconds 17-28: 0.3s tones (regular)
# - Second 29: ALWAYS SILENT (distinguishes CHU from WWV)
# - Second 30: 0.3s tone
# - Seconds 31-39: 10ms ticks only (FSK digital time code transmitted)
# - Seconds 40-49: 0.3s tones (regular)
# - Seconds 50-59: 10ms ticks only (voice announcements)

CHU_MINUTE_MARKER_DURATION = 0.5   # seconds (0.5s at :00, 1.0s at top of hour)
CHU_REGULAR_TONE_DURATION = 0.3    # seconds
CHU_TICK_DURATION = 0.01           # seconds (10ms during FSK/voice)
CHU_SILENT_SECOND = 29             # Always omitted
CHU_FSK_SECONDS = set(range(31, 40))    # Digital time code (Bell 103 AFSK)
CHU_VOICE_SECONDS = set(range(50, 60))  # Voice announcements

# DUT1 encoding seconds (split tones: 0.1s + 0.1s silence + 0.1s)
CHU_DUT1_POSITIVE_SECONDS = set(range(1, 9))   # +0.1s to +0.8s
CHU_DUT1_NEGATIVE_SECONDS = set(range(9, 17))  # -0.1s to -0.8s

# CHU FSK parameters (Bell 103 AFSK)
CHU_FSK_MARK_FREQ = 2225   # Hz (bit 1)
CHU_FSK_SPACE_FREQ = 2025  # Hz (bit 0)
CHU_FSK_BAUD_RATE = 300    # bits per second

# Extended tones for discrimination
TONE_440_HZ = 440
TONE_500_HZ = 500
TONE_600_HZ = 600

# =============================================================================
# BPM TIMING STRUCTURE
# =============================================================================
BPM_UTC_TICK_DURATION = 0.010  # 10 ms
BPM_UT1_TICK_DURATION = 0.100  # 100 ms
BPM_MINUTE_MARKER_DURATION = 0.300 # 300 ms

# UT1 Minutes: 25-29, 55-59
BPM_UT1_MINUTES = set(range(25, 30)) | set(range(55, 60))

# =============================================================================
# DETECTION THRESHOLDS
# =============================================================================

# SNR thresholds for anchor quality (dB)
ANCHOR_SNR_HIGH = 15.0      # Very confident anchor
ANCHOR_SNR_MEDIUM = 10.0    # Usable anchor
ANCHOR_SNR_LOW = 6.0        # Marginal anchor

# Confidence thresholds for transmission time solver
SOLVER_MIN_CONFIDENCE = 0.3     # Minimum confidence for valid solution
UTC_VERIFICATION_THRESHOLD_MS = 2.0  # Maximum UTC offset for verification

# =============================================================================
# PROPAGATION CONSTANTS
# =============================================================================

# Speed of light (km/s) for propagation delay calculations
SPEED_OF_LIGHT_KM_S = 299792.458

# Earth radius (km) for path length calculations
EARTH_RADIUS_KM = 6371.0

# Ionospheric layer heights (km)
E_LAYER_HEIGHT_KM = 110.0
F_LAYER_HEIGHT_KM = 300.0

# Ionospheric delay per hop (ms) - empirical average
IONOSPHERIC_DELAY_PER_HOP_MS = 0.15

# Maximum frequency-dependent dispersion (ms)
MAX_DISPERSION_MS = 3.0

# Safety margin for initial guided search (ms)
# Widen the window to account for unmodeled delays/errors on first lock
GUIDED_SEARCH_SAFETY_MARGIN_MS = 5.0

# CRITICAL PHYSICS CONSTRAINT:
# The total search window (Dispersion + Safety) MUST be significantly smaller
# than the separation between stations to ensure ambiguity resolution.
#
# Limit: (3.0 ms + 5.0 ms) = 8.0 ms << 15.0 ms (Station Separation)
# This guarantees that a window centered on WWV will NOT capture WWVH.

# Minimum WWV-WWVH time separation (ms) (Path difference ~4000km)
STATION_SEPARATION_MS = 15.0

# =============================================================================
# PROPAGATION PLAUSIBILITY BOUNDS (ms)
# =============================================================================
# 
# These define the plausible range of propagation delays for each station.
# Used to reject false detections outside reasonable ionospheric paths.
#
# Conservative bounds that should work for continental US receivers:
# - Ground wave: ~3-7 ms/1000km
# - 1-hop F: adds ~2-5 ms over ground wave
# - Multi-hop: each additional hop adds ~2-3 ms
#
# Station                Distance (typical US)    Plausible delay range
# ------                 --------------------     ---------------------
# WWV (Fort Collins)     500-3000 km             3-25 ms
# CHU (Ottawa)           1000-4000 km            5-30 ms  
# WWVH (Hawaii)          4000-6000 km            15-50 ms

# Propagation delay bounds by station (ms)
# Format: (min_delay_ms, max_delay_ms)
PROPAGATION_BOUNDS_MS = {
    'WWV': (2.0, 35.0),    # Continental US to Fort Collins
    'WWVH': (12.0, 60.0),  # Continental US to Hawaii (longer path)
    'CHU': (3.0, 40.0),    # Continental US to Ottawa
    'BPM': (25.0, 50.0),   # Continental US to China (very long path)
}

# Default bounds for unknown stations
DEFAULT_PROPAGATION_BOUNDS_MS = (0.0, 100.0)

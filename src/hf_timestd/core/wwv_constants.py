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
    Coordinates: see STATION_CATALOG — this module derives them below and
    does not restate them, which is how the last stale copy got here.
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

SAMPLE_RATE_FULL = 24000  # Hz - Full rate from radiod RTP stream (24 kHz for integer WWVH cycles)
SAMPLE_RATE_LEGACY = 16000  # Hz - Legacy 16 kHz mode (deprecated)

# =============================================================================
# STATION BROADCAST SCHEDULES
# =============================================================================

# Valid station/frequency combinations (MHz) — re-exported from the
# hamsci-dsp station catalog (split design §5.2: pure station data lives in
# `hamsci_dsp.stations`; this module keeps timing schedules/tones/thresholds).
# STATION_CATALOG is also the source for the STATION COORDINATES section below.
from hamsci_dsp.stations import BUILTIN_CATALOG as STATION_CATALOG
WWV_FREQUENCIES = list(STATION_CATALOG.get('WWV').frequencies_mhz)
WWVH_FREQUENCIES = list(STATION_CATALOG.get('WWVH').frequencies_mhz)  # NOT 20/25 MHz
CHU_FREQUENCIES = list(STATION_CATALOG.get('CHU').frequencies_mhz)
BPM_FREQUENCIES = list(STATION_CATALOG.get('BPM').frequencies_mhz)

# Shared frequencies requiring discrimination (WWV vs WWVH vs BPM) — derived
# from the per-station lists so they cannot drift from the catalog.
SHARED_FREQUENCIES = sorted(
    set(WWV_FREQUENCIES) & set(WWVH_FREQUENCIES) & set(BPM_FREQUENCIES)
)

# Station-specific frequencies (no discrimination needed)
# Maps frequency (MHz) to station name.  CHU shares no frequency with the
# other stations, so all of its channels are station-specific.
STATION_SPECIFIC_FREQ = {
    **{f: 'WWV' for f in WWV_FREQUENCIES if f not in SHARED_FREQUENCIES},
    **{f: 'CHU' for f in CHU_FREQUENCIES},
}

# Minutes where only one station broadcasts 500/600 Hz tones (ground truth)
# These provide unambiguous station identification
WWV_ONLY_TONE_MINUTES: Set[int] = {1, 16, 17, 19}
WWVH_ONLY_TONE_MINUTES: Set[int] = {2, 43, 44, 45, 46, 47, 48, 49, 50, 51}

# Scientific-modulation test signal schedule.
#
# ⚠ Corrected 2026-08-26 (mjh): WWVH transmits its test signal in minute
# **48**, not 44.  The wrong minute was carried consistently in four
# places -- metrology_service, probabilistic_discriminator (twice) and
# the wwvh_discrimination docstring table -- so every WWVH test-signal
# observation was taken from a minute in which WWVH transmits no test
# signal, guaranteeing non-detection.  Defined once here so a schedule
# fact has a single home rather than four copies to drift apart.
TEST_SIGNAL_MINUTE: dict = {'WWV': 8, 'WWVH': 48}
TEST_SIGNAL_MINUTES: Set[int] = set(TEST_SIGNAL_MINUTE.values())


def station_for_test_minute(minute: int):
    """Which station transmits its test signal in this minute, or None."""
    for station, m in TEST_SIGNAL_MINUTE.items():
        if minute == m:
            return station
    return None


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

# Station coordinates are RE-EXPORTED from the hamsci-dsp station catalog
# (split design §5.2: pure station data lives in `hamsci_dsp.stations`;
# this module keeps timing schedules/tones/thresholds).  The NIST/NRC
# provenance notes and dd°mm'ss" derivations moved with the data — see
# hamsci_dsp/stations.py and its tests, which pin the exact values.
# (STATION_CATALOG is imported once, in the STATION BROADCAST SCHEDULES
# section above.)

WWV_LAT = STATION_CATALOG.get('WWV').lat
WWV_LON = STATION_CATALOG.get('WWV').lon
WWVH_LAT = STATION_CATALOG.get('WWVH').lat
WWVH_LON = STATION_CATALOG.get('WWVH').lon
CHU_LAT = STATION_CATALOG.get('CHU').lat
CHU_LON = STATION_CATALOG.get('CHU').lon
BPM_LAT = STATION_CATALOG.get('BPM').lat
BPM_LON = STATION_CATALOG.get('BPM').lon
WWVB_LAT = STATION_CATALOG.get('WWVB').lat
WWVB_LON = STATION_CATALOG.get('WWVB').lon

# Convenience tuples for programmatic access
WWV_COORDINATES = STATION_CATALOG.get('WWV').coordinates
WWVH_COORDINATES = STATION_CATALOG.get('WWVH').coordinates
CHU_COORDINATES = STATION_CATALOG.get('CHU').coordinates
BPM_COORDINATES = STATION_CATALOG.get('BPM').coordinates
WWVB_COORDINATES = STATION_CATALOG.get('WWVB').coordinates

STATION_LOCATIONS = STATION_CATALOG.locations()

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

# Pure carrier minutes (no time code modulation) - useful for BPM carrier measurement
BPM_PURE_CARRIER_MINUTES = set(range(10, 16)) | set(range(40, 46))

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
# 
# BOOTSTRAP vs CALIBRATED bounds:
# - Bootstrap: Wide bounds to find initial lock (but still physically plausible)
# - Calibrated: Tight bounds (3σ around learned expectation)
#
# These are the BOOTSTRAP bounds - physically plausible for continental US
# NOTE (2026-01-24): Widened temporarily to allow detections while investigating
# systematic ~50ms offset. Original values were tighter.
PROPAGATION_BOUNDS_MS_BOOTSTRAP = {
    'WWV': (-10.0, 80.0),    # Fort Collins: 500-3000km → 3-25ms + margin + investigation
    'WWVH': (0.0, 100.0),    # Hawaii: 4000-6000km → 15-50ms + margin + investigation
    'CHU': (-10.0, 80.0),    # Ottawa: 1000-4000km → 5-30ms + margin + investigation
    'BPM': (30.0, 150.0),    # China: 10960km → 36ms light + iono = 40-100ms multi-hop
                             # FIXED 2026-02-05: Was (10.0, 150.0) which accepted WWV/WWVH signals
}

# Legacy alias for backwards compatibility (uses bootstrap bounds)
PROPAGATION_BOUNDS_MS = PROPAGATION_BOUNDS_MS_BOOTSTRAP

# Default bounds for unknown stations
DEFAULT_PROPAGATION_BOUNDS_MS = (0.0, 100.0)

# =============================================================================
# UNAMBIGUOUS BOOTSTRAP CHANNELS
# =============================================================================
# These channels have only ONE station transmitting, so there's no ambiguity
# about which station is being detected. Prefer these during bootstrap.
#
# WWV-only frequencies: 20 MHz, 25 MHz (WWVH doesn't transmit)
# CHU: All frequencies (3.33, 7.85, 14.67 MHz) - unique station
#
UNAMBIGUOUS_BOOTSTRAP_CHANNELS = {
    'WWV_20.0': 'WWV',
    'WWV_25.0': 'WWV', 
    'CHU_3.33': 'CHU',
    'CHU_7.85': 'CHU',
    'CHU_14.67': 'CHU',
}

# =============================================================================
# CROSS-STATION GEOGRAPHIC CONSISTENCY
# =============================================================================
# Stations at similar distances should have similar propagation delays.
# These pairs should agree within the specified tolerance (accounting for
# ionospheric variation and path differences).
#
# Format: ((station1, freq1), (station2, freq2), max_difference_ms)
#
GEOGRAPHIC_CONSISTENCY_PAIRS = [
    # Similar distance pairs (daytime, stable ionosphere)
    # WWV 5 MHz and CHU 3.33 MHz: both ~1000-2000km, should be within ~10ms
    (('WWV', 5.0), ('CHU', 3.33), 15.0),
    # WWV 15 MHz and CHU 14.67 MHz: similar frequency, similar distance
    (('WWV', 15.0), ('CHU', 14.67), 15.0),
    # WWV 10 MHz and CHU 7.85 MHz: mid-band comparison
    (('WWV', 10.0), ('CHU', 7.85), 15.0),
]

# Maximum calibration offset allowed (ms)
# Any offset larger than this indicates a systematic error, not real propagation
MAX_CALIBRATION_OFFSET_MS = 80.0  # BPM multi-hop can be ~60-70ms

"""
TEC Validation Module

Validates HF-derived TEC measurements against GPS VTEC from IONEX files.
Calculates bias and populates validation fields in TEC measurements.

HF-derived TEC is a SLANT quantity, integrated along the oblique propagation
path; GPS IONEX TEC is VERTICAL. The two are mapped onto a common (vertical)
basis via the thin-shell obliquity factor before differencing (P-H9), and the
GPS map is sampled at the great-circle ionospheric pierce point.

Uses existing IONEX infrastructure from ionospheric_model.py and the shared
spherical-Earth geometry in tec_geometry.py.

Author: HF-TimeStd Science Team
"""

from typing import Dict, Optional, Tuple
from datetime import datetime, timezone
from pathlib import Path
import logging
import numpy as np

from hf_timestd.core.tec_geometry import (
    DEFAULT_IONO_HEIGHT_KM,
    calculate_elevation_angle,
    calculate_midpoint,
    convert_slant_to_vertical,
)
from hf_timestd.core.wwv_constants import STATION_LOCATIONS

logger = logging.getLogger(__name__)


class TECValidator:
    """
    Validates HF-derived TEC against GPS VTEC from IONEX files.

    HF timing yields slant TEC along the oblique propagation path; IONEX maps
    are vertical. The validator maps the HF slant TEC down to vertical at the
    ionospheric pierce point so the comparison is vertical-vs-vertical.
    """

    # Validation thresholds
    MAX_TEC_DIFFERENCE_TECU = 50.0  # Maximum reasonable TEC difference
    MIN_CONFIDENCE_FOR_VALIDATION = 0.5  # Minimum TEC confidence to validate

    # Validation flags
    FLAG_VALIDATED = 'VALIDATED'
    FLAG_UNVALIDATED = 'UNVALIDATED'
    FLAG_VTEC_UNAVAILABLE = 'VTEC_UNAVAILABLE'
    FLAG_VALIDATION_FAILED = 'VALIDATION_FAILED'

    def __init__(self, ionex_dir: Optional[Path] = None):
        """
        Initialize TEC validator.

        Args:
            ionex_dir: Directory containing IONEX files (default: /var/lib/timestd/ionex)
        """
        self.ionex_dir = Path(ionex_dir) if ionex_dir else Path('/var/lib/timestd/ionex')

        # Import ionospheric model for IONEX access
        try:
            from hf_timestd.core.ionospheric_model import IonosphericModel
            self.iono_model = IonosphericModel(
                enable_iri=False,  # Don't need IRI for validation
                enable_calibration=False,
                ionex_dir=self.ionex_dir
            )
            logger.info(f"TEC Validator initialized with IONEX dir: {self.ionex_dir}")
        except Exception as e:
            logger.warning(f"Failed to initialize ionospheric model: {e}")
            self.iono_model = None

    def validate_tec_measurement(
        self,
        tec_measurement: Dict,
        ipp_lat: float,
        ipp_lon: float,
        elevation_deg: float,
        h_iono_km: float = DEFAULT_IONO_HEIGHT_KM,
    ) -> Dict:
        """
        Validate a single HF TEC measurement against GPS VTEC.

        HF-derived TEC is a SLANT quantity (integrated along the oblique
        propagation path); GPS IONEX TEC is VERTICAL. Differencing them
        directly — the pre-P-H9 behaviour — reports mostly the obliquity
        factor (2-3x at low elevation), not a real bias. This method maps the
        HF slant TEC down to vertical at the pierce point before comparing,
        so ``tec_bias_tecu`` is a genuine vertical-vs-vertical difference.

        Args:
            tec_measurement: TEC measurement dictionary with keys:
                - timestamp_utc: ISO8601 string, Unix epoch, or datetime
                - tec_tecu: HF-derived SLANT TEC in TECU
                - confidence: TEC confidence (0-1)
                - station: Station name
            ipp_lat, ipp_lon: Ionospheric pierce point (degrees) — where the
                GPS VTEC map is sampled.
            elevation_deg: Elevation angle of the HF path at the ionosphere
                (degrees); drives the slant->vertical obliquity mapping.
            h_iono_km: Thin-shell ionospheric height (km).

        Returns:
            Updated measurement dictionary with validation fields:
                - vtec_tecu: GPS VTEC sampled at the IPP
                - hf_vtec_tecu: HF TEC mapped to vertical
                - obliquity_factor: slant/vertical ratio used for the mapping
                - tec_bias_tecu: hf_vtec_tecu - vtec_tecu (vertical vs vertical)
                - validation_flag: Validation status
        """
        # Initialize validation fields
        validation_fields = {
            'vtec_tecu': None,
            'hf_vtec_tecu': None,
            'obliquity_factor': None,
            'tec_bias_tecu': None,
            'validation_flag': self.FLAG_UNVALIDATED,
        }

        # Check if we have minimum required data
        if self.iono_model is None:
            validation_fields['validation_flag'] = self.FLAG_VTEC_UNAVAILABLE
            return validation_fields

        # Check TEC confidence
        confidence = tec_measurement.get('confidence', 0.0)
        if confidence < self.MIN_CONFIDENCE_FOR_VALIDATION:
            logger.debug(f"TEC confidence too low for validation: {confidence:.2f}")
            validation_fields['validation_flag'] = self.FLAG_UNVALIDATED
            return validation_fields

        # Parse timestamp
        try:
            timestamp_str = tec_measurement.get('timestamp_utc')
            if isinstance(timestamp_str, str):
                timestamp = datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))
            elif isinstance(timestamp_str, (int, float)):
                timestamp = datetime.fromtimestamp(timestamp_str, tz=timezone.utc)
            else:
                timestamp = timestamp_str
        except Exception as e:
            logger.warning(f"Failed to parse timestamp: {e}")
            validation_fields['validation_flag'] = self.FLAG_VALIDATION_FAILED
            return validation_fields

        # Get HF-derived (slant) TEC
        hf_tec = tec_measurement.get('tec_tecu')
        if hf_tec is None or not np.isfinite(hf_tec):
            validation_fields['validation_flag'] = self.FLAG_VALIDATION_FAILED
            return validation_fields

        # A single-hop reflection needs a positive elevation. A non-positive
        # (or non-finite) value means the thin-shell point is below the local
        # horizon at this range — no valid obliquity mapping exists.
        if not np.isfinite(elevation_deg) or elevation_deg <= 0.0:
            logger.warning(f"Non-physical elevation for validation: {elevation_deg}")
            validation_fields['validation_flag'] = self.FLAG_VALIDATION_FAILED
            return validation_fields

        # Map HF slant TEC -> vertical at the pierce point so the comparison
        # against GPS VTEC is vertical-vs-vertical (P-H9).
        hf_vtec, obliquity = convert_slant_to_vertical(hf_tec, elevation_deg, h_iono_km)
        validation_fields['hf_vtec_tecu'] = float(hf_vtec)
        validation_fields['obliquity_factor'] = float(obliquity)

        # Get GPS VTEC from IONEX at the ionospheric pierce point
        try:
            ionex_result = self.iono_model.get_ionex_vtec(
                lat=ipp_lat,
                lon=ipp_lon,
                timestamp=timestamp
            )

            if ionex_result is None:
                validation_fields['validation_flag'] = self.FLAG_VTEC_UNAVAILABLE
                return validation_fields

            gps_vtec, source_file = ionex_result

            # Sanity check on VTEC value. The floor is 0.1 TECU, not 1.0
            # (P-M7): deep-night ionospheric VTEC genuinely drops below
            # 1 TECU, and the old 1.0 floor rejected those valid low-TEC
            # samples as out-of-range.
            if not (0.1 <= gps_vtec < 500.0):
                logger.warning(f"GPS VTEC out of range: {gps_vtec:.2f} TECU")
                validation_fields['validation_flag'] = self.FLAG_VALIDATION_FAILED
                return validation_fields

            # Vertical-vs-vertical bias (both quantities now vertical).
            tec_bias = hf_vtec - gps_vtec

            # Check if bias is reasonable
            if abs(tec_bias) > self.MAX_TEC_DIFFERENCE_TECU:
                logger.warning(
                    f"TEC bias too large: {tec_bias:.2f} TECU "
                    f"(HF vertical: {hf_vtec:.2f}, GPS: {gps_vtec:.2f})"
                )
                validation_fields['validation_flag'] = self.FLAG_VALIDATION_FAILED
            else:
                validation_fields['validation_flag'] = self.FLAG_VALIDATED
                logger.debug(
                    f"TEC validated: HF vertical={hf_vtec:.2f} TECU, "
                    f"GPS={gps_vtec:.2f} TECU, bias={tec_bias:.2f} TECU"
                )

            # Populate validation fields
            validation_fields['vtec_tecu'] = float(gps_vtec)
            validation_fields['tec_bias_tecu'] = float(tec_bias)

        except (OSError, ValueError) as e:
            # IONEX read/parse failure (a missing or corrupt file) — the GPS
            # VTEC is genuinely unavailable, so validation could not run.
            # P-M7: flag VTEC_UNAVAILABLE, not VALIDATION_FAILED, and catch
            # only IO/parse errors — a real bug now propagates instead of
            # being silently recorded as a failed validation.
            logger.warning(f"IONEX VTEC unavailable for validation: {e}")
            validation_fields['validation_flag'] = self.FLAG_VTEC_UNAVAILABLE

        return validation_fields

    def calculate_ipp_location(
        self,
        tx_lat: float,
        tx_lon: float,
        rx_lat: float,
        rx_lon: float,
        iono_height_km: float = DEFAULT_IONO_HEIGHT_KM
    ) -> Tuple[float, float]:
        """
        Calculate the Ionospheric Pierce Point (IPP) for a single-hop HF path.

        For a single-hop thin-shell reflection the sub-ionospheric point sits
        at the great-circle midpoint of the TX-RX path. The pre-P-H9 code used
        a Cartesian mean of latitude/longitude, which is wrong for the long,
        wide-longitude-span paths typical of HF time signals (WWV-CHU spans
        ~30 deg of longitude) and breaks entirely near the poles and the
        antimeridian.

        Args:
            tx_lat, tx_lon: Transmitter location (degrees)
            rx_lat, rx_lon: Receiver location (degrees)
            iono_height_km: Ionospheric layer height (km) — reserved; the
                symmetric single-hop apex is the great-circle midpoint
                independent of shell height.

        Returns:
            (ipp_lat, ipp_lon) in degrees
        """
        return calculate_midpoint(tx_lat, tx_lon, rx_lat, rx_lon)

    def get_station_location(self, station: str) -> Optional[Tuple[float, float]]:
        """
        Get station coordinates.

        Args:
            station: Station name (WWV, WWVH, CHU, BPM)

        Returns:
            (latitude, longitude) in degrees or None if unknown

        Coordinates come from the single source of truth,
        wwv_constants.STATION_LOCATIONS (P-M6). The local copy this replaced
        had BPM at 31.207°N, 121.200°E — Shanghai — ~1100 km from the real
        Pucheng transmitter, which silently corrupted every BPM HF-path
        geometry computed for validation.
        """
        loc = STATION_LOCATIONS.get(station.upper())
        if loc is None:
            return None
        return (loc['lat'], loc['lon'])

    def validate_batch(
        self,
        tec_measurements: list,
        receiver_lat: float,
        receiver_lon: float
    ) -> list:
        """
        Validate a batch of TEC measurements.

        Args:
            tec_measurements: List of TEC measurement dictionaries
            receiver_lat: Receiver latitude (degrees)
            receiver_lon: Receiver longitude (degrees)

        Returns:
            List of measurements with validation fields added
        """
        validated_measurements = []

        for measurement in tec_measurements:
            # Get station location
            station = measurement.get('station', 'UNKNOWN')
            station_coords = self.get_station_location(station)

            if station_coords is None:
                logger.warning(f"Unknown station: {station}")
                validation_fields = {
                    'vtec_tecu': None,
                    'hf_vtec_tecu': None,
                    'obliquity_factor': None,
                    'tec_bias_tecu': None,
                    'validation_flag': self.FLAG_VALIDATION_FAILED
                }
            else:
                tx_lat, tx_lon = station_coords

                # Great-circle IPP and the path elevation at the ionosphere,
                # both from the shared spherical-Earth geometry module.
                ipp_lat, ipp_lon = self.calculate_ipp_location(
                    tx_lat, tx_lon, receiver_lat, receiver_lon
                )
                elevation_deg = calculate_elevation_angle(
                    receiver_lat, receiver_lon, tx_lat, tx_lon
                )

                # Validate using IPP location and path geometry
                validation_fields = self.validate_tec_measurement(
                    measurement,
                    ipp_lat,
                    ipp_lon,
                    elevation_deg,
                )

            # Add validation fields to measurement
            validated_measurement = {**measurement, **validation_fields}
            validated_measurements.append(validated_measurement)

        return validated_measurements

"""
TEC Validation Module

Validates HF-derived TEC measurements against GPS VTEC from IONEX files.
Calculates bias and populates validation fields in TEC measurements.

Uses existing IONEX infrastructure from ionospheric_model.py.

Author: HF-TimeStd Science Team
"""

from typing import Dict, Optional, Tuple
from datetime import datetime, timezone
from pathlib import Path
import logging
import numpy as np

logger = logging.getLogger(__name__)


class TECValidator:
    """
    Validates HF-derived TEC against GPS VTEC from IONEX files.
    
    Compares slant TEC measurements from HF timing with vertical TEC
    from GPS IONEX maps, accounting for geometry and propagation path.
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
        station_lat: float,
        station_lon: float
    ) -> Dict:
        """
        Validate a single TEC measurement against GPS VTEC.
        
        Args:
            tec_measurement: TEC measurement dictionary with keys:
                - timestamp_utc: ISO8601 timestamp
                - tec_tecu: HF-derived TEC in TECU
                - confidence: TEC confidence (0-1)
                - station: Station name
            station_lat: Station latitude (degrees)
            station_lon: Station longitude (degrees)
        
        Returns:
            Updated measurement dictionary with validation fields:
                - vtec_tecu: GPS VTEC value
                - tec_bias_tecu: HF TEC - GPS VTEC
                - validation_flag: Validation status
        """
        # Initialize validation fields
        validation_fields = {
            'vtec_tecu': None,
            'tec_bias_tecu': None,
            'validation_flag': self.FLAG_UNVALIDATED
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
        
        # Get HF-derived TEC
        hf_tec = tec_measurement.get('tec_tecu')
        if hf_tec is None or not np.isfinite(hf_tec):
            validation_fields['validation_flag'] = self.FLAG_VALIDATION_FAILED
            return validation_fields
        
        # Get ionospheric pierce point (IPP) location
        # For now, use station location as approximation
        # TODO: Calculate actual IPP based on propagation path and ionospheric height
        ipp_lat = station_lat
        ipp_lon = station_lon
        
        # Get GPS VTEC from IONEX
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
            
            # Sanity check on VTEC value
            if not (1.0 < gps_vtec < 500.0):
                logger.warning(f"GPS VTEC out of range: {gps_vtec:.2f} TECU")
                validation_fields['validation_flag'] = self.FLAG_VALIDATION_FAILED
                return validation_fields
            
            # Calculate bias
            # Note: HF TEC is slant TEC, GPS VTEC is vertical TEC
            # For oblique paths, HF TEC should be higher than VTEC
            # Bias = HF_TEC - GPS_VTEC
            tec_bias = hf_tec - gps_vtec
            
            # Check if bias is reasonable
            if abs(tec_bias) > self.MAX_TEC_DIFFERENCE_TECU:
                logger.warning(
                    f"TEC bias too large: {tec_bias:.2f} TECU "
                    f"(HF: {hf_tec:.2f}, GPS: {gps_vtec:.2f})"
                )
                validation_fields['validation_flag'] = self.FLAG_VALIDATION_FAILED
            else:
                validation_fields['validation_flag'] = self.FLAG_VALIDATED
                logger.debug(
                    f"TEC validated: HF={hf_tec:.2f} TECU, GPS={gps_vtec:.2f} TECU, "
                    f"bias={tec_bias:.2f} TECU"
                )
            
            # Populate validation fields
            validation_fields['vtec_tecu'] = float(gps_vtec)
            validation_fields['tec_bias_tecu'] = float(tec_bias)
            
        except Exception as e:
            logger.warning(f"TEC validation failed: {e}")
            validation_fields['validation_flag'] = self.FLAG_VALIDATION_FAILED
        
        return validation_fields
    
    def calculate_ipp_location(
        self,
        tx_lat: float,
        tx_lon: float,
        rx_lat: float,
        rx_lon: float,
        iono_height_km: float = 350.0
    ) -> Tuple[float, float]:
        """
        Calculate Ionospheric Pierce Point (IPP) location.
        
        For oblique HF paths, the IPP is where the ray path intersects
        the ionospheric layer. This is a simplified calculation assuming
        a single-layer ionosphere.
        
        Args:
            tx_lat: Transmitter latitude (degrees)
            tx_lon: Transmitter longitude (degrees)
            rx_lat: Receiver latitude (degrees)
            rx_lon: Receiver longitude (degrees)
            iono_height_km: Ionospheric layer height (km)
        
        Returns:
            (ipp_lat, ipp_lon) in degrees
        """
        # Simplified: Use midpoint between TX and RX
        # TODO: Implement proper ray tracing with Earth curvature
        ipp_lat = (tx_lat + rx_lat) / 2.0
        ipp_lon = (tx_lon + rx_lon) / 2.0
        
        return ipp_lat, ipp_lon
    
    def get_station_location(self, station: str) -> Optional[Tuple[float, float]]:
        """
        Get station coordinates.
        
        Args:
            station: Station name (WWV, WWVH, CHU, BPM)
        
        Returns:
            (latitude, longitude) in degrees or None if unknown
        """
        # Station coordinates (approximate)
        STATION_COORDS = {
            'WWV': (40.678, -105.038),      # Fort Collins, CO
            'WWVH': (21.987, -159.763),     # Kauai, HI
            'CHU': (45.295, -75.752),       # Ottawa, ON
            'BPM': (31.207, 121.200),       # Shanghai, China
        }
        
        return STATION_COORDS.get(station.upper())
    
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
                    'tec_bias_tecu': None,
                    'validation_flag': self.FLAG_VALIDATION_FAILED
                }
            else:
                tx_lat, tx_lon = station_coords
                
                # Calculate IPP (simplified: use midpoint)
                ipp_lat, ipp_lon = self.calculate_ipp_location(
                    tx_lat, tx_lon,
                    receiver_lat, receiver_lon
                )
                
                # Validate using IPP location
                validation_fields = self.validate_tec_measurement(
                    measurement,
                    ipp_lat,
                    ipp_lon
                )
            
            # Add validation fields to measurement
            validated_measurement = {**measurement, **validation_fields}
            validated_measurements.append(validated_measurement)
        
        return validated_measurements

"""
Ionosphere Science Helper Module
Provides calculations for propagation delay, layer heights, and IRI-2020 predictions using
the hf_timestd core libraries.
"""
import numpy as np
from datetime import datetime
import logging

try:
    from hf_timestd.core.ionospheric_model import calculate_ionospheric_delay, get_hmF2
    from hf_timestd.core.physics_propagation import PhysicsPropagationModel
except ImportError:
    # Mock for development if core libs missing
    logging.warning("hf_timestd core libraries not found, using mocks")
    def calculate_ionospheric_delay(*args, **kwargs): return 0.0
    def get_hmF2(*args, **kwargs): return 300.0
    class PhysicsPropagationModel:
        def __init__(self, *args): pass
        def predict_path(self, *args): return {'delay_ms': 5.0}

logger = logging.getLogger(__name__)

STATIONS = {
    'WWV': {'lat': 40.6782, 'lon': -105.0408},
    'WWVH': {'lat': 21.9872, 'lon': -159.7617},
    'CHU': {'lat': 45.2978, 'lon': -75.7525},
    'BPM': {'lat': 35.0, 'lon': 109.5}
}

def get_iri_prediction(station: str, receiver_lat: float, receiver_lon: float, date: datetime):
    """
    Get IRI-2020 prediction for a path
    """
    if station not in STATIONS:
        return None
        
    tx = STATIONS[station]
    
    # Calculate midpoint
    mid_lat = (tx['lat'] + receiver_lat) / 2
    mid_lon = (tx['lon'] + receiver_lon) / 2
    
    # Get hmF2 (F2 Peak Height)
    hmf2 = get_hmF2(date, mid_lat, mid_lon)
    
    return {
        'hmF2_km': hmf2,
        'midpoint': {'lat': mid_lat, 'lon': mid_lon}
    }

def calculate_inferred_height(delay_ms: float, distance_km: float, elevation_angle_deg: float):
    """
    Invert propagation delay to estimate reflection height
    Simple triangular geometry approximation for single hop
    """
    c = 299.792458 # km/ms
    
    # Slant range from delay
    slant_range_km = delay_ms * c
    
    # Height calculation (simplified flat earth for short paths, need spherical for long)
    # h = sqrt(s^2 - (d/2)^2)
    
    try:
        if slant_range_km < distance_km:
            return None # Impossible
            
        height = np.sqrt((slant_range_km/2)**2 - (distance_km/2)**2)
        return height
    except:
        return None

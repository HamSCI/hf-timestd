import numpy as np
import logging
from collections import defaultdict

logger = logging.getLogger(__name__)

# Constants
C = 299792458.0  # Speed of light m/s
TECU = 1e16      # Electrons per m^2
K = 40.308       # Ionospheric constant m^3/s^2

# Frequencies (Hz)
FREQ_GPS_L1 = 1575.42e6
FREQ_GPS_L2 = 1227.60e6

class GNSSTECAnalyzer:
    def __init__(self, dcb_data=None):
        self.dcb_data = dcb_data if dcb_data else {}
        self.state = defaultdict(dict) # sv_key -> {'leveling_bias': float, 'count': int, 'last_phase_gf': float}
        self.sat_positions = {} # sv_key -> {'elev': float, 'azim': float, 'updated': float}
        
    def update_satellite_positions(self, nav_sat_msg, timestamp):
        """
        Update satellite Elevation/Azimuth from NAV-SAT message.
        """
        for sat in nav_sat_msg['sats']:
            gnss_id = sat['gnssId']
            sv_id = sat['svId']
            if gnss_id != 0: continue # Only GPS for now
            
            key = f"G{sv_id:02d}"
            self.sat_positions[key] = {
                'elev': sat['elev'], # degrees
                'azim': sat['azim'], # degrees
                'updated': timestamp
            }

    def process_rawx(self, rawx_msg):
        """
        Process RXM-RAWX epoch.
        Returns dictionary of results: {sv_key: {'vtec': float, 'stec': float, 'elev': float}}
        """
        results = {}
        
        # Group by Satellite
        sat_obs = defaultdict(dict)
        for meas in rawx_msg['measurements']:
            gnss_id = meas['gnssId']
            sv_id = meas['svId']
            if gnss_id != 0: continue # Only GPS
            
            key = f"G{sv_id:02d}"
            sig_id = meas['sigId']
            
            # Map sigId to Band (L1/L2)
            # GPS: L1C/A (sigId=0), L2CL (sigId=3), L2CM (sigId=4) -> ZED-F9P usually 0 and 3/4? 
            # Note: ZED-F9P RAWX sigIds:
            # 0: L1C/A
            # 3: L2C (L2 CL)
            # 4: L2C (L2 CM)
            # Need to create 'L1' and 'L2' logical observations
            
            if sig_id == 0:
                sat_obs[key]['P1'] = meas['prMes']
                sat_obs[key]['L1'] = meas['cpMes']
            elif sig_id in [3, 4]: 
                # Prefer one? Or just overwrite. L2CL/CM are same freq.
                sat_obs[key]['P2'] = meas['prMes']
                sat_obs[key]['L2'] = meas['cpMes']
                
        # Calculate TEC for each satellite
        for key, obs in sat_obs.items():
            if 'P1' not in obs or 'P2' not in obs or 'L1' not in obs or 'L2' not in obs:
                continue
                
            # Get Elevation
            sat_pos = self.sat_positions.get(key)
            if not sat_pos: continue # Need elevation for mapping
            if sat_pos['elev'] < 10: continue # Elevation mask
            
            # 1. Geometry-Free Combination (Code)
            # STEC_code = F * ( (P2 - P1) - c * DCB_sat )
            # F = f1^2 * f2^2 / (40.3 * (f1^2 - f2^2))
            
            f1 = FREQ_GPS_L1
            f2 = FREQ_GPS_L2
            
            p1 = obs['P1']
            p2 = obs['P2']
            
            # DCB Correction
            dcb_meters = 0.0
            
            lookup_keys = [
                (key, 'C1C', 'C2L'), # L1 C/A - L2C(L)
                (key, 'C1C', 'C2X'), # L1 C/A - L2C(M+L)
                (key, 'C1C', 'C2W'), # L1 C/A - L2P(Y) - Standard P1-P2
            ]
            
            found_dcb = False
            for k in lookup_keys:
                if k in self.dcb_data:
                    dcb_meters = self.dcb_data[k]
                    found_dcb = True
                    break
            
            # Calculate Raw Code STEC
            # The ionospheric delay difference (I1 - I2) is negative since I2 > I1 (P2 > P1).
            # 1/f1^2 - 1/f2^2 is also negative. The negatives cancel to yield a positive STEC.
            term = (p1 - p2) + dcb_meters # Ignoring receiver bias for now
            denom = K * (1.0/f1**2 - 1.0/f2**2)  # Preserved actual sign (negative)
            stec_code = term / denom             # neg / neg = positive STEC
            
            # 2. Carrier Phase Smoothing (Levelling)
            lamb1 = C / f1
            lamb2 = C / f2
            l1_m = obs['L1'] * lamb1
            l2_m = obs['L2'] * lamb2
            
            # Phase difference (geometry free)
            l_gf = l1_m - l2_m # Meters
            
            # L_gf = L1_m - L2_m = - K * TEC * (1/f1^2 - 1/f2^2) = - denom * TEC
            stec_phase_raw = l_gf / (-denom)

            
            # Levelling logic: STEC = STEC_phase_raw + Offset
            # Offset = Mean(STEC_code - STEC_phase_raw)
            
            state = self.state[key]
            if 'count' not in state:
                state['count'] = 0
                state['sum_diff'] = 0.0
                state['smooth_stec'] = 0.0
                state['last_l_gf'] = None
                
            # Carrier Smoothing (Arc based)
            # Check for cycle slip (large jump in L_gf)
            # A cycle slip causes a sudden jump in the geometry-free phase combination
            CYCLE_SLIP_THRESHOLD = 0.5  # meters - typical L1 wavelength is ~0.19m
            
            if state['last_l_gf'] is not None:
                l_gf_jump = abs(l_gf - state['last_l_gf'])
                if l_gf_jump > CYCLE_SLIP_THRESHOLD:
                    # Cycle slip detected - reset leveling for this satellite
                    logger.debug(f"{key}: Cycle slip detected (jump={l_gf_jump:.2f}m), resetting leveling")
                    state['count'] = 0
                    state['sum_diff'] = 0.0
            
            state['last_l_gf'] = l_gf
            
            diff = stec_code - stec_phase_raw
            
            # Update running mean (Levelling) with exponential weighting
            # This prevents unbounded accumulation of bias
            n = state['count'] + 1
            
            # Use exponential moving average after initial convergence (100 samples)
            if n <= 100:
                # Initial convergence: simple accumulation
                state['sum_diff'] = state.get('sum_diff', 0.0) + diff
                state['count'] = n
                offset = state['sum_diff'] / n
            else:
                # After convergence: exponential moving average (alpha = 0.01)
                # This allows slow adaptation while preventing runaway drift
                alpha = 0.01
                old_offset = state['sum_diff'] / state['count']
                offset = (1 - alpha) * old_offset + alpha * diff
                state['sum_diff'] = offset * state['count']  # Update sum to match new offset
            
            stec_smooth = stec_phase_raw + offset
            
            # Sanity check: STEC should be positive (ionosphere delays signals)
            # If negative, use code-only STEC (less precise but always valid)
            if stec_smooth < 0:
                logger.debug(f"{key}: Negative STEC ({stec_smooth/TECU:.2f} TECU), using code-only")
                stec_smooth = stec_code
            
            # 3. Mapping Function (VTEC)
            # SLM (Single Layer Model)
            el_rad = np.radians(sat_pos['elev'])
            Re = 6371e3
            H_ion = 350e3
            m_factor = 1.0 / np.sqrt(1.0 - (Re * np.cos(el_rad) / (Re + H_ion))**2)
            
            vtec = stec_smooth / m_factor
            
            results[key] = {
                'vtec': vtec, # In electrons/m^2
                'vtec_u': vtec / TECU,
                'stec_u': stec_smooth / TECU,
                'elev': sat_pos['elev'],
                'azim': sat_pos['azim']
            }
            
        return results


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
        # Receiver DCB estimation state
        self._rx_dcb_tecu = 0.0   # Current receiver DCB estimate (TECU)
        self._rx_dcb_count = 0    # Number of epochs used
        self._rx_dcb_converged = False
        
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
            # ZED-F9P uses L1 C/A (C1C) and L2C (C2L). CAS Rapid DCB files
            # rarely provide direct C1C→C2L; instead they provide C1C→C2W and
            # C2W→C2L separately. Compose: DCB(C1C,C2L) = DCB(C1C,C2W) + DCB(C2W,C2L).
            dcb_meters = 0.0
            
            # 1. Try direct lookup first (L2C signals only — matches ZED-F9P)
            lookup_keys = [
                (key, 'C1C', 'C2L'), # L1 C/A - L2C(L)
                (key, 'C1C', 'C2X'), # L1 C/A - L2C(M+L)
            ]
            
            found_dcb = False
            for k in lookup_keys:
                if k in self.dcb_data:
                    dcb_meters = self.dcb_data[k]
                    found_dcb = True
                    break
            
            # 2. Compose C1C→C2W + C2W→C2L (or C2W→C2X) if no direct match
            if not found_dcb:
                c1c_c2w = self.dcb_data.get((key, 'C1C', 'C2W'))
                if c1c_c2w is not None:
                    c2w_c2l = self.dcb_data.get((key, 'C2W', 'C2L'))
                    c2w_c2x = self.dcb_data.get((key, 'C2W', 'C2X'))
                    if c2w_c2l is not None:
                        dcb_meters = c1c_c2w + c2w_c2l
                        found_dcb = True
                    elif c2w_c2x is not None:
                        dcb_meters = c1c_c2w + c2w_c2x
                        found_dcb = True
            
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
                'vtec': vtec, # In electrons/m^2 (before rx DCB)
                'vtec_u': vtec / TECU,  # Will be corrected below
                'stec_u': stec_smooth / TECU,
                'elev': sat_pos['elev'],
                'azim': sat_pos['azim']
            }
        
        # Receiver DCB estimation and correction
        # The receiver hardware bias is common to all satellites.
        # Estimate it as the median per-satellite VTEC offset, then subtract.
        if len(results) >= 3:
            raw_vtecs = [r['vtec_u'] for r in results.values() if r['elev'] > 20]
            if len(raw_vtecs) >= 3:
                epoch_median = float(np.median(raw_vtecs))
                
                # Exponential moving average for receiver DCB
                self._rx_dcb_count += 1
                if self._rx_dcb_count == 1:
                    self._rx_dcb_tecu = epoch_median
                else:
                    # Fast convergence initially (alpha=0.1), then slow tracking (alpha=0.005)
                    alpha = 0.1 if self._rx_dcb_count < 30 else 0.005
                    self._rx_dcb_tecu = (1 - alpha) * self._rx_dcb_tecu + alpha * epoch_median
                
                if self._rx_dcb_count >= 10 and not self._rx_dcb_converged:
                    self._rx_dcb_converged = True
                    logger.info(f"Receiver DCB converged: {self._rx_dcb_tecu:.2f} TECU "
                                f"({self._rx_dcb_tecu * TECU * K * (1/FREQ_GPS_L1**2 - 1/FREQ_GPS_L2**2):.3f} m)")
                
                # Only apply correction after initial convergence
                if self._rx_dcb_converged:
                    for r in results.values():
                        r['vtec'] -= self._rx_dcb_tecu * TECU
                        r['vtec_u'] -= self._rx_dcb_tecu
                        r['stec_u'] -= self._rx_dcb_tecu  # Approximate
        
        return results


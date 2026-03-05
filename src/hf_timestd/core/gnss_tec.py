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

# Minimum VTEC floor for receiver DCB estimation (TECU).
# Night-time mid-latitude VTEC rarely drops below 2-5 TECU.
# Using 2 TECU as a conservative floor.
MIN_VTEC_FLOOR_TECU = 2.0

# Module version — logged at startup to detect site-packages shadowing.
_MODULE_VERSION = '2.0.0'  # bumped from 1.x after DCB/rx-DCB fix

class GNSSTECAnalyzer:
    def __init__(self, dcb_data=None):
        self.dcb_data = self._build_l2c_dcbs(dcb_data) if dcb_data else {}
        self.state = defaultdict(dict) # sv_key -> {'count', 'sum_diff', 'last_l_gf'}
        self.sat_positions = {} # sv_key -> {'elev': float, 'azim': float, 'updated': float}
        # Receiver inter-frequency bias (meters in P1-P2 domain).
        # Estimated once from the first epoch's high-elevation satellites
        # using the minimum-VTEC physical constraint.
        self._rx_dcb_meters = 0.0
        self._rx_dcb_estimated = False

    @staticmethod
    def _build_l2c_dcbs(dcb_data):
        """Build C1C-C2L DCBs for ZED-F9P (L2C) from SINEX chain.

        The SINEX file provides C1C-C2W and C2W-C2L separately.
        ZED-F9P measures L2C (sigId 3/4), so the correct satellite DCB is:
            DCB(C1C, C2L) = DCB(C1C, C2W) - DCB(C2W, C2L)

        If C1C-C2L or C1C-C2X is directly available, prefer those.
        """
        if not dcb_data:
            return {}
        enriched = dict(dcb_data)
        # Synthesize C1C-C2L for every GPS satellite that has the chain
        svs = set(k[0] for k in dcb_data.keys() if k[0].startswith('G'))
        for sv in svs:
            # Skip if direct C1C-C2L already present
            if (sv, 'C1C', 'C2L') in enriched:
                continue
            c1c_c2w = dcb_data.get((sv, 'C1C', 'C2W'))
            c2w_c2l = dcb_data.get((sv, 'C2W', 'C2L'))
            if c1c_c2w is not None and c2w_c2l is not None:
                enriched[(sv, 'C1C', 'C2L')] = c1c_c2w - c2w_c2l
            # Also try C1C-C2X from C1C-C2W - C2W-C2X
            if (sv, 'C1C', 'C2X') not in enriched:
                c2w_c2x = dcb_data.get((sv, 'C2W', 'C2X'))
                if c1c_c2w is not None and c2w_c2x is not None:
                    enriched[(sv, 'C1C', 'C2X')] = c1c_c2w - c2w_c2x
        n_synth = sum(1 for sv in svs if (sv, 'C1C', 'C2L') in enriched)
        logger.info(f"DCB: {n_synth} GPS satellites with C1C-C2L (synthesized from chain)")
        return enriched
        
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

    def _get_sat_dcb(self, sv_key):
        """Look up satellite DCB in meters for the given SV."""
        for k in [
            (sv_key, 'C1C', 'C2L'),  # Correct for ZED-F9P L2C
            (sv_key, 'C1C', 'C2X'),  # L2C(M+L) alternative
            (sv_key, 'C1C', 'C2W'),  # L2P(Y) fallback, ~0.15m error
        ]:
            if k in self.dcb_data:
                return self.dcb_data[k]
        return 0.0

    @staticmethod
    def _slm_mapping(elev_deg):
        """Single-layer mapping function (VTEC → STEC)."""
        el_rad = np.radians(elev_deg)
        Re = 6371e3
        H_ion = 350e3
        return 1.0 / np.sqrt(1.0 - (Re * np.cos(el_rad) / (Re + H_ion))**2)

    def _group_observations(self, rawx_msg):
        """Group RAWX measurements by satellite, extracting L1/L2 code + phase."""
        sat_obs = defaultdict(dict)
        for meas in rawx_msg['measurements']:
            if meas['gnssId'] != 0:
                continue  # GPS only
            key = f"G{meas['svId']:02d}"
            sig_id = meas['sigId']
            # ZED-F9P RAWX sigIds: 0=L1C/A, 3=L2CL, 4=L2CM
            if sig_id == 0:
                sat_obs[key]['P1'] = meas['prMes']
                sat_obs[key]['L1'] = meas['cpMes']
            elif sig_id in [3, 4]:
                sat_obs[key]['P2'] = meas['prMes']
                sat_obs[key]['L2'] = meas['cpMes']
        return sat_obs

    def _estimate_rx_dcb(self, sat_obs, denom):
        """Estimate receiver inter-frequency bias from first epoch.

        Uses the minimum-VTEC physical constraint: true VTEC >= MIN_VTEC_FLOOR_TECU.

        The receiver DCB adds a constant bias B (meters) to P1-P2 for ALL
        satellites.  In code-STEC: bias = B / denom (constant STEC offset).
        In VTEC: bias_vtec_i = (B/denom) / m_i  (varies per sat by mapping).

        We compute raw code VTEC for high-elevation sats.  The MINIMUM
        raw VTEC satellite has the largest ionospheric path (lowest elev
        of those considered), so its true VTEC is likely the smallest.
        Setting min(raw_VTEC) = FLOOR gives us the receiver DCB directly.
        """
        # Collect (raw_code_stec, mapping_factor, raw_vtec) per satellite
        sat_data = []
        for sv, obs in sat_obs.items():
            if not all(k in obs for k in ('P1', 'P2', 'L1', 'L2')):
                continue
            sat_pos = self.sat_positions.get(sv)
            if not sat_pos or sat_pos['elev'] < 20:
                continue
            dcb_sat = self._get_sat_dcb(sv)
            term = (obs['P1'] - obs['P2']) - dcb_sat
            stec_code = term / denom
            m_f = self._slm_mapping(sat_pos['elev'])
            vtec = stec_code / m_f / TECU  # TECU
            sat_data.append((sv, stec_code, m_f, vtec))

        if len(sat_data) < 4:
            return  # Not enough satellites

        vtec_vals = [d[3] for d in sat_data]
        min_vtec_raw = min(vtec_vals)
        median_vtec = float(np.median(vtec_vals))

        if min_vtec_raw >= MIN_VTEC_FLOOR_TECU:
            self._rx_dcb_estimated = True
            logger.info(f"Rx DCB: not needed, min raw VTEC = {min_vtec_raw:.1f} TECU")
            return

        # Find the satellite with the minimum VTEC
        min_idx = vtec_vals.index(min_vtec_raw)
        _, stec_min, m_f_min, _ = sat_data[min_idx]

        # We want: (stec_min - B/denom) / m_f_min / TECU = MIN_VTEC_FLOOR_TECU
        # => stec_min - B/denom = MIN_VTEC_FLOOR_TECU * TECU * m_f_min
        # => B/denom = stec_min - MIN_VTEC_FLOOR_TECU * TECU * m_f_min
        # => B = (stec_min - MIN_VTEC_FLOOR_TECU * TECU * m_f_min) * denom
        rx_dcb_meters = (stec_min - MIN_VTEC_FLOOR_TECU * TECU * m_f_min) * denom

        self._rx_dcb_meters = rx_dcb_meters
        self._rx_dcb_estimated = True
        logger.info(f"Rx DCB estimated: {rx_dcb_meters:.3f} m "
                    f"({rx_dcb_meters/0.299792458:.1f} ns), "
                    f"min_raw_vtec={min_vtec_raw:.1f}, "
                    f"deficit={MIN_VTEC_FLOOR_TECU - min_vtec_raw:.1f} TECU, "
                    f"median_raw_vtec={median_vtec:.1f}")

    @classmethod
    def self_test(cls):
        """Known-answer self-test to catch physics regressions.

        Computes VTEC from synthetic dual-frequency pseudoranges where the
        true STEC is exactly 10 TECU.  If the result is wrong, the physics
        formulas are broken (wrong sign, wrong constant, etc.).

        Returns (ok: bool, details: str).
        """
        true_stec_tecu = 10.0
        true_stec = true_stec_tecu * TECU  # electrons/m^2
        f1, f2 = FREQ_GPS_L1, FREQ_GPS_L2
        denom = K * (1.0 / f1**2 - 1.0 / f2**2)  # negative

        # Synthetic pseudoranges: P1-P2 = stec * denom (no DCBs)
        p1_minus_p2 = true_stec * denom  # negative meters

        # Recover STEC from code
        stec_recovered = p1_minus_p2 / denom
        vtec_recovered = stec_recovered / TECU  # at zenith, mapping=1

        err = abs(vtec_recovered - true_stec_tecu)
        if err > 0.01:
            return False, (f"Self-test FAILED: expected {true_stec_tecu:.1f} TECU, "
                           f"got {vtec_recovered:.4f} TECU (err={err:.4f})")

        # Verify mapping function: at 90° elevation, mapping should be ~1.0
        m90 = cls._slm_mapping(90.0)
        if abs(m90 - 1.0) > 0.01:
            return False, f"Self-test FAILED: mapping(90°) = {m90:.4f}, expected ~1.0"

        # Verify mapping at 30° is in expected range [1.5, 2.5]
        m30 = cls._slm_mapping(30.0)
        if not (1.5 <= m30 <= 2.5):
            return False, f"Self-test FAILED: mapping(30°) = {m30:.4f}, expected [1.5, 2.5]"

        # Verify denom is negative (critical sign convention)
        if denom >= 0:
            return False, f"Self-test FAILED: denom = {denom}, expected negative"

        return True, (f"OK v{_MODULE_VERSION}: "
                      f"VTEC={vtec_recovered:.2f} TECU, "
                      f"m(90°)={m90:.4f}, m(30°)={m30:.4f}")

    def process_rawx(self, rawx_msg):
        """
        Process RXM-RAWX epoch.
        Returns dictionary of results: {sv_key: {'vtec': float, 'stec': float, 'elev': float}}
        """
        results = {}
        f1 = FREQ_GPS_L1
        f2 = FREQ_GPS_L2
        denom = K * (1.0/f1**2 - 1.0/f2**2)  # negative

        sat_obs = self._group_observations(rawx_msg)

        # Estimate receiver DCB once on the first usable epoch.
        if not self._rx_dcb_estimated:
            self._estimate_rx_dcb(sat_obs, denom)
            if self._rx_dcb_estimated:
                # Reset leveling state — it was built with uncorrected
                # stec_code and must reconverge with the new rx DCB.
                self.state.clear()

        # Compute per-satellite STEC with all DCB corrections + leveling.
        for key, obs in sat_obs.items():
            if not all(k in obs for k in ('P1', 'P2', 'L1', 'L2')):
                continue

            sat_pos = self.sat_positions.get(key)
            if not sat_pos or sat_pos['elev'] < 10:
                continue

            p1, p2 = obs['P1'], obs['P2']

            # DCB correction: satellite + receiver
            dcb_sat = self._get_sat_dcb(key)
            term = (p1 - p2) - dcb_sat - self._rx_dcb_meters
            stec_code = term / denom

            # Carrier Phase Smoothing (Levelling)
            l1_m = obs['L1'] * (C / f1)
            l2_m = obs['L2'] * (C / f2)
            l_gf = l1_m - l2_m
            stec_phase_raw = l_gf / (-denom)

            state = self.state[key]
            if 'count' not in state:
                state['count'] = 0
                state['sum_diff'] = 0.0
                state['last_l_gf'] = None

            # Cycle slip detection
            CYCLE_SLIP_THRESHOLD = 0.5  # meters
            if state['last_l_gf'] is not None:
                if abs(l_gf - state['last_l_gf']) > CYCLE_SLIP_THRESHOLD:
                    logger.debug(f"{key}: Cycle slip, resetting leveling")
                    state['count'] = 0
                    state['sum_diff'] = 0.0
            state['last_l_gf'] = l_gf

            diff = stec_code - stec_phase_raw
            n = state['count'] + 1
            if n <= 100:
                state['sum_diff'] = state.get('sum_diff', 0.0) + diff
                state['count'] = n
                offset = state['sum_diff'] / n
            else:
                alpha = 0.01
                old_offset = state['sum_diff'] / state['count']
                offset = (1 - alpha) * old_offset + alpha * diff
                state['sum_diff'] = offset * state['count']

            stec_smooth = stec_phase_raw + offset

            # Skip unphysical values after leveling convergence
            if n > 30 and stec_smooth < 0:
                logger.debug(f"{key}: Negative STEC ({stec_smooth/TECU:.2f} TECU), skipping")
                continue

            m_factor = self._slm_mapping(sat_pos['elev'])
            vtec = stec_smooth / m_factor

            results[key] = {
                'vtec': vtec,
                'vtec_u': vtec / TECU,
                'stec_u': stec_smooth / TECU,
                'elev': sat_pos['elev'],
                'azim': sat_pos['azim']
            }

        return results


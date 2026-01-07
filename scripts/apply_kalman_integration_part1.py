#!/usr/bin/env python3
"""
Apply Kalman filter integration to phase2_analytics_service.py

This script adds the remaining Kalman filter update logic to the _write_clock_offset method.
"""

import re
from pathlib import Path

# Target file
target_file = Path("/home/mjh/git/hf-timestd/src/hf_timestd/core/phase2_analytics_service.py")

# Read the file
with open(target_file, 'r') as f:
    content = f.read()

# The code to insert after propagation delay extraction
kalman_update_code = '''
                    # ================================================================
                    # Per-Broadcast Kalman Filter Update (Science-First v5.0)
                    # ================================================================
                    # Update Kalman filter for this specific broadcast
                    # This tracks ionospheric path dynamics [ToF, Doppler]
                    
                    tof_kalman_ms = None
                    tof_uncertainty_ms = None
                    doppler_ms_per_min = None
                    gpsdo_consistent = None
                    
                    if tone_detected_flag and not math.isnan(raw_arr):
                        # Get filter for this broadcast
                        broadcast_id = f"{station}_{int(frequency_mhz)}"
                        
                        if broadcast_id in self.broadcast_filters:
                            filter = self.broadcast_filters[broadcast_id]
                            
                            # Compute ToF from raw arrival time
                            tof_measurement = raw_arr
                            
                            # Get SNR for dynamic measurement noise
                            snr = snr_db if snr_db > 0 else 10.0
                            
                            # Check GPSDO temporal continuity
                            is_consistent, residual = filter.check_gpsdo_continuity(tof_measurement)
                            gpsdo_consistent = is_consistent
                            
                            if not is_consistent:
                                logger.info(
                                    f"GPSDO continuity check: {broadcast_id} residual = {residual:.3f} ms "
                                    f"(propagation change or anomaly)"
                                )
                            
                            # Update Kalman filter
                            tof_kalman_ms, tof_uncertainty_ms = filter.update(
                                measurement_ms=tof_measurement,
                                snr_db=snr
                            )
                            
                            # Get Doppler (rate of change)
                            state = filter.get_state()
                            doppler_ms_per_min = state['doppler_ms_per_min']
                            
                            logger.debug(
                                f"Kalman update {broadcast_id}: "
                                f"ToF={tof_kalman_ms:.3f}±{tof_uncertainty_ms:.3f} ms, "
                                f"Doppler={doppler_ms_per_min:.4f} ms/min"
                            )
                            
                            # Save state periodically (every 10 minutes)
                            if self.minutes_processed % 10 == 0:
                                filter.save_state(self.kalman_state_dir)
                        else:
                            logger.warning(f"No Kalman filter for broadcast {broadcast_id}")
                    else:
                        # No tone detected - predict only (coast)
                        broadcast_id = f"{station}_{int(frequency_mhz)}"
                        
                        if broadcast_id in self.broadcast_filters:
                            filter = self.broadcast_filters[broadcast_id]
                            
                            # Predict (coast during fading)
                            tof_kalman_ms, tof_uncertainty_ms = filter.predict()
                            
                            state = filter.get_state()
                            doppler_ms_per_min = state['doppler_ms_per_min']
                            gpsdo_consistent = False  # No measurement to check
                            
                            logger.debug(
                                f"Kalman predict {broadcast_id}: "
                                f"ToF={tof_kalman_ms:.3f}±{tof_uncertainty_ms:.3f} ms (coasting)"
                            )
                    
'''

# Find the insertion point (after propagation delay extraction)
pattern = r'(                        f"No propagation delay for \{station\} at \{frequency_mhz:.2f\} MHz - "\n                            f"setting propagation_delay_ms=NaN"\n                        \)\n                    )'

# Insert the Kalman update code
new_content = re.sub(pattern, r'\1' + kalman_update_code, content)

if new_content == content:
    print("ERROR: Pattern not found - file may have changed")
    exit(1)

# Write back
with open(target_file, 'w') as f:
    f.write(new_content)

print(f"✅ Successfully added Kalman filter update logic to {target_file}")
print("Next: Add Kalman fields to L2TimingMeasurement instantiation")

#!/usr/bin/env python3
"""
Radiod Health Monitor
Continuously monitors radiod status and writes health status for web UI
"""

import subprocess
import time
import json
from pathlib import Path
from datetime import datetime, timezone
import sys
import logging

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def check_radiod_process():
    """Check if radiod process is running using pgrep"""
    try:
        result = subprocess.run(
            ['pgrep', '-x', 'radiod'],
            capture_output=True,
            text=True,
            timeout=2
        )
        
        if result.returncode == 0:
            pids = result.stdout.strip().split('\n')
            return {
                'running': True,
                'pid': int(pids[0]) if pids else None,
                'count': len(pids)
            }
        else:
            return {
                'running': False,
                'pid': None,
                'count': 0
            }
    except Exception as e:
        logger.error(f"Error checking radiod process: {e}")
        return {
            'running': False,
            'pid': None,
            'count': 0,
            'error': str(e)
        }


def get_radiod_uptime(pid):
    """Get process uptime from /proc"""
    try:
        with open(f'/proc/{pid}/stat', 'r') as f:
            stat = f.read().split()
            # Field 22 is start time in jiffies since boot
            start_jiffies = int(stat[21])
            
        with open('/proc/uptime', 'r') as f:
            system_uptime = float(f.read().split()[0])
            
        # Get clock ticks per second
        clock_ticks = 100  # Standard Linux value
        
        # Calculate process uptime
        with open('/proc/stat', 'r') as f:
            for line in f:
                if line.startswith('btime'):
                    boot_time = int(line.split()[1])
                    break
        
        start_time = boot_time + (start_jiffies / clock_ticks)
        uptime = time.time() - start_time
        
        return max(0, int(uptime))
        
    except Exception as e:
        logger.warning(f"Could not get uptime for PID {pid}: {e}")
        return None


def check_radiod_connectivity():
    """Check if radiod is responsive (listening on multicast)"""
    try:
        # Check if radiod is listening on its typical ports
        result = subprocess.run(
            ['ss', '-lun'],
            capture_output=True,
            text=True,
            timeout=2
        )
        
        # Look for multicast addresses (239.*)
        has_multicast = '239.' in result.stdout
        
        return has_multicast
        
    except Exception as e:
        logger.warning(f"Could not check radiod connectivity: {e}")
        return None


def check_rtp_timestamp_advancing(state_file: Path) -> dict:
    """Check if RTP timestamps are advancing (not frozen).
    
    This detects the failure mode where radiod is running but its
    RTP clock is frozen/stuck, causing downstream data loss.
    
    Returns:
        dict with 'advancing', 'last_rtp', 'current_rtp', 'drift_seconds'
    """
    try:
        # Read last known RTP timestamp from state
        rtp_state_file = state_file.parent / 'radiod-rtp-state.json'
        
        last_check = None
        if rtp_state_file.exists():
            with open(rtp_state_file, 'r') as f:
                last_check = json.load(f)
        
        # Get current RTP timestamp from core-recorder status
        recorder_status_file = state_file.parent.parent / 'status' / 'core-recorder-status.json'
        if not recorder_status_file.exists():
            return {'advancing': None, 'error': 'No recorder status file'}
        
        with open(recorder_status_file, 'r') as f:
            recorder_status = json.load(f)
        
        # Extract RTP info from any channel
        current_rtp = None
        for ch_key, ch_data in recorder_status.get('channels', {}).items():
            if 'last_rtp_timestamp' in ch_data:
                current_rtp = ch_data['last_rtp_timestamp']
                break
        
        if current_rtp is None:
            # Try to get from the raw buffer metadata files
            hot_buffer = Path('/dev/shm/timestd/raw_buffer')
            if hot_buffer.exists():
                # Find most recent JSON sidecar
                today = datetime.now(timezone.utc).strftime('%Y%m%d')
                for channel_dir in hot_buffer.iterdir():
                    if not channel_dir.is_dir():
                        continue
                    today_dir = channel_dir / today
                    if not today_dir.exists():
                        continue
                    json_files = sorted(today_dir.glob('*.json'), key=lambda f: f.stat().st_mtime, reverse=True)
                    if json_files:
                        with open(json_files[0], 'r') as f:
                            meta = json.load(f)
                            current_rtp = meta.get('start_rtp_timestamp')
                            break
        
        now = time.time()
        result = {
            'advancing': None,
            'current_rtp': current_rtp,
            'check_time': now
        }
        
        if current_rtp is None:
            result['error'] = 'Could not get current RTP timestamp'
            return result
        
        # Compare with last check
        if last_check and 'current_rtp' in last_check:
            last_rtp = last_check['current_rtp']
            last_time = last_check.get('check_time', now - 60)
            time_delta = now - last_time
            rtp_delta = current_rtp - last_rtp
            
            # RTP should advance roughly 1 per sample (20kHz = 20000/sec)
            # Over 60 seconds, expect ~1.2M RTP ticks
            expected_rtp_delta = time_delta * 20000  # Approximate
            
            result['last_rtp'] = last_rtp
            result['rtp_delta'] = rtp_delta
            result['time_delta'] = time_delta
            
            # If RTP hasn't advanced at all, or advanced way too little, it's frozen
            if rtp_delta <= 0:
                result['advancing'] = False
                result['drift_seconds'] = time_delta  # Frozen for this long
                logger.error(f"RTP CLOCK FROZEN: RTP={current_rtp}, last={last_rtp}, no advancement in {time_delta:.0f}s")
            elif rtp_delta < expected_rtp_delta * 0.5:
                result['advancing'] = False
                result['drift_seconds'] = time_delta - (rtp_delta / 20000)
                logger.warning(f"RTP CLOCK SLOW: Expected ~{expected_rtp_delta:.0f} ticks, got {rtp_delta}")
            else:
                result['advancing'] = True
                result['drift_seconds'] = 0
        
        # Save current state for next check
        with open(rtp_state_file, 'w') as f:
            json.dump(result, f)
        
        return result
        
    except Exception as e:
        logger.warning(f"Could not check RTP timestamp: {e}")
        return {'advancing': None, 'error': str(e)}


def write_status(status, output_file):
    """Write status to JSON file atomically"""
    try:
        output_path = Path(output_file)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        
        # Write to temp file first
        temp_file = output_path.with_suffix('.tmp')
        with open(temp_file, 'w') as f:
            json.dump(status, f, indent=2)
        
        # Atomic rename
        temp_file.rename(output_path)
        
    except Exception as e:
        logger.error(f"Error writing status: {e}")


def main():
    # Configuration
    output_file = sys.argv[1] if len(sys.argv) > 1 else '/tmp/timestd-test/state/radiod-status.json'
    poll_interval = int(sys.argv[2]) if len(sys.argv) > 2 else 10  # seconds
    
    logger.info(f"Starting radiod health monitor")
    logger.info(f"Output: {output_file}")
    logger.info(f"Poll interval: {poll_interval}s")
    
    consecutive_failures = 0
    last_known_good = None
    
    while True:
        try:
            # Check process status
            proc_status = check_radiod_process()
            
            status = {
                'timestamp': datetime.now(timezone.utc).isoformat(),
                'process': proc_status,
                'uptime_seconds': None,
                'connectivity': None,
                'health': 'unknown',
                'alerts': []
            }
            
            if proc_status['running']:
                consecutive_failures = 0
                last_known_good = time.time()
                
                # Get uptime
                if proc_status['pid']:
                    status['uptime_seconds'] = get_radiod_uptime(proc_status['pid'])
                
                # Check connectivity
                status['connectivity'] = check_radiod_connectivity()
                
                # Check RTP timestamp advancement (detect frozen clocks)
                rtp_check = check_rtp_timestamp_advancing(Path(output_file))
                status['rtp_status'] = rtp_check
                
                # Determine health
                if status['connectivity'] is True:
                    # Check for frozen RTP clock
                    if rtp_check.get('advancing') is False:
                        status['health'] = 'critical'
                        drift = rtp_check.get('drift_seconds', 0)
                        status['alerts'].append({
                            'severity': 'critical',
                            'message': f'RTP clock frozen/drifted by {drift:.0f}s - data loss occurring!'
                        })
                    else:
                        status['health'] = 'healthy'
                elif status['connectivity'] is False:
                    status['health'] = 'degraded'
                    status['alerts'].append({
                        'severity': 'warning',
                        'message': 'Radiod running but no multicast detected'
                    })
                else:
                    status['health'] = 'unknown'
                    
            else:
                consecutive_failures += 1
                status['health'] = 'critical'
                status['alerts'].append({
                    'severity': 'critical',
                    'message': f'Radiod process not found (failed {consecutive_failures} checks)'
                })
                
                if last_known_good:
                    downtime = int(time.time() - last_known_good)
                    status['alerts'].append({
                        'severity': 'critical',
                        'message': f'Radiod down for {downtime} seconds'
                    })
            
            # Write status
            write_status(status, output_file)
            
            # Log status changes
            if proc_status['running']:
                logger.info(f"✓ radiod healthy (PID {proc_status['pid']}, uptime {status['uptime_seconds']}s)")
            else:
                logger.error(f"✗ radiod NOT RUNNING (consecutive failures: {consecutive_failures})")
            
            time.sleep(poll_interval)
            
        except KeyboardInterrupt:
            logger.info("Shutting down monitor")
            break
        except Exception as e:
            logger.error(f"Monitor error: {e}", exc_info=True)
            time.sleep(poll_interval)


if __name__ == '__main__':
    main()

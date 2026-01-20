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
    """Check if radiod process is running using pgrep (local or containerized)"""
    try:
        # First try local process
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
                'count': len(pids),
                'containerized': False
            }
        
        # Check for containerized radiod (docker)
        try:
            docker_result = subprocess.run(
                ['docker', 'ps', '--filter', 'name=radiod', '--format', '{{.ID}}'],
                capture_output=True,
                text=True,
                timeout=5
            )
            if docker_result.returncode == 0 and docker_result.stdout.strip():
                container_id = docker_result.stdout.strip().split('\n')[0]
                return {
                    'running': True,
                    'pid': 1,  # Container PID namespace
                    'count': 1,
                    'containerized': True,
                    'container_id': container_id[:12]
                }
        except FileNotFoundError:
            pass  # Docker not installed
        except Exception:
            pass  # Docker check failed, continue
        
        return {
            'running': False,
            'pid': None,
            'count': 0,
            'containerized': False
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


def check_radiod_status_channel(status_address: str = None, timeout: float = 2.0):
    """Check if radiod status channel is responding"""
    try:
        from ka9q import RadiodControl
        
        # Try common status addresses if none specified
        addresses_to_try = []
        if status_address:
            addresses_to_try.append(status_address)
        else:
            # Try to read from config or use common defaults
            addresses_to_try = ['bee4-status.local', 'bee1-status.local', 'radiod-status.local']
        
        for addr in addresses_to_try:
            try:
                ctrl = RadiodControl(addr)
                # If we can create the control object and it has a valid status address, radiod is responding
                if hasattr(ctrl, 'status_mcast_addr') and ctrl.status_mcast_addr:
                    return {
                        'responding': True,
                        'status_address': addr,
                        'multicast_addr': ctrl.status_mcast_addr
                    }
            except Exception:
                continue
        
        return {'responding': False, 'status_address': None}
    except ImportError:
        logger.warning("ka9q module not available for status check")
        return {'responding': None, 'status_address': None}
    except Exception as e:
        logger.error(f"Error checking radiod status channel: {e}")
        return {'responding': False, 'error': str(e)}


def check_radiod_connectivity():
    """Check if radiod is responsive (listening on multicast) - legacy method"""
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


def check_data_flow(state_file: Path) -> dict:
    """Check if data is flowing by verifying raw buffer files are being written.
    
    This is more reliable than checking RTP timestamps directly, as it verifies
    the end-to-end data flow from radiod through core-recorder to disk.
    
    Returns:
        dict with 'flowing', 'latest_file_age', 'error'
    """
    try:
        # Check hot buffer for recent files
        hot_buffer = Path('/dev/shm/timestd/raw_buffer')
        cold_buffer = Path('/var/lib/timestd/raw_buffer')
        
        search_path = hot_buffer if hot_buffer.exists() else cold_buffer
        if not search_path.exists():
            return {'flowing': None, 'error': 'No raw buffer directory'}
        
        # Find most recent .bin file
        from datetime import timedelta
        now = datetime.now(timezone.utc)
        dates_to_check = [
            now.strftime('%Y%m%d'),
            (now - timedelta(days=1)).strftime('%Y%m%d')
        ]
        
        latest_mtime = 0
        latest_file = None
        
        for channel_dir in search_path.iterdir():
            if not channel_dir.is_dir():
                continue
            for date_str in dates_to_check:
                day_dir = channel_dir / date_str
                if not day_dir.exists():
                    continue
                for f in day_dir.glob('*.bin*'):
                    try:
                        mtime = f.stat().st_mtime
                        if mtime > latest_mtime:
                            latest_mtime = mtime
                            latest_file = f
                    except (OSError, IOError):
                        continue
        
        if latest_file is None:
            return {'flowing': False, 'error': 'No raw buffer files found'}
        
        file_age = time.time() - latest_mtime
        
        result = {
            'flowing': file_age < 120,  # Data flowing if file < 2 min old
            'latest_file': str(latest_file.name),
            'latest_file_age': file_age
        }
        
        if file_age > 300:  # 5 minutes
            result['flowing'] = False
            logger.error(f"DATA FLOW STOPPED: Latest file {latest_file.name} is {file_age:.0f}s old")
        elif file_age > 120:  # 2 minutes
            logger.warning(f"DATA FLOW SLOW: Latest file {latest_file.name} is {file_age:.0f}s old")
        
        return result
        
    except Exception as e:
        logger.warning(f"Could not check data flow: {e}")
        return {'flowing': None, 'error': str(e)}


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
            status = {
                'timestamp': datetime.now(timezone.utc).isoformat(),
                'process': {'running': False, 'pid': None, 'count': 0},
                'uptime_seconds': None,
                'connectivity': None,
                'health': 'unknown',
                'alerts': []
            }
            
            # Primary check: Is radiod status channel responding?
            status_check = check_radiod_status_channel()
            status['status_channel'] = status_check
            
            if status_check.get('responding') is True:
                # Radiod is responding - this is the authoritative health check
                consecutive_failures = 0
                last_known_good = time.time()
                status['health'] = 'healthy'
                status['process']['running'] = True
                status['process']['pid'] = 1  # Unknown PID (may be remote/container)
                status['connectivity'] = True
                
                # Check data flow for additional diagnostics
                data_flow = check_data_flow(Path(output_file))
                status['data_flow'] = data_flow
                
                if data_flow.get('flowing') is False:
                    # Status channel works but no data - degraded
                    status['health'] = 'degraded'
                    age = data_flow.get('latest_file_age', 0)
                    status['alerts'].append({
                        'severity': 'warning',
                        'message': f'Radiod responding but no recent data (last file {age:.0f}s ago)'
                    })
                    
            else:
                # Status channel not responding - check local process as fallback
                proc_status = check_radiod_process()
                status['process'] = proc_status
                
                if proc_status['running']:
                    # Local process running but status channel not responding
                    status['health'] = 'degraded'
                    status['alerts'].append({
                        'severity': 'warning',
                        'message': 'Radiod process running but status channel not responding'
                    })
                    if proc_status['pid']:
                        status['uptime_seconds'] = get_radiod_uptime(proc_status['pid'])
                else:
                    # No status channel, no local process - critical
                    consecutive_failures += 1
                    status['health'] = 'critical'
                    status['alerts'].append({
                        'severity': 'critical',
                        'message': f'Radiod not responding (failed {consecutive_failures} checks)'
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
            if status['health'] == 'healthy':
                sc = status.get('status_channel', {})
                logger.info(f"✓ radiod healthy (status: {sc.get('status_address', 'unknown')}, mcast: {sc.get('multicast_addr', 'unknown')})")
            elif status['health'] == 'critical':
                logger.error(f"✗ radiod CRITICAL (consecutive failures: {consecutive_failures})")
            else:
                logger.warning(f"⚠ radiod {status['health']}")
            
            time.sleep(poll_interval)
            
        except KeyboardInterrupt:
            logger.info("Shutting down monitor")
            break
        except Exception as e:
            logger.error(f"Monitor error: {e}", exc_info=True)
            time.sleep(poll_interval)


if __name__ == '__main__':
    main()

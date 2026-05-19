#!/usr/bin/env python3
"""Real-time GNSS VTEC monitor for ZED-F9P with dual-clock timing metadata.

This script consumes UBX messages (NAV-SAT + RXM-RAWX) from a ZED-F9P (typically
via a TCP stream) and produces a station-local VTEC time series.

Timing model
------------

This pipeline carries two distinct clocks:

- **GNSS observation time**: `(week, rcvTow)` from `RXM-RAWX`, where `rcvTow` is
  a fractional (sub-second) receiver time-of-week captured by the GNSS
  measurement engine at the observation epoch. GNSS-derived observables should
  be indexed by this time base for defensible rate/variability metrics.

- **System receipt time**: `unix_timestamp = time.time()` when the message is
  received/processed on the host. This time base may include OS/network jitter
  and is recorded for cross-stream alignment (e.g., to HF/RTP metadata).

To support defensible cross-correlation between GNSS and HF products, the
outputs include an explicit estimate of the system-vs-GNSS offset and a running
mean/std of that offset.

CSV output
----------

When CSV saving is enabled, each VTEC line is written as:

`unix_timestamp,gnss_week,gnss_rcvTow_s,vtec_tecu,n_satellites,unix_minus_gnss_s,unix_minus_gnss_mean_s,unix_minus_gnss_std_s`

HDF5 output
-----------

When HDF5 saving is enabled, each appended record includes the same fields in
addition to standard metadata (quality flag, elevation mask, DCB corrected).
"""

import socket
import logging
import time
import sys
import argparse
from datetime import datetime
from collections import deque
import math

# Systemd watchdog support
try:
    from systemd import daemon as systemd_daemon
    SYSTEMD_AVAILABLE = True
except ImportError:
    SYSTEMD_AVAILABLE = False

from hf_timestd.core.ubx_parser import UBXParser
from hf_timestd.core.gnss_tec import GNSSTECAnalyzer
from hf_timestd.cddis import CDDISDownloader
from hf_timestd.io import make_data_product_writer

logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s %(levelname)s: %(message)s'
)
logger = logging.getLogger("live_vtec")

import tomllib
import os

def load_config(config_path="config/timestd-config.toml"):
    if not os.path.exists(config_path):
        return {}
    try:
        with open(config_path, "rb") as f:
            return tomllib.load(f)
    except Exception as e:
        logger.error(f"Failed to load config: {e}")
        return {}

def main():
    parser = argparse.ArgumentParser(description="Real-time VTEC from ZED-F9P")
    parser.add_argument("--host", help="ZED-F9P IP (overrides config)")
    parser.add_argument("--port", type=int, help="ZED-F9P TCP Port (overrides config)")
    parser.add_argument("--download-only", action="store_true", help="Download DCBs and exit")
    parser.add_argument("--config", default="config/timestd-config.toml", help="Path to config file")
    args = parser.parse_args()

    # Load Config
    config = load_config(args.config)
    gnss_cfg = config.get("gnss_vtec", {})
    
    # Determine settings (CLI > Config > Default)
    host = args.host if args.host else gnss_cfg.get("host", "192.168.0.202")
    port = args.port if args.port else gnss_cfg.get("port", 2001)
    enabled = gnss_cfg.get("enabled", False)
    
    # If specific args are provided, assume user wants to run regardless of config 'enabled'
    # But if run as a service with no args, respect 'enabled'.
    if not (args.host or args.port or args.download_only):
        if not enabled:
            # Stay alive in idle so Type=notify is satisfied — exiting here
            # makes systemd flag Result=protocol (READY=1 never sent) and
            # Restart=always turns the disabled state into a tight respawn
            # loop.  Notify ready, then sleep; the watchdog kick keeps
            # systemd happy if WatchdogSec is set.
            logger.info("GNSS VTEC monitoring is disabled in config. Idle.")
            if SYSTEMD_AVAILABLE:
                systemd_daemon.notify('READY=1')
                systemd_daemon.notify('STATUS=GNSS VTEC disabled in config')
            while True:
                time.sleep(60)
                if SYSTEMD_AVAILABLE:
                    systemd_daemon.notify('WATCHDOG=1')

    # 1. Prepare DCB Data (only if GNSS VTEC is enabled)
    dcb_data = {}
    if enabled:
        logger.info("GNSS VTEC enabled - initializing CDDIS Downloader for DCB corrections...")
        downloader = CDDISDownloader()
        dcb_file = downloader.download_latest_rapid_dcb()
        
        if not dcb_file:
            logger.error("Failed to download DCB file. VTEC accuracy will be degraded (0 bias assumed).")
        else:
            logger.info(f"Parsing DCB file: {dcb_file}")
            dcb_data = downloader.parse_biases(dcb_file)
            logger.info(f"Loaded {len(dcb_data)} bias entries.")
    else:
        logger.info("GNSS VTEC disabled - skipping DCB download")


    if args.download_only:
        return

    # 2. Processing components (persist across reconnections)
    parser_ubx = UBXParser()
    analyzer = GNSSTECAnalyzer(dcb_data)

    # Startup self-test: catch physics regressions before entering main loop
    from hf_timestd.core.gnss_tec import _MODULE_VERSION
    ok, details = GNSSTECAnalyzer.self_test()
    if ok:
        logger.info(f"GNSSTECAnalyzer self-test: {details}")
    else:
        logger.error(f"GNSSTECAnalyzer self-test: {details}")
        logger.error("Aborting — VTEC physics self-test failed. "
                     "Check for stale site-packages or code regression.")
        return
    logger.info(f"gnss_tec module version: {_MODULE_VERSION} "
                f"(source: {GNSSTECAnalyzer.__module__})")

    # Notify systemd we're ready
    if SYSTEMD_AVAILABLE:
        systemd_daemon.notify('READY=1')
        logger.info("Systemd watchdog enabled")
    
    # Open CSV if enabled
    csv_file = None
    if gnss_cfg.get("save_csv", False):
        csv_path = gnss_cfg.get("csv_path", "data/gnss_vtec.csv")
        try:
             # Ensure dir exists
            os.makedirs(os.path.dirname(os.path.abspath(csv_path)), exist_ok=True)
            csv_file = open(csv_path, "a", buffering=1) # Line buffered
        except Exception as e:
            logger.error(f"Failed to open CSV {csv_path}: {e}")
    
    # Initialize HDF5 writer for GNSS VTEC
    hdf5_writer = None
    logger.info(f"Checking HDF5 configuration: save_hdf5={gnss_cfg.get('save_hdf5', True)}")
    if gnss_cfg.get("save_hdf5", True):  # Default to True
        logger.info("Attempting to initialize HDF5 writer...")
        try:
            from pathlib import Path
            from datetime import datetime, timezone
            
            hdf5_path = gnss_cfg.get("hdf5_path", "data/gnss_vtec")
            logger.info(f"HDF5 output path: {hdf5_path}")
            output_dir = Path(hdf5_path)
            output_dir.mkdir(parents=True, exist_ok=True)
            logger.info(f"Created HDF5 output directory: {output_dir}")
            
            hdf5_writer = make_data_product_writer(
                output_dir=output_dir,
                product_level='L3',
                product_name='gnss_vtec',
                channel='GNSS',
                processing_version='1.0.0',
                storage_config=config.get('storage', {}) or {},
            )
            logger.info(f"✓ HDF5 writer initialized successfully: {output_dir}")
        except ImportError as e:
            logger.error(f"Failed to import HDF5 writer: {e}")
        except Exception as e:
            logger.error(f"Failed to initialize HDF5 writer: {e}", exc_info=True)

    # Plausibility bounds for VTEC (TECU).  Nighttime minimum ~1, daytime
    # storm maximum ~300, but sustained values outside [0,150] indicate a
    # processing bug rather than real ionospheric conditions.
    VTEC_PLAUSIBLE_MIN = -1.0   # allow slight noise around zero
    VTEC_PLAUSIBLE_MAX = 150.0
    CONSECUTIVE_REJECT_LIMIT = 300  # ~5 min at 1 Hz

    bytes_received = 0
    msg_count = 0
    last_log_time = time.time()
    last_data_time = time.time()
    consecutive_rejects = 0
    hdf5_write_buffer = []
    BATCH_FLUSH_INTERVAL = 60
    last_hdf5_flush = time.time()
    reconnect_delay = 5  # seconds, grows with backoff
    MAX_RECONNECT_DELAY = 120

    gps_epoch_unix = 315964800.0
    offset_window = deque(maxlen=600)

    # ── Outer reconnection loop ──
    # On socket errors we reconnect instead of crashing.
    # The UBX parser and TEC analyzer persist across reconnections.
    try:
      while True:
        # 3. Connect to Stream
        sock = None
        try:
            logger.info(f"Connecting to {host}:{port}...")
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(60.0)
            sock.connect((host, port))
            logger.info("Connected!")
            reconnect_delay = 5  # reset backoff on success
            last_data_time = time.time()  # reset data watchdog on fresh connection
        except Exception as e:
            logger.warning(f"Connection failed: {e} — retrying in {reconnect_delay}s")
            if SYSTEMD_AVAILABLE:
                systemd_daemon.notify('WATCHDOG=1')
            time.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, MAX_RECONNECT_DELAY)
            continue

        # ── Inner data loop ──
        try:
          while True:
            try:
                data = sock.recv(4096)
            except socket.timeout:
                logger.warning("Socket timeout (60s no data) — will reconnect")
                break  # break inner loop → reconnect
            
            if not data:
                logger.warning("Socket closed by peer — will reconnect")
                break  # break inner loop → reconnect
            
            # Watchdog: Check if we've produced VTEC data recently
            # If no VTEC output for 5 minutes, something is wrong
            if time.time() - last_data_time > 300:
                logger.error("No VTEC data produced for 5 minutes — will reconnect")
                break  # break inner loop → reconnect
            
            bytes_received += len(data)
            
            # Log data reception rate every 10 seconds
            now = time.time()
            if now - last_log_time > 10:
                logger.info(f"Receiving data: {bytes_received} bytes, {msg_count} UBX messages processed")
                last_log_time = now
                bytes_received = 0
                msg_count = 0
                
                # Notify systemd watchdog
                if SYSTEMD_AVAILABLE:
                    systemd_daemon.notify('WATCHDOG=1')
            
            # Flush HDF5 buffer periodically (batch write reduces file bloat)
            if hdf5_write_buffer and hdf5_writer and now - last_hdf5_flush >= BATCH_FLUSH_INTERVAL:
                try:
                    hdf5_writer.write_measurements_batch(hdf5_write_buffer)
                    last_data_time = now
                    logger.debug(f"Flushed {len(hdf5_write_buffer)} VTEC measurements to HDF5")
                except Exception as e:
                    logger.error(f"Failed to flush HDF5 batch: {e}")
                hdf5_write_buffer = []
                last_hdf5_flush = now
                
            for msg_class, msg_id, payload in parser_ubx.process_data(data):
                msg_count += 1
                timestamp = time.time()
                
                logger.debug(f"UBX message: class=0x{msg_class:02x} id=0x{msg_id:02x} len={len(payload)}")
                
                if msg_class == 0x01 and msg_id == 0x35: # NAV-SAT
                    logger.debug(f"Processing NAV-SAT message ({len(payload)} bytes)")
                    analyzer.update_satellite_positions(payload, timestamp)
                    
                elif msg_class == 0x02 and msg_id == 0x15: # RXM-RAWX
                    logger.debug(f"Processing RXM-RAWX message ({len(payload)} bytes)")
                    results = analyzer.process_rawx(payload)

                    week = payload.get('week')
                    rcvTow = payload.get('rcvTow')
                    leapS = payload.get('leapS')
                    recStat = payload.get('recStat')
                    offset_s = None
                    offset_mean_s = None
                    offset_std_s = None
                    if week is not None and rcvTow is not None and leapS is not None:
                        gnss_cont_s = float(week) * 604800.0 + float(rcvTow)
                        unix_from_gnss_s = gps_epoch_unix + gnss_cont_s - float(leapS)
                        offset_s = float(timestamp) - unix_from_gnss_s
                        offset_window.append(offset_s)
                        if len(offset_window) >= 2:
                            offset_mean_s = sum(offset_window) / len(offset_window)
                            var = sum((x - offset_mean_s) ** 2 for x in offset_window) / (len(offset_window) - 1)
                            offset_std_s = math.sqrt(var)
                    
                    if results:
                        logger.debug(f"RAWX processed: {len(results)} satellites with VTEC")
                        valid_vtecs = [r['vtec_u'] for r in results.values() if r['elev'] > 20]
                        if valid_vtecs:
                            avg_vtec = sum(valid_vtecs) / len(valid_vtecs)
                            logger.info(f"VTEC: {avg_vtec:.2f} TECU (Sats: {len(valid_vtecs)})")

                            # ── Plausibility gate ──
                            if not (VTEC_PLAUSIBLE_MIN <= avg_vtec <= VTEC_PLAUSIBLE_MAX):
                                consecutive_rejects += 1
                                if consecutive_rejects == 1 or consecutive_rejects % 60 == 0:
                                    logger.warning(
                                        f"VTEC plausibility rejection #{consecutive_rejects}: "
                                        f"{avg_vtec:.2f} TECU outside [{VTEC_PLAUSIBLE_MIN}, {VTEC_PLAUSIBLE_MAX}]")
                                if consecutive_rejects >= CONSECUTIVE_REJECT_LIMIT:
                                    logger.error(
                                        f"{consecutive_rejects} consecutive implausible VTEC values "
                                        f"— exiting for restart (possible processing bug)")
                                    break
                                continue  # skip CSV + HDF5 write
                            consecutive_rejects = 0
                            
                            # Write to CSV
                            line = f"{timestamp},{week},{rcvTow},{avg_vtec:.2f},{len(valid_vtecs)},{offset_s},{offset_mean_s},{offset_std_s}\n"
                            print(line, end='')
                            if csv_file:
                                csv_file.write(line)
                            
                            # Update data watchdog on every valid VTEC
                            last_data_time = time.time()

                            # Buffer for batch HDF5 write
                            if hdf5_writer:
                                from datetime import datetime as dt, timezone as tz
                                measurement = {
                                    'timestamp_utc': dt.fromtimestamp(timestamp, tz.utc).isoformat().replace('+00:00', 'Z'),
                                    'unix_timestamp': timestamp,
                                    'gnss_week': week,
                                    'gnss_rcvTow_s': rcvTow,
                                    'gnss_leapS': leapS,
                                    'gnss_recStat': recStat,
                                    'unix_minus_gnss_s': offset_s,
                                    'unix_minus_gnss_mean_s': offset_mean_s,
                                    'unix_minus_gnss_std_s': offset_std_s,
                                    'vtec_tecu': avg_vtec,
                                    'n_satellites': len(valid_vtecs),
                                    'quality_flag': 'GOOD' if len(valid_vtecs) >= 6 else 'MARGINAL' if len(valid_vtecs) >= 4 else 'BAD',
                                    'processing_version': '1.0.0',
                                    'min_elevation_deg': 20.0,
                                    'dcb_corrected': bool(dcb_data)
                                }
                                hdf5_write_buffer.append(measurement)
                        else:
                            logger.debug(f"No valid VTEC solutions (Low elevation or no lock). Total sats: {len(results)}")
                    else:
                        # Debug: show what's in the RAWX message
                        if payload and 'measurements' in payload:
                            num_meas = len(payload['measurements'])
                            
                            # Analyze signal types and satellite pairing
                            sig_types = {}
                            sat_signals = {}  # Track which signals each satellite has
                            for m in payload['measurements']:
                                gnss = m['gnssId']
                                sig = m['sigId']
                                sv = m['svId']
                                key = f"GNSS{gnss}_Sig{sig}"
                                sig_types[key] = sig_types.get(key, 0) + 1
                                
                                # Track GPS satellites
                                if gnss == 0:  # GPS only
                                    sat_key = f"G{sv:02d}"
                                    if sat_key not in sat_signals:
                                        sat_signals[sat_key] = set()
                                    sat_signals[sat_key].add(sig)
                            
                            # Count satellites with both L1 and L2
                            dual_freq_sats = [sat for sat, sigs in sat_signals.items() if 0 in sigs and (3 in sigs or 4 in sigs)]
                            
                            logger.debug(f"RAWX has {num_meas} measurements but no VTEC results (need L1+L2 on same sat)")
                            logger.debug(f"  Signal distribution: {sig_types}")
                            
                            # Check if we have any L2 signals (sigId 3 or 4 for GPS)
                            l1_count = sum(1 for m in payload['measurements'] if m['gnssId'] == 0 and m['sigId'] == 0)
                            l2_count = sum(1 for m in payload['measurements'] if m['gnssId'] == 0 and m['sigId'] in [3, 4])
                            logger.debug(f"  GPS: L1={l1_count}, L2={l2_count}, Dual-freq={len(dual_freq_sats)}")
                            logger.debug(f"  Dual-freq sats: {dual_freq_sats[:5]}")  # Show first 5
                            
                            if num_meas > 0:
                                # Show first few measurements
                                for i, m in enumerate(payload['measurements'][:3]):
                                    logger.debug(f"  Meas {i}: GNSS={m['gnssId']} SV={m['svId']} Sig={m['sigId']} CNO={m['cno']} Lock={m['locktime']}")
                        else:
                            logger.debug("RAWX processing returned no results")

          # end inner data loop
        finally:
            if sock:
                try:
                    sock.close()
                except Exception:
                    pass
            # Pet watchdog during reconnection gap
            if SYSTEMD_AVAILABLE:
                systemd_daemon.notify('WATCHDOG=1')
            logger.info(f"Reconnecting in {reconnect_delay}s...")
            time.sleep(reconnect_delay)

      # end outer reconnection loop
    except KeyboardInterrupt:
        logger.info("Stopping...")
    finally:
        # Flush any remaining buffered measurements before exit
        if hdf5_write_buffer and hdf5_writer:
            try:
                hdf5_writer.write_measurements_batch(hdf5_write_buffer)
                logger.info(f"Final flush: {len(hdf5_write_buffer)} VTEC measurements to HDF5")
            except Exception as e:
                logger.error(f"Failed final HDF5 flush: {e}")
        if sock:
            try:
                sock.close()
            except Exception:
                pass
        if csv_file: csv_file.close()
        if hdf5_writer: hdf5_writer.close()

if __name__ == "__main__":
    main()

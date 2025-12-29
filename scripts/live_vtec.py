#!/usr/bin/env python3
import socket
import logging
import time
import sys
import argparse
from datetime import datetime



from hf_timestd.core.ubx_parser import UBXParser
from hf_timestd.core.gnss_tec import GNSSTECAnalyzer
from hf_timestd.cddis import CDDISDownloader

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
            logger.info("GNSS VTEC monitoring is disabled in config. Exiting.")
            return

    # 1. Prepare DCB Data
    logger.info("Initializing CDDIS Downloader...")
    downloader = CDDISDownloader()
    dcb_file = downloader.download_latest_rapid_dcb()
    
    if not dcb_file:
        logger.error("Failed to download DCB file. VTEC accuracy will be degraded (0 bias assumed).")
        dcb_data = {}
    else:
        logger.info(f"Parsing DCB file: {dcb_file}")
        dcb_data = downloader.parse_biases(dcb_file)
        logger.info(f"Loaded {len(dcb_data)} bias entries.")

    if args.download_only:
        return

    # 2. Connect to Stream
    logger.info(f"Connecting to {host}:{port}...")
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.connect((host, port))
        logger.info("Connected!")
    except Exception as e:
        logger.error(f"Connection failed: {e}")
        return

    # 3. Processing Loop
    parser_ubx = UBXParser()
    analyzer = GNSSTECAnalyzer(dcb_data)
    
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

    try:
        bytes_received = 0
        msg_count = 0
        last_log_time = time.time()
        
        while True:
            data = sock.recv(4096)
            if not data:
                logger.warning("Socket closed.")
                break
            
            bytes_received += len(data)
            
            # Log data reception rate every 10 seconds
            if time.time() - last_log_time > 10:
                logger.info(f"Receiving data: {bytes_received} bytes, {msg_count} UBX messages processed")
                last_log_time = time.time()
                bytes_received = 0
                msg_count = 0
                
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
                    
                    if results:
                        logger.debug(f"RAWX processed: {len(results)} satellites with VTEC")
                        valid_vtecs = [r['vtec_u'] for r in results.values() if r['elev'] > 20]
                        if valid_vtecs:
                            avg_vtec = sum(valid_vtecs) / len(valid_vtecs)
                            # Sanity check: 0-150 TECU is normal.
                            logger.info(f"VTEC: {avg_vtec:.2f} TECU (Sats: {len(valid_vtecs)})")
                            
                            line = f"{timestamp},{avg_vtec:.2f},{len(valid_vtecs)}\n"
                            print(line, end='')
                            if csv_file:
                                csv_file.write(line)
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
                            
    except KeyboardInterrupt:
        logger.info("Stopping...")
    finally:
        sock.close()
        if csv_file: csv_file.close()

if __name__ == "__main__":
    main()

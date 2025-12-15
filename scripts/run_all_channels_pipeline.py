#!/usr/bin/env python3
"""
Run Three-Phase Pipeline on All Channels

Starts the new three-phase pipeline for all channels defined in timestd-config.toml.
Records raw 20 kHz IQ to Phase 1 immutable archive.

Usage:
    python scripts/run_all_channels_pipeline.py
    python scripts/run_all_channels_pipeline.py --duration 3600  # 1 hour
    python scripts/run_all_channels_pipeline.py --output /data/grape
"""

import argparse
import logging
import signal
import sys
import time
import threading
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, List, Optional

# Try tomllib (Python 3.11+), fall back to alternatives
try:
    import tomllib
    def load_toml(path):
        with open(path, 'rb') as f:
            return tomllib.load(f)
except ImportError:
    try:
        import tomli
        def load_toml(path):
            with open(path, 'rb') as f:
                return tomli.load(f)
    except ImportError:
        import toml
        def load_toml(path):
            return toml.load(path)

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s: %(message)s'
)
logger = logging.getLogger(__name__)


class ChannelPipeline:
    """Manages a single channel's three-phase pipeline."""
    
    def __init__(
        self,
        channel_desc: str,
        frequency_hz: float,
        sample_rate: int,
        output_dir: Path,
        station_config: Dict,
        status_address: str
    ):
        self.channel_desc = channel_desc
        self.frequency_hz = frequency_hz
        self.sample_rate = sample_rate
        self.output_dir = output_dir
        self.station_config = station_config
        self.status_address = status_address
        
        self.channel_name = channel_desc.replace(' ', '_')
        self.orchestrator = None
        self.stream = None
        self.ka9q_channel = None
        self.ssrc = None
        self.control = None
        
        # Statistics
        self.samples_received = 0
        self.callbacks_received = 0
        self.last_rtp_timestamp = None
        self.running = False
    
    def start(self, control) -> bool:
        """Start the channel pipeline."""
        import numpy as np
        from grape_recorder.grape import PipelineConfig, PipelineOrchestrator
        from ka9q import RTPRecorder, discover_channels, PacketResequencer, rtp_to_wallclock, Encoding
        
        self.control = control
        
        try:
            # SSRC should be pre-assigned by ChannelManager
            if not self.ssrc:
                logger.error(f"[{self.channel_desc}] No SSRC assigned!")
                return False
            
            # Discover channel info using the pre-assigned SSRC
            channels = discover_channels(self.status_address)
            self.ka9q_channel = channels.get(self.ssrc)
            
            if not self.ka9q_channel:
                logger.error(f"[{self.channel_desc}] SSRC {self.ssrc} not found in radiod")
                return False
            
            # Log the channel info
            logger.info(f"[{self.channel_desc}] Channel ready: freq={self.ka9q_channel.frequency/1e6:.3f} MHz, "
                        f"rate={self.ka9q_channel.sample_rate}, mcast={self.ka9q_channel.multicast_address}:{self.ka9q_channel.port}")
            
            # Create pipeline config
            pipeline_config = PipelineConfig(
                data_dir=self.output_dir,
                channel_name=self.channel_name,
                frequency_hz=self.frequency_hz,
                sample_rate=self.sample_rate,
                receiver_grid=self.station_config['grid_square'],
                station_config=self.station_config,
                raw_archive_compression='gzip',
                raw_archive_file_duration_sec=3600,
                analysis_latency_sec=120,
                output_sample_rate=10,
                streaming_latency_minutes=2
            )
            
            # Create orchestrator
            self.orchestrator = PipelineOrchestrator(pipeline_config)
            
            # Create resequencer for sample-level ordering
            samples_per_packet = self.sample_rate // 100  # 10ms packets at sample_rate
            self.resequencer = PacketResequencer(
                buffer_size=64,
                samples_per_packet=samples_per_packet,
                sample_rate=self.sample_rate
            )
            
            # Packet callback - uses RTPRecorder for proper sequencing
            def on_packet(header, payload, wallclock):
                if not self.running:
                    return
                
                self.callbacks_received += 1
                
                # Decode payload to IQ samples
                # Payload type 111 = 32-bit float (F32), 97/120 = 16-bit int
                try:
                    if header.payload_type in (97, 120):
                        # 16-bit signed integer encoding (4 bytes per IQ sample)
                        n_bytes = len(payload) - (len(payload) % 4)  # Align to 4 bytes
                        samples_int16 = np.frombuffer(payload[:n_bytes], dtype=np.int16)
                        samples = samples_int16.astype(np.float32) / 32768.0
                    else:
                        # 32-bit float encoding (F32, payload_type 111)
                        # 8 bytes per IQ sample (4 bytes I + 4 bytes Q)
                        n_bytes = len(payload) - (len(payload) % 8)  # Align to 8 bytes
                        if n_bytes == 0:
                            return
                        samples = np.frombuffer(payload[:n_bytes], dtype=np.float32)
                    
                    # Convert to complex (ensure even number of samples)
                    n_pairs = len(samples) // 2
                    if n_pairs == 0:
                        return
                    samples = samples[:n_pairs * 2]
                    i_samples = samples[0::2]
                    q_samples = samples[1::2]
                    iq_samples = (i_samples + 1j * q_samples).astype(np.complex64)
                    
                    self.samples_received += len(iq_samples)
                    
                    # Use RTP timestamp for proper ordering
                    rtp_timestamp = header.timestamp
                    
                    # Feed to pipeline with actual RTP timestamp
                    # NOTE: wallclock from RTPRecorder is NOT Unix time - use time.time()
                    self.orchestrator.process_samples(
                        samples=iq_samples,
                        rtp_timestamp=rtp_timestamp,
                        system_time=time.time()
                    )
                    
                except Exception as e:
                    logger.warning(f"[{self.channel_desc}] Packet decode error: {e}")
            
            # Create RTPRecorder with resequencing
            self.recorder = RTPRecorder(
                channel=self.ka9q_channel,
                on_packet=on_packet,
                max_packet_gap=10,
                resync_threshold=5,
                pass_all_packets=False  # Let RTPRecorder handle sequencing
            )
            
            # Start
            self.orchestrator.start()
            self.recorder.start()
            self.recorder.start_recording()
            self.running = True
            
            logger.info(f"[{self.channel_desc}] ✅ Pipeline started (using RTPRecorder)")
            return True
            
        except Exception as e:
            logger.error(f"[{self.channel_desc}] Failed to start: {e}")
            import traceback
            traceback.print_exc()
            return False
    
    def stop(self):
        """Stop the channel pipeline."""
        self.running = False
        
        # Get final stream health metrics before stopping
        rtp_metrics = {}
        if hasattr(self, 'recorder') and self.recorder:
            try:
                rtp_metrics = self.recorder.get_metrics()
                self.recorder.stop_recording()
                self.recorder.stop()
            except:
                pass
        
        if self.orchestrator:
            try:
                # Pass stream health to archive before closing
                if rtp_metrics:
                    self.orchestrator.set_stream_health(rtp_metrics)
                self.orchestrator.stop()
            except:
                pass
        
        logger.info(f"[{self.channel_desc}] Stopped (samples: {self.samples_received:,})")
    
    def get_stats(self) -> Dict:
        """Get channel statistics."""
        pipeline_stats = {}
        if self.orchestrator:
            pipeline_stats = self.orchestrator.get_stats()
        
        # Get RTP stream health metrics
        rtp_metrics = {}
        if hasattr(self, 'recorder') and self.recorder:
            rtp_metrics = self.recorder.get_metrics()
        
        return {
            'channel': self.channel_desc,
            'frequency_mhz': self.frequency_hz / 1e6,
            'ssrc': self.ssrc,
            'samples_received': self.samples_received,
            'callbacks': self.callbacks_received,
            'samples_archived': pipeline_stats.get('samples_archived', 0),
            'minutes_analyzed': pipeline_stats.get('minutes_analyzed', 0),
            'running': self.running,
            # RTP stream health metrics
            'packets_received': rtp_metrics.get('packets_received', 0),
            'packets_dropped': rtp_metrics.get('packets_dropped', 0),
            'packets_out_of_order': rtp_metrics.get('packets_out_of_order', 0),
            'sequence_errors': rtp_metrics.get('sequence_errors', 0),
            'timestamp_jumps': rtp_metrics.get('timestamp_jumps', 0),
            'state_changes': rtp_metrics.get('state_changes', 0),
        }


def run_all_channels(
    config_path: Path,
    output_dir: Path,
    duration_sec: Optional[int] = None,
    quota_gb: Optional[float] = None
):
    """
    Run three-phase pipeline on all channels.
    
    Args:
        config_path: Path to timestd-config.toml
        output_dir: Output directory for all channels
        duration_sec: Recording duration (None = run until Ctrl+C)
        quota_gb: Storage quota per channel in GB (None = unlimited)
    """
    # Load config
    config = load_toml(config_path)
    
    # Station config
    station_config = {
        'callsign': config['station']['callsign'],
        'grid_square': config['station']['grid_square'],
        'psws_station_id': config['station']['id'],
        'psws_instrument_id': config['station']['instrument_id'],
        'receiver_name': 'GRAPE',
        # Precise coordinates for improved timing accuracy (~16μs improvement)
        'latitude': config['station'].get('latitude'),
        'longitude': config['station'].get('longitude')
    }
    
    # Channel defaults
    sample_rate = config['recorder']['channel_defaults'].get('sample_rate', 20000)
    status_address = config['ka9q']['status_address']
    
    # Storage quota: CLI overrides config file
    # Config format: "500GB", "1TB", "80%", or "unlimited"
    storage_quota = config['recorder'].get('storage_quota', None)
    if quota_gb is not None:
        # CLI override - convert to string format
        storage_quota = f"{quota_gb}GB"
    
    # Get all channels
    channels_config = config['recorder']['channels']
    
    logger.info("=" * 70)
    logger.info("THREE-PHASE PIPELINE - ALL CHANNELS")
    logger.info("=" * 70)
    logger.info(f"Station: {station_config['callsign']} @ {station_config['grid_square']}")
    logger.info(f"Radiod: {status_address}")
    logger.info(f"Output: {output_dir}")
    logger.info(f"Duration: {duration_sec} seconds" if duration_sec else "Duration: Until Ctrl+C")
    logger.info(f"Storage quota: {storage_quota}" if storage_quota else "Storage quota: Unlimited")
    logger.info(f"Channels: {len(channels_config)}")
    logger.info("=" * 70)
    
    # Create output directory
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # Create storage quota manager (manages ALL channels together)
    quota_manager = None
    if storage_quota:
        from grape_recorder.grape.raw_archive_writer import StorageQuotaManager
        quota_manager = StorageQuotaManager(
            archive_root=output_dir,
            quota=storage_quota
        )
    
    # Use ChannelManager to ensure channels exist (matches by frequency, reuses existing)
    logger.info("Setting up channels via ChannelManager...")
    try:
        from grape_recorder.channel_manager import ChannelManager
        channel_manager = ChannelManager(status_address)
        
        # Build channel specs and defaults for ChannelManager
        channel_defaults = {
            'preset': 'iq',
            'sample_rate': sample_rate,
            'agc': 0,
            'gain': 0.0
        }
        
        # Ensure channels exist - this reuses existing or creates new
        freq_to_ssrc = channel_manager.ensure_channels_from_config(
            channels=channels_config,
            defaults=channel_defaults,
            destination=None  # Use radiod's default
        )
        
        if not freq_to_ssrc:
            logger.error("Failed to set up channels in radiod")
            return False
        
        logger.info(f"✅ {len(freq_to_ssrc)} channels ready")
        
    except Exception as e:
        logger.error(f"Failed to set up channels: {e}")
        import traceback
        traceback.print_exc()
        return False
    
    # Create pipelines for all channels (using allocated SSRCs)
    pipelines: List[ChannelPipeline] = []
    
    for ch_config in channels_config:
        freq_hz = int(ch_config['frequency_hz'])
        ssrc = freq_to_ssrc.get(freq_hz)
        if not ssrc:
            logger.warning(f"No SSRC for {freq_hz/1e6:.3f} MHz, skipping")
            continue
            
        pipeline = ChannelPipeline(
            channel_desc=ch_config['description'],
            frequency_hz=ch_config['frequency_hz'],
            sample_rate=sample_rate,
            output_dir=output_dir,
            station_config=station_config,
            status_address=status_address
        )
        pipeline.ssrc = ssrc  # Pre-assign SSRC from ChannelManager
        pipelines.append(pipeline)
    
    # Start all pipelines (channels already exist)
    logger.info("")
    logger.info("Starting pipelines...")
    started = 0
    
    for pipeline in pipelines:
        if pipeline.start(channel_manager.control):
            started += 1
        time.sleep(0.1)  # Brief stagger
    
    logger.info("")
    logger.info(f"✅ Started {started}/{len(pipelines)} channels")
    
    if started == 0:
        logger.error("No channels started!")
        return False
    
    # Setup signal handler
    running = True
    def signal_handler(sig, frame):
        nonlocal running
        logger.info("\nReceived shutdown signal...")
        running = False
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # Main loop
    logger.info("")
    if duration_sec:
        logger.info(f"Recording for {duration_sec} seconds... (Ctrl+C to stop early)")
    else:
        logger.info("Recording... (Ctrl+C to stop)")
    logger.info("")
    
    start_time = time.time()
    last_status_time = start_time
    
    try:
        while running:
            if duration_sec and (time.time() - start_time) >= duration_sec:
                break
            
            time.sleep(0.5)
            
            # Print status every 30 seconds
            if time.time() - last_status_time >= 30:
                elapsed = time.time() - start_time
                logger.info(f"[{elapsed:.0f}s] Channel status:")
                
                total_samples = 0
                total_archived = 0
                total_packets = 0
                total_dropped = 0
                total_ooo = 0
                total_seq_errors = 0
                
                for pipeline in pipelines:
                    stats = pipeline.get_stats()
                    total_samples += stats['samples_received']
                    total_archived += stats['samples_archived']
                    total_packets += stats['packets_received']
                    total_dropped += stats['packets_dropped']
                    total_ooo += stats['packets_out_of_order']
                    total_seq_errors += stats['sequence_errors']
                    
                    samples = stats['samples_received']
                    archived = stats['samples_archived']
                    dropped = stats['packets_dropped']
                    ooo = stats['packets_out_of_order']
                    status = "✅" if stats['running'] else "❌"
                    
                    # Show health warning if issues detected
                    health = ""
                    if dropped > 0 or ooo > 0:
                        health = f" ⚠️ drop={dropped} ooo={ooo}"
                    
                    logger.info(f"  {status} {stats['channel']}: {samples:,} recv, {archived:,} arch{health}")
                
                # Stream health summary
                if total_packets > 0:
                    drop_rate = 100.0 * total_dropped / total_packets
                    ooo_rate = 100.0 * total_ooo / total_packets
                    logger.info(f"  Stream health: {total_packets:,} pkts, {total_dropped} dropped ({drop_rate:.2f}%), {total_ooo} out-of-order ({ooo_rate:.2f}%)")
                
                logger.info(f"  Total: {total_samples:,} samples received, {total_archived:,} archived")
                
                # Enforce storage quota periodically
                if quota_manager:
                    quota_result = quota_manager.enforce_quota()
                    if quota_result.get('action') == 'cleaned':
                        logger.info(f"  Storage: freed {quota_result['bytes_freed']/1024**3:.2f} GB")
                    else:
                        stats = quota_manager.get_stats()
                        logger.info(f"  Storage: {stats['used_gb']:.2f}/{stats['quota_gb']:.2f} GB ({stats['usage_ratio']*100:.1f}%)")
                
                logger.info("")
                last_status_time = time.time()
    
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
    
    # Stop all pipelines
    logger.info("")
    logger.info("Stopping all pipelines...")
    
    for pipeline in pipelines:
        pipeline.stop()
    
    # Final summary
    elapsed = time.time() - start_time
    
    logger.info("")
    logger.info("=" * 70)
    logger.info("RECORDING COMPLETE")
    logger.info("=" * 70)
    logger.info(f"Duration: {elapsed:.1f} seconds")
    logger.info("")
    logger.info("Channel Summary:")
    
    total_samples = 0
    total_archived = 0
    total_packets = 0
    total_dropped = 0
    total_ooo = 0
    total_seq_errors = 0
    total_ts_jumps = 0
    
    for pipeline in pipelines:
        stats = pipeline.get_stats()
        total_samples += stats['samples_received']
        total_archived += stats['samples_archived']
        total_packets += stats['packets_received']
        total_dropped += stats['packets_dropped']
        total_ooo += stats['packets_out_of_order']
        total_seq_errors += stats['sequence_errors']
        total_ts_jumps += stats['timestamp_jumps']
        
        logger.info(f"  {stats['channel']}:")
        logger.info(f"    Samples received: {stats['samples_received']:,}")
        logger.info(f"    Samples archived: {stats['samples_archived']:,}")
        logger.info(f"    Minutes analyzed: {stats['minutes_analyzed']}")
        logger.info(f"    Stream health: {stats['packets_received']:,} pkts, "
                   f"{stats['packets_dropped']} dropped, {stats['packets_out_of_order']} ooo, "
                   f"{stats['sequence_errors']} seq_err")
    
    logger.info("")
    logger.info(f"Total samples: {total_samples:,}")
    logger.info(f"Total archived: {total_archived:,}")
    logger.info("")
    logger.info("Stream Health Summary:")
    logger.info(f"  Total packets: {total_packets:,}")
    logger.info(f"  Packets dropped: {total_dropped} ({100.0*total_dropped/max(1,total_packets):.3f}%)")
    logger.info(f"  Out-of-order: {total_ooo} ({100.0*total_ooo/max(1,total_packets):.3f}%)")
    logger.info(f"  Sequence errors: {total_seq_errors}")
    logger.info(f"  Timestamp jumps: {total_ts_jumps}")
    
    # Storage quota summary
    if quota_manager:
        quota_manager.write_removal_log()
        stats = quota_manager.get_stats()
        logger.info("")
        logger.info("Storage Quota Summary:")
        logger.info(f"  Quota: {stats['quota_gb']:.1f} GB")
        logger.info(f"  Used: {stats['used_gb']:.2f} GB ({stats['usage_ratio']*100:.1f}%)")
        logger.info(f"  Dates stored: {stats['oldest_date']} to {stats['newest_date']}")
        if stats['dirs_removed_total'] > 0:
            logger.info(f"  Cleaned: {stats['dirs_removed_total']} dirs, {stats['bytes_removed_total']/1024**3:.2f} GB freed")
    
    logger.info("")
    logger.info(f"Output directory: {output_dir}")
    logger.info("=" * 70)
    
    return True


def main():
    parser = argparse.ArgumentParser(
        description='Run three-phase pipeline on all channels'
    )
    parser.add_argument(
        '--config', '-f',
        type=Path,
        default=Path('config/timestd-config.toml'),
        help='Config file path'
    )
    parser.add_argument(
        '--output', '-o',
        type=Path,
        default=Path('/tmp/grape-pipeline-all'),
        help='Output directory'
    )
    parser.add_argument(
        '--duration', '-d',
        type=int,
        default=None,
        help='Recording duration in seconds (default: run until Ctrl+C)'
    )
    parser.add_argument(
        '--quota', '-q',
        type=float,
        default=None,
        help='Total storage quota in GB (overrides config file)'
    )
    
    args = parser.parse_args()
    
    if not args.config.exists():
        logger.error(f"Config file not found: {args.config}")
        return 1
    
    success = run_all_channels(
        config_path=args.config,
        output_dir=args.output,
        duration_sec=args.duration,
        quota_gb=args.quota
    )
    
    return 0 if success else 1


if __name__ == '__main__':
    sys.exit(main())

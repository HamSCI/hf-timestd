#!/usr/bin/env python3
"""
Batch generate 10 Hz decimated NPZ files from existing 16 kHz archives.

Reprocesses historical archive data to create 10 Hz decimated NPZ files
in analytics/{channel}/decimated/ directory, matching the output format
of the real-time analytics service.

Usage:
    python3 scripts/batch_generate_10hz_npz.py --data-root /tmp/timestd-test
    python3 scripts/batch_generate_10hz_npz.py --data-root /tmp/timestd-test --channel "WWV 10 MHz"
"""

import argparse
import logging
import numpy as np
from pathlib import Path
from scipy import signal as scipy_signal
from datetime import datetime, timezone
import sys

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def decimate_npz(npz_16khz: Path, npz_10hz: Path, overwrite: bool = False) -> bool:
    """
    Generate 10 Hz decimated NPZ from 16 kHz NPZ file.
    
    Args:
        npz_16khz: Path to source 16 kHz NPZ file
        npz_10hz: Path to output 10 Hz NPZ file
        overwrite: If True, regenerate even if output exists
        
    Returns:
        True if generated, False if skipped
    """
    if npz_10hz.exists() and not overwrite:
        return False  # Already exists
    
    try:
        # Load source data
        data = np.load(npz_16khz)
        iq_samples = data['iq']
        rtp_timestamp = int(data['rtp_timestamp'])
        sample_rate = int(data['sample_rate'])
        
        # Decimate from 16 kHz to 10 Hz
        decimation_factor = sample_rate // 10  # 16000 / 10 = 1600
        decimated = scipy_signal.decimate(iq_samples, decimation_factor, ftype='fir', zero_phase=True)
        decimated = decimated.astype(np.complex64)
        
        # Write compact 10 Hz file
        # Key name 'iq' matches what analytics service produces
        np.savez_compressed(
            npz_10hz,
            iq=decimated,                        # 600 samples @ 10 Hz (key name matches analytics)
            rtp_timestamp=rtp_timestamp,         # RTP timestamp of iq[0]
            sample_rate_original=sample_rate,    # Original sample rate (16000)
            sample_rate_decimated=10,            # Output rate (10 Hz)
            decimation_factor=decimation_factor, # 1600
            created_timestamp=datetime.now(tz=timezone.utc).timestamp(),
            source_file=npz_16khz.name
        )
        return True
        
    except Exception as e:
        logger.error(f"Failed to decimate {npz_16khz.name}: {e}")
        if npz_10hz.exists():
            npz_10hz.unlink()  # Clean up partial file
        return False


def process_channel(data_root: Path, channel_name: str, overwrite: bool = False):
    """Process all NPZ files for a channel."""
    from grape_recorder.paths import GRAPEPaths
    paths = GRAPEPaths(data_root)
    
    archive_dir = paths.get_archive_dir(channel_name)
    decimated_dir = paths.get_decimated_dir(channel_name)
    
    if not archive_dir.exists():
        logger.warning(f"Archive directory not found: {archive_dir}")
        return
    
    # Create output directory in analytics/decimated/
    decimated_dir.mkdir(parents=True, exist_ok=True)
    
    # Find all 16 kHz NPZ files
    npz_files = sorted(archive_dir.glob('*_iq.npz'))
    
    if not npz_files:
        logger.warning(f"No NPZ files found in {archive_dir}")
        return
    
    logger.info(f"\n{'='*60}")
    logger.info(f"Processing: {channel_name}")
    logger.info(f"  Found {len(npz_files)} NPZ files")
    logger.info(f"  Output: {decimated_dir}")
    logger.info(f"{'='*60}")
    
    generated = 0
    skipped = 0
    
    for npz_file in npz_files:
        # Output file: 20251115T000000Z_10000000_iq.npz → 20251115T000000Z_10000000_iq_10hz.npz
        # Place in analytics/decimated/ directory
        npz_10hz = decimated_dir / npz_file.name.replace('_iq.npz', '_iq_10hz.npz')
        
        if decimate_npz(npz_file, npz_10hz, overwrite):
            generated += 1
            if generated % 100 == 0:
                logger.info(f"  Generated: {generated}/{len(npz_files)}")
        else:
            skipped += 1
    
    logger.info(f"✅ {channel_name}: Generated {generated}, Skipped {skipped} (already exist)")


def main():
    parser = argparse.ArgumentParser(description='Generate 10 Hz decimated NPZ files')
    parser.add_argument('--data-root', type=Path, required=True,
                        help='Data root directory (e.g., /tmp/timestd-test)')
    parser.add_argument('--channel', type=str,
                        help='Process single channel (e.g., "WWV 10 MHz"). If omitted, processes all.')
    parser.add_argument('--overwrite', action='store_true',
                        help='Regenerate even if 10 Hz NPZ already exists')
    
    args = parser.parse_args()
    
    from grape_recorder.paths import GRAPEPaths, channel_dir_to_name
    paths = GRAPEPaths(args.data_root)
    
    archives_root = args.data_root / 'archives'
    if not archives_root.exists():
        logger.error(f"Archives directory not found: {archives_root}")
        sys.exit(1)
    
    logger.info("=" * 60)
    logger.info("Generate 10 Hz Pre-Decimated NPZ Files")
    logger.info("=" * 60)
    logger.info(f"Data root: {args.data_root}")
    logger.info(f"Overwrite: {args.overwrite}")
    
    if args.channel:
        # Process single channel
        process_channel(args.data_root, args.channel, args.overwrite)
    else:
        # Process all channels
        channel_dirs = [d for d in archives_root.iterdir() if d.is_dir()]
        logger.info(f"Found {len(channel_dirs)} channels")
        
        for channel_dir in sorted(channel_dirs):
            # Convert directory name back to channel name (WWV_10_MHz → WWV 10 MHz)
            channel_name = channel_dir.name.replace('_', ' ')
            # Handle special case: preserve decimal in frequency (WWV 2.5 MHz)
            # This is a simple heuristic; adjust if needed
            process_channel(args.data_root, channel_name, args.overwrite)
    
    logger.info("\n" + "=" * 60)
    logger.info("✅ Complete!")
    logger.info("=" * 60)
    logger.info("\nNow you can generate spectrograms from these 10 Hz files.")
    logger.info("Run: python3 scripts/check_and_generate_spectrograms.py --data-root <ROOT>")


if __name__ == '__main__':
    main()

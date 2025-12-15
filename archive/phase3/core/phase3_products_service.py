#!/usr/bin/env python3
"""
Phase 3 Products Service - Real-time spectrogram and power generation from DRF.

Reads Digital RF archive data and generates:
1. Power time series (carrier amplitude over time)
2. Rolling spectrograms (updated every minute)

This runs alongside Phase 2 analytics to provide visualization products.
"""

import argparse
import logging
import numpy as np
import os
import sys
import time
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple

# Digital RF support
try:
    import digital_rf as drf
except ImportError:
    drf = None
    print("Warning: digital_rf not available")

# Matplotlib for spectrograms
try:
    import matplotlib
    matplotlib.use('Agg')  # Non-interactive backend
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
except ImportError:
    plt = None
    print("Warning: matplotlib not available")

logger = logging.getLogger(__name__)


class Phase3ProductsService:
    """Generate products from DRF archive data in real-time."""
    
    def __init__(
        self,
        archive_dir: str,
        output_dir: str,
        channel_name: str,
        frequency_hz: float,
        sample_rate: float = 20000.0,
        poll_interval: float = 60.0
    ):
        self.archive_dir = Path(archive_dir)
        self.output_dir = Path(output_dir)
        self.channel_name = channel_name
        self.frequency_hz = frequency_hz
        self.sample_rate = sample_rate
        self.poll_interval = poll_interval
        
        # Create output directories
        self.power_dir = self.output_dir / 'power'
        self.spectrogram_dir = self.output_dir / 'spectrograms'
        self.power_dir.mkdir(parents=True, exist_ok=True)
        self.spectrogram_dir.mkdir(parents=True, exist_ok=True)
        
        # State
        self.last_processed_minute = 0
        self.power_buffer: List[Tuple[float, float, float]] = []  # (timestamp, power_db, snr_db)
        
        # Open DRF reader
        self.drf_reader = None
        if drf:
            try:
                self.drf_reader = drf.DigitalRFReader(str(self.archive_dir.parent))
            except Exception as e:
                logger.error(f"Failed to open DRF reader: {e}")
    
    def process_minute(self, minute_boundary: int) -> Optional[Dict[str, Any]]:
        """Process one minute of DRF data and generate products."""
        if not self.drf_reader:
            return None
        
        try:
            # Get channel name as stored in DRF
            channels = self.drf_reader.get_channels()
            drf_channel = self.archive_dir.name
            
            if drf_channel not in channels:
                logger.warning(f"Channel {drf_channel} not found in DRF")
                return None
            
            # Calculate sample bounds for this minute
            start_sample = int(minute_boundary * self.sample_rate)
            end_sample = int((minute_boundary + 60) * self.sample_rate)
            
            # Read the data
            try:
                data = self.drf_reader.read_vector(
                    start_sample, end_sample - start_sample,
                    drf_channel
                )
            except Exception as e:
                logger.debug(f"Could not read minute {minute_boundary}: {e}")
                return None
            
            if data is None or len(data) == 0:
                return None
            
            # Calculate power and SNR
            power_linear = np.mean(np.abs(data) ** 2)
            power_db = 10 * np.log10(power_linear + 1e-12)
            
            # Estimate noise floor from quiet portions
            sorted_power = np.sort(np.abs(data) ** 2)
            noise_floor = np.mean(sorted_power[:len(sorted_power)//10])
            snr_db = 10 * np.log10(power_linear / (noise_floor + 1e-12))
            
            # Add to buffer
            self.power_buffer.append((minute_boundary, power_db, snr_db))
            
            # Keep only last 24 hours
            cutoff = minute_boundary - 86400
            self.power_buffer = [(t, p, s) for t, p, s in self.power_buffer if t > cutoff]
            
            # Write power CSV
            self._write_power_csv(minute_boundary)
            
            # Generate spectrogram every 10 minutes
            if minute_boundary % 600 == 0:
                self._generate_spectrogram(minute_boundary)
            
            return {
                'minute': minute_boundary,
                'power_db': round(power_db, 2),
                'snr_db': round(snr_db, 2),
                'samples': len(data)
            }
            
        except Exception as e:
            logger.error(f"Error processing minute {minute_boundary}: {e}")
            return None
    
    def _write_power_csv(self, current_minute: int):
        """Write power time series to CSV."""
        date_str = datetime.utcfromtimestamp(current_minute).strftime('%Y%m%d')
        csv_file = self.power_dir / f'carrier_power_{date_str}.csv'
        
        # Filter to today's data
        day_start = (current_minute // 86400) * 86400
        today_data = [(t, p, s) for t, p, s in self.power_buffer if t >= day_start]
        
        with open(csv_file, 'w') as f:
            f.write('timestamp,utc_time,power_db,snr_db\n')
            for ts, power, snr in today_data:
                utc_time = datetime.utcfromtimestamp(ts).isoformat() + 'Z'
                f.write(f'{ts},{utc_time},{power:.2f},{snr:.2f}\n')
    
    def _generate_spectrogram(self, current_minute: int):
        """Generate rolling spectrogram image."""
        if not plt or not self.drf_reader:
            return
        
        try:
            # Get last 6 hours of data for spectrogram
            duration_hours = 6
            duration_samples = int(duration_hours * 3600 * self.sample_rate)
            end_sample = int(current_minute * self.sample_rate)
            start_sample = end_sample - duration_samples
            
            drf_channel = self.archive_dir.name
            
            # Read data in chunks to avoid memory issues
            chunk_duration = 300  # 5 minutes
            chunk_samples = int(chunk_duration * self.sample_rate)
            
            all_spectra = []
            timestamps = []
            
            for chunk_start in range(start_sample, end_sample, chunk_samples):
                try:
                    chunk_end = min(chunk_start + chunk_samples, end_sample)
                    data = self.drf_reader.read_vector(
                        chunk_start, chunk_end - chunk_start,
                        drf_channel
                    )
                    
                    if data is not None and len(data) > 0:
                        # Compute spectrum
                        nfft = 1024
                        spectrum = np.abs(np.fft.fft(data[:nfft])) ** 2
                        spectrum_db = 10 * np.log10(spectrum + 1e-12)
                        all_spectra.append(spectrum_db[:nfft//2])
                        timestamps.append(chunk_start / self.sample_rate)
                except Exception:
                    continue
            
            if len(all_spectra) < 10:
                return
            
            # Create spectrogram image
            spectrogram = np.array(all_spectra).T
            
            fig, ax = plt.subplots(figsize=(12, 6))
            
            # Frequency axis
            freq_axis = np.fft.fftfreq(1024, 1/self.sample_rate)[:512]
            freq_khz = freq_axis / 1000
            
            # Time axis
            time_labels = [datetime.utcfromtimestamp(t) for t in timestamps]
            
            im = ax.imshow(
                spectrogram,
                aspect='auto',
                origin='lower',
                extent=[0, len(timestamps), freq_khz[0], freq_khz[-1]],
                cmap='viridis',
                vmin=-60, vmax=-20
            )
            
            ax.set_xlabel('Time (UTC)')
            ax.set_ylabel('Frequency Offset (kHz)')
            ax.set_title(f'{self.channel_name} - {datetime.utcfromtimestamp(current_minute).strftime("%Y-%m-%d %H:%M")} UTC')
            
            plt.colorbar(im, ax=ax, label='Power (dB)')
            
            # Save
            date_str = datetime.utcfromtimestamp(current_minute).strftime('%Y%m%d')
            output_file = self.spectrogram_dir / f'{date_str}_spectrogram.png'
            
            plt.savefig(output_file, dpi=100, bbox_inches='tight')
            plt.close(fig)
            
            logger.info(f"Generated spectrogram: {output_file}")
            
        except Exception as e:
            logger.error(f"Error generating spectrogram: {e}")
    
    def run(self):
        """Main processing loop."""
        logger.info(f"Phase 3 Products Service starting for {self.channel_name}")
        logger.info(f"  Archive: {self.archive_dir}")
        logger.info(f"  Output: {self.output_dir}")
        
        while True:
            try:
                # Get current minute boundary
                now = time.time()
                current_minute = int(now // 60) * 60
                
                # Process if we haven't processed this minute yet
                if current_minute > self.last_processed_minute:
                    # Process the previous minute (it's complete)
                    prev_minute = current_minute - 60
                    result = self.process_minute(prev_minute)
                    
                    if result:
                        logger.info(
                            f"{self.channel_name}: minute {prev_minute} - "
                            f"power={result['power_db']:.1f}dB, snr={result['snr_db']:.1f}dB"
                        )
                    
                    self.last_processed_minute = current_minute
                
                # Wait for next poll
                time.sleep(self.poll_interval)
                
            except KeyboardInterrupt:
                logger.info("Shutting down...")
                break
            except Exception as e:
                logger.error(f"Error in main loop: {e}")
                time.sleep(10)


def main():
    parser = argparse.ArgumentParser(description='Phase 3 Products Service')
    parser.add_argument('--archive-dir', required=True, help='DRF archive directory')
    parser.add_argument('--output-dir', required=True, help='Output directory for products')
    parser.add_argument('--channel-name', required=True, help='Channel name')
    parser.add_argument('--frequency-hz', type=float, required=True, help='Center frequency Hz')
    parser.add_argument('--sample-rate', type=float, default=20000, help='Sample rate Hz')
    parser.add_argument('--poll-interval', type=float, default=60, help='Poll interval seconds')
    parser.add_argument('--log-level', default='INFO', help='Log level')
    
    args = parser.parse_args()
    
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper()),
        format='%(asctime)s - %(levelname)s - %(message)s'
    )
    
    service = Phase3ProductsService(
        archive_dir=args.archive_dir,
        output_dir=args.output_dir,
        channel_name=args.channel_name,
        frequency_hz=args.frequency_hz,
        sample_rate=args.sample_rate,
        poll_interval=args.poll_interval
    )
    
    service.run()


if __name__ == '__main__':
    main()

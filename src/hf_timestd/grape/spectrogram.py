#!/usr/bin/env python3
"""
Carrier Spectrogram Generator

Generates carrier spectrograms and power charts from decimated 10 Hz binary data.
Designed for frequent updates (every 10 minutes) and on-demand generation.

Features:
---------
- 24-hour daily spectrograms with aligned power graph
- Solar zenith angle overlays for propagation context:
  - CHU channels: Single curve (receiver ↔ Ottawa)
  - WWV 20000/25000: Single curve (receiver ↔ Ft. Collins)
  - SHARED 2500/5000/10000/15000: Triple curves (Ft. Collins + Kauai + China)
- Rolling spectrograms (last 6h, 12h, 24h)
- Quality grade coloring
- Gap visualization

Output: products/{CHANNEL}/spectrograms/
        ├── {YYYYMMDD}_daily.png      # 24h spectrogram + power + solar zenith
        ├── rolling_6h.png
        ├── rolling_12h.png
        └── rolling_24h.png

Usage:
------
    from hf_timestd.grape.carrier_spectrogram import CarrierSpectrogramGenerator
    
    gen = CarrierSpectrogramGenerator(data_root, channel_name, receiver_grid='EM38ww')
    gen.generate_daily('20251206')  # Full day with solar zenith
    gen.generate_rolling(hours=6)   # Rolling without solar zenith
"""

import numpy as np
import logging
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, List, Tuple, Any
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Channel to station mapping for solar zenith overlays
# Format: channel_pattern -> list of stations to overlay
# Channel naming: SHARED_XXXX (shared WWV/WWVH/BPM), WWV_XXXXX, CHU_XXXXX (freq in kHz)
CHANNEL_STATION_MAP = {
    # CHU channels - single station (Ottawa)
    'CHU': ['CHU'],
    
    # WWV-only frequencies (20/25 MHz) - WWVH doesn't broadcast here
    'WWV 20000': ['WWV'],
    'WWV 25000': ['WWV'],
    
    # Shared frequencies - WWV, WWVH, and BPM all broadcast
    'SHARED 2500': ['WWV', 'WWVH', 'BPM'],
    'SHARED 5000': ['WWV', 'WWVH', 'BPM'],
    'SHARED 10000': ['WWV', 'WWVH', 'BPM'],
    'SHARED 15000': ['WWV', 'WWVH', 'BPM'],
}

# Station colors for solar zenith curves
STATION_COLORS = {
    'WWV': '#e74c3c',   # Red for Ft. Collins
    'WWVH': '#3498db',  # Blue for Kauai
    'CHU': '#2ecc71',   # Green for Ottawa
    'BPM': '#9b59b6',   # Purple for China
}

# Check for matplotlib
try:
    import matplotlib
    matplotlib.use('Agg')  # Non-interactive backend
    import matplotlib.pyplot as plt
    from matplotlib.colors import LinearSegmentedColormap
    from matplotlib.patches import Rectangle
    import matplotlib.dates as mdates
    MPL_AVAILABLE = True
except ImportError:
    MPL_AVAILABLE = False
    logger.warning("matplotlib not available - spectrogram generation disabled")

# Check for scipy
try:
    from scipy import signal
    SCIPY_AVAILABLE = True
except ImportError:
    SCIPY_AVAILABLE = False

from .decimated_buffer import DecimatedBuffer, SAMPLE_RATE, SAMPLES_PER_MINUTE


@dataclass
class SpectrogramConfig:
    """Configuration for spectrogram generation."""
    nfft: int = 512  # FFT size (512 @ 10Hz = 51.2s window, better freq resolution)
    noverlap: int = 480  # 93.75% overlap for smooth display (3.2s steps)
    cmap: str = 'viridis'
    vmin_db: float = -60  # Minimum dB for colormap
    vmax_db: float = 0    # Maximum dB (relative to peak)
    dpi: int = 150
    figsize_wide: Tuple[int, int] = (16, 5)  # Wide format for rolling
    figsize_daily: Tuple[int, int] = (14, 8)  # Daily with power chart
    show_gaps: bool = True
    show_quality: bool = True


class CarrierSpectrogramGenerator:
    """
    Generate carrier spectrograms from decimated binary buffer.
    
    Reads 10 Hz IQ data and generates:
    - 24-hour daily spectrograms with power graph and solar zenith overlays
    - Carrier frequency offset spectrograms (±5 Hz around carrier)
    - Power profile charts
    - Quality/gap annotations
    """
    
    def __init__(
        self,
        data_root: Path,
        channel_name: str,
        receiver_grid: str = '',
        config: Optional[SpectrogramConfig] = None
    ):
        """
        Initialize spectrogram generator.
        
        Args:
            data_root: Root data directory
            channel_name: Channel name (e.g., "SHARED 10000")
            receiver_grid: Maidenhead grid square for solar zenith calculations
            config: Spectrogram configuration
        """
        if not MPL_AVAILABLE:
            raise ImportError("matplotlib required for spectrogram generation")
        if not SCIPY_AVAILABLE:
            raise ImportError("scipy required for spectrogram generation")
        
        self.data_root = Path(data_root)
        self.channel_name = channel_name
        self.receiver_grid = receiver_grid
        self.config = config or SpectrogramConfig()
        
        # Data source
        self.buffer = DecimatedBuffer(data_root, channel_name)
        
        # Output location
        self.channel_dir = channel_name.replace(' ', '_')
        self.output_dir = self.data_root / 'products' / self.channel_dir / 'spectrograms'
        self.output_dir.mkdir(parents=True, exist_ok=True)
        
        # Determine which stations to show for solar zenith
        self.solar_stations = self._get_stations_for_channel(channel_name)
        
        logger.info(f"CarrierSpectrogramGenerator initialized for {channel_name}")
        logger.info(f"  Output: {self.output_dir}")
        logger.info(f"  Solar zenith stations: {self.solar_stations}")
    
    def _get_stations_for_channel(self, channel_name: str) -> List[str]:
        """Determine which transmitter stations to show for solar zenith overlay."""
        # Check exact match first
        if channel_name in CHANNEL_STATION_MAP:
            return CHANNEL_STATION_MAP[channel_name]
        
        # Check prefix match (e.g., "CHU 3330" matches "CHU")
        for pattern, stations in CHANNEL_STATION_MAP.items():
            if channel_name.startswith(pattern):
                return stations
        
        # Default: if WWV in name, show both; if CHU, show CHU only
        if 'CHU' in channel_name:
            return ['CHU']
        elif 'WWV' in channel_name:
            # Check frequency for WWVH coverage
            try:
                freq_str = channel_name.split()[-1]  # e.g., "20000" from "WWV 20000"
                freq_khz = float(freq_str)
                if freq_khz >= 20000:
                    return ['WWV']  # WWVH doesn't broadcast 20/25 MHz
                else:
                    return ['WWV', 'WWVH']
            except:
                return ['WWV', 'WWVH']
        
        return []
    
    def _interpolate_zeros(self, samples: np.ndarray) -> np.ndarray:
        """
        Replace zero samples with interpolated values to reduce spectral artifacts.
        
        Zeros from gaps cause dark vertical bands in spectrograms due to
        spectral leakage. This interpolates across zero regions using
        phase-continuous samples from surrounding valid data.
        
        Args:
            samples: Complex64 IQ samples potentially containing zeros
            
        Returns:
            Samples with zeros replaced by interpolated values
        """
        zero_mask = np.abs(samples) < 1e-10
        
        if not np.any(zero_mask):
            return samples  # No zeros to interpolate
        
        result = samples.copy()
        
        # Find runs of zeros
        padded = np.concatenate([[False], zero_mask, [False]])
        diff = np.diff(padded.astype(int))
        starts = np.where(diff == 1)[0]
        ends = np.where(diff == -1)[0]
        
        for start, end in zip(starts, ends):
            gap_len = end - start
            
            # Get samples before and after gap
            before_idx = start - 1 if start > 0 else None
            after_idx = end if end < len(samples) else None
            
            if before_idx is not None and after_idx is not None:
                before_sample = samples[before_idx]
                after_sample = samples[after_idx]
                
                if np.abs(before_sample) > 1e-10 and np.abs(after_sample) > 1e-10:
                    # Linear interpolation of amplitude and phase
                    before_phase = np.angle(before_sample)
                    after_phase = np.angle(after_sample)
                    before_amp = np.abs(before_sample)
                    after_amp = np.abs(after_sample)
                    
                    # Unwrap phase difference
                    phase_diff = after_phase - before_phase
                    if phase_diff > np.pi:
                        phase_diff -= 2 * np.pi
                    elif phase_diff < -np.pi:
                        phase_diff += 2 * np.pi
                    
                    # Interpolate
                    t = np.linspace(0, 1, gap_len + 2)[1:-1]
                    interp_phase = before_phase + t * phase_diff
                    interp_amp = before_amp + t * (after_amp - before_amp)
                    result[start:end] = interp_amp * np.exp(1j * interp_phase)
                    
            elif before_idx is not None:
                before_sample = samples[before_idx]
                if np.abs(before_sample) > 1e-10:
                    result[start:end] = before_sample
                    
            elif after_idx is not None:
                after_sample = samples[after_idx]
                if np.abs(after_sample) > 1e-10:
                    result[start:end] = after_sample
        
        return result
    
    def _get_solar_zenith_data(self, date_str: str) -> Optional[Dict]:
        """Get solar zenith data for the day (path midpoint solar elevation)."""
        if not self.receiver_grid or not self.solar_stations:
            return None
        
        try:
            from ..core.solar_zenith_calculator import calculate_solar_zenith_for_day
            
            # Normalize date format
            if '-' in date_str:
                date_str = date_str.replace('-', '')
            
            return calculate_solar_zenith_for_day(
                date_str=date_str,
                receiver_grid=self.receiver_grid,
                interval_minutes=5  # 5-minute resolution
            )
        except Exception as e:
            logger.warning(f"Failed to calculate solar zenith: {e}")
            return None
    
    def generate_rolling(self, hours: int = 6) -> Optional[Path]:
        """
        Generate rolling spectrogram for last N hours.
        
        Args:
            hours: Number of hours to include
            
        Returns:
            Path to generated PNG or None if failed
        """
        logger.info(f"Generating {hours}h rolling spectrogram for {self.channel_name}")
        
        # Read data from buffer
        iq_data, metadata_list = self.buffer.read_hours(hours)
        
        if iq_data is None or len(iq_data) == 0:
            logger.warning(f"No data available for {hours}h rolling spectrogram")
            return None
        
        # Check for valid data
        if np.all(iq_data == 0):
            logger.warning("All data is zero - no valid samples")
            return None
        
        # Generate spectrogram
        output_path = self.output_dir / f'rolling_{hours}h.png'
        
        now = datetime.now(tz=timezone.utc)
        start_time = now - timedelta(hours=hours)
        
        self._generate_spectrogram(
            iq_data=iq_data,
            output_path=output_path,
            title=f'{self.channel_name} - Last {hours} Hours',
            start_time=start_time,
            metadata_list=metadata_list
        )
        
        return output_path
    
    def generate_daily(self, date_str: str) -> Optional[Path]:
        """
        Generate daily spectrogram with power chart.
        
        Args:
            date_str: Date in YYYYMMDD or YYYY-MM-DD format
            
        Returns:
            Path to generated PNG or None if failed
        """
        # Normalize date format
        if '-' in date_str:
            date_str = date_str.replace('-', '')
        
        logger.info(f"Generating daily spectrogram for {self.channel_name} on {date_str}")
        
        # Read day's data - buffer returns full 24-hour array (preallocated with zeros)
        iq_data, day_metadata = self.buffer.read_day(date_str)
        
        if iq_data is None or len(iq_data) == 0:
            logger.warning(f"No data available for {date_str}")
            return None
        
        # Buffer should return exactly 864,000 samples (1440 min * 600 samples)
        # Data is positioned at correct time offsets (zeros where no data)
        full_day_samples = 1440 * SAMPLES_PER_MINUTE
        if len(iq_data) != full_day_samples:
            logger.warning(f"Expected {full_day_samples} samples, got {len(iq_data)} - file may be incomplete")
            # Ensure we have exactly 24 hours
            if len(iq_data) < full_day_samples:
                padded = np.zeros(full_day_samples, dtype=np.complex64)
                padded[:len(iq_data)] = iq_data
                iq_data = padded
            else:
                iq_data = iq_data[:full_day_samples]
        
        # Build metadata list (always 1440 entries, one per minute)
        metadata_list = []
        for i in range(1440):
            if day_metadata and day_metadata.minutes:
                meta = day_metadata.minutes.get(str(i), {'valid': False})
            else:
                meta = {'valid': False}
            metadata_list.append(meta)
        
        # Parse date for display
        date_obj = datetime.strptime(date_str, '%Y%m%d').replace(tzinfo=timezone.utc)
        
        # Generate combined spectrogram + power chart
        # Filename: {date}_spectrogram.png (matches web-ui/monitoring-server-v3.js expectations)
        output_path = self.output_dir / f'{date_str}_spectrogram.png'
        
        self._generate_daily_combined(
            iq_data=iq_data,
            output_path=output_path,
            date_obj=date_obj,
            metadata_list=metadata_list,
            day_summary=day_metadata.to_dict().get('summary', {}) if day_metadata else {}
        )
        
        return output_path
    
    def _generate_spectrogram(
        self,
        iq_data: np.ndarray,
        output_path: Path,
        title: str,
        start_time: datetime,
        metadata_list: List[Dict]
    ):
        """Generate a standard spectrogram image."""
        fig, ax = plt.subplots(figsize=self.config.figsize_wide)
        
        # Compute spectrogram
        f, t, Sxx = signal.spectrogram(
            iq_data,
            fs=SAMPLE_RATE,
            window='blackman',  # Better sidelobe suppression than default Hann
            nperseg=self.config.nfft,
            noverlap=self.config.noverlap,
            mode='magnitude',
            return_onesided=False
        )
        
        # Shift frequencies to center (FFT gives 0 to fs, we want -fs/2 to fs/2)
        f = np.fft.fftshift(f)
        Sxx = np.fft.fftshift(Sxx, axes=0)
        
        # Convert to dB (relative to peak)
        Sxx_db = 20 * np.log10(np.abs(Sxx) + 1e-10)
        Sxx_db -= np.max(Sxx_db)  # Normalize to peak
        
        # Create time axis
        duration_hours = len(iq_data) / SAMPLE_RATE / 3600
        t_hours = t / 3600
        
        # Plot
        im = ax.pcolormesh(
            t_hours, f, Sxx_db,
            shading='auto',
            cmap=self.config.cmap,
            vmin=self.config.vmin_db,
            vmax=self.config.vmax_db
        )
        
        # Mark gaps if configured
        if self.config.show_gaps and metadata_list:
            self._overlay_gaps(ax, metadata_list, duration_hours)
        
        ax.set_xlabel('Time (hours from start)')
        ax.set_ylabel('Frequency Offset (Hz)')
        ax.set_title(title)
        ax.set_ylim(-5, 5)  # ±5 Hz around carrier
        
        plt.colorbar(im, ax=ax, label='Power (dB rel. peak)')
        
        # Add timestamp
        timestamp_str = datetime.now(tz=timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
        ax.text(0.99, 0.01, f'Generated: {timestamp_str}',
                transform=ax.transAxes, fontsize=8, ha='right', va='bottom',
                color='white', alpha=0.7)
        
        plt.tight_layout()
        plt.savefig(output_path, dpi=self.config.dpi, facecolor='white')
        plt.close(fig)
        
        logger.info(f"Generated: {output_path}")
    
    def _generate_daily_combined(
        self,
        iq_data: np.ndarray,
        output_path: Path,
        date_obj: datetime,
        metadata_list: List[Dict],
        day_summary: Dict
    ):
        """
        Generate combined spectrogram + power chart for a full day.
        
        Layout:
        - Top panel: Power graph with solar zenith overlay
        - Bottom panel: Spectrogram
        - Both panels share same time axis (0-24 UTC hours)
        """
        # Get solar zenith data for overlay
        date_str = date_obj.strftime('%Y%m%d')
        solar_data = self._get_solar_zenith_data(date_str)
        
        # Use GridSpec to ensure both plots have IDENTICAL width
        # Layout: [power plot  ] [empty]
        #         [spectrogram ] [colorbar]
        fig = plt.figure(figsize=self.config.figsize_daily)
        gs = fig.add_gridspec(2, 2, width_ratios=[20, 1], height_ratios=[1, 3],
                              hspace=0.05, wspace=0.02)
        
        ax_power = fig.add_subplot(gs[0, 0])
        ax_spec = fig.add_subplot(gs[1, 0], sharex=ax_power)
        ax_cbar = fig.add_subplot(gs[1, 1])  # Dedicated colorbar axes
        
        # --- Power Profile (top) with Solar Zenith Overlay ---
        # ALWAYS 1440 minutes (24 hours) - gaps show as NaN (empty)
        n_minutes = 1440
        power_db = np.full(n_minutes, np.nan)  # Start with NaN (gaps)
        quality_colors = []
        
        for i in range(n_minutes):
            start = i * SAMPLES_PER_MINUTE
            end = start + SAMPLES_PER_MINUTE
            
            if end <= len(iq_data):
                minute_data = iq_data[start:end]
                
                # Only calculate power if data exists (not all zeros)
                if not np.all(minute_data == 0):
                    power_db[i] = 10 * np.log10(np.mean(np.abs(minute_data)**2) + 1e-10)
                # else: leave as NaN (gap)
            
            # Get quality color (check metadata validity)
            if i < len(metadata_list):
                meta = metadata_list[i]
                if meta.get('valid', False):
                    grade = meta.get('quality_grade', 'D')
                    quality_colors.append(self._grade_to_color(grade))
                else:
                    quality_colors.append('lightgray')  # Invalid/gap
            else:
                quality_colors.append('lightgray')
        
        hours = np.arange(n_minutes) / 60  # Always 0-24
        
        # Plot power with quality coloring
        if self.config.show_quality:
            for i in range(len(hours) - 1):
                ax_power.plot(hours[i:i+2], power_db[i:i+2], 
                             color=quality_colors[i], linewidth=1)
        else:
            ax_power.plot(hours, power_db, 'b-', linewidth=0.5)
        
        ax_power.set_ylabel('Power (dB)', color='black')
        ax_power.grid(True, alpha=0.3)
        # Hide x-axis labels on power plot (shared with spectrogram below)
        plt.setp(ax_power.get_xticklabels(), visible=False)
        
        # --- Solar Zenith Overlay on Secondary Y-axis ---
        if solar_data and self.solar_stations:
            ax_solar = ax_power.twinx()
            
            # Calculate hours for solar data (5-minute intervals = 288 points)
            solar_hours = np.arange(len(solar_data['wwv_solar_elevation'])) * solar_data['interval_minutes'] / 60
            
            legend_handles = []
            
            for station in self.solar_stations:
                if station == 'WWV':
                    elevations = solar_data['wwv_solar_elevation']
                    color = STATION_COLORS['WWV']
                    label = f'WWV (Ft. Collins)'
                elif station == 'WWVH':
                    elevations = solar_data['wwvh_solar_elevation']
                    color = STATION_COLORS['WWVH']
                    label = f'WWVH (Kauai)'
                elif station == 'CHU':
                    elevations = solar_data['chu_solar_elevation']
                    color = STATION_COLORS['CHU']
                    label = f'CHU (Ottawa)'
                elif station == 'BPM':
                    elevations = solar_data['bpm_solar_elevation']
                    color = STATION_COLORS['BPM']
                    label = f'BPM (China)'
                else:
                    continue
                
                # Plot solar elevation curve
                line, = ax_solar.plot(solar_hours, elevations, color=color, 
                                     linewidth=1.5, linestyle='--', alpha=0.8)
                legend_handles.append((line, label))
                
                # Fill daytime region (elevation > 0) with subtle shading
                ax_solar.fill_between(solar_hours, 0, elevations, 
                                     where=np.array(elevations) > 0,
                                     color=color, alpha=0.1)
            
            ax_solar.set_ylabel('Solar Elevation (°)', color='gray')
            ax_solar.tick_params(axis='y', labelcolor='gray')
            ax_solar.set_ylim(-90, 90)
            ax_solar.axhline(y=0, color='gray', linewidth=0.5, alpha=0.5)  # Horizon line
            
            # Add solar zenith legend
            if legend_handles:
                ax_solar.legend([h[0] for h in legend_handles], 
                              [h[1] for h in legend_handles],
                              loc='upper left', fontsize=7, framealpha=0.9)
        
        # Calculate valid minutes from metadata
        valid_count = sum(1 for m in metadata_list if m.get('valid', False))
        completeness_pct = (valid_count / 1440) * 100
        
        ax_power.set_title(
            f'{self.channel_name} - {date_obj.strftime("%Y-%m-%d")}  '
            f'[{valid_count}/1440 min, {completeness_pct:.1f}% complete]'
        )
        
        # --- Spectrogram (bottom) ---
        # Build validity mask from metadata (which minutes have actual data)
        valid_minutes = np.zeros(1440, dtype=bool)
        for i, meta in enumerate(metadata_list):
            if meta.get('valid', False):
                valid_minutes[i] = True
        
        # Interpolate zeros to reduce vertical bar artifacts
        # Zeros from gaps cause spectral leakage and dark vertical bands
        iq_interpolated = self._interpolate_zeros(iq_data)
        
        f, t, Sxx = signal.spectrogram(
            iq_interpolated,
            fs=SAMPLE_RATE,
            window='blackman',  # Better sidelobe suppression than default Hann
            nperseg=self.config.nfft,
            noverlap=self.config.noverlap,
            mode='magnitude',
            return_onesided=False
        )
        
        f = np.fft.fftshift(f)
        Sxx = np.fft.fftshift(Sxx, axes=0)
        
        Sxx_db = 20 * np.log10(np.abs(Sxx) + 1e-10)
        
        # Make writable copy (scipy returns read-only arrays)
        Sxx_db = Sxx_db.copy()
        
        # Create time-based mask: mask columns where corresponding minute is invalid
        # t is in seconds, convert to minute index
        t_minutes = (t / 60).astype(int)
        t_minutes = np.clip(t_minutes, 0, 1439)
        
        # Create column validity mask
        column_valid = valid_minutes[t_minutes]
        
        # Set invalid columns to NaN (simple loop is clearest)
        for t_idx in range(Sxx_db.shape[1]):
            if not column_valid[t_idx]:
                Sxx_db[:, t_idx] = np.nan
        
        # Normalize using 95th percentile instead of peak to avoid
        # single bright spots washing out the rest of the spectrogram
        if np.any(~np.isnan(Sxx_db)):
            ref_db = np.nanpercentile(Sxx_db, 95)
            Sxx_db = Sxx_db - ref_db  # Now 95th percentile maps to 0 dB
        
        t_hours = t / 3600
        
        # Create colormap with gray for NaN values
        cmap = plt.get_cmap(self.config.cmap).copy()
        cmap.set_bad(color='#c0c0c0')  # Gray for NaN/masked values
        
        im = ax_spec.pcolormesh(
            t_hours, f, Sxx_db,
            shading='auto',
            cmap=cmap,
            vmin=self.config.vmin_db,
            vmax=self.config.vmax_db
        )
        
        # Note: gaps are already shown as gray (NaN). Don't overlay red bands.
        
        ax_spec.set_xlabel('Time (UTC hours)')
        ax_spec.set_ylabel('Frequency Offset (Hz)')
        ax_spec.set_ylim(-5, 5)
        
        # CRITICAL: Set identical x-axis for BOTH plots (0-24 hours)
        ax_power.set_xlim(0, 24)
        ax_spec.set_xlim(0, 24)
        
        # Same hour ticks on both
        hour_ticks = list(range(0, 25, 2))
        ax_power.set_xticks(hour_ticks)
        ax_spec.set_xticks(hour_ticks)
        
        # Colorbar in dedicated axes (doesn't steal space from spectrogram)
        plt.colorbar(im, cax=ax_cbar, label='Power (dB rel. peak)')
        
        # Quality legend in corner
        if self.config.show_quality:
            self._add_quality_legend(fig)
        
        # Add generation timestamp
        timestamp_str = datetime.now(tz=timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
        fig.text(0.99, 0.01, f'Generated: {timestamp_str}',
                fontsize=7, ha='right', va='bottom', color='gray')
        
        # Save (no tight_layout needed - GridSpec handles spacing)
        plt.savefig(output_path, dpi=self.config.dpi, facecolor='white', bbox_inches='tight')
        plt.close(fig)
        
        logger.info(f"Generated daily: {output_path}")
    
    def _overlay_gaps(self, ax, metadata_list: List[Dict], duration_hours: float):
        """Overlay gap indicators on spectrogram."""
        for i, meta in enumerate(metadata_list):
            if not meta.get('valid', False) or meta.get('gap_samples', 0) > 100:
                # Mark this minute as gap
                hour = i / 60
                if hour < duration_hours:
                    ax.axvspan(hour, hour + 1/60, alpha=0.3, color='red', zorder=10)
    
    def _grade_to_color(self, grade: str) -> str:
        """Map quality grade to color."""
        colors = {
            'A': '#2ecc71',  # Green
            'B': '#3498db',  # Blue
            'C': '#f39c12',  # Orange
            'D': '#e74c3c',  # Red
            'X': '#95a5a6',  # Gray
        }
        return colors.get(grade, '#95a5a6')
    
    def _add_quality_legend(self, fig):
        """Add quality grade legend to figure."""
        from matplotlib.patches import Patch
        
        legend_elements = [
            Patch(facecolor='#2ecc71', label='A: Excellent'),
            Patch(facecolor='#3498db', label='B: Good'),
            Patch(facecolor='#f39c12', label='C: Fair'),
            Patch(facecolor='#e74c3c', label='D: Poor'),
            Patch(facecolor='#95a5a6', label='X: No data'),
        ]
        
        fig.legend(handles=legend_elements, loc='upper right', 
                  fontsize=8, title='Quality', ncol=5,
                  bbox_to_anchor=(0.98, 0.98))
    
    def generate_all_rolling(self) -> List[Path]:
        """Generate all rolling spectrograms (6h, 12h, 24h)."""
        paths = []
        for hours in [6, 12, 24]:
            path = self.generate_rolling(hours)
            if path:
                paths.append(path)
        return paths
    
    def get_output_paths(self) -> Dict[str, Path]:
        """Get paths to expected output files."""
        return {
            'rolling_6h': self.output_dir / 'rolling_6h.png',
            'rolling_12h': self.output_dir / 'rolling_12h.png',
            'rolling_24h': self.output_dir / 'rolling_24h.png',
        }


def generate_all_channel_spectrograms(
    data_root: Path,
    channels: Optional[List[str]] = None,
    receiver_grid: str = '',
    hours: int = 6,
    date_str: Optional[str] = None
) -> Dict[str, Optional[Path]]:
    """
    Generate spectrograms for multiple channels.
    
    Args:
        data_root: Root data directory
        channels: List of channel names (None = auto-discover)
        receiver_grid: Maidenhead grid square for solar zenith
        hours: Hours to include in rolling spectrogram
        date_str: If provided, generate daily spectrogram for this date
        
    Returns:
        Dict mapping channel name to output path (or None if failed)
    """
    if channels is None:
        # Auto-discover from products directory (Phase 3 derived products)
        products_dir = Path(data_root) / 'products'
        channels = []
        if products_dir.exists():
            for d in products_dir.iterdir():
                if d.is_dir() and (d / 'decimated').exists():
                    channels.append(d.name.replace('_', ' '))
    
    results = {}
    for channel in channels:
        try:
            gen = CarrierSpectrogramGenerator(data_root, channel, receiver_grid=receiver_grid)
            if date_str:
                path = gen.generate_daily(date_str)
            else:
                path = gen.generate_rolling(hours)
            results[channel] = path
        except Exception as e:
            logger.error(f"Error generating spectrogram for {channel}: {e}")
            results[channel] = None
    
    return results


# CLI interface
if __name__ == '__main__':
    import argparse
    
    parser = argparse.ArgumentParser(
        description='Generate carrier spectrograms from decimated buffer'
    )
    parser.add_argument('--data-root', type=Path, required=True,
                       help='Root data directory')
    parser.add_argument('--channel', type=str,
                       help='Channel name (e.g., "SHARED 10000")')
    parser.add_argument('--all-channels', action='store_true',
                       help='Generate for all channels')
    parser.add_argument('--hours', type=int, default=6,
                       help='Hours for rolling spectrogram (default: 6)')
    parser.add_argument('--date', type=str,
                       help='Generate daily spectrogram for date (YYYYMMDD)')
    parser.add_argument('--grid', type=str, default='',
                       help='Receiver grid square for solar zenith overlay (e.g., EM38ww)')
    
    args = parser.parse_args()
    
    logging.basicConfig(level=logging.INFO)
    
    # Handle special date keywords
    if args.date:
        if args.date.lower() == 'today':
            args.date = datetime.now(timezone.utc).strftime('%Y%m%d')
        elif args.date.lower() == 'yesterday':
            args.date = (datetime.now(timezone.utc) - timedelta(days=1)).strftime('%Y%m%d')
    
    if args.all_channels:
        results = generate_all_channel_spectrograms(
            data_root=args.data_root,
            receiver_grid=args.grid,
            hours=args.hours,
            date_str=args.date
        )
        for channel, path in results.items():
            if path:
                print(f"✅ {channel}: {path}")
            else:
                print(f"❌ {channel}: failed")
    elif args.channel:
        gen = CarrierSpectrogramGenerator(args.data_root, args.channel, receiver_grid=args.grid)
        if args.date:
            path = gen.generate_daily(args.date)
        else:
            path = gen.generate_rolling(args.hours)
        if path:
            print(f"Generated: {path}")
        else:
            print("Generation failed")
    else:
        parser.print_help()

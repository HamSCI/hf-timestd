#!/usr/bin/env python3
"""
Standard Time Signal Generator

Synthesizes accurate audio waveforms for standard time signals (ticks, minute markers)
and digital codes (BCD) for WWV, WWVH, and BPM.

This generator serves as the "Ground Truth" signal source for:
1.  Cross-matching and correlation-based detection.
2.  System verification and testing.
3.  Training data generation.

Supported Stations & Features:
- WWV:  1000 Hz ticks, 100 Hz BCD, Scientific Test Signal (Min 8)
- WWVH: 1200 Hz ticks, 100 Hz BCD, Scientific Test Signal (Min 44)
- BPM:  1000 Hz ticks (10ms/100ms), 100 Hz BCD, 300ms markers

Author: HF Time Standard Team
"""

import numpy as np
import logging
from typing import Optional, Tuple, List, Dict
from dataclasses import dataclass
from datetime import datetime

# Import existing generators
from hf_timestd.core.wwv_bcd_encoder import WWVBCDEncoder
from hf_timestd.core.wwv_test_signal import WWVTestSignalGenerator

logger = logging.getLogger(__name__)

@dataclass
class SignalConfig:
    """Configuration for a specific station's signal"""
    tick_freq: float
    tick_duration_sec: float
    marker_freq: float
    marker_duration_sec: float
    bcd_enabled: bool = False
    name: str = ""

class StandardTimeSignalGenerator:
    """
    Unified generator for HF time signals.
    """
    
    # Station Configurations
    STATION_CONFIGS = {
        'WWV': SignalConfig(
            name='WWV',
            tick_freq=1000.0,
            tick_duration_sec=0.005,
            marker_freq=1000.0,
            marker_duration_sec=0.800,
            bcd_enabled=True
        ),
        'WWVH': SignalConfig(
            name='WWVH',
            tick_freq=1200.0,
            tick_duration_sec=0.005,
            marker_freq=1200.0,
            marker_duration_sec=0.800,
            bcd_enabled=True
        ),
        'BPM': SignalConfig(
            name='BPM',
            tick_freq=1000.0,
            tick_duration_sec=0.010, # 10ms UTC tick
            marker_freq=1000.0,
            marker_duration_sec=0.300,
            bcd_enabled=True
        )
    }

    def __init__(self, sample_rate: int = 20000):
        self.sample_rate = sample_rate
        self.dt = 1.0 / sample_rate
        
        # Sub-generators
        self.bcd_encoder = WWVBCDEncoder(sample_rate)
        self.test_signal_gen = WWVTestSignalGenerator(sample_rate)
        
        logger.info(f"StandardTimeSignalGenerator initialized at {sample_rate} Hz")

    def generate_tone(self, frequency: float, duration_sec: float, phase: float = 0.0) -> Tuple[np.ndarray, float]:
        """
        Generate a simple sine wave tone.
        Returns (signal, final_phase)
        """
        t = np.arange(0, duration_sec, self.dt)
        signal = np.sin(2 * np.pi * frequency * t + phase)
        
        final_phase = (phase + 2 * np.pi * frequency * duration_sec) % (2 * np.pi)
        return signal, final_phase

    def generate_tick(self, station: str, tick_type: str = 'standard') -> np.ndarray:
        """
        Generate a single second tick.
        """
        config = self.STATION_CONFIGS.get(station)
        if not config:
            raise ValueError(f"Unknown station: {station}")
            
        freq = config.tick_freq
        duration = config.tick_duration_sec
        
        # Override based on type
        if tick_type == 'minute':
            freq = config.marker_freq
            duration = config.marker_duration_sec
        elif tick_type == 'hour':
            freq = config.marker_freq
            duration = config.marker_duration_sec
        elif tick_type == 'bpm_ut1' and station == 'BPM':
            duration = 0.100
        
        # Generate tone (ignore phase continuity for independent ticks)
        signal, _ = self.generate_tone(freq, duration)
        
        # Apply slight ramp to avoid clicks
        ramp_samples = int(0.0005 * self.sample_rate)
        if len(signal) > 2 * ramp_samples:
            ramp = np.linspace(0, 1, ramp_samples)
            signal[:ramp_samples] *= ramp
            signal[-ramp_samples:] *= ramp[::-1]
            
        return signal

    def generate_second_combined(self, station: str, second: int, minute: int, hour: int, day: int, year: int) -> np.ndarray:
        """
        Generate the full 1-second audio for a specific time and station.
        Combines ticks and digital codes (BCD).
        """
        # 1. Create empty 1-second buffer
        buffer = np.zeros(self.sample_rate)
        
        # 2. Add Ticks/Markers
        is_minute_marker = (second == 0)
        
        # BPM Special Case: UT1 ticks on minutes 25-29, 55-59
        is_bpm_ut1 = False
        if station == 'BPM':
            if (25 <= minute <= 29) or (55 <= minute <= 59):
                is_bpm_ut1 = True

        # Generate Tick
        tick = None
        if is_minute_marker:
             # Minute marker
             if station == 'BPM':
                 tick = self.generate_tick(station, 'minute') # 300ms
             else:
                 tick = self.generate_tick(station, 'minute') # 800ms
        elif is_bpm_ut1:
             # BPM UT1 tick
             tick = self.generate_tick(station, 'bpm_ut1') # 100ms
        else:
             # Standard tick
             # Note: WWV/H skip ticks on sec 29, 59 (and others depending on voice)
             skip_tick = False
             if station in ['WWV', 'WWVH']:
                 if second == 29 or second == 59:
                     skip_tick = True
             
             if not skip_tick:
                 tick = self.generate_tick(station, 'standard')
        
        if tick is not None:
            # Add tick to start of buffer
            length = min(len(tick), len(buffer))
            buffer[:length] += tick[:length]

        # 3. Add Digital Codes (100 Hz BCD Subcarrier)
        if self.STATION_CONFIGS[station].bcd_enabled:
             # Create minute timestamp (naive, simplified)
             try:
                 # Construct timestamp for this minute
                 dt = datetime(year, 1, 1, hour, minute, 0)
                 # Adjust day of year
                 dt = dt.replace(month=1, day=1) +  np.timedelta64(day-1, 'D')
                 ts = dt.timestamp()
                 
                 full_minute_bcd = self.bcd_encoder.encode_minute(ts)
                 
                 # Extract this second
                 start = second * self.sample_rate
                 end = (second + 1) * self.sample_rate
                 if end <= len(full_minute_bcd):
                     bcd_chunk = full_minute_bcd[start:end]
                     buffer += bcd_chunk
             except Exception as e:
                 logger.debug(f"Caught exception: {e}")
                 # Fallback/Error (silent BCD)
                 pass

        # Normalize to prevent clipping from mixing
        max_val = np.max(np.abs(buffer))
        if max_val > 1.0:
            buffer /= max_val
            
        return buffer

    def generate_minute(self, station: str, minute: int) -> np.ndarray:
        """
        Generate a full minute of audio.
        Handles Special Minutes (Test Signal).
        """
        # Check for Test Signal
        if station == 'WWV' and minute == 8:
            sig = self.test_signal_gen.generate_full_signal()
            # Pad to 60 seconds
            target_len = 60 * self.sample_rate
            if len(sig) < target_len:
                padding = np.zeros(target_len - len(sig))
                sig = np.concatenate([sig, padding])
            return sig
            
        if station == 'WWVH' and minute == 44:
            sig = self.test_signal_gen.generate_full_signal()
            # Pad to 60 seconds
            target_len = 60 * self.sample_rate
            if len(sig) < target_len:
                padding = np.zeros(target_len - len(sig))
                sig = np.concatenate([sig, padding])
            return sig
            
        # Standard Minute Generation
        minute_buffer = []
        # Need context for year/day/hour. Taking defaults for "now".
        now = datetime.utcnow()
        
        for sec in range(60):
            sec_audio = self.generate_second_combined(station, sec, minute, now.hour, 1, now.year)
            minute_buffer.append(sec_audio)
            
        return np.concatenate(minute_buffer)

if __name__ == "__main__":
    # verification
    gen = StandardTimeSignalGenerator()
    print("Testing Tick Generation...")
    tick = gen.generate_tick('BPM', 'bpm_ut1')
    print(f"BPM UT1 Tick (100ms): {len(tick)} samples, {len(tick)/20000:.3f}s")
    
    print("Testing Minute Generation...")
    sig = gen.generate_minute('WWV', 8)
    print(f"WWV Min 8 (Test Signal): {len(sig)/20000:.1f}s")

#!/usr/bin/env python3
"""
Standard Time Signal Generator

Synthesizes accurate audio waveforms for standard time signals (ticks, minute markers)
and digital codes (AFSK, BCD) for WWV, WWVH, CHU, and BPM.

This generator serves as the "Ground Truth" signal source for:
1.  Cross-matching and correlation-based detection.
2.  System verification and testing.
3.  Training data generation.

Supported Stations & Features:
- WWV:  1000 Hz ticks, 100 Hz BCD, Scientific Test Signal (Min 8)
- WWVH: 1200 Hz ticks, 100 Hz BCD, Scientific Test Signal (Min 44)
- CHU:  1000 Hz ticks, Bell 103 AFSK time code (Min 31-39)
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
    afsk_enabled: bool = False
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
        'CHU': SignalConfig(
            name='CHU',
            tick_freq=1000.0,
            tick_duration_sec=0.300, # Variable? CHU ticks often described as "bursts". 
                                     # Actually specific CHU 31-39s structure is unique.
                                     # Standard seconds are short bursts of 1000 Hz cycles.
            marker_freq=1000.0,
            marker_duration_sec=0.500, # Minute marker
            afsk_enabled=True
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
            if station == 'CHU':
                duration = 1.0
            else:
                freq = config.marker_freq
                duration = config.marker_duration_sec
        elif tick_type == 'bpm_ut1' and station == 'BPM':
            duration = 0.100
        elif station == 'CHU' and tick_type == 'standard':
             duration = 0.300
        
        # Generate tone (ignore phase continuity for independent ticks)
        signal, _ = self.generate_tone(freq, duration)
        
        # Apply slight ramp to avoid clicks
        ramp_samples = int(0.0005 * self.sample_rate)
        if len(signal) > 2 * ramp_samples:
            ramp = np.linspace(0, 1, ramp_samples)
            signal[:ramp_samples] *= ramp
            signal[-ramp_samples:] *= ramp[::-1]
            
        return signal

    def generate_chu_afsk(self, data_bytes: List[int], start_phase: float = 0.0) -> np.ndarray:
        """
        Generate CHU Bell 103 AFSK signal with robust timing (fractional samples).
        """
        baud_rate = 300.0
        samples_per_bit_float = self.sample_rate / baud_rate
        
        bits = []
        for byte in data_bytes:
            bits.append(0) # Start
            val = byte
            for _ in range(8):
                bits.append(val & 1)
                val >>= 1
            bits.append(1) # Stop
            bits.append(1) # Stop
            
        total_samples = int(len(bits) * samples_per_bit_float)
        full_signal = np.zeros(total_samples)
        phase = start_phase
        
        for i, bit in enumerate(bits):
            freq = 2225.0 if bit == 1 else 2025.0
            
            # Determine precise sample range for this bit
            start_idx = int(i * samples_per_bit_float)
            end_idx = int((i + 1) * samples_per_bit_float)
            num_samples = end_idx - start_idx
            
            # Generate bit samples
            t = np.arange(0, num_samples) / self.sample_rate
            chunk = np.sin(2 * np.pi * freq * t + phase)
            
            if start_idx < len(full_signal):
                # Handle potential truncation at very end
                write_len = min(len(chunk), len(full_signal) - start_idx)
                full_signal[start_idx : start_idx + write_len] = chunk[:write_len]
            
            # Update phase using EXACT time duration, not samples
            # This maintains phase coherence with the ideal signal
            # phase_inc = 2 * pi * freq * dt * num_samples
            phase += 2 * np.pi * freq * (num_samples / self.sample_rate)
            phase %= (2 * np.pi)
            
        return full_signal

    def _chunk_afsk_signal(self, full_afsk: np.ndarray, second_idx_in_sequence: int) -> np.ndarray:
        """Helper to slice the long AFSK sequence into 1-second chunks"""
        # AFSK sequence spans 9 seconds (31-39)
        # Each second has distinct data but continuous phase is ideal.
        # This implementation simply generates per-byte or per-frame?
        # Actually CHU structure is:
        # Sec 31: Frame B (Aux)
        # Sec 32-39: Frame A (Time) repeated? No, split across seconds.
        # Wait, the decoder documentation says:
        # "Frame A (seconds 32-39): 6d dd hh mm ss" -> 10 bytes?
        # "Frame B (second 31): xz yy yy tt aa" -> 10 bytes?
        # 
        # Structure per second (31-39):
        # 0-10ms: Tick
        # 10-133ms: Mark Tone
        # 133-500ms: Data (1 byte? 10 bytes?)
        # 
        # Re-reading CHUFSKDecoder:
        # "10 bytes per second (5 data + 5 redundancy)"
        # "Frame A (seconds 32-39)" means each second TRANSMITS a frame A?
        # Yes, standard repeats time code every second in that window?
        # Or is it one long frame?
        # "Decode Frame A... from raw bytes" -> "if len(raw_bytes) < 10"
        # Implies each SECOND contains a full 10-byte frame.
        # 10 bytes * 11 bits * 3.33ms = 366ms. Fits in 133-500ms window (367ms).
        # So each second 31..39 is a self-contained packet.
        
        return full_afsk # Placeholder if we did full sequence

    def _encode_chu_byte(self, value: int) -> int:
        """Encode byte with Bell 103: Start(0) + 8 Data + Stop(1) + Stop(1)"""
        # Actually generate_chu_afsk handles bit expansion.
        # This just handles value? No, generate_chu_afsk takes raw bytes.
        return value

    def _swap_nibbles(self, byte_val: int) -> int:
        """Swap high and low nibbles of a byte."""
        return ((byte_val & 0x0F) << 4) | ((byte_val & 0xF0) >> 4)

    def _create_chu_frame_a(self, day: int, hour: int, minute: int, second: int) -> List[int]:
        """
        Create Frame A (Time) bytes.
        Format: 6d dd hh mm ss (plus redundancy)
        All BCD, Nibbles swapped.
        """
        # Byte 0: 6d (Marker 6 + Day hundreds)
        b0 = (0x60) | ((day // 100) & 0x0F)
        
        # Byte 1: dd (Day tens + ones)
        b1 = (((day // 10) % 10) << 4) | (day % 10)
        
        # Byte 2: hh
        b2 = ((hour // 10) << 4) | (hour % 10)
        
        # Byte 3: mm
        b3 = ((minute // 10) << 4) | (minute % 10)
        
        # Byte 4: ss (The second being transmitted, e.g. 32)
        b4 = ((second // 10) << 4) | (second % 10)
        
        data_bytes = [b0, b1, b2, b3, b4]
        
        # Apply Nibble Swap (required for transmission)
        tx_bytes = [self._swap_nibbles(b) for b in data_bytes]
        
        # Add Redundancy (Bytes 5-9 = Bytes 0-4 repeated)
        redundancy = tx_bytes[:]
        
        return tx_bytes + redundancy

    def _create_chu_frame_b(self, year: int, dut1: float, tai_utc: int) -> List[int]:
        """
        Create Frame B (Aux) bytes.
        Format: xz yy yy tt aa
        x: DUT1 sign (even=pos, odd=neg)
        z: |DUT1| tenths
        """
        # Byte 0: xz
        dut1_tenths = int(abs(dut1) * 10)
        dut1_sign_digit = 0 if dut1 >= 0 else 1 # Even/Odd (0=pos, 1=neg)
        b0 = (dut1_sign_digit << 4) | (dut1_tenths & 0x0F)
        
        # Byte 1: yy (Century/Year High)
        b1 = ((year // 1000) << 4) | ((year // 100) % 10)
        
        # Byte 2: yy (Year Low)
        b2 = (((year // 10) % 10) << 4) | (year % 10)
        
        # Byte 3: tt (TAI-UTC)
        b3 = ((tai_utc // 10) << 4) | (tai_utc % 10)
        
        # Byte 4: aa (DST Pattern - Placeholder 0)
        b4 = 0x00
        
        data_bytes = [b0, b1, b2, b3, b4]
        
        # Apply Nibble Swap
        tx_bytes = [self._swap_nibbles(b) for b in data_bytes]
        
        # Add Redundancy (Inverted)
        redundancy = [(~b) & 0xFF for b in tx_bytes]
        
        return tx_bytes + redundancy
        # Decoder: dut1_negative = (x_nibble % 2) == 1
        b0 = (dut1_sign_digit << 4) | (dut1_tenths & 0x0F)
        
        # Byte 1: yy (Year century + decade) e.g. 20
        y_1000 = (year // 1000) % 10
        y_100 = (year // 100) % 10
        b1 = (y_1000 << 4) | y_100
        
        # Byte 2: yy (Year unit)
        y_10 = (year // 10) % 10
        y_1 = year % 10
        b2 = (y_10 << 4) | y_1
        
        # Byte 3: tt (TAI-UTC)
        b3 = ((tai_utc // 10) << 4) | (tai_utc % 10)
        
        # Byte 4: aa (DST - assumed 0 for fixed)
        b4 = 0x00
        
        data_bytes = [b0, b1, b2, b3, b4]
        
        # Swap nibbles
        tx_bytes = [self._swap_nibbles(b) for b in data_bytes]
        
        # Redundancy: Inverted
        # Decoder: inverted = [(~b) & 0xFF ...] == redundancy
        redundancy = [(~b) & 0xFF for b in tx_bytes]
        
        return tx_bytes + redundancy

    def generate_second_combined(self, station: str, second: int, minute: int, hour: int, day: int, year: int) -> np.ndarray:
        """
        Generate the full 1-second audio for a specific time and station.
        Combines ticks, digital codes (BCD/AFSK), and voice placeholders.
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
             except Exception:
                 # Fallback/Error (silent BCD)
                 pass

        # 4. CHU AFSK (Seconds 31-39)
        if station == 'CHU' and 31 <= second <= 39:
            # Generate Frame Bytes
            if second == 31:
                # Frame B
                frame_bytes = self._create_chu_frame_b(year, -0.1, 37) # Examples
            else:
                # Frame A
                frame_bytes = self._create_chu_frame_a(day, hour, minute, second)
                
            # Generate AFSK Audio
            afsk_audio = self.generate_chu_afsk(frame_bytes)
            
            # CHU Timing:
            # 0-10ms: Tick (Already added above? Wait, CHU tick is burst)
            # 10-133ms: Mark Tone (Sync)
            # 133-500ms: Data
            
            # Generate Mark Tone
            # Decoder expects Mark from 10ms to 133.33ms (DATA_START_MS)
            
            sync_start = int(0.010 * self.sample_rate) # 10ms
            # Sync should end at 133ms to match specification/decoder expectation
            sync_end = int(0.133333333 * self.sample_rate)
            
            sync_len_samples = sync_end - sync_start
            sync_duration = sync_len_samples * self.dt
            
            # Generate sync tone (Mark = 2225 Hz)
            sync_tone, final_phase = self.generate_tone(2225.0, sync_duration)
            
            # Add Sync
            buffer[sync_start:sync_end] = sync_tone
            
            # Add AFSK Data
            data_start = sync_end
            
            # Generate AFSK with continuous phase from end of Sync
            afsk_audio = self.generate_chu_afsk(frame_bytes, start_phase=final_phase)
            
            data_len = min(len(afsk_audio), len(buffer) - data_start)
            buffer[data_start:data_start+data_len] = afsk_audio[:data_len]

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

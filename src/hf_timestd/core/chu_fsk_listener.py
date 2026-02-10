"""
CHU FSK Listener - Lightweight USB channel listener for CHU FSK decoding.

Creates USB-preset channels on CHU frequencies solely for FSK time code
decoding. These channels are NOT archived to disk — only the decoded
FSK data (DUT1, TAI-UTC, year, timing) is kept and correlated with the
corresponding IQ archive channel.

Architecture:
  radiod USB channel (12 kHz real audio)
    -> 60s ring buffer in memory
    -> CHUFSKDecoder each minute
    -> results passed to MetrologyEngine for the paired IQ channel
"""

import json
import logging
import threading
import time
import numpy as np
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger(__name__)

# Shared location for FSK results (readable by metrology service)
FSK_RESULTS_DIR = Path('/dev/shm/timestd/fsk_results')


# GPS epoch: 1980-01-06 00:00:00 UTC as Unix timestamp
GPS_EPOCH_UNIX = 315964800
GPS_LEAP_SECONDS = 18
BILLION = 1_000_000_000


class CHUFSKChannel:
    """One USB channel accumulating audio for FSK decode.

    Uses RTP timestamps from radiod's GPS-locked clock for sample-accurate
    minute alignment.

    Timing model:
      - GPS mapping (from ChannelInfo): UTC = gps_unix + (rtp - rtp_snap) / sr
      - Each callback updates (_head_rtp, _write_pos): the RTP timestamp of
        the last sample written and its ring-buffer position.
      - To extract audio at a given UTC: convert UTC→RTP, compute how many
        samples back from head, read from ring buffer.
    """

    def __init__(self, frequency_hz: int, description: str, iq_channel: str,
                 sample_rate: int = 12000):
        self.frequency_hz = frequency_hz
        self.description = description
        self.iq_channel = iq_channel  # paired IQ channel name
        self.sample_rate = sample_rate

        # Ring buffer: 75 seconds of float32 audio (extra margin)
        self._buf_len = int(sample_rate * 75)
        self._buf = np.zeros(self._buf_len, dtype=np.float32)
        self._write_pos = 0
        self._total_samples = 0
        self._lock = threading.Lock()

        # RTP-to-UTC mapping (set from ChannelInfo after channel creation)
        self._gps_time_unix: Optional[float] = None  # UTC at RTP_TIMESNAP
        self._rtp_timesnap: Optional[int] = None      # RTP counter at GPS_TIME

        # Updated every callback: RTP timestamp of the last written sample
        self._head_rtp: Optional[int] = None

        # Stream objects (set after channel creation)
        self.channel_info = None
        self.stream = None
        self.ssrc = None

    def set_timing(self, gps_time_ns: int, rtp_timesnap: int):
        """Set the authoritative RTP-to-UTC mapping from ChannelInfo."""
        self._gps_time_unix = (
            gps_time_ns + BILLION * (GPS_EPOCH_UNIX - GPS_LEAP_SECONDS)
        ) / BILLION
        self._rtp_timesnap = rtp_timesnap
        logger.debug(f"{self.description}: RTP timing set - "
                     f"GPS_UTC={self._gps_time_unix:.6f}, "
                     f"RTP_SNAP={rtp_timesnap}")

    def _utc_to_rtp(self, utc: float) -> int:
        """Convert a UTC timestamp to an RTP timestamp."""
        return self._rtp_timesnap + int(
            (utc - self._gps_time_unix) * self.sample_rate
        )

    # -- called from RadiodStream callback thread --
    def on_samples(self, samples: np.ndarray, quality):
        """Append samples from RadiodStream callback."""
        n = len(samples)
        real = samples.real if np.iscomplexobj(samples) else samples
        with self._lock:
            end = self._write_pos + n
            if end <= self._buf_len:
                self._buf[self._write_pos:end] = real
            else:
                first = self._buf_len - self._write_pos
                self._buf[self._write_pos:] = real[:first]
                self._buf[:n - first] = real[first:]
            self._write_pos = end % self._buf_len
            self._total_samples += n

            # RTP timestamp of the sample just past the last one written.
            # first_rtp_timestamp is the RTP of the very first delivered
            # sample; total_samples_delivered counts all samples so far.
            self._head_rtp = quality.first_rtp_timestamp + quality.total_samples_delivered

    def get_aligned_minute(self, minute_boundary: float) -> Optional[np.ndarray]:
        """Return 60s of audio aligned to a UTC minute boundary.

        Uses the RTP-to-UTC mapping from radiod's GPS-locked clock for
        sample-accurate alignment.

        Returns None if the buffer doesn't cover the full minute or if
        the RTP timing has not been established.
        """
        needed = self.sample_rate * 60
        with self._lock:
            if (self._total_samples < needed
                    or self._gps_time_unix is None
                    or self._head_rtp is None):
                return None

            # How many samples back from the head is the minute boundary?
            target_rtp = self._utc_to_rtp(minute_boundary)
            samples_back = self._head_rtp - target_rtp

            # The end of the minute is 60s later
            end_back = samples_back - needed

            logger.debug(
                f"{self.description}: align samples_back={samples_back} "
                f"({samples_back/self.sample_rate:.1f}s)"
            )

            # Sanity: both must be within the buffer
            if samples_back < 0 or samples_back > self._total_samples:
                logger.warning(f"{self.description}: samples_back={samples_back} out of range")
                return None
            if end_back < 0 and abs(end_back) > (self._total_samples - self._buf_len):
                logger.warning(f"{self.description}: data overwritten")
                return None

            # Map to ring-buffer positions (head is at _write_pos)
            start_ring = (self._write_pos - samples_back) % self._buf_len
            end_ring = (start_ring + needed) % self._buf_len
            if start_ring < end_ring:
                return self._buf[start_ring:end_ring].copy()
            else:
                return np.concatenate([
                    self._buf[start_ring:],
                    self._buf[:end_ring]
                ]).copy()


class CHUFSKListener:
    """
    Manages USB channels for CHU FSK decoding.

    Reads [recorder.chu_fsk] config, creates lightweight USB channels,
    and runs FSK decode each minute.  Results are stored for the
    MetrologyService to pick up and correlate with the IQ channel.
    """

    def __init__(self, config: dict, control):
        """
        Args:
            config: Full recorder config dict (contains 'chu_fsk' sub-dict)
            control: RadiodControl instance (shared with core recorder)
        """
        self.control = control
        self._running = False
        self._thread: Optional[threading.Thread] = None

        # Parse config
        fsk_cfg = config.get('chu_fsk', {})
        self.preset = fsk_cfg.get('preset', 'usb')
        self.sample_rate = int(fsk_cfg.get('sample_rate', 12000))
        self.agc = int(fsk_cfg.get('agc', 0))
        self.gain = float(fsk_cfg.get('gain', 0.0))

        # Map encoding string
        enc_str = fsk_cfg.get('encoding', 'F32').upper()
        from ka9q import Encoding
        self.encoding = {'F32': Encoding.F32, 'S16LE': Encoding.S16LE}.get(
            enc_str, Encoding.F32)

        # Build channel list
        self.channels: Dict[int, CHUFSKChannel] = {}
        for ch_spec in fsk_cfg.get('channels', []):
            freq = int(ch_spec['frequency_hz'])
            desc = ch_spec.get('description', f'CHU_{freq//1000}_FSK')
            iq_ch = ch_spec.get('iq_channel', '')
            self.channels[freq] = CHUFSKChannel(
                frequency_hz=freq,
                description=desc,
                iq_channel=iq_ch,
                sample_rate=self.sample_rate,
            )

        # FSK decoders (one per channel, created lazily)
        self._decoders: Dict[int, object] = {}

        # Latest results keyed by iq_channel name
        self._results: Dict[str, object] = {}
        self._results_lock = threading.Lock()

        logger.info(f"CHUFSKListener: {len(self.channels)} channels, "
                    f"preset={self.preset}, sr={self.sample_rate}")

    def start(self):
        """Create USB channels and start listening."""
        from ka9q import RadiodStream

        for freq, ch in self.channels.items():
            try:
                info = self.control.ensure_channel(
                    frequency_hz=freq,
                    preset=self.preset,
                    sample_rate=self.sample_rate,
                    agc_enable=self.agc,
                    gain=self.gain,
                    encoding=self.encoding,
                    timeout=10.0,
                )
                ch.channel_info = info
                ch.ssrc = info.ssrc
                ch.set_timing(info.gps_time, info.rtp_timesnap)

                stream = RadiodStream(info, on_samples=ch.on_samples)
                stream.start()
                ch.stream = stream

                logger.info(f"CHU FSK channel started: {ch.description} "
                            f"({freq/1e6:.3f} MHz) SSRC={info.ssrc:08x}")
            except Exception as e:
                logger.error(f"Failed to start FSK channel {freq}: {e}")

        # Start decode thread
        self._running = True
        self._thread = threading.Thread(
            target=self._decode_loop, daemon=True, name='chu-fsk-decode')
        self._thread.start()

    def stop(self):
        """Stop all streams and decode thread."""
        self._running = False
        for freq, ch in self.channels.items():
            if ch.stream:
                try:
                    ch.stream.stop()
                except Exception:
                    pass
        if self._thread:
            self._thread.join(timeout=5)

    def _decode_loop(self):
        """Run FSK decode at the top of each minute."""
        from hf_timestd.core.chu_fsk_decoder import CHUFSKDecoder

        # Create decoders
        for freq, ch in self.channels.items():
            self._decoders[freq] = CHUFSKDecoder(
                sample_rate=ch.sample_rate,
                channel_name=ch.description,
            )

        # Wait for first full minute of data
        time.sleep(5)

        while self._running:
            try:
                # Sleep until ~2s past the next minute boundary
                now = time.time()
                secs_into_minute = now % 60
                wait = 62 - secs_into_minute  # 2s past boundary
                if wait > 60:
                    wait -= 60
                time.sleep(wait)

                if not self._running:
                    break

                minute_boundary = (int(time.time()) // 60) * 60 - 60

                for freq, ch in self.channels.items():
                    audio = ch.get_aligned_minute(float(minute_boundary))
                    if audio is None:
                        logger.debug(f"{ch.description}: insufficient data for minute {minute_boundary}")
                        continue

                    decoder = self._decoders[freq]
                    try:
                        result = decoder.decode_minute(
                            audio,
                            float(minute_boundary),
                            is_audio=True,
                        )
                        logger.info(
                            f"{ch.description}: FSK decode "
                            f"detected={result.detected}, "
                            f"frames={result.frames_decoded}/9, "
                            f"conf={result.decode_confidence:.2f}"
                        )
                        if result.detected:
                            logger.info(
                                f"{ch.description}: DUT1={result.dut1_seconds}s, "
                                f"TAI-UTC={result.tai_utc}s, "
                                f"day={result.decoded_day} "
                                f"{result.decoded_hour}:{result.decoded_minute}"
                            )

                        # Store result keyed by IQ channel name
                        with self._results_lock:
                            self._results[ch.iq_channel] = result

                        # Write to shared JSON for metrology service
                        self._write_result_json(ch.iq_channel, result, minute_boundary)

                    except Exception as e:
                        logger.error(f"{ch.description}: FSK decode error: {e}",
                                     exc_info=True)

            except Exception as e:
                logger.error(f"CHU FSK decode loop error: {e}", exc_info=True)
                time.sleep(10)

    def _write_result_json(self, iq_channel: str, result, minute_boundary: int):
        """Write FSK result to shared JSON for metrology service."""
        try:
            FSK_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
            out = {
                'minute_boundary': minute_boundary,
                'iq_channel': iq_channel,
                'detected': result.detected,
                'frames_decoded': result.frames_decoded,
                'decode_confidence': result.decode_confidence,
                'decoded_day': result.decoded_day,
                'decoded_hour': result.decoded_hour,
                'decoded_minute': result.decoded_minute,
                'dut1_seconds': result.dut1_seconds,
                'tai_utc': result.tai_utc,
                'year': result.year,
                'timing_offset_ms': result.timing_offset_ms,
                'tick_timing_offset_ms': result.tick_timing_offset_ms,
                'tick_timing_count': result.tick_timing_count,
                'snr_db': result.snr_db,
                'frame_results': result.frame_results,
                'written_at': time.time(),
            }
            path = FSK_RESULTS_DIR / f'{iq_channel}.json'
            tmp = path.with_suffix('.tmp')
            with open(tmp, 'w') as f:
                json.dump(out, f)
            tmp.rename(path)
        except Exception as e:
            logger.warning(f"Failed to write FSK result JSON for {iq_channel}: {e}")

    def get_result(self, iq_channel_name: str):
        """Get latest FSK result for a given IQ channel. Thread-safe."""
        with self._results_lock:
            return self._results.get(iq_channel_name)

    def pop_result(self, iq_channel_name: str):
        """Get and clear latest FSK result for a given IQ channel."""
        with self._results_lock:
            return self._results.pop(iq_channel_name, None)

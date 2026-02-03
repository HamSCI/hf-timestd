#!/usr/bin/env python3
"""
Bootstrap Time Confirmation

Confirms the RTP-to-UTC mapping by decoding actual time from station broadcasts:
- WWV/WWVH: BCD time code on 100 Hz subcarrier
- CHU: FSK time code (seconds 31-39)

This is Phase 2 of bootstrap:
1. Phase 1 (clustering): Find minute boundary RTP timestamps via tone patterns
2. Phase 2 (confirmation): Decode actual UTC time to confirm which minute

The NTP system time provides an initial hypothesis, but decoded time from
the stations provides ground truth confirmation.

Author: HF Time Standard Team
"""

import numpy as np
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Dict, List, Tuple, Any
from enum import Enum

logger = logging.getLogger(__name__)


class ConfirmationSource(Enum):
    """Source of time confirmation"""
    NTP_ONLY = "ntp_only"           # Only NTP hypothesis, no decode
    CHU_FSK = "chu_fsk"             # CHU FSK time code decoded
    WWV_BCD = "wwv_bcd"             # WWV BCD time code decoded
    WWVH_BCD = "wwvh_bcd"           # WWVH BCD time code decoded
    MULTI_STATION = "multi_station" # Multiple stations agree


@dataclass
class TimeConfirmation:
    """Result of time confirmation attempt"""
    confirmed: bool = False
    source: ConfirmationSource = ConfirmationSource.NTP_ONLY
    
    # Decoded time (UTC)
    minute: Optional[int] = None
    hour: Optional[int] = None
    day_of_year: Optional[int] = None
    year: Optional[int] = None
    
    # NTP hypothesis for comparison
    ntp_minute: Optional[int] = None
    ntp_hour: Optional[int] = None
    
    # Confidence and quality
    confidence: float = 0.0
    sources_agreeing: int = 0
    
    # Details from each decoder
    chu_result: Optional[Any] = None
    wwv_result: Optional[Any] = None
    wwvh_result: Optional[Any] = None
    
    def matches_ntp(self) -> bool:
        """Check if decoded time matches NTP hypothesis"""
        if self.minute is None or self.ntp_minute is None:
            return False
        if self.hour is None or self.ntp_hour is None:
            return False
        return self.minute == self.ntp_minute and self.hour == self.ntp_hour
    
    def __str__(self):
        if not self.confirmed:
            return f"TimeConfirmation: NOT CONFIRMED (source={self.source.value})"
        return (f"TimeConfirmation: {self.hour:02d}:{self.minute:02d} "
                f"day={self.day_of_year} (source={self.source.value}, "
                f"conf={self.confidence:.2f}, agrees_with_ntp={self.matches_ntp()})")


class BootstrapTimeConfirmer:
    """
    Confirms time by decoding station broadcasts.
    
    Usage:
        confirmer = BootstrapTimeConfirmer(sample_rate=24000)
        
        # When we have a candidate minute boundary:
        result = confirmer.confirm_time(
            chu_samples=chu_iq,      # 60s of CHU IQ data
            wwv_samples=wwv_iq,      # 60s of WWV IQ data  
            ntp_time=system_time     # NTP hypothesis
        )
        
        if result.confirmed and result.matches_ntp():
            # Time confirmed! Lock the RTP-to-UTC mapping
            pass
    """
    
    def __init__(self, sample_rate: int = 24000):
        self.sample_rate = sample_rate
        
        # Lazy-load decoders to avoid circular imports
        self._chu_decoder = None
        self._wwv_decoder = None
        
        logger.info(f"BootstrapTimeConfirmer initialized (sample_rate={sample_rate})")
    
    @property
    def chu_decoder(self):
        """Lazy-load CHU FSK decoder"""
        if self._chu_decoder is None:
            from .chu_fsk_decoder import CHUFSKDecoder
            self._chu_decoder = CHUFSKDecoder(
                sample_rate=self.sample_rate,
                channel_name="CHU"
            )
        return self._chu_decoder
    
    @property
    def wwv_decoder(self):
        """Lazy-load WWV BCD decoder"""
        if self._wwv_decoder is None:
            from .wwv_bcd_decoder import WWVBCDDecoder
            self._wwv_decoder = WWVBCDDecoder(
                sample_rate=self.sample_rate,
                channel_name="WWV"
            )
        return self._wwv_decoder
    
    def confirm_time(
        self,
        ntp_time: float,
        chu_samples: Optional[np.ndarray] = None,
        wwv_samples: Optional[np.ndarray] = None,
        wwvh_samples: Optional[np.ndarray] = None,
    ) -> TimeConfirmation:
        """
        Attempt to confirm time from available station data.
        
        Args:
            ntp_time: Unix timestamp from NTP (hypothesis)
            chu_samples: 60 seconds of CHU IQ data (optional)
            wwv_samples: 60 seconds of WWV IQ data (optional)
            wwvh_samples: 60 seconds of WWVH IQ data (optional)
            
        Returns:
            TimeConfirmation with decoded time and confidence
        """
        result = TimeConfirmation()
        
        # Extract NTP hypothesis
        ntp_dt = datetime.fromtimestamp(ntp_time, tz=timezone.utc)
        result.ntp_minute = ntp_dt.minute
        result.ntp_hour = ntp_dt.hour
        
        decoded_times = []
        
        # Try CHU FSK decode
        if chu_samples is not None and len(chu_samples) >= 30 * self.sample_rate:
            try:
                import numpy as np
                iq_power_db = 10 * np.log10(np.mean(np.abs(chu_samples)**2) + 1e-10)
                logger.info(f"[CONFIRM] Attempting CHU FSK decode on {len(chu_samples)} samples, "
                           f"IQ_power={iq_power_db:.1f}dB, dtype={chu_samples.dtype}")
                chu_result = self.chu_decoder.decode_minute(
                    chu_samples, 
                    minute_boundary_unix=ntp_time
                )
                result.chu_result = chu_result
                
                logger.info(f"[CONFIRM] CHU FSK result: detected={chu_result.detected}, "
                           f"frames={chu_result.frames_decoded}/{chu_result.frames_total}, "
                           f"conf={chu_result.decode_confidence:.2f}")
                
                if chu_result.detected and chu_result.decoded_minute is not None:
                    decoded_times.append({
                        'source': ConfirmationSource.CHU_FSK,
                        'minute': chu_result.decoded_minute,
                        'hour': chu_result.decoded_hour,
                        'day': chu_result.decoded_day,
                        'confidence': chu_result.decode_confidence,
                    })
                    logger.info(f"[CONFIRM] CHU FSK decoded: "
                               f"{chu_result.decoded_hour:02d}:{chu_result.decoded_minute:02d} "
                               f"day={chu_result.decoded_day}")
            except Exception as e:
                import traceback
                logger.warning(f"[CONFIRM] CHU FSK decode failed: {e}\n{traceback.format_exc()}")
        
        # Try WWV BCD decode
        if wwv_samples is not None and len(wwv_samples) >= 30 * self.sample_rate:
            try:
                import numpy as np
                iq_power_db = 10 * np.log10(np.mean(np.abs(wwv_samples)**2) + 1e-10)
                logger.info(f"[CONFIRM] Attempting WWV BCD decode on {len(wwv_samples)} samples, "
                           f"IQ_power={iq_power_db:.1f}dB, dtype={wwv_samples.dtype}")
                wwv_result = self.wwv_decoder.decode_minute(wwv_samples)
                result.wwv_result = wwv_result
                
                logger.info(f"[CONFIRM] WWV BCD result: detected={wwv_result.detected}, "
                           f"markers={wwv_result.markers_found}/{wwv_result.markers_expected}, "
                           f"conf={wwv_result.decode_confidence:.2f}")
                
                if wwv_result.detected and wwv_result.decoded_minute is not None:
                    decoded_times.append({
                        'source': ConfirmationSource.WWV_BCD,
                        'minute': wwv_result.decoded_minute,
                        'hour': wwv_result.decoded_hour,
                        'day': wwv_result.decoded_day,
                        'year': wwv_result.decoded_year,
                        'confidence': wwv_result.decode_confidence,
                    })
                    logger.info(f"[CONFIRM] WWV BCD decoded: "
                               f"{wwv_result.decoded_hour:02d}:{wwv_result.decoded_minute:02d} "
                               f"day={wwv_result.decoded_day}")
            except Exception as e:
                logger.debug(f"WWV BCD decode failed: {e}")
        
        # Try WWVH BCD decode (same decoder, different samples)
        if wwvh_samples is not None and len(wwvh_samples) >= 30 * self.sample_rate:
            try:
                # Create separate decoder instance for WWVH
                from .wwv_bcd_decoder import WWVBCDDecoder
                wwvh_decoder = WWVBCDDecoder(
                    sample_rate=self.sample_rate,
                    channel_name="WWVH"
                )
                wwvh_result = wwvh_decoder.decode_minute(wwvh_samples)
                result.wwvh_result = wwvh_result
                
                if wwvh_result.detected and wwvh_result.decoded_minute is not None:
                    decoded_times.append({
                        'source': ConfirmationSource.WWVH_BCD,
                        'minute': wwvh_result.decoded_minute,
                        'hour': wwvh_result.decoded_hour,
                        'day': wwvh_result.decoded_day,
                        'year': wwvh_result.decoded_year,
                        'confidence': wwvh_result.decode_confidence,
                    })
                    logger.info(f"[CONFIRM] WWVH BCD decoded: "
                               f"{wwvh_result.decoded_hour:02d}:{wwvh_result.decoded_minute:02d} "
                               f"day={wwvh_result.decoded_day}")
            except Exception as e:
                logger.debug(f"WWVH BCD decode failed: {e}")
        
        # Determine confirmation result
        if not decoded_times:
            # No decodes succeeded
            result.source = ConfirmationSource.NTP_ONLY
            return result
        
        # Check for consensus among decoded times
        if len(decoded_times) >= 2:
            # Multiple sources - check if they agree
            consensus = self._find_consensus(decoded_times)
            if consensus:
                result.confirmed = True
                result.source = ConfirmationSource.MULTI_STATION
                result.minute = consensus['minute']
                result.hour = consensus['hour']
                result.day_of_year = consensus.get('day')
                result.year = consensus.get('year')
                result.confidence = consensus['confidence']
                result.sources_agreeing = consensus['count']
                logger.info(f"[CONFIRM] Multi-station consensus: "
                           f"{result.hour:02d}:{result.minute:02d} "
                           f"({result.sources_agreeing} sources agree)")
                return result
        
        # Single source or no consensus - use best decode
        best = max(decoded_times, key=lambda x: x['confidence'])
        
        # Single source confirmation requires high confidence
        if best['confidence'] >= 0.7:
            result.confirmed = True
            result.source = best['source']
            result.minute = best['minute']
            result.hour = best['hour']
            result.day_of_year = best.get('day')
            result.year = best.get('year')
            result.confidence = best['confidence']
            result.sources_agreeing = 1
        else:
            # Low confidence single source - not confirmed
            result.source = best['source']
            result.minute = best['minute']
            result.hour = best['hour']
            result.confidence = best['confidence']
        
        return result
    
    def _find_consensus(self, decoded_times: List[Dict]) -> Optional[Dict]:
        """
        Find consensus among multiple decoded times.
        
        Returns consensus if at least 2 sources agree on minute and hour.
        """
        if len(decoded_times) < 2:
            return None
        
        # Group by (minute, hour)
        groups = {}
        for dt in decoded_times:
            key = (dt['minute'], dt['hour'])
            if key not in groups:
                groups[key] = []
            groups[key].append(dt)
        
        # Find largest group
        best_key = max(groups.keys(), key=lambda k: len(groups[k]))
        best_group = groups[best_key]
        
        if len(best_group) < 2:
            return None
        
        # Build consensus result
        minute, hour = best_key
        confidences = [dt['confidence'] for dt in best_group]
        
        # Get day/year from highest confidence source
        best_source = max(best_group, key=lambda x: x['confidence'])
        
        return {
            'minute': minute,
            'hour': hour,
            'day': best_source.get('day'),
            'year': best_source.get('year'),
            'confidence': sum(confidences) / len(confidences),
            'count': len(best_group),
        }
    
    def confirm_from_buffers(
        self,
        ntp_time: float,
        buffer_manager,
        minute_rtp: int,
    ) -> TimeConfirmation:
        """
        Confirm time using data from bootstrap buffer manager.
        
        This is the main entry point for bootstrap integration.
        
        Args:
            ntp_time: Unix timestamp from NTP
            buffer_manager: BootstrapBufferManager with channel data
            minute_rtp: RTP timestamp of the candidate minute boundary
            
        Returns:
            TimeConfirmation result
        """
        samples_per_minute = 60 * self.sample_rate
        
        chu_samples = None
        wwv_samples = None
        wwvh_samples = None
        
        # Extract samples from each relevant channel
        for channel_name, buffer in buffer_manager.buffers.items():
            channel_upper = channel_name.upper()
            
            # Get samples around the minute boundary
            try:
                samples = buffer.get_samples_at_rtp(
                    minute_rtp, 
                    samples_per_minute
                )
                if samples is None or len(samples) < samples_per_minute * 0.5:
                    continue
                    
                if 'CHU' in channel_upper:
                    # Use highest frequency CHU channel (best SNR typically)
                    if chu_samples is None or len(samples) > len(chu_samples):
                        chu_samples = samples
                elif 'WWV' in channel_upper and 'WWVH' not in channel_upper:
                    # WWV channel (not WWVH)
                    if wwv_samples is None or len(samples) > len(wwv_samples):
                        wwv_samples = samples
                elif 'WWVH' in channel_upper or 'SHARED' in channel_upper:
                    # WWVH or shared frequency (could be either)
                    # For shared, we'd need discrimination first
                    pass
                    
            except Exception as e:
                logger.debug(f"Failed to get samples from {channel_name}: {e}")
        
        return self.confirm_time(
            ntp_time=ntp_time,
            chu_samples=chu_samples,
            wwv_samples=wwv_samples,
            wwvh_samples=wwvh_samples,
        )

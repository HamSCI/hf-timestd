#!/usr/bin/env python3
"""
Radiod Audio Client - AM Audio Channel Management for hf-timestd

Creates AM audio channels with AGC for listening to WWV/CHU signals.
SSRC allocation is handled automatically by ka9q.

Usage:
    # Create audio channel for WWV 10 MHz
    python radiod_audio_client.py --radiod-host bee1-hf-status.local \
        create --frequency 10000000
    
    # Stop audio channel
    python radiod_audio_client.py --radiod-host bee1-hf-status.local \
        stop --frequency 10000000
"""

import sys
import json
import argparse
import time
from typing import Dict

# Use ka9q library from venv (ka9q-python package)
try:
    from ka9q import RadiodControl, discover_channels
except ImportError as e:
    print(json.dumps({
        'success': False,
        'error': f'ka9q library not available: {e}',
        'detail': 'Ensure venv is activated with ka9q-python installed'
    }))
    sys.exit(1)


# Default audio sample rate for AM listening
AUDIO_SAMPLE_RATE = 12000

# Audio SSRC = frequency + 999 (legacy convention for audio channels)
AUDIO_SSRC_OFFSET = 999


def get_audio_ssrc(frequency_hz: float) -> int:
    """Calculate audio SSRC from frequency (legacy convention)."""
    return int(frequency_hz) + AUDIO_SSRC_OFFSET


def create_audio_channel(radiod_host: str, frequency_hz: float) -> Dict:
    """
    Create an AM audio channel with AGC for the specified frequency.
    
    Args:
        radiod_host: Radiod hostname (e.g., 'bee1-hf-status.local')
        frequency_hz: Center frequency in Hz
    
    Returns:
        Dict with channel information including ssrc for web-ui tracking
    """
    try:
        control = RadiodControl(radiod_host)
        
        # First check if an AM channel already exists for this frequency
        try:
            channels = discover_channels(radiod_host)
            for ssrc, ch in channels.items():
                if ch.preset == 'am' and abs(ch.frequency - frequency_hz) < 1000:
                    # AM channel already exists for this frequency
                    return {
                        'success': True,
                        'ssrc': ssrc,
                        'frequency_hz': ch.frequency,
                        'multicast_address': ch.multicast_address,
                        'port': ch.port,
                        'sample_rate': ch.sample_rate,
                        'preset': 'am',
                        'mode': 'existing'
                    }
        except Exception:
            pass
        
        # No existing channel - create new AM channel
        ssrc = control.create_channel(
            frequency_hz=frequency_hz,
            preset='am',
            sample_rate=AUDIO_SAMPLE_RATE,
            agc_enable=1
        )
        
        # Explicitly set preset to AM (in case create_channel didn't apply it)
        control.set_preset(ssrc, 'am')
        
        # Enable AGC with settings for comfortable listening
        control.set_agc(ssrc, enable=True, headroom=6.0)  # Target 6dB below full scale
        control.set_agc_threshold(ssrc, threshold_db=0.0)  # Activate above noise floor
        control.set_gain(ssrc, gain_db=40.0)  # Boost gain for low SNR signals
        
        # Wait for radiod to process
        time.sleep(0.3)
        
        # Try to discover channel info for multicast details
        try:
            channels = discover_channels(radiod_host)
            if ssrc in channels:
                ch = channels[ssrc]
                return {
                    'success': True,
                    'ssrc': ssrc,
                    'frequency_hz': ch.frequency,
                    'multicast_address': ch.multicast_address,
                    'port': ch.port,
                    'sample_rate': ch.sample_rate,
                    'preset': 'am',
                    'mode': 'created'
                }
        except Exception:
            pass
        
        # Return success with SSRC even if discovery failed
        return {
            'success': True,
            'ssrc': ssrc,
            'frequency_hz': frequency_hz,
            'sample_rate': AUDIO_SAMPLE_RATE,
            'preset': 'am',
            'mode': 'created'
        }
        
    except Exception as e:
        import traceback
        return {
            'success': False,
            'error': str(e),
            'traceback': traceback.format_exc(),
            'frequency_hz': frequency_hz
        }


def stop_audio_channel(radiod_host: str, frequency_hz: float) -> Dict:
    """
    Stop/delete an audio channel.
    
    Args:
        radiod_host: Radiod hostname
        frequency_hz: Frequency to stop
    
    Returns:
        Dict with success status
    """
    try:
        control = RadiodControl(radiod_host)
        
        # Find the channel by frequency and remove it
        channels = discover_channels(radiod_host, timeout=2.0)
        for ssrc, ch in channels.items():
            if abs(ch.frequency - frequency_hz) < 1000:  # Within 1 kHz
                control.remove_channel(ssrc)
                return {
                    'success': True,
                    'frequency_hz': frequency_hz,
                    'action': 'stopped'
                }
        
        return {
            'success': True,
            'frequency_hz': frequency_hz,
            'action': 'not_found'
        }
        
    except Exception as e:
        import traceback
        return {
            'success': False,
            'error': str(e),
            'traceback': traceback.format_exc()
        }


def list_audio_channels(radiod_host: str) -> Dict:
    """
    List all active AM audio channels.
    
    Args:
        radiod_host: Radiod hostname
    
    Returns:
        Dict with list of audio channels
    """
    try:
        channels = discover_channels(radiod_host, timeout=2.0)
        
        audio_channels = []
        for ssrc, ch in channels.items():
            # Check if this is an AM audio channel
            if ch.preset == 'am':
                audio_channels.append({
                    'frequency_hz': ch.frequency,
                    'frequency_mhz': ch.frequency / 1e6,
                    'multicast_address': ch.multicast_address,
                    'port': ch.port,
                    'sample_rate': ch.sample_rate
                })
        
        return {
            'success': True,
            'count': len(audio_channels),
            'channels': audio_channels
        }
        
    except Exception as e:
        return {
            'success': False,
            'error': str(e),
            'channels': []
        }


def main():
    parser = argparse.ArgumentParser(
        description='hf-timestd Audio Channel Manager - Creates AM channels with AGC for WWV/CHU listening'
    )
    parser.add_argument('--radiod-host', required=True, 
                       help='Radiod hostname (e.g., bee1-hf-status.local)')
    
    subparsers = parser.add_subparsers(dest='command', required=True)
    
    # create command
    create_parser = subparsers.add_parser('create',
        help='Create AM audio channel')
    create_parser.add_argument('--frequency', type=float, required=True,
        help='Center frequency in Hz (e.g., 10000000 for 10 MHz)')
    
    # Legacy alias for backward compatibility
    create_parser2 = subparsers.add_parser('get-or-create',
        help='Alias for create')
    create_parser2.add_argument('--frequency', type=float, required=True,
        help='Center frequency in Hz')
    
    # stop command
    stop_parser = subparsers.add_parser('stop',
        help='Stop/delete an audio channel')
    stop_parser.add_argument('--frequency', type=float, required=True,
        help='Center frequency in Hz')
    
    # list command
    list_parser = subparsers.add_parser('list',
        help='List active AM audio channels')
    
    args = parser.parse_args()
    
    try:
        if args.command in ('create', 'get-or-create'):
            result = create_audio_channel(args.radiod_host, args.frequency)
        elif args.command == 'stop':
            result = stop_audio_channel(args.radiod_host, args.frequency)
        elif args.command == 'list':
            result = list_audio_channels(args.radiod_host)
        
        print(json.dumps(result))
        return 0 if result.get('success', False) else 1
        
    except Exception as e:
        import traceback
        print(json.dumps({
            'success': False,
            'error': str(e),
            'traceback': traceback.format_exc()
        }))
        return 1


if __name__ == '__main__':
    sys.exit(main())

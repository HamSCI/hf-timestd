"""
Channel management for ka9q-radio

This module creates and configures channels in radiod using the TLV control protocol.
"""

import logging
import time
import subprocess
from typing import List, Dict, Optional
from ka9q import discover_channels, ChannelInfo, RadiodControl

logger = logging.getLogger(__name__)


def resolve_mdns_to_ip(name: str) -> Optional[str]:
    """
    Resolve an mDNS name to an IP address using avahi-resolve.
    
    Args:
        name: mDNS name like "bee1-hf-data.local"
        
    Returns:
        IP address string, or None if resolution fails
    """
    if not name:
        return None
    
    # If it's already an IP, return it
    if name.startswith('239.') or (name.count('.') == 3 and name.split('.')[0].isdigit()):
        return name.split(':')[0]
    
    # Try avahi-resolve for .local names
    if name.endswith('.local'):
        try:
            result = subprocess.run(
                ['avahi-resolve', '-n', name],
                capture_output=True,
                text=True,
                timeout=5
            )
            if result.returncode == 0 and result.stdout.strip():
                # Output format: "name\tIP"
                parts = result.stdout.strip().split()
                if len(parts) >= 2:
                    ip = parts[-1]
                    logger.info(f"Resolved {name} -> {ip}")
                    return ip
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            logger.warning(f"avahi-resolve failed for {name}: {e}")
    
    return None





class ChannelManager:
    """
    Manages channel creation and configuration for ka9q-radio
    
    Uses the TLV control protocol to send commands directly to radiod.
    """
    
    def __init__(self, status_address: str):
        """
        Initialize channel manager
        
        Args:
            status_address: mDNS name or IP:port of radiod status stream
        """
        self.status_address = status_address
        self.control = RadiodControl(status_address)
    
    def discover_existing_channels(self) -> Dict[int, ChannelInfo]:
        """
        Discover all existing channels from radiod
        
        Returns:
            Dictionary mapping SSRC to ChannelInfo
        """
        logger.info(f"Discovering existing channels from {self.status_address}")
        channels = discover_channels(self.status_address)
        logger.info(f"Found {len(channels)} existing channels")
        return channels
    
    def create_channel(self, frequency_hz: float, preset: str = "iq", 
                      sample_rate: Optional[int] = None, agc: int = 0, gain: float = 0.0,
                      destination: Optional[str] = None, ssrc: Optional[int] = None,
                      description: str = "") -> Optional[int]:
        """
        Create a new channel in radiod
        
        Args:
            frequency_hz: Frequency in Hz
            preset: Preset/mode (default: "iq")
            sample_rate: Sample rate in Hz (optional)
            agc: AGC enable (0=off, 1=on) (default: 0)
            gain: Manual gain in dB (default: 0.0)
            destination: RTP destination (mDNS or IP:port), e.g. "time-station-data.local"
            ssrc: Optional SSRC. If None, auto-allocated by ka9q-python
            description: Human-readable description (for logging)
        
        Returns:
            SSRC of created channel, or None if failed
        """
        ssrc_str = f"SSRC {ssrc}" if ssrc else "auto-SSRC"
        logger.info(f"🔧 create_channel() called for {frequency_hz/1e6:.3f} MHz ({ssrc_str})")
        try:
            # Resolve mDNS destination to IP if needed
            resolved_dest = None
            if destination:
                resolved_dest = resolve_mdns_to_ip(destination)
                if not resolved_dest:
                    logger.warning(f"Could not resolve destination '{destination}' - channel may use default")
            
            logger.info(
                f"Creating channel: freq={frequency_hz/1e6:.3f} MHz, "
                f"preset={preset}, rate={sample_rate}Hz, "
                f"agc={agc}, gain={gain}dB, "
                f"destination={destination} -> {resolved_dest}, "
                f"description='{description}'"
            )
            
            # Use radiod_control to create and configure channel
            # Returns the SSRC (auto-allocated if not provided)
            allocated_ssrc = self.control.create_channel(
                frequency_hz=frequency_hz,
                preset=preset,
                sample_rate=sample_rate,
                agc_enable=agc,
                gain=gain,
                destination=resolved_dest,  # Use resolved IP
                ssrc=ssrc
            )
            
            logger.info(f"Channel creation complete (SSRC={allocated_ssrc}), waiting 0.5s...")
            
            # Wait for radiod to process
            time.sleep(0.5)
            
            logger.info(f"Verifying channel {allocated_ssrc}...")
            
            # Verify the channel was created
            if self.control.verify_channel(allocated_ssrc, frequency_hz):
                logger.info(f"✓ Channel {allocated_ssrc} ({frequency_hz/1e6:.3f} MHz) created successfully")
                return allocated_ssrc
            else:
                logger.warning(f"✗ Channel {allocated_ssrc} verification failed")
                return None
                
        except Exception as e:
            logger.error(f"❌ EXCEPTION in create_channel({frequency_hz/1e6:.3f} MHz): {e}", exc_info=True)
            return None
    
    def ensure_channels_exist(self, required_channels: List[Dict], update_existing: bool = False) -> bool:
        """
        Ensure all required channels exist, creating missing ones and optionally updating existing ones
        
        Args:
            required_channels: List of channel specifications, each with:
                - ssrc: int
                - frequency_hz: float
                - preset: str (optional, default "iq")
                - sample_rate: int (optional)
                - description: str (optional)
            update_existing: If True, update existing channels if parameters differ
        
        Returns:
            True if all channels exist or were created successfully
        """
        logger.info(f"📋 ensure_channels_exist() called with {len(required_channels)} channels")
        logger.info(f"Required SSRCs: {[ch['ssrc'] for ch in required_channels]}")
        
        # Discover existing channels
        logger.info("Discovering existing channels...")
        existing = self.discover_existing_channels()
        existing_ssrcs = set(existing.keys())
        logger.info(f"Found {len(existing_ssrcs)} existing: {sorted(existing_ssrcs)}")
        
        # Check which channels need to be created
        required_ssrcs = {ch['ssrc'] for ch in required_channels}
        missing_ssrcs = required_ssrcs - existing_ssrcs
        logger.info(f"Missing SSRCs: {sorted(missing_ssrcs)}")
        
        # Check which existing channels need updates
        channels_to_update = []
        if update_existing:
            for channel_spec in required_channels:
                ssrc = channel_spec['ssrc']
                if ssrc in existing_ssrcs:
                    existing_ch = existing[ssrc]
                    req_freq = channel_spec['frequency_hz']
                    req_preset = channel_spec.get('preset', 'iq')
                    
                    # Check if frequency, preset, or sample_rate differs
                    freq_diff = abs(existing_ch.frequency - req_freq) > 1.0  # 1 Hz tolerance
                    preset_diff = existing_ch.preset != req_preset
                    req_sample_rate = channel_spec.get('sample_rate')
                    sample_rate_diff = req_sample_rate and existing_ch.sample_rate != req_sample_rate
                    
                    if freq_diff or preset_diff or sample_rate_diff:
                        reasons = []
                        if freq_diff:
                            reasons.append(f"freq={existing_ch.frequency/1e6:.3f}->{req_freq/1e6:.3f} MHz")
                        if preset_diff:
                            reasons.append(f"preset={existing_ch.preset}->{req_preset}")
                        if sample_rate_diff:
                            reasons.append(f"sample_rate={existing_ch.sample_rate}->{req_sample_rate} Hz")
                        logger.info(f"Channel {ssrc} needs update: {', '.join(reasons)}")
                        channels_to_update.append(channel_spec)
        
        if not missing_ssrcs and not channels_to_update:
            logger.info("✓ All required channels already exist with correct parameters")
            return True
        
        if missing_ssrcs:
            logger.info(f"⚙️  Need to create {len(missing_ssrcs)} missing channels: {sorted(missing_ssrcs)}")
        if channels_to_update:
            logger.info(f"Need to update {len(channels_to_update)} existing channels")
        
        # Create missing channels
        logger.info(f"🔄 Starting channel creation loop for {len(required_channels)} required channels")
        create_success = 0
        for channel_spec in required_channels:
            ssrc = channel_spec['ssrc']
            logger.info(f"  Loop iteration: SSRC {ssrc}")
            
            if ssrc not in missing_ssrcs:
                logger.info(f"    ↪️ SSRC {ssrc} already exists, skipping")
                continue  # Already exists
            
            logger.info(f"    ▶️  Calling create_channel() for SSRC {ssrc}")
            if self.create_channel(
                ssrc=ssrc,
                frequency_hz=channel_spec['frequency_hz'],
                preset=channel_spec.get('preset', 'iq'),
                sample_rate=channel_spec.get('sample_rate', 16000),
                agc=channel_spec.get('agc', 0),
                gain=channel_spec.get('gain', 0.0),
                description=channel_spec.get('description', '')
            ):
                create_success += 1
        
        # Update existing channels
        update_success = 0
        for channel_spec in channels_to_update:
            ssrc = channel_spec['ssrc']
            logger.info(f"Updating channel {ssrc}")
            
            # Update channel using create_channel (new ka9q-python API)
            # Note: create_channel updates if channel already exists
            try:
                self.control.create_channel(
                    ssrc=ssrc,
                    frequency_hz=channel_spec['frequency_hz'],
                    preset=channel_spec.get('preset', 'iq'),
                    sample_rate=channel_spec.get('sample_rate', 16000),
                    agc_enable=channel_spec.get('agc', 0),
                    gain=channel_spec.get('gain', 0.0)
                )
                time.sleep(0.5)
                
                if self.control.verify_channel(ssrc, channel_spec['frequency_hz']):
                    logger.info(f"✓ Channel {ssrc} updated successfully")
                    update_success += 1
                else:
                    logger.warning(f"✗ Channel {ssrc} update verification failed")
            except Exception as e:
                logger.error(f"Failed to update channel {ssrc}: {e}")
        
        # Report results
        total_operations = len(missing_ssrcs) + len(channels_to_update)
        total_success = create_success + update_success
        
        if total_success == total_operations:
            logger.info(f"✓ All {total_operations} channel operations successful")
            return True
        else:
            logger.warning(
                f"⚠ Only {total_success}/{total_operations} channel operations successful"
            )
            return False
    
    def ensure_channels_from_config(
        self, 
        channels: List[Dict], 
        defaults: Dict,
        destination: Optional[str] = None
    ) -> Dict[int, int]:
        """
        Ensure channels exist based on config format with auto-SSRC.
        
        Matches channels by frequency AND destination. Key behaviors:
        - If channel exists at freq with OUR destination: reuse or reconfigure
        - If channel exists at freq with DIFFERENT destination: create NEW channel
          (radiod supports multiple clients at same frequency)
        - If no channel at freq: create new channel
        
        Args:
            channels: List of channel specs, each with at least:
                - frequency_hz: float (required)
                - description: str (optional)
                - Can override any default parameter
            defaults: Default parameters for all channels:
                - preset: str (default "iq")
                - sample_rate: int (default 20000)
                - agc: int (default 0)
                - gain: float (default 0.0)
                - encoding: str (default "float")
            destination: RTP destination for all channels (our multicast IP)
        
        Returns:
            Dict mapping frequency_hz -> allocated SSRC
        """
        logger.info(f"📋 ensure_channels_from_config() with {len(channels)} channels")
        logger.info(f"  Destination: {destination}")
        logger.info(f"  Defaults: preset={defaults.get('preset', 'iq')}, "
                   f"sample_rate={defaults.get('sample_rate', 20000)}, "
                   f"encoding={defaults.get('encoding', 'float')}")
        
        # Resolve our destination to IP for comparison
        our_mcast_ip = resolve_mdns_to_ip(destination) if destination else None
        logger.info(f"  Our multicast IP: {our_mcast_ip}")
        
        # Discover existing channels
        existing = self.discover_existing_channels()
        
        # Build lookup by frequency
        # If multiple channels exist at same frequency, we pick the first one
        # This effectively enables channel sharing
        channels_by_freq: Dict[int, tuple] = {}  # freq_hz -> (ssrc, channel_info)
        
        for ssrc, ch in existing.items():
            freq_hz = int(round(ch.frequency))
            if freq_hz not in channels_by_freq:
                channels_by_freq[freq_hz] = (ssrc, ch)
            
        logger.info(f"  Found {len(channels_by_freq)} existing frequencies")
        
        # Process each required channel
        freq_to_ssrc: Dict[int, int] = {}
        success_count = 0
        
        for ch_spec in channels:
            freq_hz = int(ch_spec['frequency_hz'])
            description = ch_spec.get('description', f'{freq_hz/1e6:.3f} MHz')
            
            # Merge with defaults
            preset = ch_spec.get('preset', defaults.get('preset', 'iq'))
            sample_rate = ch_spec.get('sample_rate', defaults.get('sample_rate', 20000))
            agc = ch_spec.get('agc', defaults.get('agc', 0))
            gain = ch_spec.get('gain', defaults.get('gain', 0.0))
            encoding = ch_spec.get('encoding', defaults.get('encoding', 'float'))
            
            # Check if ANY channel exists at this frequency
            if freq_hz in channels_by_freq:
                existing_ssrc, existing_ch = channels_by_freq[freq_hz]
                
                # Check if parameters match
                params_match = (
                    existing_ch.preset == preset and
                    existing_ch.sample_rate == sample_rate
                )
                
                if params_match:
                    logger.info(f"✓ Channel {freq_hz/1e6:.3f} MHz exists (SSRC {existing_ssrc}) - reusing")
                    freq_to_ssrc[freq_hz] = existing_ssrc
                    success_count += 1
                else:
                    # Reconfigure existing channel
                    logger.info(f"⚙️ Reconfiguring channel {freq_hz/1e6:.3f} MHz: "
                               f"preset={existing_ch.preset}->{preset}, "
                               f"rate={existing_ch.sample_rate}->{sample_rate}")
                    allocated = self._reconfigure_channel(
                        ssrc=existing_ssrc,
                        frequency_hz=freq_hz,
                        preset=preset,
                        sample_rate=sample_rate,
                        encoding=encoding
                    )
                    if allocated:
                        freq_to_ssrc[freq_hz] = allocated
                        success_count += 1
            else:
                # No channel at this frequency
                # Create new channel (SSRC auto-allocated)
                logger.info(f"➕ Creating {freq_hz/1e6:.3f} MHz ({description})")
                allocated = self.create_channel(
                    frequency_hz=freq_hz,
                    preset=preset,
                    sample_rate=sample_rate,
                    agc=agc,
                    gain=gain,
                    destination=destination, # Usually None (defaults)
                    ssrc=None,  # Auto-allocate
                    description=description
                )
                if allocated:
                    # Set encoding after creation
                    if encoding == 'float':
                        self._set_float_encoding(allocated)
                    freq_to_ssrc[freq_hz] = allocated
                    success_count += 1
                else:
                    logger.error(f"❌ Failed to create channel {freq_hz/1e6:.3f} MHz")
        
        logger.info(f"Channel setup complete: {success_count}/{len(channels)} successful")
        return freq_to_ssrc
    
    def _reconfigure_channel(self, ssrc: int, frequency_hz: float, 
                            preset: str, sample_rate: int, encoding: str) -> Optional[int]:
        """
        Reconfigure an existing channel via tune().
        
        Args:
            ssrc: Existing channel SSRC
            frequency_hz: Frequency in Hz
            preset: Preset/mode
            sample_rate: Sample rate in Hz
            encoding: "float" or "int16"
            
        Returns:
            SSRC if successful, None otherwise
        """
        try:
            # Use tune() to update channel parameters
            result = self.control.tune(
                ssrc=ssrc,
                frequency_hz=frequency_hz,
                preset=preset,
                sample_rate=sample_rate,
                encoding=self._encoding_to_int(encoding)
            )
            
            time.sleep(0.3)
            
            if self.control.verify_channel(ssrc, frequency_hz):
                logger.info(f"✓ Channel {ssrc} reconfigured successfully")
                return ssrc
            else:
                logger.warning(f"✗ Channel {ssrc} reconfiguration verification failed")
                return None
                
        except Exception as e:
            logger.error(f"Failed to reconfigure channel {ssrc}: {e}")
            return None
    
    def _set_float_encoding(self, ssrc: int):
        """
        Set channel output encoding to float (complex64).
        
        Args:
            ssrc: Channel SSRC
        """
        try:
            # Encoding value for float - check ka9q-python constants
            # Typically: 0=S16, 1=F32, etc.
            self.control.set_output_encoding(ssrc, self._encoding_to_int('float'))
            logger.debug(f"Set encoding to float for SSRC {ssrc}")
        except Exception as e:
            logger.warning(f"Could not set float encoding for SSRC {ssrc}: {e}")
    
    @staticmethod
    def _encoding_to_int(encoding: str) -> int:
        """
        Convert encoding string to ka9q-radio integer value.
        
        Args:
            encoding: "float" or "int16"
            
        Returns:
            Integer encoding value for radiod
        """
        # ka9q-radio encoding values (from misc.h):
        # S16 = 0, F32 = 1, OPUS = 2, etc.
        if encoding == 'float':
            return 1  # F32
        elif encoding == 'int16':
            return 0  # S16
        else:
            logger.warning(f"Unknown encoding '{encoding}', defaulting to float")
            return 1
    
    def close(self):
        """Close the control connection"""
        if self.control:
            self.control.close()


if __name__ == '__main__':
    import argparse
    import toml
    
    parser = argparse.ArgumentParser(description='Manage radiod channels')
    parser.add_argument('--config', required=True, help='Path to configuration file')
    parser.add_argument('--create', action='store_true', help='Create missing channels from config')
    parser.add_argument('--status-address', help='Radiod status address (default: from config)')
    args = parser.parse_args()
    
    # Load config
    with open(args.config) as f:
        config = toml.load(f)
    
    # Get status address
    status_address = args.status_address or config.get('ka9q', {}).get('status_address', '239.192.152.141')
    
    # Create channel manager
    manager = ChannelManager(status_address)
    
    if args.create:
        # Get channels from config
        channels = config.get('recorder', {}).get('channels', [])
        enabled_channels = [ch for ch in channels if ch.get('enabled', True)]
        
        if not enabled_channels:
            print("No enabled channels found in configuration")
            exit(1)
        
        # Build channel specs
        channel_specs = []
        for ch in enabled_channels:
            channel_specs.append({
                'ssrc': ch['ssrc'],
                'frequency_hz': ch['frequency_hz'],
                'preset': ch.get('preset', 'iq'),
                'sample_rate': ch.get('sample_rate', 16000),
                'agc': ch.get('agc', 0),
                'gain': ch.get('gain', 0.0),
                'description': ch.get('description', '')
            })
        
        print(f"Creating {len(channel_specs)} channels...")
        success = manager.ensure_channels_exist(channel_specs, update_existing=False)
        
        if success:
            print("✓ All channels created successfully")
            exit(0)
        else:
            print("⚠ Some channels failed to create")
            exit(1)
    else:
        # Just discover channels
        channels = manager.discover_existing_channels()
        print(f"Found {len(channels)} existing channels:")
        for ssrc, info in channels.items():
            print(f"  {ssrc}: {info.frequency/1e6:.3f} MHz, {info.preset}, {info.sample_rate} Hz")

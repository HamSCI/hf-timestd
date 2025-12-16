/**
 * Radiod Channel Management Utilities
 * 
 * Uses ka9q-python library to create and verify channels in radiod
 */

import { spawn } from 'child_process';
import { join } from 'path';

const isProduction = process.env.NODE_ENV === 'production';
const installDir = isProduction ? '/opt/hf-timestd' : join(process.cwd(), '..');
const venvPython = join(installDir, 'venv', 'bin', 'python');

/**
 * Create a channel in radiod using ka9q-python
 * 
 * @param {Object} channel - Channel configuration
 * @param {number} channel.ssrc - Channel SSRC (must be unique)
 * @param {number} channel.frequencyHz - Frequency in Hz
 * @param {string} channel.preset - Preset name (default: 'iq')
 * @param {number} channel.sampleRate - Sample rate in Hz (default: 16000)
 * @param {string} channel.statusAddress - Radiod status address (default: '239.192.152.141')
 * @returns {Promise<Object>} Result with success status and channel info
 */
export async function createChannel(channel) {
  const {
    ssrc,
    frequencyHz,
    preset = 'iq',
    sampleRate = 16000,
    statusAddress = '239.192.152.141'
  } = channel;

  // Validate inputs
  if (!ssrc || !frequencyHz) {
    throw new Error('SSRC and frequency are required');
  }

  if (ssrc < 0 || ssrc > 0xFFFFFFFF) {
    throw new Error('SSRC must be a valid 32-bit unsigned integer');
  }

  if (frequencyHz < 0 || frequencyHz > 30e6) {
    throw new Error('Frequency must be between 0 and 30 MHz');
  }

  // Create Python script to create channel using ka9q-python
  const pythonScript = `
import sys
from ka9q import RadiodControl

try:
    # Create control instance
    control = RadiodControl("${statusAddress}")
    
    # Create and configure channel
    result = control.create_and_configure_channel(
        ssrc=${ssrc},
        frequency_hz=${frequencyHz},
        preset="${preset}",
        sample_rate=${sampleRate}
    )
    
    print("SUCCESS: Channel created: SSRC=${ssrc}, Freq=${frequencyHz} Hz")
    sys.exit(0)
    
except Exception as e:
    print("ERROR: " + str(e), file=sys.stderr)
    sys.exit(1)
`;

  return new Promise((resolve, reject) => {
    const python = spawn(venvPython, ['-c', pythonScript], {
      timeout: 10000,
      stdio: ['pipe', 'pipe', 'pipe']
    });

    let stdout = '';
    let stderr = '';

    python.stdout.on('data', (data) => {
      stdout += data.toString();
    });

    python.stderr.on('data', (data) => {
      stderr += data.toString();
    });

    python.on('close', (code) => {
      if (code === 0) {
        // Wait 1 second for radiod to process and broadcast status
        setTimeout(() => {
          resolve({
            success: true,
            message: 'Channel created successfully',
            channel: {
              ssrc,
              frequencyHz,
              preset,
              sampleRate
            },
            output: stdout.trim()
          });
        }, 1000);
      } else {
        reject(new Error(`Failed to create channel: ${stderr.trim() || stdout.trim()}`));
      }
    });

    python.on('error', (error) => {
      reject(new Error(`Failed to spawn Python: ${error.message}`));
    });
  });
}

/**
 * Verify a channel exists in radiod
 * 
 * @param {number} ssrc - Channel SSRC to verify
 * @param {string} statusAddress - Radiod status address
 * @returns {Promise<Object>} Channel info if exists, null if not
 */
export async function verifyChannel(ssrc, statusAddress = 'bee1-hf-status.local') {
  const pythonScript = `
import sys
from ka9q import discover_channels
import time

try:
    # Wait a bit for status broadcasts
    time.sleep(0.5)
    
    # Discover channels  
    channels_dict = discover_channels("${statusAddress}", listen_duration=2.0)
    
    if ${ssrc} in channels_dict:
        print("VERIFIED: Channel SSRC=${ssrc} exists")
        sys.exit(0)
    else:
        print("NOT_FOUND: Channel SSRC=${ssrc} does not exist", file=sys.stderr)
        sys.exit(1)
        
except Exception as e:
    print("ERROR: " + str(e), file=sys.stderr)
    sys.exit(1)
`;

  return new Promise((resolve, reject) => {
    const python = spawn(venvPython, ['-c', pythonScript], {
      timeout: 5000,
      stdio: ['pipe', 'pipe', 'pipe']
    });

    let stdout = '';
    let stderr = '';

    python.stdout.on('data', (data) => {
      stdout += data.toString();
    });

    python.stderr.on('data', (data) => {
      stderr += data.toString();
    });

    python.on('close', (code) => {
      if (code === 0) {
        resolve({
          exists: true,
          ssrc,
          message: 'Channel verified'
        });
      } else {
        resolve({
          exists: false,
          ssrc,
          message: 'Channel not found'
        });
      }
    });

    python.on('error', (error) => {
      reject(new Error(`Failed to verify channel: ${error.message}`));
    });
  });
}

/**
 * Discover all channels currently in radiod
 * 
 * @param {string} statusAddress - Radiod status address
 * @param {number} timeout - Discovery timeout in seconds
 * @returns {Promise<Array>} List of discovered channels
 */
export async function discoverChannels(statusAddress = 'bee1-hf-status.local', listenDuration = 3.0) {
  const pythonScript = `
import sys
import json
from ka9q import discover_channels

try:
    channels_dict = discover_channels("${statusAddress}", listen_duration=${listenDuration})
    
    # Convert to JSON list
    channel_list = []
    for ssrc, ch in channels_dict.items():
        channel_list.append({
            'ssrc': ch.ssrc,
            'frequency': ch.frequency,
            'preset': ch.preset,
            'multicast_address': ch.multicast_address,
            'multicast_port': ch.multicast_port
        })
    
    print(json.dumps(channel_list))
    sys.exit(0)
    
except Exception as e:
    print(f"ERROR: {str(e)}", file=sys.stderr)
    sys.exit(1)
`;

  return new Promise((resolve, reject) => {
    const python = spawn(venvPython, ['-c', pythonScript], {
      timeout: (timeout + 2) * 1000,
      stdio: ['pipe', 'pipe', 'pipe']
    });

    let stdout = '';
    let stderr = '';

    python.stdout.on('data', (data) => {
      stdout += data.toString();
    });

    python.stderr.on('data', (data) => {
      stderr += data.toString();
    });

    python.on('close', (code) => {
      if (code === 0) {
        try {
          const channels = JSON.parse(stdout.trim());
          resolve(channels);
        } catch (error) {
          reject(new Error(`Failed to parse channel list: ${error.message}`));
        }
      } else {
        reject(new Error(`Failed to discover channels: ${stderr.trim()}`));
      }
    });

    python.on('error', (error) => {
      reject(new Error(`Failed to discover channels: ${error.message}`));
    });
  });
}

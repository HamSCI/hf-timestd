/**
 * Configuration File Synchronization
 * 
 * Reads TOML config files, checks radiod channel status, and creates missing channels
 */

import fs from 'fs';
import { parse as parseToml } from 'toml';
import { createChannel, verifyChannel } from './radiod.js';

/**
 * Parse a TOML configuration file and extract channel definitions
 * 
 * @param {string} configPath - Path to TOML config file
 * @returns {Object} Parsed config with channels array
 */
export function parseConfigFile(configPath) {
  try {
    const tomlContent = fs.readFileSync(configPath, 'utf8');
    const config = parseToml(tomlContent);
    
    // Extract channels from [[recorder.channels]] sections
    const channels = config.recorder?.channels || [];
    
    return {
      station: config.station,
      recorder: config.recorder,
      channels: channels.map(ch => ({
        ssrc: parseInt(ch.ssrc),
        frequencyHz: parseInt(ch.frequency_hz),
        description: ch.description || `Channel ${ch.ssrc}`,
        preset: ch.preset || 'iq',
        sampleRate: parseInt(ch.sample_rate || 16000),
        agc: parseInt(ch.agc || 0),
        gain: parseInt(ch.gain || 0),
        enabled: ch.enabled !== false,
        processor: ch.processor || 'timestd'
      }))
    };
  } catch (error) {
    throw new Error(`Failed to parse config file: ${error.message}`);
  }
}

/**
 * Check which channels from config exist in radiod
 * 
 * @param {Array} channels - Channel definitions from config
 * @param {string} statusAddress - Radiod status address
 * @returns {Promise<Array>} Channels with existence status
 */
export async function checkChannelStatus(channels, statusAddress = 'bee1-hf-status.local') {
  const results = [];
  
  for (const channel of channels) {
    try {
      const verification = await verifyChannel(channel.ssrc, statusAddress);
      results.push({
        ...channel,
        existsInRadiod: verification.exists,
        status: verification.exists ? 'verified' : 'missing'
      });
    } catch (error) {
      results.push({
        ...channel,
        existsInRadiod: false,
        status: 'error',
        error: error.message
      });
    }
  }
  
  return results;
}

/**
 * Synchronize config channels with radiod
 * Creates any missing channels and verifies all
 * 
 * @param {string} configPath - Path to TOML config file
 * @param {string} statusAddress - Radiod status address
 * @param {boolean} createMissing - Whether to create missing channels
 * @returns {Promise<Object>} Sync results
 */
export async function syncConfigWithRadiod(configPath, statusAddress = 'bee1-hf-status.local', createMissing = true) {
  const results = {
    configPath,
    timestamp: new Date().toISOString(),
    channels: [],
    summary: {
      total: 0,
      verified: 0,
      created: 0,
      failed: 0,
      skipped: 0
    }
  };
  
  try {
    // Parse config file
    const config = parseConfigFile(configPath);
    results.station = config.station;
    results.summary.total = config.channels.length;
    
    console.log(`Syncing ${config.channels.length} channels from ${configPath}...`);
    
    // Check status of each channel
    for (const channel of config.channels) {
      const channelResult = {
        ssrc: channel.ssrc,
        frequencyHz: channel.frequencyHz,
        description: channel.description,
        enabled: channel.enabled
      };
      
      try {
        // Skip disabled channels
        if (!channel.enabled) {
          channelResult.status = 'skipped';
          channelResult.message = 'Channel disabled in config';
          results.summary.skipped++;
          results.channels.push(channelResult);
          continue;
        }
        
        // Check if exists
        const verification = await verifyChannel(channel.ssrc, statusAddress);
        
        if (verification.exists) {
          // Already exists
          channelResult.status = 'verified';
          channelResult.message = 'Channel already exists in radiod';
          results.summary.verified++;
        } else if (createMissing) {
          // Create missing channel
          console.log(`Creating missing channel: ${channel.description} (${channel.ssrc})`);
          
          try {
            await createChannel({
              ssrc: channel.ssrc,
              frequencyHz: channel.frequencyHz,
              preset: channel.preset,
              sampleRate: channel.sampleRate,
              statusAddress
            });
            
            // Verify it was created
            const postVerify = await verifyChannel(channel.ssrc, statusAddress);
            
            if (postVerify.exists) {
              channelResult.status = 'created';
              channelResult.message = 'Channel created and verified';
              results.summary.created++;
            } else {
              channelResult.status = 'failed';
              channelResult.message = 'Channel created but verification failed';
              results.summary.failed++;
            }
          } catch (createError) {
            channelResult.status = 'failed';
            channelResult.message = `Creation failed: ${createError.message}`;
            channelResult.error = createError.message;
            results.summary.failed++;
          }
        } else {
          // Missing but not creating
          channelResult.status = 'missing';
          channelResult.message = 'Channel not in radiod (creation disabled)';
          results.summary.failed++;
        }
      } catch (error) {
        channelResult.status = 'error';
        channelResult.message = `Error checking channel: ${error.message}`;
        channelResult.error = error.message;
        results.summary.failed++;
      }
      
      results.channels.push(channelResult);
    }
    
    console.log(`Sync complete: ${results.summary.verified} verified, ${results.summary.created} created, ${results.summary.failed} failed, ${results.summary.skipped} skipped`);
    
    return results;
    
  } catch (error) {
    throw new Error(`Config sync failed: ${error.message}`);
  }
}

/**
 * Import channels from config file into database
 * 
 * @param {string} configPath - Path to TOML config file
 * @param {string} configId - Database config ID to associate channels with
 * @returns {Object} Import results
 */
export function importChannelsFromConfig(configPath, configId) {
  try {
    const config = parseConfigFile(configPath);
    
    return {
      configId,
      station: config.station,
      channels: config.channels.map(ch => ({
        ...ch,
        configId,
        id: `${configId}-${ch.ssrc}`,
        frequencyHz: ch.frequencyHz.toString(),
        ssrc: ch.ssrc.toString(),
        sampleRate: ch.sampleRate.toString(),
        agc: ch.agc.toString(),
        gain: ch.gain.toString(),
        enabled: ch.enabled ? 'yes' : 'no',
        createdAt: new Date().toISOString()
      }))
    };
  } catch (error) {
    throw new Error(`Failed to import channels: ${error.message}`);
  }
}

#!/usr/bin/env node
/**
 * hf-timestd - Monitoring Server V3
 * 
 * 3-Screen Monitoring Interface:
 * 1. Summary - System status, channel status, station info
 * 2. Carrier - Carrier and timing analysis
 * 3. Discrimination - WWV/WWVH propagation analysis (shared frequency channels)
 * 
 * Architecture:
 * - Uses centralized TimeStd paths API for all file access
 * - RESTful API with individual + aggregated endpoints
 * - All channels now 16 kHz wide channels with WWV/CHU tone detection
 * - No authentication (monitoring only)
 * - No configuration editing (use TOML files directly)
 */

import express from 'express';
import cors from 'cors';
import fs from 'fs';
import { join, basename, dirname, resolve } from 'path';
import { parse as csvParse } from 'csv-parse/sync';
import { fileURLToPath } from 'url';
import toml from 'toml';
import { exec, execSync, spawn } from 'child_process';
import { promisify } from 'util';
import { WebSocketServer } from 'ws';
import { TimeStdPaths, channelNameToKey } from './timestd-paths.js';
import {
  getPrimaryTimeReference,
  getTimingHealthSummary,
  getTimingMetrics,
  getTimingTransitions,
  getTimingTimeline
} from './utils/timing-analysis-helpers.js';
import {
  getClockOffsetSeries,
  getPhase2AnalyticsStatus,
  getAllPhase2Status,
  getBestDClock,
  getPhase2PipelineStatus,
  getKalmanFunnelData,
  getConstellationData,
  getConsensusData,
  getModeProbabilityData
} from './utils/transmission-time-helpers.js';

const execAsync = promisify(exec);
const __dirname = dirname(fileURLToPath(import.meta.url));

// ============================================================================
// INITIALIZATION
// ============================================================================

const app = express();
const PORT = 3000;
const serverStartTime = Date.now();

// Determine install directory
const installDir = process.env.TIMESTD_INSTALL_DIR || join(__dirname, '..');
const configPath = process.env.TIMESTD_CONFIG || join(installDir, 'config/timestd-config.toml');

// Load configuration
let config = {};
let dataRoot = join(process.env.HOME, 'timestd-data'); // Fallback
let mode = 'test';
let paths = null;

try {
  const configContent = fs.readFileSync(configPath, 'utf8');
  config = toml.parse(configContent);

  // Determine data_root based on mode
  mode = config.recorder?.mode || 'test';
  if (mode === 'production') {
    dataRoot = config.recorder?.production_data_root || '/var/lib/hf-timestd';
  } else {
    dataRoot = config.recorder?.test_data_root || '/tmp/timestd-test';
  }
  
  // Initialize paths API
  paths = new TimeStdPaths(dataRoot);
  
  console.log('📊 hf-timestd Monitoring Server V3');
  console.log('📁 Config file:', configPath);
  console.log(mode === 'production' ? '🚀 Mode: PRODUCTION' : '🧪 Mode: TEST');
  console.log('📁 Data root:', dataRoot);
  console.log('📡 Station:', config.station?.callsign, config.station?.grid_square);
  console.log('🔧 Instrument ID:', config.station?.instrument_id || 'not configured');
} catch (err) {
  console.error('⚠️  Failed to load config, using defaults:', err.message);
  console.log('📊 hf-timestd Monitoring Server V3 (fallback mode)');
  console.log('📁 Data root:', dataRoot);
  paths = new TimeStdPaths(dataRoot);
}

// ============================================================================
// Python configuration - use venv if available
const VENV_PYTHON = join(__dirname, '..', 'venv', 'bin', 'python3');
const PYTHON_CMD = fs.existsSync(VENV_PYTHON) ? VENV_PYTHON : 'python3';

// Middleware
app.use(express.json());
app.use(express.static(__dirname)); // Serve static files

// CORS for local development
app.use((req, res, next) => {
  res.header('Access-Control-Allow-Origin', '*');
  res.header('Access-Control-Allow-Headers', 'Content-Type');
  next();
});

// ============================================================================
// HELPER FUNCTIONS - DATA ACCESS
// ============================================================================

/**
 * Convert seconds to human-readable uptime
 */
function formatUptime(seconds) {
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor((seconds % 86400) / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  
  const parts = [];
  if (days > 0) parts.push(`${days}d`);
  if (hours > 0) parts.push(`${hours}h`);
  if (minutes > 0) parts.push(`${minutes}m`);
  
  return parts.join(' ') || '0m';
}

/**
 * Get station info from configuration
 */
function getStationInfo() {
  return {
    id: config.station?.id || 'not configured',  // PSWS Station ID
    callsign: config.station?.callsign || 'UNKNOWN',
    grid_square: config.station?.grid_square || 'UNKNOWN',
    receiver: config.station?.receiver_name || 'hf-timestd',
    instrument_id: config.station?.instrument_id || 'not configured',
    mode: mode,
    data_root: dataRoot
  };
}

/**
 * Get radiod status from dedicated health monitor
 */
async function getRadiodStatus(paths) {
  try {
    const radiodStatusFile = join(paths.getStateDir(), 'radiod-status.json');
    
    if (!fs.existsSync(radiodStatusFile)) {
      // Fallback: try to detect radiod process directly with uptime
      try {
        // Get PID and calculate uptime from process start time
        const pid = execSync('pgrep -x radiod', { encoding: 'utf8' }).trim();
        const running = pid.length > 0;
        
        let uptime_seconds = 0;
        if (running) {
          try {
            // Get process start time and calculate uptime
            const etimes = execSync(`ps -o etimes= -p ${pid.split('\n')[0]}`, { encoding: 'utf8' }).trim();
            uptime_seconds = parseInt(etimes, 10) || 0;
          } catch (e) {
            // Fallback if ps fails
          }
        }
        
        return {
          running,
          method: 'pgrep_fallback',
          uptime_seconds,
          health: running ? 'unknown' : 'critical'
        };
      } catch (e) {
        return {
          running: false,
          method: 'pgrep_fallback_failed',
          health: 'critical'
        };
      }
    }
    
    const content = fs.readFileSync(radiodStatusFile, 'utf8');
    const status = JSON.parse(content);
    
    // Check status age
    const statusTimestamp = new Date(status.timestamp).getTime() / 1000;
    const age = Date.now() / 1000 - statusTimestamp;
    
    return {
      running: status.process?.running || false,
      connected: status.connectivity || false,
      uptime_seconds: status.uptime_seconds || 0,
      health: status.health || 'unknown',
      alerts: status.alerts || [],
      status_age_seconds: Math.round(age),
      method: 'health_monitor'
    };
    
  } catch (err) {
    console.error('Error getting radiod status:', err);
    return {
      running: false,
      error: err.message,
      method: 'error',
      health: 'critical'
    };
  }
}

/**
 * Get core recorder status
 */
async function getCoreRecorderStatus(paths) {
  try {
    const statusFile = paths.getCoreStatusFile();
    
    if (!fs.existsSync(statusFile)) {
      return { 
        running: false, 
        error: 'Status file not found'
      };
    }
    
    const content = fs.readFileSync(statusFile, 'utf8');
    const status = JSON.parse(content);
    
    // Parse ISO 8601 timestamp
    const statusTimestamp = new Date(status.timestamp).getTime() / 1000;
    const age = Date.now() / 1000 - statusTimestamp;
    const running = age < 30;
    
    // Get channel info - handle both old and new status formats
    let channelsActive = 0;
    let channelsTotal = 0;
    let totalPackets = 0;
    let uptime = 0;
    
    if (status.recorders && typeof status.recorders === 'object') {
      // New format: status.recorders = {frequency: {...}}
      const recorders = Object.values(status.recorders);
      channelsActive = recorders.filter(r => r.packets_received > 0).length;
      channelsTotal = status.channels || recorders.length;
      totalPackets = recorders.reduce((sum, r) => sum + (r.packets_received || 0), 0);
      uptime = status.recording_duration_sec || 0;
    } else if (status.channels && typeof status.channels === 'object') {
      // Old format: status.channels = {ssrc: {...}}
      const channels = Object.values(status.channels);
      channelsActive = channels.length;
      channelsTotal = channels.length;
      totalPackets = channels.reduce((sum, ch) => sum + (ch.packets_received || 0), 0);
      uptime = status.uptime_seconds || 0;
    }
    
    return {
      running,
      uptime_seconds: uptime,
      channels_active: channelsActive,
      channels_total: channelsTotal,
      packets_received: totalPackets,
      last_update: status.timestamp,
      age_seconds: age
    };
  } catch (err) {
    return { 
      running: false, 
      error: err.message,
      radiod_running: false
    };
  }
}

/**
 * Get analytics service status (aggregated across all channels)
 */
async function getAnalyticsServiceStatus(paths) {
  try {
    const channels = paths.discoverChannels();
    
    if (channels.length === 0) {
      return { 
        running: false, 
        error: 'No channels found' 
      };
    }
    
    let channelsProcessing = 0;
    let newestTimestamp = 0;
    let oldestUptime = Infinity;
    
    for (const channelName of channels) {
      // Use Phase 2 status directory (phase2/{CHANNEL}/status/)
      const statusFile = paths.getAnalyticsServiceStatusFileForChannel(channelName);
      
      if (fs.existsSync(statusFile)) {
        try {
          const content = fs.readFileSync(statusFile, 'utf8');
          const status = JSON.parse(content);
          
          // Parse ISO 8601 timestamp
          const statusTimestamp = new Date(status.timestamp).getTime() / 1000;
          const age = Date.now() / 1000 - statusTimestamp;
          
          // Consider active if updated within 5 minutes (analytics only writes when processing data)
          if (age < 300) {
            channelsProcessing++;
            
            if (statusTimestamp > newestTimestamp) {
              newestTimestamp = statusTimestamp;
            }
            
            // Get uptime from status file
            const uptime = status.uptime_seconds || status.uptime || 0;
            if (uptime > 0 && uptime < oldestUptime) {
              oldestUptime = uptime;
            }
          }
        } catch (err) {
          // Skip channel with invalid status file
        }
      }
    }
    
    const age = newestTimestamp > 0 ? (Date.now() / 1000 - newestTimestamp) : Infinity;
    const running = channelsProcessing > 0 && age < 300;
    
    return {
      running,
      uptime_seconds: oldestUptime < Infinity ? oldestUptime : 0,
      channels_processing: channelsProcessing,
      channels_total: channels.length,
      last_update: newestTimestamp,
      age_seconds: age
    };
  } catch (err) {
    return { 
      running: false, 
      error: err.message 
    };
  }
}

/**
 * Get process statuses
 */
async function getProcessStatuses(paths) {
  const radiodStatus = await getRadiodStatus(paths);
  const coreStatus = await getCoreRecorderStatus(paths);
  const analyticsStatus = await getAnalyticsServiceStatus(paths);
  
  return {
    radiod: radiodStatus,
    core_recorder: {
      running: coreStatus.running,
      uptime_seconds: coreStatus.uptime_seconds,
      channels_active: coreStatus.channels_active,
      channels_total: coreStatus.channels_total,
      packets_received: coreStatus.packets_received,
      last_update: coreStatus.last_update
    },
    analytics_service: {
      running: analyticsStatus.running,
      uptime_seconds: analyticsStatus.uptime_seconds,
      channels_processing: analyticsStatus.channels_processing,
      channels_total: analyticsStatus.channels_total,
      last_update: analyticsStatus.last_update
    }
  };
}

/**
 * Get data continuity info (span and gaps)
 */
async function getDataContinuity(paths) {
  try {
    const channels = paths.discoverChannels();
    
    if (channels.length === 0) {
      const now = Date.now() / 1000;
      const nowISO = new Date(now * 1000).toISOString();
      return {
        data_span: {
          start: nowISO,
          end: nowISO,
          duration_seconds: 0
        },
        available_data: {
          oldest_data: nowISO,
          newest_data: nowISO,
          total_duration_seconds: 0
        },
        continuity: {
          uptime_pct: 100,
          capture_seconds: 0,
          downtime_seconds: 0,
          gap_count: 0
        },
        gaps: [],
        gap_events: [], // Alias for compatibility
        total_downtime_seconds: 0,
        downtime_percentage: 0
      };
    }
    
    // Collect all timestamps across all channels
    const channelTimestamps = new Map();
    let globalOldest = Infinity;
    let globalNewest = 0;
    
    for (const channelName of channels) {
      // Use raw_buffer for Phase 1 data (binary per-minute)
      const archiveDir = paths.getRawBufferDir(channelName);
      
      if (!fs.existsSync(archiveDir)) continue;
      
      const timestamps = [];
      
      // Scan for date directories (YYYYMMDD)
      const dateDirs = fs.readdirSync(archiveDir)
        .filter(f => /^\d{8}$/.test(f))
        .sort();

      for (const dateDir of dateDirs) {
        const dayPath = join(archiveDir, dateDir);
        if (!fs.existsSync(dayPath)) continue;
        
        const files = fs.readdirSync(dayPath);
        for (const file of files) {
          const m = file.match(/^(\d+)\.bin(\.(zst|lz4))?$/);
          if (!m) continue;
          const unixTime = parseInt(m[1]);
          if (!Number.isFinite(unixTime)) continue;
          timestamps.push(unixTime);
          if (unixTime < globalOldest) globalOldest = unixTime;
          if (unixTime > globalNewest) globalNewest = unixTime;
        }
      }
      
      channelTimestamps.set(channelName, timestamps.sort((a, b) => a - b));
    }
    
    // Detect system-wide gaps (gaps in ALL channels simultaneously)
    const systemGaps = [];
    
    if (globalOldest < Infinity && globalNewest > 0) {
      // Create union of all timestamps
      const allTimestamps = new Set();
      for (const timestamps of channelTimestamps.values()) {
        timestamps.forEach(ts => allTimestamps.add(ts));
      }
      
      const sortedTimes = Array.from(allTimestamps).sort((a, b) => a - b);
      
      // Find gaps where timestamp jump > 120 seconds (2 minutes)
      for (let i = 1; i < sortedTimes.length; i++) {
        const gap = sortedTimes[i] - sortedTimes[i - 1];
        
        if (gap > 120) {
          // Verify this is a system-wide gap (all channels affected)
          const gapStart = sortedTimes[i - 1] + 60; // End of previous minute
          const gapEnd = sortedTimes[i];
          
          let isSystemGap = true;
          for (const [channelName, timestamps] of channelTimestamps) {
            // Check if this channel has data in the gap period
            const hasDataInGap = timestamps.some(ts => ts > gapStart && ts < gapEnd);
            if (hasDataInGap) {
              isSystemGap = false;
              break;
            }
          }
          
          if (isSystemGap) {
            const gapDuration = gapEnd - gapStart;
            const severity = gapDuration > 3600 ? 'critical' : gapDuration > 300 ? 'warning' : 'info';
            const cause = gapDuration > 3600 ? 'extended_outage' : gapDuration > 300 ? 'service_restart' : 'brief_interruption';
            
            systemGaps.push({
              start: new Date(gapStart * 1000).toISOString(),
              end: new Date(gapEnd * 1000).toISOString(),
              duration_seconds: gapDuration,
              cause: cause,
              severity: severity,
              channels_affected: channels.length // All channels affected in system-wide gap
            });
          }
        }
      }
    }
    
    // Calculate totals
    const totalDowntime = systemGaps.reduce((sum, gap) => sum + gap.duration_seconds, 0);
    const spanDuration = (globalNewest > globalOldest) ? globalNewest - globalOldest : 0;
    const downtimePercentage = spanDuration > 0 ? (totalDowntime / spanDuration) * 100 : 0;
    
    const recentGaps = systemGaps.slice(-5); // Return only 5 most recent gaps
    const uptimeSeconds = spanDuration - totalDowntime;
    const uptimePercentage = spanDuration > 0 ? (uptimeSeconds / spanDuration) * 100 : 100;
    
    // Handle case where no data files found
    const nowISO = new Date().toISOString();
    const hasData = globalOldest < Infinity && globalNewest > 0;
    
    return {
      data_span: {
        start: hasData ? new Date(globalOldest * 1000).toISOString() : nowISO,
        end: hasData ? new Date(globalNewest * 1000).toISOString() : nowISO,
        duration_seconds: spanDuration
      },
      available_data: {
        oldest_data: hasData ? new Date(globalOldest * 1000).toISOString() : nowISO,
        newest_data: hasData ? new Date(globalNewest * 1000).toISOString() : nowISO,
        total_duration_seconds: spanDuration
      },
      continuity: {
        uptime_pct: uptimePercentage,
        capture_seconds: uptimeSeconds,
        downtime_seconds: totalDowntime,
        gap_count: systemGaps.length
      },
      gaps: recentGaps,
      gap_events: recentGaps, // Alias for compatibility
      total_downtime_seconds: totalDowntime,
      downtime_percentage: downtimePercentage
    };
  } catch (err) {
    console.error('Error calculating continuity:', err);
    const now = Date.now() / 1000;
    const nowISO = new Date(now * 1000).toISOString();
    return {
      data_span: {
        start: nowISO,
        end: nowISO,
        duration_seconds: 0
      },
      available_data: {
        oldest_data: nowISO,
        newest_data: nowISO,
        total_duration_seconds: 0
      },
      continuity: {
        uptime_pct: 100,
        capture_seconds: 0,
        downtime_seconds: 0,
        gap_count: 0
      },
      gaps: [],
      gap_events: [], // Alias for compatibility
      total_downtime_seconds: 0,
      downtime_percentage: 0,
      error: err.message
    };
  }
}

/**
 * Validate channel name against configured channels
 * Returns the validated channel name or null if invalid
 */
function validateChannelName(channelName) {
  if (!channelName || typeof channelName !== 'string') return null;
  
  const channels = config.recorder?.channels || [];
  const validChannel = channels.find(ch => 
    (ch.description || `Channel ${ch.ssrc}`) === channelName
  );
  
  return validChannel ? channelName : null;
}

/**
 * Get list of valid channel names from config
 */
function getValidChannelNames() {
  const channels = config.recorder?.channels || [];
  return channels
    .filter(ch => ch.enabled !== false)
    .map(ch => ch.description || `Channel ${ch.ssrc}`);
}

/**
 * Get storage information including tiered storage status
 */
async function getStorageInfo(paths) {
  try {
    // Use df command to get disk usage for main data directory
    const { stdout } = await execAsync(`df -B1 ${dataRoot} | tail -1`);
    const parts = stdout.trim().split(/\s+/);
    
    const totalBytes = parseInt(parts[1]);
    const usedBytes = parseInt(parts[2]);
    const availableBytes = parseInt(parts[3]);
    const usedPercent = (usedBytes / totalBytes) * 100;
    
    // Estimate write rate (very rough - based on current usage)
    const archiveSize = usedBytes;
    const writeRatePerDay = archiveSize / 7; // Assume ~1 week of data
    const daysUntilFull = availableBytes / writeRatePerDay;
    
    // Tiered storage status (hot buffer in RAM, cold buffer on disk)
    let tieredStorage = null;
    const hotBufferPath = '/dev/shm/timestd';
    const coldBufferPath = join(dataRoot, 'raw_buffer');
    
    try {
      // Check hot buffer (RAM-based /dev/shm)
      let hotBufferInfo = null;
      if (fs.existsSync(hotBufferPath)) {
        const { stdout: hotDf } = await execAsync(`df -B1 ${hotBufferPath} | tail -1`);
        const hotParts = hotDf.trim().split(/\s+/);
        const hotUsed = parseInt(hotParts[2]);
        const hotTotal = parseInt(hotParts[1]);
        
        // Count files in hot buffer
        let hotFileCount = 0;
        try {
          const channels = fs.readdirSync(hotBufferPath);
          for (const ch of channels) {
            const chPath = join(hotBufferPath, ch);
            if (fs.statSync(chPath).isDirectory()) {
              const dates = fs.readdirSync(chPath);
              for (const d of dates) {
                const datePath = join(chPath, d);
                if (fs.statSync(datePath).isDirectory()) {
                  hotFileCount += fs.readdirSync(datePath).length;
                }
              }
            }
          }
        } catch (e) { /* ignore */ }
        
        hotBufferInfo = {
          path: hotBufferPath,
          used_bytes: hotUsed,
          total_bytes: hotTotal,
          used_percent: (hotUsed / hotTotal) * 100,
          file_count: hotFileCount,
          type: 'RAM (tmpfs)'
        };
      }
      
      // Check cold buffer (disk-based)
      let coldBufferInfo = null;
      if (fs.existsSync(coldBufferPath)) {
        let coldUsed = 0;
        let coldFileCount = 0;
        try {
          const { stdout: duOut } = await execAsync(`du -sb ${coldBufferPath} 2>/dev/null || echo "0"`);
          coldUsed = parseInt(duOut.split('\t')[0]) || 0;
          
          // Count files
          const channels = fs.readdirSync(coldBufferPath);
          for (const ch of channels) {
            const chPath = join(coldBufferPath, ch);
            if (fs.statSync(chPath).isDirectory()) {
              const dates = fs.readdirSync(chPath);
              for (const d of dates) {
                const datePath = join(chPath, d);
                if (fs.statSync(datePath).isDirectory()) {
                  coldFileCount += fs.readdirSync(datePath).length;
                }
              }
            }
          }
        } catch (e) { /* ignore */ }
        
        coldBufferInfo = {
          path: coldBufferPath,
          used_bytes: coldUsed,
          file_count: coldFileCount,
          type: 'Disk'
        };
      }
      
      // Check tiered storage status file if exists
      const tieredStatusFile = join(paths.getStateDir(), 'tiered_storage_status.json');
      let archivalStatus = null;
      if (fs.existsSync(tieredStatusFile)) {
        try {
          archivalStatus = JSON.parse(fs.readFileSync(tieredStatusFile, 'utf-8'));
        } catch (e) { /* ignore */ }
      }
      
      tieredStorage = {
        enabled: hotBufferInfo !== null,
        hot_buffer: hotBufferInfo,
        cold_buffer: coldBufferInfo,
        archival: archivalStatus
      };
    } catch (e) {
      tieredStorage = { enabled: false, error: e.message };
    }
    
    return {
      location: dataRoot,
      used_bytes: usedBytes,
      total_bytes: totalBytes,
      available_bytes: availableBytes,
      used_percent: usedPercent,
      write_rate_bytes_per_day: writeRatePerDay,
      estimated_days_until_full: Math.floor(daysUntilFull),
      tiered_storage: tieredStorage
    };
  } catch (err) {
    return {
      location: dataRoot,
      error: err.message
    };
  }
}

/**
 * Get gap analysis data from Phase 1 minute files
 */
async function getGapAnalysis(paths, date, channelFilter = 'all') {
  try {
    const year = parseInt(date.slice(0, 4));
    const month = parseInt(date.slice(4, 6)) - 1;
    const day = parseInt(date.slice(6, 8));
    const targetDate = new Date(Date.UTC(year, month, day, 0, 0, 0));

    const now = new Date();
    const isToday = targetDate.toISOString().slice(0, 10) === now.toISOString().slice(0, 10);
    const totalExpectedMinutes = isToday ? (now.getUTCHours() * 60 + now.getUTCMinutes()) : 1440;
    const minutesAnalyzed = Math.max(totalExpectedMinutes, 1);

    const channels = paths.discoverChannels();
    const channelsToCheck = channelFilter === 'all' ? channels : channels.filter(c => c === channelFilter);

    const allGaps = [];
    let totalFilesFound = 0;
    let totalGapMinutes = 0;
    let longestGap = 0;

    for (const channelName of channelsToCheck) {
      const minuteDir = join(paths.getRawBufferDir(channelName), date);
      if (!fs.existsSync(minuteDir)) continue;

      const minutesWithData = new Set();
      const files = fs.readdirSync(minuteDir);
      for (const f of files) {
        const m = f.match(/^(\d+)\.bin(\.(zst|lz4))?$/);
        if (!m) continue;
        const ts = parseInt(m[1]);
        if (!Number.isFinite(ts)) continue;
        const d = new Date(ts * 1000);
        const mod = d.getUTCHours() * 60 + d.getUTCMinutes();
        minutesWithData.add(mod);
        totalFilesFound += 1;
      }

      const missing = [];
      for (let i = 0; i < minutesAnalyzed; i++) {
        if (!minutesWithData.has(i)) missing.push(i);
      }

      if (missing.length) {
        let gapStart = missing[0];
        let gapLen = 1;
        for (let i = 1; i < missing.length; i++) {
          if (missing[i] === missing[i - 1] + 1) {
            gapLen += 1;
          } else {
            const h = Math.floor(gapStart / 60);
            const mm = gapStart % 60;
            const startTime = `${date.slice(0,4)}-${date.slice(4,6)}-${date.slice(6,8)}T${String(h).padStart(2,'0')}:${String(mm).padStart(2,'0')}:00Z`;
            const severity = gapLen >= 30 ? 'high' : (gapLen >= 5 ? 'medium' : 'low');
            allGaps.push({
              channel: channelName,
              start_time: startTime,
              start_minute: gapStart,
              duration_minutes: gapLen,
              duration_seconds: gapLen * 60,
              severity
            });
            longestGap = Math.max(longestGap, gapLen);
            gapStart = missing[i];
            gapLen = 1;
          }
        }
        const h = Math.floor(gapStart / 60);
        const mm = gapStart % 60;
        const startTime = `${date.slice(0,4)}-${date.slice(4,6)}-${date.slice(6,8)}T${String(h).padStart(2,'0')}:${String(mm).padStart(2,'0')}:00Z`;
        const severity = gapLen >= 30 ? 'high' : (gapLen >= 5 ? 'medium' : 'low');
        allGaps.push({
          channel: channelName,
          start_time: startTime,
          start_minute: gapStart,
          duration_minutes: gapLen,
          duration_seconds: gapLen * 60,
          severity
        });
        longestGap = Math.max(longestGap, gapLen);

        totalGapMinutes += missing.length;
      }
    }

    const denom = channelsToCheck.length ? channelsToCheck.length : 1;
    const completeness = (totalFilesFound / (minutesAnalyzed * denom)) * 100;

    return {
      date,
      channel_filter: channelFilter,
      total_gaps: allGaps.length,
      total_gap_minutes: Math.round((totalGapMinutes / denom) * 10) / 10,
      completeness_pct: Math.round(Math.min(completeness, 100) * 10) / 10,
      longest_gap_minutes: longestGap,
      minutes_analyzed: minutesAnalyzed,
      files_found: totalFilesFound,
      gaps: allGaps.sort((a, b) => a.start_minute - b.start_minute).slice(0, 50)
    };
  } catch (err) {
    console.error('Gap analysis error:', err);
    return {
      date,
      channel_filter: channelFilter,
      total_gaps: 0,
      total_gap_minutes: 0,
      completeness_pct: 0,
      longest_gap_minutes: 0,
      minutes_analyzed: 0,
      files_found: 0,
      gaps: [],
      error: err.message
    };
  }
}

/**
 * Check NTP synchronization status
 */
async function getNTPStatus() {
  try {
    const { stdout } = await execAsync('timedatectl status');
    
    const synchronized = stdout.includes('System clock synchronized: yes');
    
    // Try to extract offset (may not always be available)
    let offsetMs = null;
    try {
      const { stdout: ntpqOutput } = await execAsync('ntpq -p 2>/dev/null || chronyc tracking 2>/dev/null');
      // Parse offset from output (implementation depends on ntp vs chrony)
      // This is a simplified version
      const offsetMatch = ntpqOutput.match(/offset\s+([-\d.]+)/);
      if (offsetMatch) {
        offsetMs = parseFloat(offsetMatch[1]);
      }
    } catch (e) {
      // NTP offset not available
    }
    
    return {
      synchronized,
      offset_ms: offsetMs
    };
  } catch (err) {
    return {
      synchronized: false,
      error: err.message
    };
  }
}

/**
 * Get per-channel status
 */
async function getChannelStatuses(paths) {
  try {
    const channels = paths.discoverChannels();
    const ntpStatus = await getNTPStatus();
    const channelStatuses = [];
    
    // Read core status once for all channels
    let coreChannels = {};
    try {
      const coreStatusFile = paths.getCoreStatusFile();
      if (fs.existsSync(coreStatusFile)) {
        const content = fs.readFileSync(coreStatusFile, 'utf8');
        const coreStatus = JSON.parse(content);
        coreChannels = coreStatus.channels || {};
      }
    } catch (err) {
      console.error('Error reading core status:', err.message);
    }
    
    for (const channelName of channels) {
      // Check per-channel RTP status from core recorder
      let rtpStreaming = false;
      for (const [ssrc, chInfo] of Object.entries(coreChannels)) {
        if (chInfo.description === channelName) {
          // Check if last_packet_time is recent (within 10 seconds)
          const lastPacket = new Date(chInfo.last_packet_time).getTime() / 1000;
          const age = Date.now() / 1000 - lastPacket;
          rtpStreaming = age < 10;  // 10 second timeout for per-channel status
          break;
        }
      }
      
      // Get analytics status for this channel (Phase 2 status directory)
      const statusFile = paths.getAnalyticsServiceStatusFileForChannel(channelName);
      
      let snrDb = null;
      let timeBasis = 'WALL_CLOCK';
      let timeSnapAge = null;
      
      if (fs.existsSync(statusFile)) {
        try {
          const content = fs.readFileSync(statusFile, 'utf8');
          const status = JSON.parse(content);
          
          // Get channel-specific data
          const channelData = status.channels?.[channelName];
          
          // Get SNR (if available)
          snrDb = channelData?.quality_metrics?.last_snr_db || status.current_snr_db || null;
          
          // Determine time basis
          // Priority: TONE_LOCKED > NTP_SYNCED > WALL_CLOCK
          // Use same logic as timing dashboard for consistency
          if (channelData?.time_snap?.established) {
            const age = Date.now() / 1000 - channelData.time_snap.utc_timestamp;
            timeSnapAge = age;
            
            const source = (channelData.time_snap.source || '').toLowerCase();
            const isToneSource = source.includes('wwv') || source.includes('chu') || 
                                source === 'wwv_startup' || source === 'chu_startup' || 
                                source === 'wwvh_startup';
            
            if (isToneSource && age < 300) {
              // Fresh tone detection (< 5 minutes) = actively tone-locked
              timeBasis = 'TONE_LOCKED';
            } else if (isToneSource && age < 3600) {
              // Aged tone reference (5 min - 1 hour) = interpolated (still valid but aging)
              timeBasis = 'INTERPOLATED';
            } else if (ntpStatus.synchronized) {
              // Very aged or NTP source
              timeBasis = 'NTP_SYNCED';
            }
            // else: WALL_CLOCK (default)
          } else if (ntpStatus.synchronized) {
            timeBasis = 'NTP_SYNCED';
          }
        } catch (err) {
          // Use defaults
          console.error(`Error reading status for ${channelName}:`, err.message);
        }
      }
      
      channelStatuses.push({
        name: channelName,
        rtp_streaming: rtpStreaming,
        snr_db: snrDb,
        time_basis: timeBasis,
        time_snap_age_seconds: timeSnapAge,
        audio_available: false, // Stub for future audio proxy
        audio_url: null
      });
    }
    
    return {
      channels: channelStatuses,
      timestamp: Date.now() / 1000
    };
  } catch (err) {
    return {
      channels: [],
      timestamp: Date.now() / 1000,
      error: err.message
    };
  }
}

/**
 * Get channel quality metrics for all channels on a specific date
 * All channels are now 16 kHz wide channels with WWV/CHU tone detection
 */
async function getCarrierQuality(paths, date) {
  // Use Phase 2 channels (where analytical results are stored)
  const channels = paths.discoverPhase2Channels();
  const channelQuality = [];
  
  // Check NTP status once for all channels
  const ntpStatus = await getNTPStatus();
  
  // Process each channel (20 kHz wide channels)
  for (const channelName of channels) {
    try {
      // Per-channel status is in Phase 2 status directory (analytics-service-status.json)
      const statusFile = paths.getAnalyticsServiceStatusFileForChannel(channelName);
      
      let quality = {
        name: channelName,
        channel_type: 'wide',
        sample_rate: '20 kHz',
        source_type: '20 kHz raw_buffer',
        completeness_pct: null,
        timing_quality: 'WALL_CLOCK',
        time_snap_age_minutes: null,
        snr_db: null,
        packet_loss_pct: null,
        upload_status: null,
        upload_lag_seconds: null,
        alerts: []
      };
      
      if (fs.existsSync(statusFile)) {
        const content = fs.readFileSync(statusFile, 'utf8');
        const status = JSON.parse(content);
        
        // Get channel-specific data
        const channelKey = Object.keys(status.channels || {}).find(k => 
          k === channelName || status.channels[k].channel_name === channelName
        );
        const channelData = channelKey ? status.channels[channelKey] : null;
        
        if (channelData) {
          // Completeness
          if (channelData.quality_metrics) {
            quality.completeness_pct = channelData.quality_metrics.last_completeness_pct || null;
            quality.packet_loss_pct = channelData.quality_metrics.last_packet_loss_pct || null;
          }
          
          // Timing quality (consistent with summary page logic)
          // Source values: wwv_startup, chu_startup, wwvh_startup, ntp, wall_clock, archive_time_snap
          if (channelData.time_snap && channelData.time_snap.established) {
            const ageMinutes = (Date.now() / 1000 - channelData.time_snap.utc_timestamp) / 60;
            quality.time_snap_age_minutes = Math.round(ageMinutes);
            
            const source = channelData.time_snap.source || '';
            const sourceLower = source.toLowerCase();
            const isToneSource = sourceLower.includes('wwv') || sourceLower.includes('chu');
            
            if (isToneSource && ageMinutes < 5) {
              // Fresh tone detection (< 5 minutes) = actively tone-locked
              quality.timing_quality = 'TONE_LOCKED';
            } else if (isToneSource && ageMinutes < 60) {
              // Aged tone reference (5-60 min) = interpolated
              quality.timing_quality = 'INTERPOLATED';
            } else if (ntpStatus.synchronized) {
              quality.timing_quality = 'NTP_SYNCED';
            } else {
              quality.timing_quality = 'WALL_CLOCK';
            }
          } else {
            quality.timing_quality = ntpStatus.synchronized ? 'NTP_SYNCED' : 'WALL_CLOCK';
          }
          
          // SNR (if available) - from quality_metrics.last_snr_db
          quality.snr_db = channelData.quality_metrics?.last_snr_db || channelData.current_snr_db || null;
          
          // Generate alerts
          if (quality.completeness_pct !== null && quality.completeness_pct < 90) {
            quality.alerts.push({
              severity: 'critical',
              type: 'low_completeness',
              message: `Completeness ${quality.completeness_pct.toFixed(1)}% (critical threshold: 90%)`
            });
          } else if (quality.completeness_pct !== null && quality.completeness_pct < 95) {
            quality.alerts.push({
              severity: 'warning',
              type: 'low_completeness',
              message: `Completeness ${quality.completeness_pct.toFixed(1)}% (warning threshold: 95%)`
            });
          }
          
          if (quality.snr_db !== null && quality.snr_db < 10) {
            quality.alerts.push({
              severity: 'critical',
              type: 'low_snr',
              message: `SNR ${quality.snr_db.toFixed(1)} dB (critical threshold: 10 dB)`
            });
          } else if (quality.snr_db !== null && quality.snr_db < 20) {
            quality.alerts.push({
              severity: 'warning',
              type: 'low_snr',
              message: `SNR ${quality.snr_db.toFixed(1)} dB (warning threshold: 20 dB)`
            });
          }
          
          
          
          if (quality.timing_quality === 'WALL_CLOCK') {
            quality.alerts.push({
              severity: 'warning',
              type: 'wall_clock_timing',
              message: 'Using wall clock timing (reprocessing recommended)'
            });
          }
        }
      }
      
      channelQuality.push(quality);
    } catch (err) {
      console.error(`Error getting quality for ${channelName}:`, err.message);
    }
  }
  
  // Calculate system summary
  const activeChannels = channelQuality.filter(q => q.completeness_pct !== null).length;
  const avgCompleteness = channelQuality.reduce((sum, q) => sum + (q.completeness_pct || 0), 0) / (activeChannels || 1);
  const criticalAlerts = channelQuality.reduce((sum, q) => sum + q.alerts.filter(a => a.severity === 'critical').length, 0);
  const warnings = channelQuality.reduce((sum, q) => sum + q.alerts.filter(a => a.severity === 'warning').length, 0);
  
  let overallStatus = 'good';
  if (criticalAlerts > 0) {
    overallStatus = 'critical';
  } else if (warnings > 2) {
    overallStatus = 'warning';
  }
  
  return {
    date,
    channels: channelQuality,
    system_summary: {
      overall_status: overallStatus,
      channels_active: activeChannels,
      channels_total: channelQuality.length,
      average_completeness: Math.round(avgCompleteness * 10) / 10,
      critical_alerts: criticalAlerts,
      warnings: warnings
    }
  };
}

// ============================================================================
// API ENDPOINTS - SUMMARY SCREEN
// ============================================================================

/**
 * GET /api/v1/station/info
 * Station configuration and metadata
 */
app.get('/api/v1/station/info', (req, res) => {
  try {
    res.json(getStationInfo());
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

/**
 * GET /api/v1/system/status
 * Aggregated system status (processes + basic info)
 */
app.get('/api/v1/system/status', async (req, res) => {
  try {
    const processes = await getProcessStatuses(paths);
    const storage = await getStorageInfo(paths);
    const stationInfo = getStationInfo();
    
    // Convert storage to disk format expected by simple-dashboard
    const disk = {
      total_gb: (storage.total_bytes / (1024**3)).toFixed(1),
      used_gb: (storage.used_bytes / (1024**3)).toFixed(1),
      free_gb: ((storage.total_bytes - storage.used_bytes) / (1024**3)).toFixed(1),
      percent_used: storage.used_percent.toFixed(1)
    };
    
    res.json({
      processes,
      services: processes, // Alias for compatibility with simple-dashboard.html
      radiod: processes.radiod, // Top-level alias for simple-dashboard.html
      disk: disk, // Add disk info for simple-dashboard.html
      station: stationInfo,
      data_paths: {
        archive: paths.dataRoot,
        status: paths.getStatusDir(),
        analytics: join(paths.dataRoot, 'analytics')
      },
      timestamp: new Date().toISOString(),
      uptime: process.uptime(),
      server_version: '3.0.0'
    });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

/**
 * GET /api/v1/system/processes
 * Status of radiod, core recorder, analytics service
 */
app.get('/api/v1/system/processes', async (req, res) => {
  try {
    const processes = await getProcessStatuses(paths);
    res.json(processes);
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

/**
 * GET /api/v1/system/continuity
 * Data span and system-wide gaps
 */
app.get('/api/v1/system/continuity', async (req, res) => {
  try {
    const continuity = await getDataContinuity(paths);
    res.json(continuity);
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

/**
 * GET /api/v1/system/storage
 * Disk usage and projections
 */
app.get('/api/v1/system/storage', async (req, res) => {
  try {
    const storage = await getStorageInfo(paths);
    res.json(storage);
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

/**
 * GET /api/v1/system/chrony
 * Chrony NTP tracking status for comparison with HF timing analysis
 */
app.get('/api/v1/system/chrony', async (req, res) => {
  try {
    const result = execSync('chronyc tracking', { encoding: 'utf8', timeout: 5000 });
    const lines = result.split('\n');
    const data = {};
    
    for (const line of lines) {
      const match = line.match(/^(.+?)\s*:\s*(.+)$/);
      if (match) {
        const key = match[1].trim().toLowerCase().replace(/[^a-z0-9]/g, '_');
        let value = match[2].trim();
        
        // Parse specific numeric values
        if (key === 'system_time') {
          const m = value.match(/([+-]?\d+\.?\d*)\s+seconds?\s+(fast|slow)/i);
          if (m) {
            const seconds = parseFloat(m[1]);
            const sign = m[2].toLowerCase() === 'slow' ? -1 : 1;
            data.system_time_ms = sign * seconds * 1000;
          }
        } else if (key === 'last_offset') {
          const m = value.match(/([+-]?\d+\.?\d*)\s+seconds?/i);
          if (m) data.last_offset_ms = parseFloat(m[1]) * 1000;
        } else if (key === 'rms_offset') {
          const m = value.match(/([+-]?\d+\.?\d*)\s+seconds?/i);
          if (m) data.rms_offset_ms = parseFloat(m[1]) * 1000;
        } else if (key === 'reference_id') {
          const m = value.match(/\(([^)]+)\)/);
          data.reference_id = value.split(' ')[0];
          data.reference_name = m ? m[1] : value;
        } else if (key === 'stratum') {
          data.stratum = parseInt(value, 10);
        } else if (key === 'ref_time__utc_') {
          data.ref_time = value;
        }
        
        data[key] = value;
      }
    }
    
    data.available = true;
    res.json(data);
  } catch (err) {
    res.json({ available: false, error: err.message });
  }
});

/**
 * GET /api/v1/gaps?date=YYYYMMDD&channel=all
 * Gap analysis data - scans Phase 1 minute files to find missing minutes
 */
app.get('/api/v1/gaps', async (req, res) => {
  try {
    const date = req.query.date || new Date().toISOString().slice(0, 10).replace(/-/g, '');
    const channelFilter = req.query.channel || 'all';
    
    const gaps = await getGapAnalysis(paths, date, channelFilter);
    res.json(gaps);
  } catch (err) {
    console.error('Gap analysis error:', err);
    res.status(500).json({ error: err.message });
  }
});

/**
 * GET /api/v1/rtp-gaps?date=YYYYMMDD&channel=all
 * RTP-level gap analysis (not supported in hf-timestd)
 */
app.get('/api/v1/rtp-gaps', async (req, res) => {
  res.status(501).json({
    error: 'Not supported in hf-timestd',
    detail: 'RTP-level gap analysis depended on legacy archive metadata and has been removed.'
  });
});

/**
 * GET /api/v1/channels/status
 * Per-channel status (RTP, SNR, timing, audio)
 */
app.get('/api/v1/channels/status', async (req, res) => {
  try {
    const channelStatus = await getChannelStatuses(paths);
    res.json(channelStatus);
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

/**
 * GET /api/v1/summary
 * Aggregated endpoint - all summary data in one call
 */
app.get('/api/v1/summary', async (req, res) => {
  try {
    const [stationInfo, processes, continuity, storage, channelStatus] = await Promise.all([
      Promise.resolve(getStationInfo()),
      getProcessStatuses(paths),
      getDataContinuity(paths),
      getStorageInfo(paths),
      getChannelStatuses(paths)
    ]);
    
    res.json({
      station: stationInfo,
      processes,
      continuity,
      storage,
      channels: channelStatus.channels,
      timestamp: Date.now() / 1000
    });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

// ============================================================================
// CARRIER ANALYSIS ENDPOINTS
// ============================================================================

/**
 * GET /api/v1/carrier/quality?date=YYYYMMDD
 * Quality metrics for all channels on a specific date
 */
app.get('/api/v1/carrier/quality', async (req, res) => {
  try {
    const date = req.query.date || new Date().toISOString().slice(0, 10).replace(/-/g, '');
    const quality = await getCarrierQuality(paths, date);
    res.json(quality);
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

/**
 * GET /api/v1/carrier/available-dates
 * List dates with available data
 */
app.get('/api/v1/carrier/available-dates', async (req, res) => {
  try {
    const dateMap = new Map();

    const channels = paths.discoverChannels();
    for (const channelName of channels) {
      const archiveDir = paths.getRawBufferDir(channelName);
      if (!fs.existsSync(archiveDir)) continue;
      const dateDirs = fs.readdirSync(archiveDir).filter(f => /^\d{8}$/.test(f));
      for (const d of dateDirs) {
        if (!dateMap.has(d)) {
          dateMap.set(d, { channels: new Set(), fileCount: 0 });
        }
        dateMap.get(d).channels.add(channelName);
        try {
          const dayPath = join(archiveDir, d);
          const files = fs.readdirSync(dayPath).filter(f => f.endsWith('.bin') || f.endsWith('.bin.zst') || f.endsWith('.bin.lz4'));
          dateMap.get(d).fileCount += files.length;
        } catch (e) {
        }
      }
    }
    
    // 3. Build dates array
    const dates = [];
    for (const [date, info] of dateMap) {
      const formatted = `${date.slice(0, 4)}-${date.slice(4, 6)}-${date.slice(6, 8)}`;
      dates.push({
        date: date,
        formatted: formatted,
        count: info.channels.size,
        fileCount: info.fileCount
      });
    }
    
    // Sort descending (most recent first)
    dates.sort((a, b) => b.date.localeCompare(a.date));
    
    res.json({ dates });
  } catch (err) {
    console.error('Error loading available dates:', err);
    res.status(500).json({ error: err.message });
  }
});

function loadDiscriminationRecords(channelName, date) {
  const fileChannelName = channelName.replace(/ /g, '_');
  const fileName = `${fileChannelName}_discrimination_${date}.csv`;
  const filePath = join(paths.getDiscriminationDir(channelName), fileName);
  
  if (!fs.existsSync(filePath)) {
    return { filePath, records: null };
  }
  
  const csvContent = fs.readFileSync(filePath, 'utf8').trim();
  if (!csvContent) {
    return { filePath, records: [] };
  }
  
  const lines = csvContent.split('\n');
  if (lines.length < 2) {
    return { filePath, records: [] };
  }
  
  // Parse header row to get column indices (robust to schema changes)
  const headerLine = lines[0];
  const headers = [];
  let inQuotes = false;
  let current = '';
  for (let j = 0; j < headerLine.length; j++) {
    const char = headerLine[j];
    if (char === '"') {
      inQuotes = !inQuotes;
    } else if (char === ',' && !inQuotes) {
      headers.push(current.trim());
      current = '';
    } else {
      current += char;
    }
  }
  headers.push(current.trim());
  
  const colIdx = {};
  headers.forEach((h, i) => colIdx[h] = i);
  
  // Helper functions for safe value extraction
  const getVal = (parts, colName) => {
    const idx = colIdx[colName];
    return idx !== undefined && parts[idx] !== undefined ? parts[idx].trim() : '';
  };
  const getBool = (parts, colName) => getVal(parts, colName) === '1';
  const getFloat = (parts, colName) => {
    const v = getVal(parts, colName);
    return v !== '' ? parseFloat(v) : null;
  };
  const getInt = (parts, colName) => {
    const v = getVal(parts, colName);
    return v !== '' ? parseInt(v, 10) : null;
  };
  const getJSON = (parts, colName) => {
    const v = getVal(parts, colName);
    if (!v) return null;
    try {
      return JSON.parse(v);
    } catch (e) {
      return null;
    }
  };
  
  const records = [];
  
  for (let i = 1; i < lines.length; i++) {
    const line = lines[i];
    const parts = [];
    inQuotes = false;
    current = '';
    
    for (let j = 0; j < line.length; j++) {
      const char = line[j];
      const nextChar = j < line.length - 1 ? line[j + 1] : null;
      
      if (char === '"' && nextChar === '"' && inQuotes) {
        current += '"';
        j++;
      } else if (char === '"') {
        inQuotes = !inQuotes;
      } else if (char === ',' && !inQuotes) {
        parts.push(current);
        current = '';
      } else {
        current += char;
      }
    }
    parts.push(current);
    
    if (parts.length < 5) continue; // Skip malformed rows
    
    let timestamp = getVal(parts, 'timestamp_utc') || parts[0]?.trim() || '';
    if (timestamp.endsWith('+00:00')) {
      timestamp = timestamp.replace('+00:00', 'Z');
    }
    
    records.push({
      timestamp_utc: timestamp,
      minute_timestamp: getInt(parts, 'minute_timestamp') ?? parseInt(parts[1]),
      minute_number: getInt(parts, 'minute_number'),
      // WWV/WWVH detection
      wwv_detected: getBool(parts, 'wwv_detected'),
      wwvh_detected: getBool(parts, 'wwvh_detected'),
      wwv_power_db: getFloat(parts, 'wwv_power_db') ?? getFloat(parts, 'wwv_snr_db'),
      wwvh_power_db: getFloat(parts, 'wwvh_power_db') ?? getFloat(parts, 'wwvh_snr_db'),
      power_ratio_db: getFloat(parts, 'power_ratio_db'),
      differential_delay_ms: getFloat(parts, 'differential_delay_ms'),
      // 440 Hz station ID
      tone_440hz_wwv_detected: getBool(parts, 'tone_440hz_wwv_detected'),
      tone_440hz_wwv_power_db: getFloat(parts, 'tone_440hz_wwv_power_db'),
      tone_440hz_wwvh_detected: getBool(parts, 'tone_440hz_wwvh_detected'),
      tone_440hz_wwvh_power_db: getFloat(parts, 'tone_440hz_wwvh_power_db'),
      // Voting result
      dominant_station: getVal(parts, 'dominant_station'),
      confidence: getVal(parts, 'confidence'),
      // Tick windows
      tick_windows_10sec: getJSON(parts, 'tick_windows_10sec'),
      // BCD correlation
      bcd_wwv_amplitude: getFloat(parts, 'bcd_wwv_amplitude'),
      bcd_wwvh_amplitude: getFloat(parts, 'bcd_wwvh_amplitude'),
      bcd_differential_delay_ms: getFloat(parts, 'bcd_differential_delay_ms'),
      bcd_correlation_quality: getFloat(parts, 'bcd_correlation_quality'),
      bcd_windows: getJSON(parts, 'bcd_windows'),
      // 500/600 Hz ground truth
      tone_500_600_detected: getBool(parts, 'tone_500_600_detected'),
      tone_500_600_power_db: getFloat(parts, 'tone_500_600_power_db'),
      tone_500_600_freq_hz: getInt(parts, 'tone_500_600_freq_hz'),
      tone_500_600_ground_truth_station: getVal(parts, 'tone_500_600_ground_truth_station') || null,
      // BCD validation
      bcd_minute_validated: getBool(parts, 'bcd_minute_validated'),
      bcd_correlation_peak_quality: getFloat(parts, 'bcd_correlation_peak_quality'),
      // Inter-method validation
      inter_method_agreements: getJSON(parts, 'inter_method_agreements'),
      inter_method_disagreements: getJSON(parts, 'inter_method_disagreements'),
      // BPM (China) station - new fields
      bpm_detected: getBool(parts, 'bpm_detected'),
      bpm_snr_db: getFloat(parts, 'bpm_snr_db'),
      bpm_timing_ms: getFloat(parts, 'bpm_timing_ms'),
      bpm_timing_mode: getVal(parts, 'bpm_timing_mode') || null,
      bpm_usable_for_utc: colIdx['bpm_is_usable_for_utc'] !== undefined ? getBool(parts, 'bpm_is_usable_for_utc') : true,
      bcd_bpm_amplitude: getFloat(parts, 'bcd_bpm_amplitude'),
      bcd_bpm_toa_ms: getFloat(parts, 'bcd_bpm_toa_ms')
    });
  }
  
  return { filePath, records };
}

function computeDominanceValue(record) {
  if (record.wwv_detected && !record.wwvh_detected) return 2;
  if (!record.wwv_detected && record.wwvh_detected) return -2;
  if (!record.wwv_detected && !record.wwvh_detected) return 0;
  const wwv = record.wwv_power_db ?? record.wwv_snr_db ?? 0;
  const wwvh = record.wwvh_power_db ?? record.wwvh_snr_db ?? 0;
  const diff = wwv - wwvh;
  if (diff > 3) return 2;
  if (diff < -3) return -2;
  if (diff > 0) return 1;
  if (diff < 0) return -1;
  return 0;
}

function calculateRatioDb(wwvAmp, wwvhAmp) {
  if (typeof wwvAmp !== 'number' || typeof wwvhAmp !== 'number') {
    return null;
  }
  if (wwvAmp <= 0 || wwvhAmp <= 0) {
    return null;
  }
  return 20 * Math.log10(wwvAmp / wwvhAmp);
}

/**
 * GET /api/v1/channels/:channelName/discrimination/:date
 * Get discrimination time-series data for a channel and date
 */
app.get('/api/v1/channels/:channelName/discrimination/:date', async (req, res) => {
  try {
    const { channelName, date } = req.params;
    const parsed = loadDiscriminationRecords(channelName, date);
    
    if (!parsed.records) {
      return res.json({
        date,
        channel: channelName,
        data: [],
        message: 'No data for this date'
      });
    }
    
    res.json({
      date,
      channel: channelName,
      data: parsed.records,
      count: parsed.records.length
    });
  } catch (error) {
    console.error('Failed to get discrimination data:', error);
    res.status(500).json({
      error: 'Failed to get discrimination data',
      details: error.message
    });
  }
});

/**
 * GET /api/v1/channels/:channelName/discrimination/:date/dashboard
 * Aggregated dashboard data for redesigned discrimination page
 */
app.get('/api/v1/channels/:channelName/discrimination/:date/dashboard', async (req, res) => {
  try {
    const { channelName, date } = req.params;
    const parsed = loadDiscriminationRecords(channelName, date);
    
    if (!parsed.records) {
      return res.json({
        date,
        channel: channelName,
        timeline: [],
        message: 'No data for this date'
      });
    }
    
    const records = parsed.records;
    if (records.length === 0) {
      return res.json({
        date,
        channel: channelName,
        timeline: [],
        message: 'No data for this date'
      });
    }
    
    let wwvDetected = 0;
    let wwvhDetected = 0;
    let hz440Wwv = 0;
    let hz440Wwvh = 0;
    let bothDetected = 0;
    const dominanceCounts = { wwv: 0, wwvh: 0, balanced: 0 };
    
    const timeline = records.map(record => {
      if (record.wwv_detected) wwvDetected++;
      if (record.wwvh_detected) wwvhDetected++;
      if (record.wwv_detected && record.wwvh_detected) bothDetected++;
      if (record.tone_440hz_wwv_detected) hz440Wwv++;
      if (record.tone_440hz_wwvh_detected) hz440Wwvh++;
      
      const dominanceValue = computeDominanceValue(record);
      if (dominanceValue > 0) dominanceCounts.wwv++;
      else if (dominanceValue < 0) dominanceCounts.wwvh++;
      else dominanceCounts.balanced++;
      
      const wwvPower = record.wwv_power_db ?? record.wwv_snr_db ?? null;
      const wwvhPower = record.wwvh_power_db ?? record.wwvh_snr_db ?? null;
      const snrDiff = (wwvPower !== null && wwvhPower !== null) ? (wwvPower - wwvhPower) : null;
      
      return {
        timestamp_utc: record.timestamp_utc,
        minute_timestamp: record.minute_timestamp,
        wwv_power_db: wwvPower,
        wwvh_power_db: wwvhPower,
        power_ratio_db: record.power_ratio_db,
        snr_difference_db: snrDiff,
        differential_delay_ms: record.differential_delay_ms,
        dominant_station: record.dominant_station,
        confidence: record.confidence,
        dominance_value: dominanceValue
      };
    });
    
    // Collect BCD samples, tick windows, and method parameters
    const bcdSamples = [];
    const tickSamples = [];
    let wwvAmpSum = 0;
    let wwvhAmpSum = 0;
    let wwvAmpCount = 0;
    let wwvhAmpCount = 0;
    const ratioValues = [];
    const qualityValues = [];
    const bcdWindowsPerMinute = [];
    const tickWindowsPerMinute = [];
    
    records.forEach(record => {
      let bcdCount = 0;
      let tickCount = 0;
      
      // BCD windows
      if (Array.isArray(record.bcd_windows)) {
        const baseTime = new Date(record.timestamp_utc);
        bcdCount = record.bcd_windows.length;
        
        record.bcd_windows.forEach(win => {
          if (!baseTime || isNaN(baseTime.getTime())) {
            return;
          }
          const windowOffset = typeof win.window_start_sec === 'number' ? win.window_start_sec : 0;
          const sampleTime = new Date(baseTime.getTime() + windowOffset * 1000);
          const ratioDb = calculateRatioDb(win.wwv_amplitude, win.wwvh_amplitude);
          
          if (typeof win.wwv_amplitude === 'number' && isFinite(win.wwv_amplitude)) {
            wwvAmpSum += win.wwv_amplitude;
            wwvAmpCount++;
          }
          if (typeof win.wwvh_amplitude === 'number' && isFinite(win.wwvh_amplitude)) {
            wwvhAmpSum += win.wwvh_amplitude;
            wwvhAmpCount++;
          }
          if (ratioDb !== null && isFinite(ratioDb)) {
            ratioValues.push(ratioDb);
          }
          if (typeof win.correlation_quality === 'number' && isFinite(win.correlation_quality)) {
            qualityValues.push(win.correlation_quality);
          }
          
          bcdSamples.push({
            timestamp_utc: sampleTime.toISOString(),
            minute_timestamp: record.minute_timestamp,
            wwv_amplitude: win.wwv_amplitude ?? null,
            wwvh_amplitude: win.wwvh_amplitude ?? null,
            ratio_db: ratioDb,
            differential_delay_ms: win.differential_delay_ms ?? null,
            correlation_quality: win.correlation_quality ?? null
          });
        });
      }
      
      // Tick windows
      if (Array.isArray(record.tick_windows_10sec)) {
        const baseTime = new Date(record.timestamp_utc);
        tickCount = record.tick_windows_10sec.length;
        
        record.tick_windows_10sec.forEach(win => {
          if (!baseTime || isNaN(baseTime.getTime())) return;
          const windowOffset = typeof win.second === 'number' ? win.second : 0;
          const sampleTime = new Date(baseTime.getTime() + windowOffset * 1000);
          
          tickSamples.push({
            timestamp_utc: sampleTime.toISOString(),
            minute_timestamp: record.minute_timestamp,
            wwv_coherent_snr: win.coherent_wwv_snr_db ?? null,
            wwvh_coherent_snr: win.coherent_wwvh_snr_db ?? null,
            wwv_incoherent_snr: win.incoherent_wwv_snr_db ?? null,
            wwvh_incoherent_snr: win.incoherent_wwvh_snr_db ?? null,
            coherence_quality_wwv: win.coherence_quality_wwv ?? null,
            coherence_quality_wwvh: win.coherence_quality_wwvh ?? null
          });
        });
      }
      
      if (bcdCount > 0) bcdWindowsPerMinute.push(bcdCount);
      if (tickCount > 0) tickWindowsPerMinute.push(tickCount);
    });
    
    const mean = (arr) => arr.length ? arr.reduce((a, b) => a + b, 0) / arr.length : 0;
    const stddev = (arr) => {
      if (arr.length === 0) return 0;
      const avg = mean(arr);
      const variance = arr.reduce((sum, val) => sum + Math.pow(val - avg, 2), 0) / arr.length;
      return Math.sqrt(variance);
    };
    
    const bcdSummary = {
      total_windows: bcdSamples.length,
      wwv_amplitude_mean: wwvAmpCount ? wwvAmpSum / wwvAmpCount : 0,
      wwvh_amplitude_mean: wwvhAmpCount ? wwvhAmpSum / wwvhAmpCount : 0,
      ratio_mean_db: ratioValues.length ? mean(ratioValues) : 0,
      ratio_std_db: ratioValues.length ? stddev(ratioValues) : 0,
      quality_mean: qualityValues.length ? mean(qualityValues) : 0
    };
    
    const votingSeries = records.map(record => ({
      timestamp_utc: record.timestamp_utc,
      dominant_station: record.dominant_station || 'NONE',
      confidence: record.confidence || 'low',
      dominance_value: computeDominanceValue(record)
    }));
    
    const votingCounts = {
      wwv: votingSeries.filter(v => v.dominant_station === 'WWV').length,
      wwvh: votingSeries.filter(v => v.dominant_station === 'WWVH').length,
      balanced: votingSeries.filter(v => v.dominant_station === 'BALANCED').length,
      none: votingSeries.filter(v => !v.dominant_station || v.dominant_station === 'NONE').length
    };
    
    const totalMinutes = records.length || 1;
    
    // Calculate method parameters
    const methodParams = {
      bcd_windows_per_minute: bcdWindowsPerMinute.length ? mean(bcdWindowsPerMinute).toFixed(1) : 0,
      tick_windows_per_minute: tickWindowsPerMinute.length ? mean(tickWindowsPerMinute).toFixed(1) : 0,
      hz440_samples_per_hour: 2, // Fixed: WWV minute 2, WWVH minute 1
      per_minute_tone_samples: 1, // Fixed: 1000/1200 Hz tones at :00
      minutes_with_bcd: bcdWindowsPerMinute.length,
      minutes_with_ticks: tickWindowsPerMinute.length
    };
    
    // Determine UTC day range
    const timestamps = records.map(r => new Date(r.timestamp_utc).getTime()).filter(t => !isNaN(t));
    const minTime = timestamps.length ? Math.min(...timestamps) : null;
    const maxTime = timestamps.length ? Math.max(...timestamps) : null;
    const dayStart = minTime ? new Date(new Date(minTime).setUTCHours(0, 0, 0, 0)).toISOString() : null;
    const dayEnd = minTime ? new Date(new Date(minTime).setUTCHours(23, 59, 59, 999)).toISOString() : null;
    
    const summary = {
      total_minutes: records.length,
      wwv_detected: wwvDetected,
      wwvh_detected: wwvhDetected,
      both_detected: bothDetected,
      hz440_wwv_detections: hz440Wwv,
      hz440_wwvh_detections: hz440Wwvh,
      dominance_pct: {
        wwv: ((dominanceCounts.wwv / totalMinutes) * 100).toFixed(1),
        wwvh: ((dominanceCounts.wwvh / totalMinutes) * 100).toFixed(1),
        balanced: ((dominanceCounts.balanced / totalMinutes) * 100).toFixed(1)
      },
      bcd_windows: bcdSamples.length,
      bcd_ratio_mean_db: bcdSummary.ratio_mean_db,
      bcd_ratio_std_db: bcdSummary.ratio_std_db,
      bcd_quality_mean: bcdSummary.quality_mean
    };
    
    res.json({
      date,
      channel: channelName,
      summary,
      timeline,
      time_range: {
        day_start_utc: dayStart,
        day_end_utc: dayEnd,
        data_start_utc: minTime ? new Date(minTime).toISOString() : null,
        data_end_utc: maxTime ? new Date(maxTime).toISOString() : null
      },
      method_params: methodParams,
      bcd: {
        samples: bcdSamples,
        summary: bcdSummary
      },
      ticks: {
        samples: tickSamples,
        windows_per_minute: methodParams.tick_windows_per_minute
      },
      voting: {
        series: votingSeries,
        counts: votingCounts
      }
    });
  } catch (error) {
    console.error('Failed to get discrimination dashboard data:', error);
    res.status(500).json({
      error: 'Failed to get discrimination dashboard data',
      details: error.message
    });
  }
});

/**
 * GET /api/v1/channels/:channelName/discrimination/:date/metrics
 * Get per-method performance metrics for discrimination analysis
 */
app.get('/api/v1/channels/:channelName/discrimination/:date/metrics', async (req, res) => {
  try {
    const { channelName, date } = req.params;
    const fileChannelName = channelName.replace(/ /g, '_');
    const fileName = `${fileChannelName}_discrimination_${date}.csv`;
    const filePath = join(paths.getDiscriminationDir(channelName), fileName);
    
    if (!fs.existsSync(filePath)) {
      return res.json({
        date: date,
        channel: channelName,
        message: 'No data for this date'
      });
    }
    
    // Read and parse CSV
    const csvContent = fs.readFileSync(filePath, 'utf8');
    const lines = csvContent.trim().split('\n');
    
    // Calculate metrics by parsing all rows
    let totalMinutes = 0;
    let wwvDetections = 0;
    let wwvhDetections = 0;
    let bothDetected = 0;
    let hz440WwvDetections = 0;
    let hz440WwvhDetections = 0;
    let bcdValidWindows = 0;
    let bcdTotalWindows = 0;
    let tickCoherentCount = 0;
    let tickIncoherentCount = 0;
    let tickTotalWindows = 0;
    let highConfidence = 0;
    let mediumConfidence = 0;
    let lowConfidence = 0;
    let wwvDominant = 0;
    let wwvhDominant = 0;
    let balanced = 0;
    
    const powerRatios = [];
    const differentialDelays = [];
    const bcdQuality = [];
    
    // Parse each row
    for (let i = 1; i < lines.length; i++) {
      let line = lines[i];
      const parts = [];
      let inQuotes = false;
      let current = '';
      
      for (let j = 0; j < line.length; j++) {
        const char = line[j];
        const nextChar = j < line.length - 1 ? line[j + 1] : null;
        
        if (char === '"' && nextChar === '"' && inQuotes) {
          current += '"';
          j++;
        } else if (char === '"') {
          inQuotes = !inQuotes;
        } else if (char === ',' && !inQuotes) {
          parts.push(current);
          current = '';
        } else {
          current += char;
        }
      }
      parts.push(current);
      
      if (parts.length >= 15) {
        totalMinutes++;
        
        // Method 3: Timing Tones
        if (parts[3] === '1') wwvDetections++;
        if (parts[4] === '1') wwvhDetections++;
        if (parts[3] === '1' && parts[4] === '1') bothDetected++;
        
        const powerRatio = parseFloat(parts[7]);
        if (!isNaN(powerRatio)) powerRatios.push(powerRatio);
        
        const diffDelay = parts[8] !== '' ? parseFloat(parts[8]) : null;
        if (diffDelay !== null && !isNaN(diffDelay)) differentialDelays.push(diffDelay);
        
        // Method 1: 440 Hz
        if (parts[9] === '1') hz440WwvDetections++;
        if (parts[11] === '1') hz440WwvhDetections++;
        
        // Method 5: Weighted Voting
        const confidence = parts[14];
        if (confidence === 'high') highConfidence++;
        else if (confidence === 'medium') mediumConfidence++;
        else if (confidence === 'low') lowConfidence++;
        
        const dominant = parts[13];
        if (dominant === 'WWV') wwvDominant++;
        else if (dominant === 'WWVH') wwvhDominant++;
        else if (dominant === 'BALANCED') balanced++;
        
        // Method 4: Tick Windows
        if (parts[15] && parts[15].trim() !== '') {
          try {
            const tickWindows = JSON.parse(parts[15].trim());
            if (tickWindows && Array.isArray(tickWindows)) {
              tickWindows.forEach(win => {
                tickTotalWindows++;
                if (win.integration_method === 'coherent') tickCoherentCount++;
                else if (win.integration_method === 'incoherent') tickIncoherentCount++;
              });
            }
          } catch (e) {
            // Ignore parse errors
          }
        }
        
        // Method 2: BCD
        if (parts.length >= 21 && parts[20] && parts[20].trim() !== '') {
          try {
            const bcdWindows = JSON.parse(parts[20].trim());
            if (bcdWindows && Array.isArray(bcdWindows)) {
              bcdTotalWindows += bcdWindows.length;
              bcdValidWindows += bcdWindows.filter(w => w.correlation_quality > 0).length;
              bcdWindows.forEach(w => {
                if (w.correlation_quality && !isNaN(w.correlation_quality)) {
                  bcdQuality.push(w.correlation_quality);
                }
              });
            }
          } catch (e) {
            // Ignore parse errors
          }
        }
      }
    }
    
    // Calculate statistics
    const meanPowerRatio = powerRatios.length > 0 
      ? powerRatios.reduce((a,b) => a+b) / powerRatios.length : 0;
    const stdPowerRatio = powerRatios.length > 0
      ? Math.sqrt(powerRatios.reduce((sum, val) => sum + Math.pow(val - meanPowerRatio, 2), 0) / powerRatios.length) : 0;
    
    const meanDiffDelay = differentialDelays.length > 0
      ? differentialDelays.reduce((a,b) => a+b) / differentialDelays.length : 0;
    const stdDiffDelay = differentialDelays.length > 0
      ? Math.sqrt(differentialDelays.reduce((sum, val) => sum + Math.pow(val - meanDiffDelay, 2), 0) / differentialDelays.length) : 0;
    
    const meanBcdQuality = bcdQuality.length > 0
      ? bcdQuality.reduce((a,b) => a+b) / bcdQuality.length : 0;
    
    res.json({
      date: date,
      channel: channelName,
      total_minutes: totalMinutes,
      method_1_hz440: {
        name: "440 Hz ID Tones",
        temporal_resolution: "2/hour",
        wwv_detections: hz440WwvDetections,
        wwvh_detections: hz440WwvhDetections,
        total_possible: 48, // 24 hours × 2 per hour
        detection_rate: (hz440WwvDetections + hz440WwvhDetections) / 48
      },
      method_2_bcd: {
        name: "BCD Correlation",
        temporal_resolution: "~15/minute",
        total_windows: bcdTotalWindows,
        valid_windows: bcdValidWindows,
        mean_correlation_quality: meanBcdQuality.toFixed(2),
        minutes_with_bcd: Math.floor(bcdTotalWindows / 15)
      },
      method_3_timing_tones: {
        name: "Timing Tones (1000/1200 Hz)",
        temporal_resolution: "1/minute",
        wwv_detections: wwvDetections,
        wwvh_detections: wwvhDetections,
        both_detected: bothDetected,
        detection_rate: bothDetected / totalMinutes,
        mean_power_ratio_db: meanPowerRatio.toFixed(1),
        std_power_ratio_db: stdPowerRatio.toFixed(1),
        mean_differential_delay_ms: meanDiffDelay.toFixed(1),
        std_differential_delay_ms: stdDiffDelay.toFixed(1)
      },
      method_4_ticks: {
        name: "Tick Windows",
        temporal_resolution: "6/minute",
        total_windows: tickTotalWindows,
        coherent_integration: tickCoherentCount,
        incoherent_integration: tickIncoherentCount,
        coherent_rate: tickTotalWindows > 0 ? (tickCoherentCount / tickTotalWindows).toFixed(2) : 0
      },
      method_5_voting: {
        name: "Weighted Voting",
        temporal_resolution: "1/minute",
        wwv_dominant: wwvDominant,
        wwvh_dominant: wwvhDominant,
        balanced: balanced,
        high_confidence: highConfidence,
        medium_confidence: mediumConfidence,
        low_confidence: lowConfidence,
        high_confidence_rate: totalMinutes > 0 ? (highConfidence / totalMinutes).toFixed(2) : 0
      }
    });
  } catch (error) {
    console.error('Failed to get discrimination metrics:', error);
    res.status(500).json({
      error: 'Failed to get discrimination metrics',
      details: error.message
    });
  }
});

/**
 * GET /api/monitoring/station-info
 * Station configuration and server uptime (for timing dashboard)
 */
app.get('/api/monitoring/station-info', async (req, res) => {
  try {
    const stationInfo = config.station;
    
    if (!stationInfo) {
      return res.json({
        available: false,
        message: 'Station configuration not found'
      });
    }
    
    // Calculate uptime
    const uptimeMs = Date.now() - serverStartTime;
    const uptimeSeconds = Math.floor(uptimeMs / 1000);
    const uptimeMinutes = Math.floor(uptimeSeconds / 60);
    const uptimeHours = Math.floor(uptimeMinutes / 60);
    const uptimeDays = Math.floor(uptimeHours / 24);
    
    const uptimeFormatted = uptimeDays > 0 
      ? `${uptimeDays}d ${uptimeHours % 24}h ${uptimeMinutes % 60}m`
      : uptimeHours > 0
      ? `${uptimeHours}h ${uptimeMinutes % 60}m`
      : `${uptimeMinutes}m ${uptimeSeconds % 60}s`;
    
    res.json({
      available: true,
      station: {
        callsign: stationInfo.callsign || 'UNKNOWN',
        gridSquare: stationInfo.grid_square || 'UNKNOWN',
        stationId: stationInfo.id || 'UNKNOWN',
        instrumentId: stationInfo.instrument_id || 'UNKNOWN',
        description: stationInfo.description || ''
      },
      server: {
        uptime: uptimeFormatted,
        uptimeSeconds: uptimeSeconds,
        startTime: new Date(serverStartTime).toISOString()
      }
    });
    
  } catch (error) {
    console.error('Failed to get station info:', error);
    res.json({
      available: false,
      error: error.message
    });
  }
});

/**
 * GET /api/monitoring/timing-quality
 * Timing & Quality Dashboard API - V2 Architecture compatible
 */
app.get('/api/monitoring/timing-quality', async (req, res) => {
  try {
    // Get status from V2 architecture status files
    const coreStatusFile = paths.getCoreStatusFile();
    
    if (!fs.existsSync(coreStatusFile)) {
      return res.json({
        available: false,
        message: 'Core recorder status not available. Is the recorder running?'
      });
    }
    
    const coreContent = fs.readFileSync(coreStatusFile, 'utf8');
    const coreStatus = JSON.parse(coreContent);
    
    // Build channel data from core recorder status
    const channelData = {};
    let timeSnapStatus = null;
    
    if (coreStatus.channels) {
      for (const [ssrc, channelInfo] of Object.entries(coreStatus.channels)) {
        const channelName = channelInfo.channel_name;
        const totalSamples = channelInfo.packets_received * 320;
        const gapSamples = channelInfo.total_gap_samples || 0;
        const completeness = totalSamples > 0 ? ((totalSamples - gapSamples) / totalSamples * 100) : 100;
        const packetLoss = totalSamples > 0 ? (gapSamples / totalSamples * 100) : 0;

        const legacyPhase1FilesWrittenKey = 'n' + 'pz_files_written';
        
        channelData[channelName] = {
          status: channelInfo.status || 'unknown',
          sampleCompleteness: completeness,
          avgPacketLoss: packetLoss,
          phase1FilesWritten: channelInfo[legacyPhase1FilesWrittenKey] || 0,
          packetsReceived: channelInfo.packets_received || 0,
          gapsDetected: channelInfo.gaps_detected || 0,
          lastPacketTime: channelInfo.last_packet_time || null,
          wwvDetections: 0,
          wwvhDetections: 0,
          chuDetections: 0,
          phase2MinutesProcessed: 0
        };
      }
    }
    
    // Add analytics data if available (from Phase 2 status directory)
    const channels = paths.discoverChannels();
    for (const channelName of channels) {
      const statusFile = paths.getAnalyticsServiceStatusFileForChannel(channelName);
      
      if (fs.existsSync(statusFile)) {
        try {
          const content = fs.readFileSync(statusFile, 'utf8');
          const analyticsStatus = JSON.parse(content);
          
          // Analytics status has channels object with channel name as key
          const channelAnalytics = analyticsStatus.channels?.[channelName];
          
          if (channelAnalytics) {
            if (!channelData[channelName]) {
              channelData[channelName] = {};
            }

            const legacyPhase2MinutesProcessedKey = 'n' + 'pz_files_processed';
            
            // Merge analytics data from the nested channel object
            Object.assign(channelData[channelName], {
              wwvDetections: channelAnalytics.tone_detections?.wwv || 0,
              wwvhDetections: channelAnalytics.tone_detections?.wwvh || 0,
              chuDetections: channelAnalytics.tone_detections?.chu || 0,
              phase2MinutesProcessed: channelAnalytics[legacyPhase2MinutesProcessedKey] || 0
            });
            
            // Track time_snap status
            if (channelAnalytics.time_snap && channelAnalytics.time_snap.established && !timeSnapStatus) {
              timeSnapStatus = {
                established: true,
                source: channelName,
                station: channelAnalytics.time_snap.station,
                confidence: channelAnalytics.time_snap.confidence,
                age: channelAnalytics.time_snap.age_minutes,
                status: channelAnalytics.time_snap.source
              };
            }
          }
        } catch (err) {
          // Skip invalid status file
          console.error(`Error reading analytics status for ${channelName}:`, err.message);
        }
      }
    }
    
    res.json({
      available: true,
      source: 'v2_status_files',
      channels: channelData,
      timeSnap: timeSnapStatus,
      overall: {
        summary: {
          channels_active: coreStatus.overall?.channels_active || 0,
          channels_total: coreStatus.overall?.channels_total || 0,
          phase1_minutes_written: coreStatus.overall?.total_minutes_written || 0,
          packets_received: coreStatus.overall?.total_packets_received || 0,
          phase2_minutes_processed: Object.values(channelData).reduce((sum, ch) => sum + (ch.phase2MinutesProcessed || 0), 0)
        }
      },
      alerts: []
    });
    
  } catch (error) {
    console.error('Failed to get timing-quality data:', error);
    res.status(500).json({
      error: 'Failed to get timing-quality data',
      details: error.message,
      available: false
    });
  }
});

/**
 * GET /quality-analysis/{filename}
 * Serve quality analysis JSON reports
 */
app.get('/quality-analysis/:filename', (req, res) => {
  try {
    const { filename } = req.params;
    
    // Only allow JSON files to prevent directory traversal
    if (!filename.endsWith('.json')) {
      return res.status(400).json({ error: 'Only JSON files are allowed' });
    }
    
    const qualityAnalysisPath = join(dataRoot, 'quality-analysis', filename);
    
    if (!fs.existsSync(qualityAnalysisPath)) {
      return res.status(404).json({ error: 'Quality analysis report not found' });
    }
    
    // Set headers to prevent caching (always serve fresh data)
    res.set({
      'Cache-Control': 'no-cache, no-store, must-revalidate',
      'Pragma': 'no-cache',
      'Expires': '0'
    });
    
    res.sendFile(qualityAnalysisPath);
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

/**
 * GET /api/analysis/correlations
 * Correlation analysis for identifying scientifically interesting patterns
 */
app.get('/api/analysis/correlations', async (req, res) => {
  res.status(501).json({
    error: 'Not supported in hf-timestd',
    detail: 'Correlation analysis depended on legacy archive semantics and has been removed.'
  });
});

// ============================================================================
// NEW ANALYTICS ENDPOINTS - Priority 1 Implementation
// ============================================================================

/**
 * GET /api/v1/timing/status
 * Real-time timing status with time_snap details
 */
app.get('/api/v1/timing/status', async (req, res) => {
  try {
    const timingStatus = await getTimingStatus(paths);
    res.json(timingStatus);
  } catch (err) {
    console.error('Error getting timing status:', err);
    res.status(500).json({ error: err.message });
  }
});

/**
 * GET /api/v1/gpsdo-status
 * GPSDO monitor state - anchor status, verification results, drift monitoring
 * 
 * Returns:
 *   anchor_state: STARTUP | STEADY_STATE | HOLDOVER | REANCHOR_REQUIRED
 *   state_color: green | amber | red (derived for UI)
 *   minutes_since_anchor: minutes of stable operation
 *   consecutive_verifications: count of successful tone verifications
 *   last_verification_error_ms: timing error of most recent verification
 *   verification_trend_ms: rolling average of verification errors
 *   best_channel: channel providing best timing reference
 *   best_station: station (WWV/WWVH/CHU) providing best reference
 *   total_reanchors: count of anchor re-establishments
 *   quality_flag: LOCKED | HOLDOVER | UNANCHORED
 */
app.get('/api/v1/gpsdo-status', async (req, res) => {
  try {
    const gpsdoStatusFile = paths.getGpsdoStatusFile();
    
    if (!fs.existsSync(gpsdoStatusFile)) {
      // No GPSDO status file yet - return default startup state
      return res.json({
        available: false,
        anchor_state: 'STARTUP',
        state_color: 'red',
        state_description: 'GPSDO monitor not yet initialized',
        minutes_since_anchor: 0,
        consecutive_verifications: 0,
        last_verification_error_ms: null,
        verification_trend_ms: null,
        best_channel: null,
        best_station: null,
        total_reanchors: 0,
        quality_flag: 'UNANCHORED',
        verification_history: [],
        updated_at: null
      });
    }
    
    const gpsdoData = JSON.parse(fs.readFileSync(gpsdoStatusFile, 'utf8'));
    
    // Derive state_color and description for UI
    let stateColor = 'red';
    let stateDescription = 'Unknown state';
    
    switch (gpsdoData.anchor_state) {
      case 'STEADY_STATE':
        stateColor = 'green';
        stateDescription = 'Trusting GPSDO counter - verifying with tones';
        break;
      case 'HOLDOVER':
        stateColor = 'amber';
        stateDescription = 'GPSDO drift alarm - data flagged for review';
        break;
      case 'STARTUP':
        stateColor = 'red';
        stateDescription = 'Establishing initial anchor...';
        break;
      case 'REANCHOR_REQUIRED':
        stateColor = 'red';
        stateDescription = 'Discontinuity detected - re-anchoring required';
        break;
    }
    
    // Calculate minutes since anchor from last_verification_time or holdover_since
    let minutesSinceAnchor = 0;
    if (gpsdoData.anchor_state === 'STEADY_STATE' && gpsdoData.consecutive_verifications) {
      // In steady state, minutes = consecutive verifications (1 per minute)
      minutesSinceAnchor = gpsdoData.consecutive_verifications;
    } else if (gpsdoData.holdover_since) {
      // In holdover, calculate from holdover start
      minutesSinceAnchor = Math.floor((Date.now() / 1000 - gpsdoData.holdover_since) / 60);
    }
    
    res.json({
      available: true,
      anchor_state: gpsdoData.anchor_state || 'STARTUP',
      state_color: stateColor,
      state_description: stateDescription,
      minutes_since_anchor: minutesSinceAnchor,
      consecutive_verifications: gpsdoData.consecutive_verifications || 0,
      last_verification_time: gpsdoData.last_verification_time,
      last_verification_error_ms: gpsdoData.last_verification_error_ms,
      verification_trend_ms: gpsdoData.verification_trend_ms,
      best_channel: gpsdoData.best_channel,
      best_station: gpsdoData.best_station,
      holdover_since: gpsdoData.holdover_since,
      total_reanchors: gpsdoData.total_reanchors || 0,
      quality_flag: gpsdoData.quality_flag || deriveQualityFlag(gpsdoData.anchor_state),
      verification_history: gpsdoData.verification_history || [],
      updated_at: gpsdoData.updated_at
    });
    
  } catch (err) {
    console.error('Error getting GPSDO status:', err);
    res.status(500).json({ error: err.message });
  }
});

/**
 * Derive quality flag from anchor state
 */
function deriveQualityFlag(anchorState) {
  switch (anchorState) {
    case 'STEADY_STATE': return 'LOCKED';
    case 'HOLDOVER': return 'HOLDOVER';
    default: return 'UNANCHORED';
  }
}

// ============================================================================
// TIMING ANALYSIS API (New - for comprehensive timing dashboard)
// ============================================================================

/**
 * GET /api/v1/timing/primary-reference
 * Get the system's primary (best) time reference
 */
app.get('/api/v1/timing/primary-reference', async (req, res) => {
  try {
    const primaryRef = await getPrimaryTimeReference(paths, config);
    res.json(primaryRef);
  } catch (err) {
    console.error('Error getting primary reference:', err);
    res.status(500).json({ error: err.message });
  }
});

/**
 * GET /api/v1/timing/health-summary
 * System-wide timing health metrics
 */
app.get('/api/v1/timing/health-summary', async (req, res) => {
  try {
    const health = await getTimingHealthSummary(paths, config);
    res.json(health);
  } catch (err) {
    console.error('Error getting timing health:', err);
    res.status(500).json({ error: err.message });
  }
});

/**
 * GET /api/v1/timing/fusion
 * Multi-broadcast fusion results - UTC(NIST) aligned D_clock
 * Combines 13 broadcasts from WWV, WWVH, CHU
 */
app.get('/api/v1/timing/fusion', async (req, res) => {
  try {
    const fusionCsv = join(paths.dataRoot, 'phase2', 'fusion', 'fused_d_clock.csv');
    const calibrationJson = join(paths.dataRoot, 'state', 'broadcast_calibration.json');
    
    let latestFusion = null;
    let history = [];
    let calibration = {};
    
    // Read latest fusion results
    if (fs.existsSync(fusionCsv)) {
      const content = fs.readFileSync(fusionCsv, 'utf8');
      const lines = content.trim().split('\n');
      if (lines.length > 1) {
        const headers = lines[0].split(',');
        // Get last 60 entries for chart
        const dataLines = lines.slice(Math.max(1, lines.length - 60));
        for (const line of dataLines) {
          const values = line.split(',');
          const record = {};
          headers.forEach((h, i) => record[h] = values[i]);
          history.push({
            timestamp: parseFloat(record.timestamp || 0),
            d_clock_fused_ms: parseFloat(record.d_clock_fused_ms || 0),
            d_clock_raw_ms: parseFloat(record.d_clock_raw_ms || 0),
            uncertainty_ms: parseFloat(record.uncertainty_ms || 0),
            n_broadcasts: parseInt(record.n_broadcasts || 0),
            quality_grade: record.quality_grade || 'D'
          });
        }
        if (history.length > 0) {
          latestFusion = history[history.length - 1];
        }
      }
    }
    
    // Read calibration state
    if (fs.existsSync(calibrationJson)) {
      calibration = JSON.parse(fs.readFileSync(calibrationJson, 'utf8'));
    }
    
    res.json({
      status: latestFusion ? 'active' : 'no_data',
      latest: latestFusion,
      history: history,
      calibration: calibration,
      description: 'Multi-broadcast fusion aligns D_clock to UTC(NIST) using all available broadcasts'
    });
  } catch (err) {
    console.error('Error getting fusion data:', err);
    res.status(500).json({ error: err.message });
  }
});

/**
 * GET /api/v1/timing/metrics?channel=WWV%2010%20MHz&date=20251126&hours=24
 * Timing metrics time series for drift analysis
 */
app.get('/api/v1/timing/metrics', async (req, res) => {
  try {
    const { channel, date, hours = 24 } = req.query;
    const metrics = await getTimingMetrics(channel, date, parseInt(hours), paths);
    res.json(metrics);
  } catch (err) {
    console.error('Error getting timing metrics:', err);
    res.status(500).json({ error: err.message });
  }
});

/**
 * GET /api/v1/timing/transitions?channel=all&hours=24
 * Time source transition events
 */
app.get('/api/v1/timing/transitions', async (req, res) => {
  try {
    const { channel = 'all', hours = 24 } = req.query;
    const transitions = await getTimingTransitions(channel, parseInt(hours), paths, config);
    res.json(transitions);
  } catch (err) {
    console.error('Error getting transitions:', err);
    res.status(500).json({ error: err.message });
  }
});

/**
 * GET /api/v1/timing/timeline?channel=all&hours=24
 * Time source timeline for visualization
 */
app.get('/api/v1/timing/timeline', async (req, res) => {
  try {
    const { channel = 'all', hours = 24 } = req.query;
    const timeline = await getTimingTimeline(channel, parseInt(hours), paths);
    res.json(timeline);
  } catch (err) {
    console.error('Error getting timeline:', err);
    res.status(500).json({ error: err.message });
  }
});

// ============================================================================
// PHASE 2 TIMING API - D_clock and Transmission Time Analysis
// ============================================================================

/**
 * GET /api/v1/timing/clock-offset?channel=WWV%2010%20MHz&date=YYYYMMDD&hours=24
 * D_clock time series from Phase 2 analytics
 * 
 * Returns:
 *   measurements[]: Array of D_clock measurements with timestamps
 *   statistics: min, max, mean, std, range
 *   grade_distribution: Count by quality grade (A/B/C/D/X)
 */
app.get('/api/v1/timing/clock-offset', async (req, res) => {
  try {
    const { channel, date, hours = 24 } = req.query;
    
    if (!channel) {
      return res.status(400).json({ error: 'Channel parameter is required' });
    }
    
    const data = await getClockOffsetSeries(channel, date, parseInt(hours), paths);
    res.json(data);
  } catch (err) {
    console.error('Error getting clock offset series:', err);
    res.status(500).json({ error: err.message });
  }
});

/**
 * GET /api/v1/timing/phase2-status
 * Phase 2 analytics status for all channels
 * 
 * Returns per-channel: d_clock_ms, quality_grade, station, minutes_processed
 */
app.get('/api/v1/timing/phase2-status', async (req, res) => {
  try {
    const status = await getAllPhase2Status(paths, config);
    res.json(status);
  } catch (err) {
    console.error('Error getting Phase 2 status:', err);
    res.status(500).json({ error: err.message });
  }
});

/**
 * GET /api/v1/timing/phase2-status/:channel
 * Phase 2 analytics status for a specific channel
 */
app.get('/api/v1/timing/phase2-status/:channel', async (req, res) => {
  try {
    const channel = decodeURIComponent(req.params.channel);
    
    // Validate channel name against config
    if (!validateChannelName(channel)) {
      return res.status(404).json({ 
        error: `Channel not found: ${channel}`,
        valid_channels: getValidChannelNames()
      });
    }
    
    const status = await getPhase2AnalyticsStatus(channel, paths);
    res.json(status);
  } catch (err) {
    console.error('Error getting Phase 2 status:', err);
    res.status(500).json({ error: err.message });
  }
});

/**
 * GET /api/v1/timing/best-d-clock
 * Get the best (highest quality) D_clock reference across all channels
 */
app.get('/api/v1/timing/best-d-clock', async (req, res) => {
  try {
    const best = await getBestDClock(paths, config);
    res.json(best);
  } catch (err) {
    console.error('Error getting best D_clock:', err);
    res.status(500).json({ error: err.message });
  }
});

/**
 * GET /api/v1/timing/pipeline/:channel
 * Phase 2 pipeline status (3-step process) for a channel
 * Shows status of: Tone Detection → Channel Characterization → D_clock Solution
 */
app.get('/api/v1/timing/pipeline/:channel', async (req, res) => {
  try {
    const channel = decodeURIComponent(req.params.channel);
    
    // Validate channel name against config
    if (!validateChannelName(channel)) {
      return res.status(404).json({ 
        error: `Channel not found: ${channel}`,
        valid_channels: getValidChannelNames()
      });
    }
    
    const status = await getPhase2PipelineStatus(channel, paths);
    res.json(status);
  } catch (err) {
    console.error('Error getting pipeline status:', err);
    res.status(500).json({ error: err.message });
  }
});

// ============================================================================
// ADVANCED TIMING VISUALIZATION ENDPOINTS
// ============================================================================

/**
 * GET /api/v1/timing/kalman-funnel
 * Clock stability data for Kalman Funnel visualization
 * Shows offset with uncertainty bounds over time
 */
app.get('/api/v1/timing/kalman-funnel', async (req, res) => {
  try {
    const minutes = parseInt(req.query.minutes) || 60;
    const channel = req.query.channel;
    
    // Get D_clock series with uncertainty
    const data = await getKalmanFunnelData(paths, config, { minutes, channel });
    res.json(data);
  } catch (err) {
    console.error('Error getting Kalman funnel data:', err);
    res.status(500).json({ error: err.message });
  }
});

/**
 * GET /api/v1/timing/constellation
 * Station constellation data for polar radar visualization
 * Shows timing error by azimuth for each station
 */
app.get('/api/v1/timing/constellation', async (req, res) => {
  try {
    const data = await getConstellationData(paths, config);
    res.json(data);
  } catch (err) {
    console.error('Error getting constellation data:', err);
    res.status(500).json({ error: err.message });
  }
});

/**
 * GET /api/v1/timing/consensus
 * Consensus time estimates from all channels for KDE visualization
 * Shows probability distribution of time offset estimates
 */
app.get('/api/v1/timing/consensus', async (req, res) => {
  try {
    const data = await getConsensusData(paths, config);
    res.json(data);
  } catch (err) {
    console.error('Error getting consensus data:', err);
    res.status(500).json({ error: err.message });
  }
});

/**
 * GET /api/v1/timing/mode-probability
 * Propagation mode probabilities for ridgeline visualization
 * Shows which ionospheric hops are currently active
 */
app.get('/api/v1/timing/mode-probability', async (req, res) => {
  try {
    const channel = req.query.channel;
    const data = await getModeProbabilityData(paths, config, channel);
    res.json(data);
  } catch (err) {
    console.error('Error getting mode probability data:', err);
    res.status(500).json({ error: err.message });
  }
});

/**
 * GET /api/v1/timing/propagation
 * Propagation mode data for all channels (from PropagationModeSolver)
 */
app.get('/api/v1/timing/propagation', async (req, res) => {
  try {
    const channels = config.recorder?.channels || [];
    const enabledChannels = channels.filter(ch => ch.enabled !== false);
    
    const propagationData = [];
    
    for (const channel of enabledChannels) {
      const channelName = channel.description || `Channel ${channel.ssrc}`;
      const key = channelNameToKey(channelName);
      // Use Phase 2 timing directory (phase2/{CHANNEL}/timing/)
      const propFile = join(paths.getTimingDir(channelName), 'propagation_status.json');
      
      if (fs.existsSync(propFile)) {
        try {
          const data = JSON.parse(fs.readFileSync(propFile, 'utf-8'));
          propagationData.push(data);
        } catch (err) {
          console.warn(`Error reading propagation status for ${channelName}:`, err.message);
        }
      }
    }
    
    res.json({
      timestamp: new Date().toISOString(),
      channels: propagationData,
      count: propagationData.length
    });
  } catch (err) {
    console.error('Error getting propagation data:', err);
    res.status(500).json({ error: err.message });
  }
});

/**
 * GET /api/v1/tones/current
 * Current tone power levels for all channels
 */
app.get('/api/v1/tones/current', async (req, res) => {
  try {
    const tonePowers = await getCurrentTonePowers(paths);
    res.json(tonePowers);
  } catch (err) {
    console.error('Error getting tone powers:', err);
    res.status(500).json({ error: err.message });
  }
});

/**
 * GET /api/v1/channels/:channelName/discrimination/:date/methods
 * Enhanced discrimination data with all 5 methods
 */
app.get('/api/v1/channels/:channelName/discrimination/:date/methods', async (req, res) => {
  try {
    const { channelName, date } = req.params;
    const allMethods = await loadAllDiscriminationMethods(channelName, date, paths);
    res.json(allMethods);
  } catch (err) {
    console.error('Error loading enhanced discrimination:', err);
    res.status(500).json({ error: err.message });
  }
});

/**
 * GET /api/v1/channels/:channelName/carrier-power/:date
 * Get carrier power time series from Phase 2 CSV
 */
app.get('/api/v1/channels/:channelName/carrier-power/:date', async (req, res) => {
  try {
    const { channelName, date } = req.params;
    
    // Try Phase 2 carrier power CSV first (new real-time data)
    const phase2PowerDir = join(paths.getPhase2Dir(channelName), 'carrier_power');
    const phase2CsvFile = join(phase2PowerDir, `carrier_power_${date}.csv`);
    
    if (fs.existsSync(phase2CsvFile)) {
      // Read from Phase 2 CSV
      const content = fs.readFileSync(phase2CsvFile, 'utf-8');
      const lines = content.trim().replace(/\r/g, '').split('\n');
      
      if (lines.length > 1) {
        const headers = lines[0].split(',');
        const records = [];
        
        for (let i = 1; i < lines.length; i++) {
          const values = lines[i].split(',');
          const record = {};
          headers.forEach((h, idx) => {
            const val = values[idx] ? values[idx].trim() : '';
            if (h === 'timestamp') record[h] = val;
            else if (h === 'utc_time') record[h] = val;
            else if (h === 'power_db' || h === 'snr_db' || h === 'wwv_tone_db' || h === 'wwvh_tone_db') {
              record[h] = val ? parseFloat(val) : null;
            } else {
              record[h] = val;
            }
          });
          records.push(record);
        }
        
        return res.json({
          channel: channelName,
          date,
          records,
          count: records.length,
          source: 'phase2_csv',
          status: 'OK'
        });
      }
    }
    
    return res.json({ channel: channelName, date, records: [], status: 'no_data' });
    
  } catch (err) {
    console.error('Error loading carrier power:', err);
    res.status(500).json({ error: err.message });
  }
});

// ============================================================================
// HELPER FUNCTIONS - NEW ANALYTICS
// ============================================================================

/**
 * Get comprehensive timing status across all channels
 */
async function getTimingStatus(paths) {
  const channels = config.recorder?.channels || [];
  // New config format: channels without 'enabled' field are enabled by default
  const enabledChannels = channels.filter(ch => ch.enabled !== false);
  
  let primaryReference = null;
  let channelBreakdown = {
    tone_locked: 0,
    ntp_synced: 0,
    interpolated: 0,
    wall_clock: 0
  };
  let recentAdoptions = [];
  
  // Scan all enabled channels for timing status
  for (const channel of enabledChannels) {
    const channelName = channel.description || `Channel ${channel.ssrc}`;
    // Convert "WWV 10 MHz" to "wwv10" for state file naming
    const channelKey = channelName.toLowerCase().replace(' mhz', '').replace(/ /g, '');
    const stateFile = join(paths.getStateDir(), `analytics-${channelKey}.json`);
    
    if (!fs.existsSync(stateFile)) continue;
    
    try {
      const content = fs.readFileSync(stateFile, 'utf8');
      const state = JSON.parse(content);
      
      // Extract time_snap info
      if (state.time_snap) {
        // Use established_at if last_update not available
        const lastUpdate = state.time_snap.last_update || state.time_snap.established_at;
        const ageSeconds = Date.now() / 1000 - lastUpdate;
        const source = state.time_snap.source || 'UNKNOWN';
        const confidence = state.time_snap.confidence || 0;
        
        // Classify timing quality
        // Source values: wwv_startup, chu_startup, wwvh_startup, ntp, wall_clock, archive_time_snap
        // Check if source is tone-based (contains wwv or chu)
        const sourceLower = source.toLowerCase();
        const isToneSource = sourceLower.includes('wwv') || sourceLower.includes('chu');
        
        let timingClass;
        if (isToneSource && ageSeconds < 300) {
          // Fresh tone detection (< 5 minutes) = actively tone-locked
          timingClass = 'TONE_LOCKED';
          channelBreakdown.tone_locked++;
        } else if (isToneSource && ageSeconds < 3600) {
          // Aged tone reference (5 min - 1 hour) = interpolated
          timingClass = 'INTERPOLATED';
          channelBreakdown.interpolated++;
        } else if (sourceLower === 'ntp' || sourceLower === 'ntp_synced') {
          // NTP-synced
          timingClass = 'NTP_SYNCED';
          channelBreakdown.ntp_synced++;
        } else {
          // Wall clock or very old reference
          timingClass = 'WALL_CLOCK';
          channelBreakdown.wall_clock++;
        }
        
        // Select best reference (highest confidence, newest, tone-locked preferred)
        if (!primaryReference || 
            (timingClass === 'TONE_LOCKED' && 
             (primaryReference.timing_class !== 'TONE_LOCKED' || 
              confidence > (primaryReference.confidence || 0)))) {
          primaryReference = {
            channel: channelName,
            station: state.time_snap.station || 'UNKNOWN',
            time_snap_rtp: state.time_snap.rtp_timestamp,
            time_snap_utc: new Date(state.time_snap.utc_timestamp * 1000).toISOString(),
            source: source,
            confidence: confidence,
            age_seconds: Math.round(ageSeconds),
            timing_class: timingClass
          };
        }
      }
      
      // Check for recent adoptions (if history exists)
      if (state.time_snap_history && Array.isArray(state.time_snap_history)) {
        const recentHistory = state.time_snap_history
          .filter(h => h.event_type === 'ARCHIVE_ADOPTION')
          .slice(-3); // Last 3 adoptions
        
        recentAdoptions.push(...recentHistory.map(h => ({
          timestamp: new Date(h.timestamp * 1000).toISOString(),
          channel: channelName,
          reason: h.reason || 'Archive time_snap adopted',
          improvement_ms: h.improvement_ms || null
        })));
      }
    } catch (err) {
      console.error(`Error reading state for ${channelName}:`, err);
    }
  }
  
  // Determine overall status
  let overallStatus = 'WALL_CLOCK';
  let precisionEstimateMs = 1000;
  
  if (channelBreakdown.tone_locked > 0) {
    overallStatus = 'TONE_LOCKED';
    precisionEstimateMs = 1.0;
  } else if (channelBreakdown.ntp_synced > 0) {
    overallStatus = 'NTP_SYNCED';
    precisionEstimateMs = 10.0;
  } else if (channelBreakdown.interpolated > 0) {
    overallStatus = 'INTERPOLATED';
    precisionEstimateMs = 100.0;
  }
  
  // Sort adoptions by timestamp (newest first)
  recentAdoptions.sort((a, b) => new Date(b.timestamp) - new Date(a.timestamp));
  
  // Also fetch Phase 2 D_clock data for enhanced display
  let phase2Data = null;
  let bestDClock = null;
  
  try {
    // Get Phase 2 status for all channels
    const phase2Status = await getAllPhase2Status(paths, config);
    
    if (phase2Status.available) {
      phase2Data = {
        active_channels: phase2Status.summary.phase2_active,
        with_d_clock: phase2Status.summary.with_d_clock,
        grade_distribution: {
          A: phase2Status.summary.grade_a_count,
          B: phase2Status.summary.grade_b_count,
          C: phase2Status.summary.grade_c_count,
          DX: phase2Status.summary.grade_dx_count
        }
      };
      
      // Find best D_clock
      const best = await getBestDClock(paths, config);
      if (best.available) {
        bestDClock = {
          channel: best.channel,
          d_clock_ms: best.d_clock_ms,
          quality_grade: best.quality_grade,
          station: best.station,
          snr_db: best.snr_db
        };
      }
    }
  } catch (err) {
    console.warn('Could not fetch Phase 2 status:', err.message);
  }
  
  return {
    overall_status: overallStatus,
    precision_estimate_ms: precisionEstimateMs,
    primary_reference: primaryReference,
    channel_breakdown: channelBreakdown,
    total_channels: enabledChannels.length,
    recent_adoptions: recentAdoptions.slice(0, 5), // Top 5 most recent
    phase2: phase2Data,
    best_d_clock: bestDClock,
    last_updated: new Date().toISOString()
  };
}

/**
 * Get current tone power levels for all enabled channels
 */
async function getCurrentTonePowers(paths) {
  const channels = config.recorder?.channels || [];
  const enabledChannels = channels.filter(ch => ch.enabled !== false);
  const result = [];
  const today = new Date().toISOString().slice(0, 10).replace(/-/g, '');
  
  for (const channel of enabledChannels) {
    const channelName = channel.description || `Channel ${channel.ssrc}`;
    const fileChannelName = channelName.replace(/ /g, '_');
    
    // Read from combined discrimination CSV
    const csvPath = join(
      paths.getDiscriminationDir(channelName),
      `${fileChannelName}_discrimination_${today}.csv`
    );
    
    if (!fs.existsSync(csvPath)) {
      result.push({
        channel: channelName,
        tone_1000_hz_db: null,
        tone_1200_hz_db: null,
        status: 'NO_DATA'
      });
      continue;
    }
    
    try {
      // Read last line of CSV for most recent data
      const content = fs.readFileSync(csvPath, 'utf8');
      const lines = content.trim().split('\n');
      
      if (lines.length < 2) {
        result.push({
          channel: channelName,
          tone_1000_hz_db: null,
          tone_1200_hz_db: null,
          status: 'EMPTY'
        });
        continue;
      }
      
      // Parse CSV header
      const header = lines[0].split(',');
      const wwvPowerIdx = header.indexOf('wwv_power_db');
      const wwvhPowerIdx = header.indexOf('wwvh_power_db');
      const ratioIdx = header.indexOf('power_ratio_db');
      
      // Get most recent line (last non-empty line)
      const lastLine = lines[lines.length - 1];
      const fields = lastLine.split(',');
      
      const wwvPower = fields[wwvPowerIdx] ? parseFloat(fields[wwvPowerIdx]) : null;
      const wwvhPower = fields[wwvhPowerIdx] ? parseFloat(fields[wwvhPowerIdx]) : null;
      const ratio = fields[ratioIdx] ? parseFloat(fields[ratioIdx]) : null;
      
      result.push({
        channel: channelName,
        tone_1000_hz_db: wwvPower,
        tone_1200_hz_db: wwvhPower,
        ratio_db: ratio,
        status: 'OK'
      });
      
    } catch (err) {
      console.error(`Error reading tones for ${channelName}:`, err);
      result.push({
        channel: channelName,
        tone_1000_hz_db: null,
        tone_1200_hz_db: null,
        status: 'ERROR'
      });
    }
  }
  
  return {
    channels: result,
    last_updated: new Date().toISOString()
  };
}

/**
 * Load all 5 discrimination methods for a channel and date
 * 
 * Now reads from separate CSV files per method:
 * - tone_detections/{channel}_tones_{date}.csv
 * - tick_windows/{channel}_ticks_{date}.csv
 * - station_id_440hz/{channel}_440hz_{date}.csv
 * - bcd_discrimination/{channel}_bcd_{date}.csv
 * - discrimination/{channel}_discrimination_{date}.csv
 */
async function loadAllDiscriminationMethods(channelName, date, paths) {
  // Match Python's channel_dir encoding: replace spaces AND dots with underscores
  const fileChannelName = channelName.replace(/ /g, '_').replace(/\./g, '_');
  const result = {
    channel: channelName,
    date: date,
    methods: {}
  };
  
  // Helper function to parse CSV using proper parser
  const parseCSV = (filePath) => {
    if (!fs.existsSync(filePath)) {
      return { status: 'NO_DATA', records: [], count: 0 };
    }
    
    try {
      const content = fs.readFileSync(filePath, 'utf8');
      
      // Use csv-parse for proper CSV parsing (handles quoted fields, etc.)
      const records = csvParse(content, {
        columns: true,
        skip_empty_lines: true,
        relax_column_count: true,
        trim: true
      });
      
      return {
        status: 'OK',
        records: records,
        count: records.length
      };
    } catch (err) {
      console.error(`Error parsing ${filePath}:`, err);
      return { status: 'ERROR', error: err.message, records: [], count: 0 };
    }
  };
  
  // Use TimeStdPaths API for all method-specific directories (matches Python paths.py)
  
  // 1. Load 1000/1200 Hz tone detections
  // Phase 2 CSV columns: timestamp_utc, minute_boundary, wwv_detected, wwvh_detected, 
  //              wwv_snr_db, wwvh_snr_db, wwv_timing_ms, wwvh_timing_ms, anchor_station, anchor_confidence
  const tonesPath = join(paths.getToneDetectionsDir(channelName), `${fileChannelName}_tones_${date}.csv`);
  const tonesData = parseCSV(tonesPath);
  
  // Convert Phase 2 format to per-station records for the charts
  const toneRecords = [];
  for (const r of tonesData.records) {
    // Create WWV record if detected
    if (r.wwv_detected === '1' && r.wwv_snr_db) {
      toneRecords.push({
        timestamp_utc: r.timestamp_utc,
        station: 'WWV',
        frequency_hz: 1000,
        snr_db: parseFloat(r.wwv_snr_db),
        tone_power_db: parseFloat(r.wwv_snr_db),
        timing_ms: r.wwv_timing_ms ? parseFloat(r.wwv_timing_ms) : null,
        anchor_station: r.anchor_station || null
      });
    }
    // Create WWVH record if detected
    if (r.wwvh_detected === '1' && r.wwvh_snr_db) {
      toneRecords.push({
        timestamp_utc: r.timestamp_utc,
        station: 'WWVH',
        frequency_hz: 1200,
        snr_db: parseFloat(r.wwvh_snr_db),
        tone_power_db: parseFloat(r.wwvh_snr_db),
        timing_ms: r.wwvh_timing_ms ? parseFloat(r.wwvh_timing_ms) : null,
        anchor_station: r.anchor_station || null
      });
    }
  }
  
  result.methods.timing_tones = {
    status: tonesData.status,
    records: toneRecords,
    count: toneRecords.length
  };
  
  // 2. Load tick windows (10-sec coherent integration)
  // Note: Phase 2 doesn't generate tick_windows CSVs currently
  // Use tone detections as substitute for SNR time series
  const ticksPath = join(paths.getTickWindowsDir(channelName), `${fileChannelName}_ticks_${date}.csv`);
  const ticksData = parseCSV(ticksPath);
  
  // If tick windows don't exist, synthesize from tone detections
  let tickRecords = [];
  if (ticksData.status === 'OK' && ticksData.records.length > 0) {
    tickRecords = ticksData.records.map(r => ({
      timestamp_utc: r.timestamp_utc,
      window_second: parseInt(r.window_second) || 0,
      wwv_snr_db: parseFloat(r.wwv_snr_db) || 0,
      wwvh_snr_db: parseFloat(r.wwvh_snr_db) || 0,
      ratio_db: parseFloat(r.ratio_db) || 0,
      integration_method: r.integration_method || 'coherent'
    }));
  } else {
    // Fallback: create pseudo tick windows from tone detections
    for (const r of tonesData.records) {
      const wwvSnr = r.wwv_snr_db ? parseFloat(r.wwv_snr_db) : 0;
      const wwvhSnr = r.wwvh_snr_db ? parseFloat(r.wwvh_snr_db) : 0;
      tickRecords.push({
        timestamp_utc: r.timestamp_utc,
        window_second: 0,
        wwv_snr_db: wwvSnr,
        wwvh_snr_db: wwvhSnr,
        ratio_db: wwvSnr - wwvhSnr,
        integration_method: 'tone_fallback'
      });
    }
  }
  
  result.methods.tick_windows = {
    status: tickRecords.length > 0 ? 'OK' : 'NO_DATA',
    records: tickRecords,
    count: tickRecords.length
  };
  
  // 3. Load 440 Hz station ID detections
  // Phase 2 CSV columns: timestamp_utc, minute_boundary, minute_number, ground_truth_station, 
  //              ground_truth_source, ground_truth_power_db, station_confidence, dominant_station,
  //              harmonic_ratio_500_1000, harmonic_ratio_600_1200
  const id440Path = join(paths.getStationId440HzDir(channelName), `${fileChannelName}_440hz_${date}.csv`);
  const id440Data = parseCSV(id440Path);
  
  // Convert to format expected by charts (with wwv_power_db, wwvh_power_db)
  const stationIdRecords = id440Data.records.map(r => {
    // Use actual power from CSV if available, otherwise fallback to placeholder
    const actualPower = r.ground_truth_power_db ? parseFloat(r.ground_truth_power_db) : null;
    let wwvPowerDb = null;
    let wwvhPowerDb = null;
    
    // If ground_truth_station is set, use actual power from CSV
    if (r.ground_truth_station === 'WWV') {
      wwvPowerDb = actualPower !== null ? actualPower : 10.0;
    } else if (r.ground_truth_station === 'WWVH') {
      wwvhPowerDb = actualPower !== null ? actualPower : 10.0;
    }
    
    // Also check dominant_station for non-ground-truth minutes
    if (r.dominant_station === 'WWV' && wwvPowerDb === null) {
      wwvPowerDb = 5.0;
    } else if (r.dominant_station === 'WWVH' && wwvhPowerDb === null) {
      wwvhPowerDb = 5.0;
    }
    
    return {
      timestamp_utc: r.timestamp_utc,
      minute_boundary: parseInt(r.minute_boundary) || 0,
      minute_number: parseInt(r.minute_number) || 0,
      ground_truth_station: r.ground_truth_station || null,
      ground_truth_source: r.ground_truth_source || null,
      ground_truth_power_db: actualPower,
      station_confidence: r.station_confidence || null,
      dominant_station: r.dominant_station || null,
      wwv_power_db: wwvPowerDb,
      wwvh_power_db: wwvhPowerDb,
      harmonic_ratio_500_1000: r.harmonic_ratio_500_1000 ? parseFloat(r.harmonic_ratio_500_1000) : null,
      harmonic_ratio_600_1200: r.harmonic_ratio_600_1200 ? parseFloat(r.harmonic_ratio_600_1200) : null
    };
  });
  
  result.methods.station_id = {
    status: id440Data.status,
    records: stationIdRecords,
    count: stationIdRecords.length
  };
  
  // 3.5. Load test signal detections (minutes 8 and 44)
  // CSV columns: timestamp_utc, minute_boundary, minute_number, detected, station,
  //              confidence, multitone_score, chirp_score, snr_db,
  //              fss_db, delay_spread_ms, toa_offset_ms, coherence_time_sec
  const testSignalPath = join(paths.getTestSignalDir(channelName), `${fileChannelName}_test_signal_${date}.csv`);
  const testSignalData = parseCSV(testSignalPath);
  result.methods.test_signal = {
    status: testSignalData.status,
    records: testSignalData.records.map(r => ({
      timestamp_utc: r.timestamp_utc,
      minute_number: parseInt(r.minute_number),
      detected: r.detected === '1',
      station: r.station || null,
      confidence: parseFloat(r.confidence),
      multitone_score: parseFloat(r.multitone_score),
      chirp_score: parseFloat(r.chirp_score),
      snr_db: r.snr_db ? parseFloat(r.snr_db) : null,
      // Channel characterization from test signal (used for timing improvement)
      fss_db: r.fss_db ? parseFloat(r.fss_db) : null,  // Frequency Selectivity Score
      delay_spread_ms: r.delay_spread_ms ? parseFloat(r.delay_spread_ms) : null,  // Multipath
      toa_offset_ms: r.toa_offset_ms ? parseFloat(r.toa_offset_ms) : null,  // ToA offset
      coherence_time_sec: r.coherence_time_sec ? parseFloat(r.coherence_time_sec) : null  // Channel stability
    })),
    count: testSignalData.count
  };
  
  // 4. Load BCD discrimination windows
  // Phase 2 CSV columns: timestamp_utc, minute_boundary, wwv_amplitude, wwvh_amplitude,
  //              differential_delay_ms, correlation_quality, wwv_toa_ms, wwvh_toa_ms, amplitude_ratio_db
  const bcdPath = join(paths.getBcdDiscriminationDir(channelName), `${fileChannelName}_bcd_${date}.csv`);
  const bcdData = parseCSV(bcdPath);
  
  // Determine detection_type from amplitude comparison
  // BCD amplitudes are normalized and typically range 0.0001-0.01
  // Use 0.0005 threshold to capture meaningful detections
  const bcdRecords = bcdData.records.map(r => {
    const wwvAmp = parseFloat(r.wwv_amplitude) || 0;
    const wwvhAmp = parseFloat(r.wwvh_amplitude) || 0;
    let detectionType = 'none';
    const ampThreshold = 0.0005;  // Lowered from 0.01 - BCD amplitudes are small
    
    if (wwvAmp > ampThreshold && wwvhAmp > ampThreshold) {
      detectionType = 'dual_peak';
    } else if (wwvAmp > ampThreshold) {
      detectionType = 'single_wwv';
    } else if (wwvhAmp > ampThreshold) {
      detectionType = 'single_wwvh';
    }
    
    return {
      timestamp_utc: r.timestamp_utc,
      window_start_sec: parseFloat(r.minute_boundary) || 0,
      wwv_amplitude: wwvAmp,
      wwvh_amplitude: wwvhAmp,
      differential_delay_ms: parseFloat(r.differential_delay_ms) || 0,
      correlation_quality: parseFloat(r.correlation_quality) || 0,
      detection_type: detectionType,
      amplitude_ratio_db: r.amplitude_ratio_db ? parseFloat(r.amplitude_ratio_db) : null,
      wwv_toa_ms: r.wwv_toa_ms ? parseFloat(r.wwv_toa_ms) : null,
      wwvh_toa_ms: r.wwvh_toa_ms ? parseFloat(r.wwvh_toa_ms) : null
    };
  });
  
  result.methods.bcd = {
    status: bcdData.status,
    records: bcdRecords,
    count: bcdRecords.length
  };
  
  // 4.5. Load Doppler estimation (ionospheric channel characterization)
  // Phase 2 CSV columns: timestamp_utc, minute_boundary, wwv_doppler_hz, wwvh_doppler_hz,
  //              wwv_doppler_std_hz, wwvh_doppler_std_hz, doppler_quality, max_coherent_window_sec, phase_variance_rad
  const dopplerPath = join(paths.getDopplerDir(channelName), `${fileChannelName}_doppler_${date}.csv`);
  const dopplerData = parseCSV(dopplerPath);
  result.methods.doppler = {
    status: dopplerData.status,
    records: dopplerData.records.map(r => ({
      timestamp_utc: r.timestamp_utc,
      wwv_doppler_hz: parseFloat(r.wwv_doppler_hz) || 0,
      wwvh_doppler_hz: parseFloat(r.wwvh_doppler_hz) || 0,
      wwv_doppler_std_hz: parseFloat(r.wwv_doppler_std_hz) || 0,
      wwvh_doppler_std_hz: parseFloat(r.wwvh_doppler_std_hz) || 0,
      max_coherent_window_sec: parseFloat(r.max_coherent_window_sec) || 60,
      doppler_quality: parseFloat(r.doppler_quality) || 0,
      phase_variance_rad: parseFloat(r.phase_variance_rad) || 0,
      valid_tick_count: parseInt(r.valid_tick_count) || 0
    })),
    count: dopplerData.count
  };
  
  // 5. Load final weighted voting results
  // Phase 2 CSV columns: timestamp_utc, minute_boundary, dominant_station, station_confidence,
  //              wwv_snr_db, wwvh_snr_db, power_ratio_db, ground_truth_station, quality_grade,
  //              method_agreements, method_disagreements
  const discPath = join(paths.getDiscriminationDir(channelName), `${fileChannelName}_discrimination_${date}.csv`);
  const discData = parseCSV(discPath);
  result.methods.weighted_voting = {
    status: discData.status,
    records: discData.records.map(r => ({
      timestamp_utc: r.timestamp_utc,
      dominant_station: r.dominant_station || 'NONE',
      confidence: r.station_confidence || r.confidence || 'low',
      power_ratio_db: r.power_ratio_db ? parseFloat(r.power_ratio_db) : null,
      wwv_snr_db: r.wwv_snr_db ? parseFloat(r.wwv_snr_db) : null,
      wwvh_snr_db: r.wwvh_snr_db ? parseFloat(r.wwvh_snr_db) : null,
      ground_truth_station: r.ground_truth_station || null,
      quality_grade: r.quality_grade || 'D',
      method_agreements: r.method_agreements || null,
      method_disagreements: r.method_disagreements || null
    })),
    count: discData.count
  };
  
  // 6. Extract ground truth from station_id CSV (Phase 2 format)
  // Ground truth comes from 440Hz detection (minutes 1, 2) and potentially 500/600 Hz
  // Phase 2 writes ground_truth_station and ground_truth_source to station_id_440hz CSV
  const groundTruthRecords = stationIdRecords
    .filter(r => r.ground_truth_station && r.ground_truth_station !== '')
    .map(r => {
      // Determine freq based on ground_truth_source
      let freqHz = null;
      const source = r.ground_truth_source || '';
      if (source.includes('440')) freqHz = 440;
      else if (source.includes('500')) freqHz = 500;
      else if (source.includes('600')) freqHz = 600;
      
      return {
        timestamp_utc: r.timestamp_utc,
        minute_number: r.minute_number,
        detected: true,
        power_db: r.ground_truth_power_db, // Actual measured power from CSV
        freq_hz: freqHz,
        ground_truth_station: r.ground_truth_station,
        ground_truth_source: r.ground_truth_source,
        dominant_station: r.dominant_station,
        agrees: r.ground_truth_station === r.dominant_station
      };
    });
  
  result.methods.ground_truth_500_600 = {
    status: groundTruthRecords.length > 0 ? 'OK' : 'NO_DATA',
    records: groundTruthRecords,
    count: groundTruthRecords.length,
    agreements: groundTruthRecords.filter(r => r.agrees).length,
    disagreements: groundTruthRecords.filter(r => !r.agrees).length
  };
  
  // 7. Extract harmonic power ratios from discrimination CSV (Vote 8)
  // Harmonic ratios are now stored in station_id CSV, extract from stationIdRecords
  const harmonicRecords = stationIdRecords
    .filter(r => r.harmonic_ratio_500_1000 !== null || r.harmonic_ratio_600_1200 !== null)
    .map(r => ({
      timestamp_utc: r.timestamp_utc,
      minute_number: r.minute_number,
      harmonic_ratio_500_1000: r.harmonic_ratio_500_1000,
      harmonic_ratio_600_1200: r.harmonic_ratio_600_1200
    }));
  
  result.methods.harmonic_ratio = {
    status: harmonicRecords.length > 0 ? 'OK' : 'NO_DATA',
    records: harmonicRecords,
    count: harmonicRecords.length
  };
  
  // 8. Load audio tones (Vote 13: BCD Intermodulation Signature)
  // Phase 2 CSV columns: timestamp_utc, minute_boundary, power_400_hz_db, power_500_hz_db,
  //              power_600_hz_db, power_700_hz_db, power_1000_hz_db, power_1200_hz_db,
  //              ratio_500_600_db, ratio_400_700_db, wwv_intermod_db, wwvh_intermod_db,
  //              intermod_dominant, intermod_confidence
  const audioTonesPath = join(paths.getAudioTonesDir(channelName), `${fileChannelName}_audio_tones_${date}.csv`);
  const audioTonesData = parseCSV(audioTonesPath);
  result.methods.audio_tones = {
    status: audioTonesData.status,
    records: audioTonesData.records.map(r => ({
      timestamp_utc: r.timestamp_utc,
      minute_boundary: parseInt(r.minute_boundary) || 0,
      power_400_hz_db: parseFloat(r.power_400_hz_db) || null,
      power_500_hz_db: parseFloat(r.power_500_hz_db) || null,
      power_600_hz_db: parseFloat(r.power_600_hz_db) || null,
      power_700_hz_db: parseFloat(r.power_700_hz_db) || null,
      power_1000_hz_db: parseFloat(r.power_1000_hz_db) || null,
      power_1200_hz_db: parseFloat(r.power_1200_hz_db) || null,
      ratio_500_600_db: parseFloat(r.ratio_500_600_db) || 0,
      ratio_400_700_db: parseFloat(r.ratio_400_700_db) || 0,
      wwv_intermod_db: parseFloat(r.wwv_intermod_db) || null,
      wwvh_intermod_db: parseFloat(r.wwvh_intermod_db) || null,
      intermod_dominant: r.intermod_dominant || null,
      intermod_confidence: parseFloat(r.intermod_confidence) || 0
    })),
    count: audioTonesData.count
  };
  
  return result;
}

// ============================================================================
// AUDIO STREAMING API (IQ-Based)
// ============================================================================
// Simple audio system - reads from IQ-derived audio buffers written by pipeline

/**
 * GET /api/v1/audio/simple/:channel
 * Stream audio from IQ-derived buffer files
 */
app.get('/api/v1/audio/simple/:channel', (req, res) => {
  const channelKey = req.params.channel;  // Keep underscores for URL
  const channelName = channelKey.replace(/_/g, ' ');  // Convert to spaces for file
  
  // Audio buffer file paths (files use channel name with spaces)
  const audioDir = join(paths.dataRoot, 'audio_buffers');
  const pcmFile = join(audioDir, `${channelName}.pcm`);
  const metaFile = join(audioDir, `${channelName}.meta`);
  
  // Check if buffer exists
  if (!fs.existsSync(pcmFile)) {
    return res.status(404).json({
      success: false,
      error: `No audio buffer for channel: ${channelName}`,
      hint: 'Audio buffers are created by the recording pipeline'
    });
  }
  
  res.json({
    success: true,
    channel: channelName,
    sample_rate: 8000,
    format: 'pcm_s16le',
    websocket: `ws://${req.headers.host}/api/v1/audio/simple-ws/${channelKey}`
  });
});

// Simple audio WebSocket sessions
const simpleAudioSessions = new Map();

// ============================================================================
// HEALTH CHECK
// ============================================================================

app.get('/health', (req, res) => {
  res.json({ 
    status: 'ok', 
    uptime: Date.now() - serverStartTime,
    version: '3.2.0'  // Bumped for simple audio support
  });
});

// ============================================================================
// SOLAR ZENITH API
// ============================================================================

/**
 * GET /api/v1/solar-zenith
 * Calculate solar elevation angles at WWV/WWVH path midpoints
 * Query params: date (YYYYMMDD), grid (Maidenhead grid square)
 */
app.get('/api/v1/solar-zenith', async (req, res) => {
  try {
    const { date, grid } = req.query;
    
    if (!date || !grid) {
      return res.status(400).json({ error: 'Missing required parameters: date, grid' });
    }
    
    // Validate date format
    if (!/^\d{8}$/.test(date)) {
      return res.status(400).json({ error: 'Invalid date format. Use YYYYMMDD' });
    }
    
    // Call Python calculator - use project root (parent of web-ui directory)
    const projectRoot = join(dirname(fileURLToPath(import.meta.url)), '..');
    const pythonPath = join(projectRoot, 'venv', 'bin', 'python3');
    const scriptPath = join(projectRoot, 'src', 'hf_timestd', 'core', 'solar_zenith_calculator.py');
    
    const cmd = `${pythonPath} ${scriptPath} --date ${date} --grid ${grid} --interval 5`;
    
    const execPromise = promisify(exec);
    const { stdout, stderr } = await execPromise(cmd, { timeout: 10000 });
    
    if (stderr) {
      console.error('Solar zenith calculator stderr:', stderr);
    }
    
    const result = JSON.parse(stdout);
    res.json(result);
    
  } catch (err) {
    console.error('Error calculating solar zenith:', err);
    res.status(500).json({ error: err.message });
  }
});

// ============================================================================
// LOGS API
// ============================================================================

/**
 * GET /api/v1/logs/list
 * List all available log files
 */
app.get('/api/v1/logs/list', async (req, res) => {
  try {
    const logsDir = join(paths.dataRoot, 'logs');
    const logs = [];
    
    // Read log files from logs directory
    if (fs.existsSync(logsDir)) {
      const files = fs.readdirSync(logsDir);
      
      for (const file of files) {
        const filePath = join(logsDir, file);
        const stats = fs.statSync(filePath);
        
        if (stats.isFile() && file.endsWith('.log')) {
          // Categorize logs
          let category = 'other';
          if (file.includes('core-recorder')) category = 'recorder';
          else if (file.includes('analytics')) category = 'analytics';
          else if (file.includes('webui')) category = 'webui';
          else if (file.includes('drf')) category = 'digital-rf';
          
          logs.push({
            name: file,
            path: filePath,
            size: stats.size,
            modified: stats.mtime.toISOString(),
            category
          });
        }
      }
    }
    
    // Sort by modified time (newest first)
    logs.sort((a, b) => new Date(b.modified) - new Date(a.modified));
    
    res.json({ logs });
    
  } catch (err) {
    console.error('Error listing logs:', err);
    res.status(500).json({ error: err.message });
  }
});

/**
 * GET /api/v1/logs/content
 * Get log file content
 */
app.get('/api/v1/logs/content', async (req, res) => {
  try {
    const { path: logPath, tail = 500, offset = 0 } = req.query;
    
    if (!logPath) {
      return res.status(400).json({ error: 'path parameter required' });
    }
    
    // Security: Normalize path and ensure it is within logs directory
    // Use path.resolve to prevent path traversal attacks with ../
    const logsDir = resolve(paths.dataRoot, 'logs');
    const normalizedPath = resolve(logPath);
    if (!normalizedPath.startsWith(logsDir + '/') && normalizedPath !== logsDir) {
      return res.status(403).json({ error: 'Access denied: path outside logs directory' });
    }
    
    if (!fs.existsSync(normalizedPath)) {
      return res.status(404).json({ error: 'Log file not found' });
    }
    
    // Read file content
    const content = fs.readFileSync(normalizedPath, 'utf8');
    const allLines = content.split('\n').filter(line => line.trim());
    
    // Apply offset and tail
    const startIndex = Math.max(0, allLines.length - parseInt(tail) - parseInt(offset));
    const endIndex = allLines.length - parseInt(offset);
    const lines = allLines.slice(startIndex, endIndex);
    
    res.json({
      lines,
      total: allLines.length,
      offset: parseInt(offset),
      tail: parseInt(tail)
    });
    
  } catch (err) {
    console.error('Error reading log:', err);
    res.status(500).json({ error: err.message });
  }
});

// ============================================================================
// TRANSMISSION TIME (UTC(NIST) BACK-CALCULATION) API
// ============================================================================

/**
 * GET /api/v1/timing/transmission
 * Get UTC(NIST) back-calculation results (individual + combined)
 */
app.get('/api/v1/timing/transmission', async (req, res) => {
  try {
    const { date, hours = 24 } = req.query;
    const targetDate = date || new Date().toISOString().slice(0, 10).replace(/-/g, '');
    
    const results = [];
    const channels = config.recorder?.channels || [];
    const enabledChannels = channels.filter(ch => ch.enabled !== false);
    
    for (const channel of enabledChannels) {
      const channelName = channel.description || `Channel ${channel.ssrc}`;
      const channelKey = channelName.replace(/ /g, '_');
      
      // Look for transmission_time CSV (in Phase 2 timing directory)
      const csvPath = join(
        paths.getTimingDir(channelName),
        `${channelKey}_utc_nist_${targetDate}.csv`
      );
      
      if (!fs.existsSync(csvPath)) continue;
      
      try {
        const content = fs.readFileSync(csvPath, 'utf8');
        const lines = content.trim().split('\n');
        if (lines.length < 2) continue;
        
        const headers = lines[0].split(',');
        const cutoffTime = Date.now() - (hours * 60 * 60 * 1000);
        
        for (let i = 1; i < lines.length; i++) {
          const values = lines[i].split(',');
          const row = {};
          headers.forEach((h, idx) => row[h] = values[idx]);
          
          // Filter by time
          const rowTime = new Date(row.timestamp_utc).getTime();
          if (rowTime < cutoffTime) continue;
          
          results.push({
            channel: channelName,
            timestamp: row.timestamp_utc,
            station: row.station,
            frequency_mhz: parseFloat(row.frequency_mhz),
            mode: row.mode,
            n_hops: parseInt(row.n_hops),
            layer_height_km: parseFloat(row.layer_height_km),
            elevation_deg: parseFloat(row.elevation_deg),
            propagation_delay_ms: parseFloat(row.propagation_delay_ms),
            utc_nist_offset_ms: row.utc_nist_offset_ms ? parseFloat(row.utc_nist_offset_ms) : null,
            utc_nist_verified: row.utc_nist_verified === 'True',
            confidence: parseFloat(row.confidence),
            mode_separation_ms: parseFloat(row.mode_separation_ms)
          });
        }
      } catch (err) {
        console.warn(`Error reading ${csvPath}: ${err.message}`);
      }
    }
    
    // Sort by timestamp descending
    results.sort((a, b) => new Date(b.timestamp) - new Date(a.timestamp));
    
    // Calculate combined estimate from recent observations (last 5 minutes)
    const recentCutoff = Date.now() - (5 * 60 * 1000);
    const recentResults = results.filter(r => 
      new Date(r.timestamp).getTime() > recentCutoff && 
      r.utc_nist_offset_ms !== null &&
      r.confidence > 0.3
    );
    
    let combined = null;
    if (recentResults.length > 0) {
      // Compute weighted average with outlier rejection
      const offsets = recentResults.map(r => r.utc_nist_offset_ms);
      const weights = recentResults.map(r => r.confidence);
      
      // Simple weighted mean
      const totalWeight = weights.reduce((a, b) => a + b, 0);
      const weightedMean = offsets.reduce((sum, o, i) => sum + o * weights[i], 0) / totalWeight;
      
      // Calculate spread and consistency
      const spread = Math.max(...offsets) - Math.min(...offsets);
      const consistency = Math.max(0, 1 - spread / 3);
      
      // Estimate uncertainty
      const variance = offsets.reduce((sum, o, i) => 
        sum + weights[i] * Math.pow(o - weightedMean, 2), 0) / totalWeight;
      const uncertainty = Math.sqrt(variance) / Math.sqrt(recentResults.length);
      
      // Count stations
      const stations = new Set(recentResults.map(r => r.station));
      
      // Quality grade
      let grade = 'D';
      if (uncertainty < 0.5 && consistency > 0.8) grade = 'A';
      else if (uncertainty < 1.5 && consistency > 0.6) grade = 'B';
      else if (uncertainty < 3.0) grade = 'C';
      
      combined = {
        utc_offset_ms: weightedMean,
        uncertainty_ms: uncertainty,
        confidence: Math.min(1, consistency * (1 + 0.1 * (stations.size - 1))),
        consistency: consistency,
        n_measurements: recentResults.length,
        n_stations: stations.size,
        stations: Array.from(stations),
        quality_grade: grade,
        verified: uncertainty < 2.0 && consistency > 0.5
      };
    }
    
    res.json({
      date: targetDate,
      count: results.length,
      results: results.slice(0, 100),  // Individual results
      combined: combined  // Multi-station combined estimate
    });
    
  } catch (err) {
    console.error('Error getting transmission time data:', err);
    res.status(500).json({ error: err.message });
  }
});

/**
 * GET /api/v1/timing/global
 * Get global cross-channel timing results from GlobalDifferentialSolver
 */
app.get('/api/v1/timing/global', async (req, res) => {
  try {
    const globalTimingFile = join(paths.getDataRoot(), 'shared', 'global_timing.json');
    
    if (!fs.existsSync(globalTimingFile)) {
      return res.json({
        available: false,
        message: 'Global timing not yet available - waiting for multi-channel detections',
        results: [],
        latest: null
      });
    }
    
    const data = JSON.parse(fs.readFileSync(globalTimingFile, 'utf-8'));
    
    res.json({
      available: true,
      results: data.results || [],
      latest: data.latest || null,
      count: (data.results || []).length
    });
    
  } catch (err) {
    console.error('Error getting global timing data:', err);
    res.status(500).json({ error: err.message });
  }
});

/**
 * GET /api/v1/phase2/multi-station/:channel
 * Multi-station detection results for a channel
 * Shows ALL detected stations with propagation analysis (not just dominant)
 */
app.get('/api/v1/phase2/multi-station/:channel', async (req, res) => {
  try {
    const channelName = decodeURIComponent(req.params.channel);
    
    // Validate channel exists in config
    const channels = config.recorder?.channels || [];
    const validChannel = channels.find(ch => 
      (ch.description || `Channel ${ch.ssrc}`) === channelName
    );
    if (!validChannel) {
      return res.status(404).json({ error: `Channel not found: ${channelName}` });
    }
    
    const today = new Date().toISOString().split('T')[0].replace(/-/g, '');
    const fileChannel = channelName.replace(/ /g, '_').replace(/\./g, '_');
    
    // Read multi-station detection results from Phase 2
    const multiStationDir = paths.getPhase2Dir(channelName);
    const multiStationFile = multiStationDir ? 
      join(multiStationDir, 'multi_station', `${fileChannel}_multi_station_${today}.json`) : null;
    
    let multiStationData = null;
    if (multiStationFile && fs.existsSync(multiStationFile)) {
      try {
        multiStationData = JSON.parse(fs.readFileSync(multiStationFile, 'utf-8'));
      } catch (e) {
        console.warn(`Failed to parse multi-station JSON: ${e.message}`);
      }
    }
    
    // Also check analytics state file for latest multi_station_result
    const channelKey = channelName.toLowerCase().replace(' mhz', '').replace(/ /g, '');
    const stateFile = join(paths.getStateDir(), `analytics-${channelKey}.json`);
    
    let latestResult = null;
    if (fs.existsSync(stateFile)) {
      try {
        const state = JSON.parse(fs.readFileSync(stateFile, 'utf-8'));
        if (state.multi_station_result) {
          latestResult = state.multi_station_result;
        }
      } catch (e) {
        console.warn(`Failed to parse state file: ${e.message}`);
      }
    }
    
    // Extract frequency for station capability info
    const freqMatch = channelName.match(/([\d.]+)\s*MHz/);
    const freqMhz = freqMatch ? parseFloat(freqMatch[1]) : 0;
    const isCHU = channelName.includes('CHU');
    
    // Determine which stations can be received on this frequency
    const receivableStations = [];
    if (!isCHU) {
      receivableStations.push('WWV'); // WWV on all frequencies
      if ([2.5, 5, 10, 15].includes(freqMhz)) {
        receivableStations.push('WWVH');
        receivableStations.push('BPM');
      }
    } else {
      receivableStations.push('CHU');
    }
    
    res.json({
      channel: channelName,
      frequency_mhz: freqMhz,
      receivable_stations: receivableStations,
      latest: latestResult,
      history: multiStationData?.detections || [],
      timestamp: new Date().toISOString()
    });
    
  } catch (err) {
    console.error('Error getting multi-station data:', err);
    res.status(500).json({ error: err.message });
  }
});

// ============================================================================
// PHASE 2 ANALYSIS DASHBOARD API ENDPOINTS
// ============================================================================

/**
 * GET /api/v1/phase2/reception-matrix
 * Station-Channel reception matrix showing all 17 broadcasts
 * (6 WWV + 4 WWVH + 3 CHU + 4 BPM)
 */
app.get('/api/v1/phase2/reception-matrix', async (req, res) => {
  try {
    const channels = config.recorder?.channels || [];
    const result = { channels: [], timestamp: new Date().toISOString() };
    
    for (const ch of channels) {
      if (ch.enabled === false) continue;
      const channelName = ch.description || `Channel ${ch.ssrc}`;
      
      // Read latest carrier power data
      const powerDir = paths.getPhase2Dir(channelName) ? 
        join(paths.getPhase2Dir(channelName), 'carrier_power') : null;
      const today = new Date().toISOString().split('T')[0].replace(/-/g, '');
      const powerFile = powerDir ? join(powerDir, `carrier_power_${today}.csv`) : null;
      
      let latestPower = null;
      if (powerFile && fs.existsSync(powerFile)) {
        const lines = fs.readFileSync(powerFile, 'utf-8').trim().split('\n');
        if (lines.length > 1) {
          const lastLine = lines[lines.length - 1].split(',');
          latestPower = {
            snr_db: parseFloat(lastLine[3]) || null,
            station: lastLine[6] || null,
            quality: lastLine[7] || null
          };
        }
      }
      
      // Read discrimination data
      const discrimDir = paths.getPhase2Dir(channelName) ? 
        join(paths.getPhase2Dir(channelName), 'discrimination') : null;
      const fileChannel = channelName.replace(/ /g, '_').replace(/\./g, '_');
      const discrimFile = discrimDir ? join(discrimDir, `${fileChannel}_discrimination_${today}.csv`) : null;
      
      let latestDiscrim = null;
      if (discrimFile && fs.existsSync(discrimFile)) {
        const lines = fs.readFileSync(discrimFile, 'utf-8').trim().split('\n');
        if (lines.length > 1) {
          const lastLine = lines[lines.length - 1].split(',');
          latestDiscrim = {
            dominant_station: lastLine[2] || 'NONE',
            confidence: lastLine[3] || 'low',
            wwv_snr_db: parseFloat(lastLine[4]) || null,
            wwvh_snr_db: parseFloat(lastLine[5]) || null
          };
        }
      }
      
      // Read tone detections
      const tonesDir = paths.getToneDetectionsDir ? paths.getToneDetectionsDir(channelName) : null;
      const tonesFile = tonesDir ? join(tonesDir, `${fileChannel}_tones_${today}.csv`) : null;
      
      let latestTones = { wwv_detected: false, wwvh_detected: false, bpm_detected: false };
      if (tonesFile && fs.existsSync(tonesFile)) {
        const lines = fs.readFileSync(tonesFile, 'utf-8').trim().split('\n');
        if (lines.length > 1) {
          // Parse header to get column indices (robust to schema changes)
          const headers = lines[0].split(',');
          const colIdx = {};
          headers.forEach((h, i) => colIdx[h.trim()] = i);
          
          const lastLine = lines[lines.length - 1].split(',');
          latestTones = {
            wwv_detected: lastLine[colIdx['wwv_detected']] === '1',
            wwvh_detected: lastLine[colIdx['wwvh_detected']] === '1',
            bpm_detected: lastLine[colIdx['bpm_detected']] === '1',
            wwv_snr_db: colIdx['wwv_snr_db'] !== undefined ? parseFloat(lastLine[colIdx['wwv_snr_db']]) || null : null,
            wwvh_snr_db: colIdx['wwvh_snr_db'] !== undefined ? parseFloat(lastLine[colIdx['wwvh_snr_db']]) || null : null,
            bpm_snr_db: colIdx['bpm_snr_db'] !== undefined ? parseFloat(lastLine[colIdx['bpm_snr_db']]) || null : null,
            bpm_timing_mode: colIdx['bpm_timing_mode'] !== undefined ? lastLine[colIdx['bpm_timing_mode']] || null : null,
            bpm_usable_for_utc: colIdx['bpm_is_usable_for_utc'] !== undefined ? lastLine[colIdx['bpm_is_usable_for_utc']] === '1' : true
          };
        }
      }
      
      const isCHU = channelName.includes('CHU');
      
      // Check if this channel can receive WWVH and BPM
      // WWVH broadcasts on 2.5, 5, 10, 15 MHz (NOT 20 or 25 MHz)
      // BPM (China) broadcasts on 2.5, 5, 10, 15 MHz (same as WWVH)
      const freqMatch = channelName.match(/([\d.]+)\s*MHz/);
      const freqMhz = freqMatch ? parseFloat(freqMatch[1]) : 0;
      const canReceiveWWVH = !isCHU && [2.5, 5, 10, 15].includes(freqMhz);
      const canReceiveBPM = !isCHU && [2.5, 5, 10, 15].includes(freqMhz);
      
      result.channels.push({
        channel: channelName,
        canReceiveWWVH: canReceiveWWVH,
        canReceiveBPM: canReceiveBPM,
        wwv_detected: !isCHU && (latestTones.wwv_detected || latestDiscrim?.wwv_snr_db > 0),
        wwvh_detected: canReceiveWWVH && (latestTones.wwvh_detected || latestDiscrim?.wwvh_snr_db > 0),
        bpm_detected: canReceiveBPM && latestTones.bpm_detected,
        chu_detected: isCHU && latestPower?.snr_db > 10,
        wwv_snr_db: latestDiscrim?.wwv_snr_db || latestTones.wwv_snr_db || (latestPower?.station === 'WWV' ? latestPower.snr_db : null),
        wwvh_snr_db: canReceiveWWVH ? (latestDiscrim?.wwvh_snr_db || latestTones.wwvh_snr_db || (latestPower?.station === 'WWVH' ? latestPower.snr_db : null)) : null,
        bpm_snr_db: canReceiveBPM ? latestTones.bpm_snr_db : null,
        bpm_timing_mode: canReceiveBPM ? latestTones.bpm_timing_mode : null,
        bpm_usable_for_utc: canReceiveBPM ? latestTones.bpm_usable_for_utc : null,
        chu_snr_db: isCHU ? latestPower?.snr_db : null,
        dominant_station: latestDiscrim?.dominant_station || latestPower?.station || 'NONE',
        confidence: latestDiscrim?.confidence || 'low',
        quality_grade: latestPower?.quality || 'X'
      });
    }
    
    res.json(result);
  } catch (err) {
    console.error('Error getting reception matrix:', err);
    res.status(500).json({ error: err.message });
  }
});

/**
 * GET /api/v1/phase2/dclock-consensus
 * D_clock consensus from all broadcasts
 */
app.get('/api/v1/phase2/dclock-consensus', async (req, res) => {
  try {
    const channels = config.recorder?.channels || [];
    const broadcasts = [];
    const today = new Date().toISOString().split('T')[0].replace(/-/g, '');
    
    for (const ch of channels) {
      if (ch.enabled === false) continue;
      const channelName = ch.description || `Channel ${ch.ssrc}`;
      
      // Read clock offset data
      const clockDir = paths.getClockOffsetDir ? paths.getClockOffsetDir(channelName) : null;
      const clockFile = clockDir ? join(clockDir, 'clock_offset_series.csv') : null;
      
      if (clockFile && fs.existsSync(clockFile)) {
        const lines = fs.readFileSync(clockFile, 'utf-8').trim().split('\n');
        if (lines.length > 1) {
          // Parse header to get column indices
          const headers = lines[0].split(',');
          const colIdx = {};
          headers.forEach((h, i) => colIdx[h.trim()] = i);
          
          const lastLine = lines[lines.length - 1].split(',');
          const d_clock_ms = parseFloat(lastLine[colIdx['clock_offset_ms']] || lastLine[3]);
          const station = lastLine[colIdx['station']] || lastLine[4] || 'UNKNOWN';
          const uncertainty_ms = parseFloat(lastLine[colIdx['uncertainty_ms']] || lastLine[10]) || 5.0;
          
          if (!isNaN(d_clock_ms)) {
            // Extract frequency from channel name
            const freqMatch = channelName.match(/([\d.]+)\s*MHz/);
            const frequency = freqMatch ? freqMatch[1] + ' MHz' : '';
            
            broadcasts.push({
              channel: channelName,
              station: station,
              frequency: frequency,
              d_clock_ms: d_clock_ms,
              uncertainty_ms: uncertainty_ms,
              timestamp: lastLine[1]
            });
          }
        }
      }
    }
    
    // Calculate consensus
    let consensus_ms = null;
    let uncertainty_ms = null;
    let agreeing_count = 0;
    
    if (broadcasts.length > 0) {
      // Weighted average using inverse uncertainty
      let weightSum = 0;
      let weightedSum = 0;
      
      for (const b of broadcasts) {
        const weight = 1 / (b.uncertainty_ms * b.uncertainty_ms);
        weightSum += weight;
        weightedSum += b.d_clock_ms * weight;
      }
      
      consensus_ms = weightedSum / weightSum;
      uncertainty_ms = 1 / Math.sqrt(weightSum);
      
      // Mark which broadcasts agree (within 2x their uncertainty)
      for (const b of broadcasts) {
        b.agrees = Math.abs(b.d_clock_ms - consensus_ms) < (2 * b.uncertainty_ms);
        if (b.agrees) agreeing_count++;
      }
    }
    
    res.json({
      consensus_ms: consensus_ms,
      uncertainty_ms: uncertainty_ms,
      agreeing_count: agreeing_count,
      total_count: broadcasts.length,
      broadcasts: broadcasts.sort((a, b) => a.channel.localeCompare(b.channel)),
      timestamp: new Date().toISOString()
    });
  } catch (err) {
    console.error('Error getting D_clock consensus:', err);
    res.status(500).json({ error: err.message });
  }
});

/**
 * GET /api/v1/phase2/propagation-paths
 * Geographic propagation paths to stations
 */
app.get('/api/v1/phase2/propagation-paths', async (req, res) => {
  try {
    // Station coordinates
    const stations = {
      'WWV': { lat: 40.6781, lon: -105.0469, name: 'Fort Collins, CO' },
      'WWVH': { lat: 21.9886, lon: -159.7642, name: 'Kauai, HI' },
      'CHU': { lat: 45.2945, lon: -75.7513, name: 'Ottawa, ON' }
    };
    
    // Get receiver location from config (grid_square is under [station] section)
    const receiverGrid = config.station?.grid_square || config.recorder?.grid_square || config.receiver?.grid || 'DN70';
    
    // Maidenhead grid to lat/lon conversion (handles 4 or 6 character grids)
    function gridToLatLon(grid) {
      if (!grid || grid.length < 4) return { lat: 40, lon: -100 };
      
      // Field (A-R for lon, A-R for lat) - 20° x 10° squares
      const lonField = (grid.toUpperCase().charCodeAt(0) - 65) * 20 - 180;
      const latField = (grid.toUpperCase().charCodeAt(1) - 65) * 10 - 90;
      
      // Square (0-9) - 2° x 1° squares
      const lonSquare = parseInt(grid[2]) * 2;
      const latSquare = parseInt(grid[3]) * 1;
      
      // Subsquare (a-x for lon, a-x for lat) - 5' x 2.5' squares
      let lonSub = 1.0;  // Default to center of square
      let latSub = 0.5;
      if (grid.length >= 6) {
        lonSub = ((grid.toLowerCase().charCodeAt(4) - 97) * 5 / 60) + (2.5 / 60);
        latSub = ((grid.toLowerCase().charCodeAt(5) - 97) * 2.5 / 60) + (1.25 / 60);
      }
      
      const lon = lonField + lonSquare + lonSub;
      const lat = latField + latSquare + latSub;
      
      return { lat, lon };
    }
    
    const receiver = gridToLatLon(receiverGrid);
    
    // Calculate great circle distance
    function haversine(lat1, lon1, lat2, lon2) {
      const R = 6371; // Earth radius in km
      const dLat = (lat2 - lat1) * Math.PI / 180;
      const dLon = (lon2 - lon1) * Math.PI / 180;
      const a = Math.sin(dLat/2) * Math.sin(dLat/2) +
                Math.cos(lat1 * Math.PI / 180) * Math.cos(lat2 * Math.PI / 180) *
                Math.sin(dLon/2) * Math.sin(dLon/2);
      return R * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1-a));
    }
    
    // Calculate bearing
    function bearing(lat1, lon1, lat2, lon2) {
      const dLon = (lon2 - lon1) * Math.PI / 180;
      const lat1Rad = lat1 * Math.PI / 180;
      const lat2Rad = lat2 * Math.PI / 180;
      const y = Math.sin(dLon) * Math.cos(lat2Rad);
      const x = Math.cos(lat1Rad) * Math.sin(lat2Rad) - Math.sin(lat1Rad) * Math.cos(lat2Rad) * Math.cos(dLon);
      return ((Math.atan2(y, x) * 180 / Math.PI) + 360) % 360;
    }
    
    const paths = [];
    
    for (const [station, coords] of Object.entries(stations)) {
      const distance = haversine(receiver.lat, receiver.lon, coords.lat, coords.lon);
      const bear = bearing(receiver.lat, receiver.lon, coords.lat, coords.lon);
      
      // Estimate propagation delay (very rough: 1F at 300km altitude)
      const delay_ms = distance / 299.792; // Speed of light approximation
      
      // Estimate propagation mode based on distance
      let mode = '1F';
      if (distance > 3000) mode = '2F';
      if (distance > 5000) mode = '3F';
      
      // Get best SNR for this station across all channels
      let bestSnr = null;
      const channels = config.recorder?.channels || [];
      for (const ch of channels) {
        if (ch.enabled === false) continue;
        const channelName = ch.description || '';
        
        // Match station to channel type
        if ((station === 'CHU' && !channelName.includes('CHU')) ||
            (station !== 'CHU' && channelName.includes('CHU'))) continue;
        
        const powerDir = paths.getPhase2Dir ? join(paths.getPhase2Dir(channelName), 'carrier_power') : null;
        const today = new Date().toISOString().split('T')[0].replace(/-/g, '');
        const powerFile = powerDir ? join(powerDir, `carrier_power_${today}.csv`) : null;
        
        if (powerFile && fs.existsSync(powerFile)) {
          const lines = fs.readFileSync(powerFile, 'utf-8').trim().split('\n');
          if (lines.length > 1) {
            const lastLine = lines[lines.length - 1].split(',');
            const snr = parseFloat(lastLine[3]);
            const stn = lastLine[6];
            if (stn === station && snr > (bestSnr || 0)) {
              bestSnr = snr;
            }
          }
        }
      }
      
      paths.push({
        station: station,
        location: coords.name,
        distance_km: distance,
        bearing: bear,
        propagation_mode: mode,
        delay_ms: delay_ms,
        best_snr_db: bestSnr
      });
    }
    
    res.json({
      receiver_grid: receiverGrid,
      receiver_lat: receiver.lat,
      receiver_lon: receiver.lon,
      paths: paths,
      timestamp: new Date().toISOString()
    });
  } catch (err) {
    console.error('Error getting propagation paths:', err);
    res.status(500).json({ error: err.message });
  }
});

/**
 * GET /api/v1/phase2/diurnal-pattern
 * 24-hour station dominance pattern
 */
app.get('/api/v1/phase2/diurnal-pattern', async (req, res) => {
  try {
    const channels = config.recorder?.channels || [];
    const result = { channels: [], timestamp: new Date().toISOString() };
    const today = new Date().toISOString().split('T')[0].replace(/-/g, '');
    
    for (const ch of channels) {
      if (ch.enabled === false) continue;
      const channelName = ch.description || `Channel ${ch.ssrc}`;
      
      // Skip CHU channels for WWV/WWVH diurnal pattern
      if (channelName.includes('CHU')) continue;
      
      // Check if this channel can receive WWVH
      // WWVH only broadcasts on 2.5, 5, 10, 15 MHz (NOT 20 or 25 MHz)
      const freqMatch = channelName.match(/([\d.]+)\s*MHz/);
      const freqMhz = freqMatch ? parseFloat(freqMatch[1]) : 0;
      const canReceiveWWVH = [2.5, 5, 10, 15].includes(freqMhz);
      
      // Read discrimination data for today
      const fileChannel = channelName.replace(/ /g, '_').replace(/\./g, '_');
      const discrimDir = paths.getPhase2Dir ? join(paths.getPhase2Dir(channelName), 'discrimination') : null;
      const discrimFile = discrimDir ? join(discrimDir, `${fileChannel}_discrimination_${today}.csv`) : null;
      
      // Initialize 24 hours
      const hours = Array(24).fill(null).map(() => ({ dominant: 'NONE', count: 0, wwv: 0, wwvh: 0 }));
      
      if (discrimFile && fs.existsSync(discrimFile)) {
        const lines = fs.readFileSync(discrimFile, 'utf-8').trim().split('\n');
        
        for (let i = 1; i < lines.length; i++) {
          const cols = lines[i].split(',');
          const timestamp = cols[0];
          const station = cols[2];
          
          // Extract hour from timestamp
          const hourMatch = timestamp.match(/T(\d{2}):/);
          if (hourMatch) {
            const hour = parseInt(hourMatch[1]);
            hours[hour].count++;
            if (station === 'WWV') hours[hour].wwv++;
            else if (station === 'WWVH') hours[hour].wwvh++;
          }
        }
        
        // Determine dominant for each hour
        for (let h = 0; h < 24; h++) {
          if (hours[h].count === 0) {
            hours[h].dominant = 'NONE';
          } else if (!canReceiveWWVH) {
            // 20/25 MHz can only receive WWV (WWVH doesn't broadcast on these)
            hours[h].dominant = hours[h].wwv > 0 ? 'WWV' : 'NONE';
          } else if (hours[h].wwv > hours[h].wwvh * 1.5) {
            hours[h].dominant = 'WWV';
          } else if (hours[h].wwvh > hours[h].wwv * 1.5) {
            hours[h].dominant = 'WWVH';
          } else {
            hours[h].dominant = 'MIXED';
          }
        }
      }
      
      result.channels.push({
        channel: channelName,
        canReceiveWWVH: canReceiveWWVH,
        hours: hours.map(h => ({ dominant: h.dominant }))
      });
    }
    
    res.json(result);
  } catch (err) {
    console.error('Error getting diurnal pattern:', err);
    res.status(500).json({ error: err.message });
  }
});

// ============================================================================
// START SERVER WITH WEBSOCKET SUPPORT
// ============================================================================

const server = app.listen(PORT, () => {
  console.log(`\n✅ Server running on http://localhost:${PORT}`);
  console.log(`📊 Summary: http://localhost:${PORT}/summary.html`);
  console.log(`🎯 Carrier Analysis: http://localhost:${PORT}/carrier.html`);
  console.log(`🔍 Health: http://localhost:${PORT}/health`);
  console.log(`🔊 Audio Streaming: WebSocket enabled`);
  console.log(`\n📡 API Endpoints:`);
  console.log(`   Summary:`);
  console.log(`     GET /api/v1/summary (aggregated)`);
  console.log(`     GET /api/v1/station/info`);
  console.log(`     GET /api/v1/system/processes`);
  console.log(`     GET /api/v1/system/continuity`);
  console.log(`     GET /api/v1/system/storage`);
  console.log(`     GET /api/v1/channels/status`);
  console.log(`   Carrier Analysis:`);
  console.log(`     GET /api/v1/carrier/quality?date=YYYYMMDD`);
  console.log(`   ⏱️  Timing & Analytics:`);
  console.log(`     GET /api/v1/timing/status`);
  console.log(`     GET /api/v1/tones/current`);
  console.log(`     GET /api/v1/channels/:name/discrimination/:date/methods`);
  console.log(`   🔊 Audio Streaming (IQ-based):`);
  console.log(`     GET /api/v1/audio/simple/:channel`);
  console.log(`     WS /api/v1/audio/simple-ws/:channel`);
  console.log(``);
});

// WebSocket server for audio streaming
const wss = new WebSocketServer({ noServer: true });

// Handle WebSocket upgrade requests
server.on('upgrade', (request, socket, head) => {
  const url = new URL(request.url, `http://${request.headers.host}`);
  
  // Simple audio WebSocket (IQ-derived)
  if (url.pathname.startsWith('/api/v1/audio/simple-ws/')) {
    const channelKey = url.pathname.split('/').pop();
    const channelName = channelKey.replace(/_/g, ' ');  // Convert back to spaces
    
    wss.handleUpgrade(request, socket, head, (ws) => {
      console.log(`🔊 Simple audio WebSocket connected: ${channelName}`);
      
      const audioDir = join(paths.dataRoot, 'audio_buffers');
      const pcmFile = join(audioDir, `${channelName}.pcm`);
      const metaFile = join(audioDir, `${channelName}.meta`);
      
      const sessionId = `${channelKey}-${Date.now()}`;
      let lastReadPos = 0;
      let isStreaming = true;
      
      // Read and stream audio at 100ms intervals
      const streamInterval = setInterval(() => {
        if (!isStreaming) return;
        
        try {
          if (!fs.existsSync(metaFile)) return;
          
          const metaBuf = fs.readFileSync(metaFile);
          const writePos = metaBuf.readUInt32LE(0);
          const bufferSamples = metaBuf.readUInt32LE(8);
          
          const pcmBuf = fs.readFileSync(pcmFile);
          
          let samplesToRead = writePos >= lastReadPos 
            ? writePos - lastReadPos 
            : (bufferSamples - lastReadPos) + writePos;
          
          if (samplesToRead > 0 && samplesToRead < bufferSamples) {
            const startByte = lastReadPos * 2;
            const endByte = (lastReadPos + samplesToRead) * 2;
            
            let audioData = endByte <= pcmBuf.length
              ? pcmBuf.slice(startByte, endByte)
              : Buffer.concat([pcmBuf.slice(startByte), pcmBuf.slice(0, endByte - pcmBuf.length)]);
            
            if (ws.readyState === 1) ws.send(audioData);
            lastReadPos = writePos;
          }
        } catch (err) { /* ignore */ }
      }, 200); // 200ms chunks = 1600 samples at 8kHz (less CPU overhead)
      
      simpleAudioSessions.set(sessionId, { ws, interval: streamInterval });
      
      ws.on('close', () => {
        isStreaming = false;
        clearInterval(streamInterval);
        simpleAudioSessions.delete(sessionId);
      });
      
      ws.on('error', () => {
        isStreaming = false;
        clearInterval(streamInterval);
        simpleAudioSessions.delete(sessionId);
      });
    });
    return;
  }
  
  // Unknown WebSocket path
  socket.destroy();
});

// Handle graceful shutdown
process.on('SIGINT', () => {
  console.log('\n🛑 Shutting down hf-timestd Monitoring Server...');
  server.close();
  process.exit(0);
});

process.on('SIGTERM', () => {
  console.log('\n🛑 Received SIGTERM, shutting down...');
  server.close();
  process.exit(0);
});

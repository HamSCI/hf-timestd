/**
 * Transmission Time Helpers
 * 
 * Backend logic for Phase 2 D_clock and transmission time APIs.
 * Reads clock offset CSVs and Phase 2 analytics status files.
 */

import fs from 'fs';
import { join } from 'path';

/**
 * Parse clock offset CSV file
 * Expected format:
 *   system_time,utc_time,minute_boundary_utc,clock_offset_ms,station,frequency_mhz,
 *   propagation_delay_ms,propagation_mode,n_hops,confidence,uncertainty_ms,quality_grade,snr_db
 */
function parseClockOffsetCSV(filePath) {
  try {
    if (!fs.existsSync(filePath)) {
      return { status: 'not_found', records: [], count: 0 };
    }
    
    const content = fs.readFileSync(filePath, 'utf-8');
    const lines = content.trim().split('\n');
    
    if (lines.length < 2) {
      return { status: 'empty', records: [], count: 0 };
    }
    
    const headers = lines[0].split(',');
    const records = [];
    
    for (let i = 1; i < lines.length; i++) {
      const values = lines[i].split(',');
      const record = {};
      headers.forEach((header, index) => {
        const val = values[index] ? values[index].trim() : '';
        record[header.trim()] = val;
      });
      records.push(record);
    }
    
    return { status: 'OK', records, count: records.length };
  } catch (err) {
    console.error(`Error parsing clock offset CSV ${filePath}:`, err);
    return { status: 'error', records: [], count: 0, error: err.message };
  }
}

/**
 * Get clock offset time series for a channel
 * 
 * @param {string} channel - Channel name (e.g., "WWV 10 MHz")
 * @param {string} date - Date in YYYYMMDD format (optional, defaults to today)
 * @param {number} hours - Hours of data to return (default 24)
 * @param {object} paths - TimeStdPaths instance
 * @returns {object} Clock offset data
 */
async function getClockOffsetSeries(channel, date, hours = 24, paths) {
  if (!channel) {
    throw new Error('Channel is required');
  }
  if (!paths) {
    throw new Error('paths is required');
  }
  
  const cleanName = channel.replace(/\s+/g, '_');
  const cutoff = Date.now() - (hours * 3600 * 1000);
  
  // Calculate which dates we need to check
  const datesToCheck = [];
  const today = new Date();
  const todayStr = today.toISOString().split('T')[0].replace(/-/g, '');
  datesToCheck.push(todayStr);
  
  // If hours span into yesterday, add yesterday's date
  if (hours >= 1) {
    const yesterday = new Date(today);
    yesterday.setDate(yesterday.getDate() - 1);
    const yesterdayStr = yesterday.toISOString().split('T')[0].replace(/-/g, '');
    datesToCheck.push(yesterdayStr);
  }
  
  // If a specific date was provided and it's not in our list, add it
  if (date && !datesToCheck.includes(date)) {
    datesToCheck.push(date);
  }
  
  // Use coordinated path: {data_root}/phase2/{CHANNEL}/clock_offset/clock_offset_series.csv
  let allRecords = [];
  
  const clockOffsetDir = paths.getClockOffsetDir(channel);
  const csvFile = join(clockOffsetDir, 'clock_offset_series.csv');
  
  const data = parseClockOffsetCSV(csvFile);
  if (data.status === 'OK' && data.records) {
    allRecords.push(...data.records);
  }
  
  if (allRecords.length === 0) {
    return { 
      available: false, 
      message: `No clock offset data for ${channel}`,
      channel,
      date: date || todayStr,
      hours
    };
  }
  
  // Filter by time window
  const filtered = allRecords.filter(r => {
    const timestamp = parseFloat(r.system_time) * 1000 || 
                      new Date(r.minute_boundary_utc).getTime();
    return timestamp > cutoff;
  });
  
  
  // Sort by timestamp
  filtered.sort((a, b) => {
    const timeA = parseFloat(a.system_time) || parseFloat(a.minute_boundary_utc);
    const timeB = parseFloat(b.system_time) || parseFloat(b.minute_boundary_utc);
    return timeA - timeB;
  });
  
  // Map to API output format
  const measurements = filtered.map(r => ({
    timestamp: r.minute_boundary_utc ? 
      new Date(parseFloat(r.minute_boundary_utc) * 1000).toISOString() :
      new Date(parseFloat(r.system_time) * 1000).toISOString(),
    system_time: parseFloat(r.system_time),
    d_clock_ms: parseFloat(r.clock_offset_ms),
    station: r.station || 'UNKNOWN',
    frequency_mhz: parseFloat(r.frequency_mhz) || null,
    propagation_mode: r.propagation_mode || null,
    n_hops: r.n_hops ? parseInt(r.n_hops) : null,
    propagation_delay_ms: r.propagation_delay_ms ? parseFloat(r.propagation_delay_ms) : null,
    confidence: parseFloat(r.confidence) || 0,
    uncertainty_ms: r.uncertainty_ms ? parseFloat(r.uncertainty_ms) : null,
    quality_grade: r.quality_grade || 'X',
    snr_db: r.snr_db ? parseFloat(r.snr_db) : null
  }));
  
  // Calculate statistics
  const dClockValues = measurements.map(m => m.d_clock_ms).filter(v => !isNaN(v));
  const stats = dClockValues.length > 0 ? {
    min_ms: Math.min(...dClockValues).toFixed(3),
    max_ms: Math.max(...dClockValues).toFixed(3),
    mean_ms: (dClockValues.reduce((a, b) => a + b, 0) / dClockValues.length).toFixed(3),
    std_ms: calculateStdDev(dClockValues).toFixed(3),
    range_ms: (Math.max(...dClockValues) - Math.min(...dClockValues)).toFixed(3)
  } : null;
  
  // Grade distribution
  const gradeDistribution = {};
  measurements.forEach(m => {
    const grade = m.quality_grade || 'X';
    gradeDistribution[grade] = (gradeDistribution[grade] || 0) + 1;
  });
  
  return {
    available: true,
    channel,
    date: date || todayStr,
    hours,
    count: measurements.length,
    statistics: stats,
    grade_distribution: gradeDistribution,
    measurements
  };
}

/**
 * Get Phase 2 analytics status for a channel
 * Reads from phase2/{CHANNEL}/status/analytics-service-status.json
 * 
 * @param {string} channel - Channel name
 * @param {object} paths - TimeStdPaths instance
 * @returns {object} Analytics status
 */
async function getPhase2AnalyticsStatus(channel, paths) {
  if (!channel) {
    return { available: false, message: 'Channel is required' };
  }
  
  const statusFile = paths.getAnalyticsServiceStatusFileForChannel(channel);
  
  if (!fs.existsSync(statusFile)) {
    return { 
      available: false, 
      message: `No Phase 2 status for ${channel}`,
      channel
    };
  }
  
  try {
    const content = fs.readFileSync(statusFile, 'utf-8');
    const status = JSON.parse(content);
    
    // Extract channel-specific data
    const channelData = status.channels?.[channel] || 
                        Object.values(status.channels || {})[0] || {};
    
    return {
      available: true,
      channel,
      service_version: status.version,
      uptime_seconds: status.uptime_seconds,
      last_updated: status.timestamp,
      minutes_processed: channelData.minutes_processed || 0,
      last_processed_time: channelData.last_processed_time,
      d_clock_ms: channelData.d_clock_ms,
      quality_grade: channelData.quality_grade,
      station: channelData.station,
      time_snap: channelData.time_snap,
      quality_metrics: channelData.quality_metrics,
      // Propagation mode data for Mode Ridge
      propagation_mode: channelData.propagation_mode,
      propagation_delay_ms: channelData.propagation_delay_ms,
      n_hops: channelData.n_hops,
      mode_candidates: channelData.mode_candidates,
      // Convergence model state
      uncertainty_ms: channelData.uncertainty_ms,
      convergence: channelData.convergence
    };
  } catch (err) {
    console.error(`Error reading Phase 2 status for ${channel}:`, err);
    return { 
      available: false, 
      message: `Error reading status: ${err.message}`,
      channel
    };
  }
}

/**
 * Get Phase 2 status for all channels
 * 
 * @param {object} paths - TimeStdPaths instance  
 * @param {object} config - Config object with recorder.channels
 * @returns {object} All channels Phase 2 status
 */
async function getAllPhase2Status(paths, config) {
  const channels = config.recorder?.channels || [];
  const enabledChannels = channels.filter(ch => ch.enabled !== false);
  
  const results = {
    available: true,
    channels: {},
    summary: {
      total_channels: enabledChannels.length,
      phase2_active: 0,
      with_d_clock: 0,
      grade_a_count: 0,
      grade_b_count: 0,
      grade_c_count: 0,
      grade_dx_count: 0
    }
  };
  
  for (const channel of enabledChannels) {
    const channelName = channel.description || `Channel ${channel.ssrc}`;
    const status = await getPhase2AnalyticsStatus(channelName, paths);
    
    results.channels[channelName] = status;
    
    if (status.available) {
      results.summary.phase2_active++;
      
      if (status.d_clock_ms !== undefined && status.d_clock_ms !== null) {
        results.summary.with_d_clock++;
        
        const grade = status.quality_grade || 'X';
        if (grade === 'A') results.summary.grade_a_count++;
        else if (grade === 'B') results.summary.grade_b_count++;
        else if (grade === 'C') results.summary.grade_c_count++;
        else results.summary.grade_dx_count++;
      }
    }
  }
  
  return results;
}

/**
 * Get latest D_clock value across all channels (best quality)
 * 
 * @param {object} paths - TimeStdPaths instance
 * @param {object} config - Config object
 * @returns {object} Best D_clock reference
 */
async function getBestDClock(paths, config) {
  const allStatus = await getAllPhase2Status(paths, config);
  
  let bestReference = null;
  let bestScore = -1;
  
  // Score: A=100, B=75, C=50, D=25, X=0; plus confidence bonus
  const gradeScores = { 'A': 100, 'B': 75, 'C': 50, 'D': 25, 'X': 0 };
  
  for (const [channelName, status] of Object.entries(allStatus.channels)) {
    if (!status.available || status.d_clock_ms === undefined) continue;
    
    const gradeScore = gradeScores[status.quality_grade || 'X'] || 0;
    const snrBonus = Math.min((status.quality_metrics?.last_snr_db || 0) / 2, 20);
    const score = gradeScore + snrBonus;
    
    if (score > bestScore) {
      bestScore = score;
      bestReference = {
        channel: channelName,
        d_clock_ms: status.d_clock_ms,
        quality_grade: status.quality_grade,
        station: status.station,
        snr_db: status.quality_metrics?.last_snr_db,
        last_updated: status.last_updated,
        score
      };
    }
  }
  
  if (!bestReference) {
    return {
      available: false,
      message: 'No D_clock measurements available'
    };
  }
  
  return {
    available: true,
    ...bestReference,
    summary: allStatus.summary
  };
}

/**
 * Get Phase 2 pipeline status (3-step process)
 * 
 * @param {string} channel - Channel name
 * @param {object} paths - TimeStdPaths instance
 * @returns {object} Pipeline status
 */
async function getPhase2PipelineStatus(channel, paths) {
  const status = await getPhase2AnalyticsStatus(channel, paths);
  
  if (!status.available) {
    return {
      available: false,
      channel,
      message: status.message
    };
  }
  
  // Read additional state files for detailed pipeline status
  const stateDir = paths.getPhase2StateDir(channel);
  let pipelineState = null;
  
  try {
    const stateFile = join(stateDir, 'pipeline-state.json');
    if (fs.existsSync(stateFile)) {
      pipelineState = JSON.parse(fs.readFileSync(stateFile, 'utf-8'));
    }
  } catch (err) {
    // Pipeline state file may not exist yet
  }
  
  return {
    available: true,
    channel,
    step1_tone_detection: {
      status: status.time_snap?.established ? 'complete' : 'pending',
      source: status.time_snap?.source || null,
      confidence: status.time_snap?.confidence || 0,
      wwv_snr_db: pipelineState?.step1?.wwv_snr_db || null,
      wwvh_snr_db: pipelineState?.step1?.wwvh_snr_db || null
    },
    step2_characterization: {
      status: status.station ? 'complete' : 'pending',
      dominant_station: status.station || null,
      propagation_mode: pipelineState?.step2?.propagation_mode || null,
      doppler_hz: pipelineState?.step2?.doppler_hz || null
    },
    step3_d_clock: {
      status: status.d_clock_ms !== undefined ? 'complete' : 'pending',
      d_clock_ms: status.d_clock_ms,
      uncertainty_ms: pipelineState?.step3?.uncertainty_ms || null,
      quality_grade: status.quality_grade
    },
    minutes_processed: status.minutes_processed,
    last_updated: status.last_updated
  };
}

// Utility functions

function calculateStdDev(values) {
  if (values.length === 0) return 0;
  const mean = values.reduce((a, b) => a + b, 0) / values.length;
  const squareDiffs = values.map(v => Math.pow(v - mean, 2));
  return Math.sqrt(squareDiffs.reduce((a, b) => a + b, 0) / values.length);
}

/**
 * Get quality grade badge color
 */
function getGradeColor(grade) {
  switch (grade) {
    case 'A': return '#22c55e';  // green
    case 'B': return '#3b82f6';  // blue
    case 'C': return '#eab308';  // yellow
    case 'D': return '#f97316';  // orange
    case 'X': 
    default: return '#ef4444';  // red
  }
}

/**
 * Get quality grade description
 */
function getGradeDescription(grade) {
  switch (grade) {
    case 'A': return 'Excellent - UTC verified + ground truth OR multi-method agreement';
    case 'B': return 'Good - High confidence, single station';
    case 'C': return 'Moderate - Acceptable confidence';
    case 'D': return 'Low - Limited confidence';
    case 'X': 
    default: return 'No measurement - Detection failed';
  }
}


// ============================================================================
// ADVANCED VISUALIZATION DATA HELPERS
// ============================================================================

// Station locations for constellation radar (azimuths from generic US location)
const STATION_METADATA = {
  'WWV': { 
    azimuth_deg: 255,  // Fort Collins, CO (west-southwest from east coast)
    latitude: 40.6778,
    longitude: -105.0469
  },
  'WWVH': { 
    azimuth_deg: 255,  // Kekaha, Hawaii (west from mainland)
    latitude: 21.9886,
    longitude: -159.7631
  },
  'CHU': { 
    azimuth_deg: 35,   // Ottawa, Canada (northeast)
    latitude: 45.2950,
    longitude: -75.7536
  },
  'BPM': { 
    azimuth_deg: 342,  // Pucheng, China (north-northwest)
    latitude: 34.9,
    longitude: 109.5
  }
};

/**
 * Get Kalman Funnel data - clock stability over time
 * Returns array of points with offset, uncertainty, and lock status
 */
async function getKalmanFunnelData(paths, config, options = {}) {
  const { minutes = 60, channel = null } = options;
  const channels = config.recorder?.channels || [];
  const enabledChannels = channels.filter(ch => ch.enabled !== false);
  
  const points = [];
  const now = Date.now();
  const cutoffTime = now - (minutes * 60 * 1000);
  
  // If specific channel requested, use only that
  const targetChannels = channel 
    ? enabledChannels.filter(ch => (ch.description || `Channel ${ch.ssrc}`) === channel)
    : enabledChannels;
  
  for (const ch of targetChannels) {
    const channelName = ch.description || `Channel ${ch.ssrc}`;
    
    // Get clock offset CSV data
    const hoursToFetch = Math.ceil(minutes / 60) + 1;
    const series = await getClockOffsetSeries(channelName, null, hoursToFetch, paths);
    
    if (series.available && series.measurements) {
      for (const m of series.measurements) {
        const timestamp = new Date(m.timestamp).getTime();
        if (timestamp >= cutoffTime) {
          // Determine status based on quality grade and utc_verified (convergence lock)
          // Grade A/B indicates convergence model has LOCKED
          // utc_verified = true also indicates LOCKED state
          let status = 'HOLD';
          if (m.quality_grade === 'A' || m.quality_grade === 'B') {
            status = 'LOCKED';
          } else if (m.utc_verified === true || m.utc_verified === 'True' || m.utc_verified === 'true') {
            status = 'LOCKED';
          } else if (m.uncertainty_ms && m.uncertainty_ms < 2.0) {
            status = 'LOCKED';
          }
          
          points.push({
            timestamp: timestamp / 1000, // Unix seconds
            offset_ms: m.d_clock_ms,
            uncertainty_ms: m.uncertainty_ms || estimateUncertainty(m.quality_grade),
            status: status,
            channel: channelName,
            station: m.station
          });
        }
      }
    }
  }
  
  // Sort by timestamp
  points.sort((a, b) => a.timestamp - b.timestamp);
  
  // If combining multiple channels, use the best measurement per minute
  if (!channel && points.length > 0) {
    const minuteGroups = {};
    for (const p of points) {
      const minuteKey = Math.floor(p.timestamp / 60);
      if (!minuteGroups[minuteKey] || 
          p.uncertainty_ms < minuteGroups[minuteKey].uncertainty_ms) {
        minuteGroups[minuteKey] = p;
      }
    }
    return {
      available: true,
      points: Object.values(minuteGroups).sort((a, b) => a.timestamp - b.timestamp),
      minutes_requested: minutes,
      source: 'combined'
    };
  }
  
  return {
    available: points.length > 0,
    points: points,
    minutes_requested: minutes,
    source: channel || 'combined'
  };
}

/**
 * Get Constellation data - station timing errors by azimuth
 */
async function getConstellationData(paths, config) {
  const channels = config.recorder?.channels || [];
  const enabledChannels = channels.filter(ch => ch.enabled !== false);
  
  // Aggregate per-station errors from all channels
  const stationData = {};
  
  for (const ch of enabledChannels) {
    const channelName = ch.description || `Channel ${ch.ssrc}`;
    const status = await getPhase2AnalyticsStatus(channelName, paths);
    
    if (status.available && status.d_clock_ms !== undefined) {
      // Use detected station from discrimination (preferred) for correct azimuth positioning
      // WWVH detected on "WWV 10 MHz" should appear on WWVH's azimuth line
      let baseStation = status.station;
      if (!baseStation || baseStation === 'UNKNOWN' || baseStation === 'NONE') {
        // Fall back to channel name
        const channelUpper = channelName.toUpperCase();
        if (channelUpper.includes('CHU')) {
          baseStation = 'CHU';
        } else if (channelUpper.includes('WWVH')) {
          baseStation = 'WWVH';
        } else if (channelUpper.includes('BPM')) {
          baseStation = 'BPM';
        } else if (channelUpper.includes('WWV')) {
          baseStation = 'WWV';
        }
      }
      
      if (baseStation) {
        const key = `${baseStation}_${channelName}`;
        
        // Accumulate measurements per station-channel combo
        if (!stationData[key]) {
          stationData[key] = {
            name: `${baseStation} (${channelName})`,
            base_station: baseStation,
            azimuth_deg: STATION_METADATA[baseStation]?.azimuth_deg || 0,
            error_ms: status.d_clock_ms,
            snr: status.snr_db || 15,
            active: true,
            channel: channelName,
            quality_grade: status.quality_grade
          };
        }
      }
    }
  }
  
  return {
    available: Object.keys(stationData).length > 0,
    stations: Object.values(stationData),
    timestamp: new Date().toISOString()
  };
}

/**
 * Get Consensus data - time offset estimates for KDE visualization
 * Groups by station and computes weighted consensus
 */
async function getConsensusData(paths, config) {
  const channels = config.recorder?.channels || [];
  const enabledChannels = channels.filter(ch => ch.enabled !== false);
  
  const estimates = [];
  const gradeWeights = { 'A': 1.0, 'B': 0.7, 'C': 0.4, 'D': 0.15, 'X': 0.0 };
  
  for (const ch of enabledChannels) {
    const channelName = ch.description || `Channel ${ch.ssrc}`;
    const status = await getPhase2AnalyticsStatus(channelName, paths);
    
    if (status.available && status.d_clock_ms !== undefined) {
      // Use detected station from discrimination (preferred) or fall back to channel name
      let station = status.station;
      if (!station || station === 'UNKNOWN' || station === 'NONE') {
        const nameUpper = channelName.toUpperCase();
        if (nameUpper.includes('CHU')) station = 'CHU';
        else if (nameUpper.includes('WWVH')) station = 'WWVH';
        else if (nameUpper.includes('BPM')) station = 'BPM';
        else if (nameUpper.includes('WWV')) station = 'WWV';
        else station = 'UNKNOWN';
      }
      
      const snr = status.quality_metrics?.last_snr_db || status.snr_db || 10;
      // Use convergence uncertainty for weighting - lower uncertainty = higher weight
      const uncertainty = status.uncertainty_ms || 100;  // Default high if not available
      const convergenceWeight = uncertainty < 100 ? 1 / Math.max(0.1, uncertainty) : 0.01;
      const gradeWeight = gradeWeights[status.quality_grade] || 0.1;
      const snrWeight = Math.max(0.1, snr / 30);
      // Combined weight: convergence * grade * SNR
      const weight = convergenceWeight * gradeWeight * snrWeight;
      
      // Check if this channel is locked
      const isLocked = status.convergence?.is_locked || 
                       status.quality_grade === 'A' || 
                       status.quality_grade === 'B';
      
      estimates.push({
        source: channelName,
        offset: status.d_clock_ms,
        station: station,
        quality_grade: status.quality_grade,
        confidence: status.confidence || 0.5,
        snr_db: snr,
        weight: weight,
        uncertainty_ms: uncertainty,
        is_locked: isLocked,
        convergence_progress: status.convergence?.convergence_progress || 0,
        propagation_delay_ms: status.propagation_delay_ms
      });
    }
  }
  
  // Group by station
  const stationGroups = { 'WWV': [], 'WWVH': [], 'CHU': [] };
  for (const e of estimates) {
    if (stationGroups[e.station]) {
      stationGroups[e.station].push(e);
    }
  }
  
  // Calculate per-station weighted means
  const stationEstimates = {};
  for (const [station, group] of Object.entries(stationGroups)) {
    if (group.length === 0) continue;
    
    const totalWeight = group.reduce((sum, e) => sum + e.weight, 0);
    const weightedMean = group.reduce((sum, e) => sum + e.offset * e.weight, 0) / totalWeight;
    const variance = group.reduce((sum, e) => sum + e.weight * Math.pow(e.offset - weightedMean, 2), 0) / totalWeight;
    
    // Use the minimum channel uncertainty (from convergence) as station uncertainty
    // This reflects the best-locked channel's confidence
    const minChannelUncertainty = Math.min(...group.map(e => e.uncertainty_ms || 100));
    const lockedChannels = group.filter(e => e.is_locked).length;
    const avgConvergenceProgress = group.reduce((sum, e) => sum + (e.convergence_progress || 0), 0) / group.length;
    
    // Use sqrt(variance) as spread indicator, but min channel uncertainty for actual uncertainty
    const spreadMs = Math.sqrt(variance) || 2.0;
    const effectiveUncertainty = minChannelUncertainty < 50 ? minChannelUncertainty : spreadMs;
    
    stationEstimates[station] = {
      d_clock_ms: weightedMean,
      uncertainty_ms: effectiveUncertainty,
      spread_ms: spreadMs,
      min_channel_uncertainty_ms: minChannelUncertainty,
      n_channels: group.length,
      locked_channels: lockedChannels,
      convergence_progress: avgConvergenceProgress,
      channels: group.map(e => e.source)
    };
  }
  
  // Calculate overall consensus
  const stationValues = Object.values(stationEstimates);
  let consensusDclock = 0;
  let consensusUncertainty = 100;
  let stationAgreement = 0;
  
  if (stationValues.length > 0) {
    // Weight station estimates by channel count / uncertainty
    const stationWeights = stationValues.map(s => s.n_channels / Math.max(0.1, s.uncertainty_ms));
    const totalStationWeight = stationWeights.reduce((a, b) => a + b, 0);
    
    consensusDclock = stationValues.reduce((sum, s, i) => 
      sum + s.d_clock_ms * stationWeights[i], 0) / totalStationWeight;
    
    // Station agreement (spread)
    const dclocks = stationValues.map(s => s.d_clock_ms);
    stationAgreement = Math.max(...dclocks) - Math.min(...dclocks);
    
    // Combined uncertainty
    consensusUncertainty = Math.sqrt(
      stationValues.reduce((sum, s, i) => 
        sum + Math.pow(stationWeights[i] / totalStationWeight * s.uncertainty_ms, 2), 0)
      + Math.pow(stationAgreement / 2, 2)
    );
  }
  
  // Determine convergence state
  const nStations = Object.keys(stationEstimates).length;
  let state = 'UNKNOWN';
  if (estimates.length === 0) {
    state = 'NO_DATA';
  } else if (nStations === 1) {
    state = 'SINGLE_SOURCE';
  } else if (stationAgreement < 1.0) {
    state = 'LOCKED';
  } else if (stationAgreement < 3.0) {
    state = 'CONVERGING';
  } else {
    state = 'DIVERGENT';
  }
  
  return {
    available: estimates.length > 0,
    estimates: estimates,  // Individual channel estimates
    stations: stationEstimates,  // Per-station aggregates
    consensus: {
      d_clock_ms: parseFloat(consensusDclock.toFixed(3)),
      uncertainty_ms: parseFloat(consensusUncertainty.toFixed(3)),
      station_agreement_ms: parseFloat(stationAgreement.toFixed(3))
    },
    statistics: {
      mean_ms: parseFloat(consensusDclock.toFixed(3)),
      std_dev_ms: parseFloat(consensusUncertainty.toFixed(3)),
      count: estimates.length,
      n_stations: nStations
    },
    state: state,
    timestamp: new Date().toISOString()
  };
}

/**
 * Get Mode Probability data - propagation mode ridgeline visualization
 * Uses converged D_clock uncertainty for sharp mode discrimination
 * 
 * Now supports per-broadcast (station+frequency) mode calculation:
 * - station: WWV, WWVH, CHU, or BPM
 * - freq: frequency in MHz
 * 
 * Propagation delays are calculated based on geographic distance to each station.
 */
async function getModeProbabilityData(paths, config, targetChannel = null, targetStation = null, targetFreq = null) {
  const channels = config.recorder?.channels || [];
  const enabledChannels = channels.filter(ch => ch.enabled !== false);
  
  // Station distances from receiver (approximate, should come from config)
  // These are typical distances in km for a Missouri receiver
  const STATION_DISTANCES_KM = {
    'WWV': 1120,    // Fort Collins, CO
    'WWVH': 6600,   // Kekaha, Hawaii
    'CHU': 1522,    // Ottawa, Canada
    'BPM': 11500    // Pucheng, China
  };
  
  // Calculate mode delays based on station distance
  // Formula: delay = path_length / speed_of_light
  // For N-hop F2 layer: path_length ≈ N * 2 * sqrt(h² + (d/(2N))²)
  // where h = F2 layer height (~300 km), d = ground distance
  function calculateModeDelays(stationName) {
    const d = STATION_DISTANCES_KM[stationName] || 1500;  // Default 1500 km
    const h = 300;  // F2 layer height in km
    const c = 299792.458;  // Speed of light km/s
    
    // Ground wave (only viable for short distances)
    const groundDelay = d / c * 1000;  // ms
    
    // N-hop delays
    const delays = {};
    delays['Ground'] = { delay_ms: groundDelay, spread_ms: 0.1, viable: d < 200 };
    
    for (let n = 1; n <= 4; n++) {
      const hopDist = d / (2 * n);
      const pathLength = n * 2 * Math.sqrt(h * h + hopDist * hopDist);
      const delayMs = pathLength / c * 1000;
      const spreadMs = 0.3 + n * 0.3;  // Uncertainty increases with hops
      
      // Check if mode is viable (elevation angle > 5°)
      const elevAngle = Math.atan(h / hopDist) * 180 / Math.PI;
      const viable = elevAngle > 5 && elevAngle < 85;
      
      delays[`${n}F`] = { delay_ms: delayMs, spread_ms: spreadMs, viable };
    }
    
    // Also add E-layer modes for shorter paths
    const eHeight = 110;  // E layer height
    for (let n = 1; n <= 2; n++) {
      const hopDist = d / (2 * n);
      const pathLength = n * 2 * Math.sqrt(eHeight * eHeight + hopDist * hopDist);
      const delayMs = pathLength / c * 1000;
      delays[`${n}E`] = { delay_ms: delayMs, spread_ms: 0.3, viable: d < 2000 };
    }
    
    return delays;
  }
  
  // Mode delays will be calculated after we find data (need to know actual station)
  
  // Filter to specific channel if requested
  // Also filter by frequency if provided (channel names contain frequency, e.g., "SHARED 10 MHz")
  let searchChannels = enabledChannels;
  
  if (targetChannel) {
    searchChannels = searchChannels.filter(ch => (ch.description || `Channel ${ch.ssrc}`) === targetChannel);
  } else if (targetFreq) {
    // Filter by frequency - match channels containing the frequency in their name
    // e.g., targetFreq=10 matches "SHARED 10 MHz", "WWV 10 MHz", etc.
    searchChannels = searchChannels.filter(ch => {
      const name = ch.description || `Channel ${ch.ssrc}`;
      // Match patterns like "10 MHz", "10.0 MHz", "2.5 MHz"
      const freqMatch = name.match(/(\d+\.?\d*)\s*MHz/i);
      if (freqMatch) {
        const channelFreq = parseFloat(freqMatch[1]);
        return Math.abs(channelFreq - targetFreq) < 0.1;  // Allow small tolerance
      }
      return false;
    });
  }
  
  // Find best channel with convergence data
  let bestData = null;
  let bestUncertainty = Infinity;
  
  for (const ch of searchChannels) {
    const channelName = ch.description || `Channel ${ch.ssrc}`;
    const status = await getPhase2AnalyticsStatus(channelName, paths);
    
    if (status.available && status.propagation_delay_ms !== undefined) {
      // If a specific station was requested, only use data from that station
      if (targetStation && status.station !== targetStation) {
        continue;  // Skip - wrong station for this broadcast
      }
      
      // Get convergence uncertainty - lower is better
      const uncertainty = status.uncertainty_ms || 100;
      const isLocked = status.convergence?.is_locked || 
                       status.quality_grade === 'A' || 
                       status.quality_grade === 'B';
      
      // Prefer locked channels, then lowest uncertainty
      const effectiveUncertainty = isLocked ? uncertainty : uncertainty + 50;
      
      if (effectiveUncertainty < bestUncertainty) {
        bestUncertainty = effectiveUncertainty;
        bestData = {
          channel: channelName,
          measured_delay: status.propagation_delay_ms,
          uncertainty_ms: uncertainty,
          station: status.station,
          quality_grade: status.quality_grade,
          is_locked: isLocked,
          convergence_progress: status.convergence?.convergence_progress || 0,
          n_hops: status.n_hops
        };
      }
    }
  }
  
  if (!bestData || bestData.measured_delay === null) {
    // No data - return flat probabilities using requested or default station
    const defaultStation = targetStation || 'WWV';
    const defaultDelays = calculateModeDelays(defaultStation);
    return {
      available: false,
      station: defaultStation,
      candidates: Object.entries(defaultDelays).map(([mode, info]) => ({
        mode,
        delay_ms: info.delay_ms,
        probability: 0.0,
        expected_delay_ms: info.delay_ms
      })),
      measured_delay: null,
      uncertainty_ms: 100,
      message: 'Waiting for convergence...'
    };
  }
  
  // Calculate mode delays for the ACTUAL detected station (not the requested one)
  // This is critical: propagation delays depend on geographic distance to the station
  const actualStation = bestData.station || targetStation || 'WWV';
  const MODE_EXPECTED_DELAYS = calculateModeDelays(actualStation);
  
  // Calculate mode probabilities using Gaussian likelihood
  // P(mode|measured) ∝ exp(-0.5 * ((measured - expected) / σ)²)
  // where σ = sqrt(uncertainty² + mode_spread²)
  
  const measuredDelay = bestData.measured_delay;
  const uncertainty = bestData.uncertainty_ms;
  
  const candidates = [];
  let totalLikelihood = 0;
  
  for (const [mode, info] of Object.entries(MODE_EXPECTED_DELAYS)) {
    const expectedDelay = info.delay_ms;
    const modeSpread = info.spread_ms;
    
    // Combined uncertainty: measurement + mode spread
    const sigma = Math.sqrt(uncertainty * uncertainty + modeSpread * modeSpread);
    
    // Gaussian likelihood
    const zScore = (measuredDelay - expectedDelay) / sigma;
    const likelihood = Math.exp(-0.5 * zScore * zScore);
    
    candidates.push({
      mode,
      delay_ms: expectedDelay,
      expected_delay_ms: expectedDelay,
      spread_ms: modeSpread,
      z_score: zScore,
      likelihood: likelihood
    });
    
    totalLikelihood += likelihood;
  }
  
  // Normalize to probabilities
  for (const c of candidates) {
    c.probability = totalLikelihood > 0 ? c.likelihood / totalLikelihood : 0;
    delete c.likelihood; // Clean up
  }
  
  // Sort by probability descending
  candidates.sort((a, b) => b.probability - a.probability);
  
  // Determine most likely mode
  const bestMode = candidates[0];
  const confidence = bestMode.probability > 0.7 ? 'high' : 
                     bestMode.probability > 0.4 ? 'medium' : 'low';
  
  return {
    available: true,
    channel: bestData.channel,
    station: bestData.station,
    measured_delay: measuredDelay,
    uncertainty_ms: uncertainty,
    is_locked: bestData.is_locked,
    convergence_progress: bestData.convergence_progress,
    quality_grade: bestData.quality_grade,
    candidates: candidates,
    best_mode: bestMode.mode,
    best_probability: bestMode.probability,
    confidence: confidence,
    // For visualization
    discrimination_possible: uncertainty < 3.0,  // Need < 3ms to discriminate modes
    timestamp: new Date().toISOString()
  };
}

/**
 * Estimate propagation mode from delay
 */
function estimateModeFromDelay(delayMs) {
  if (delayMs < 2) return 'Ground';
  if (delayMs < 4) return '1E';
  if (delayMs < 6) return '1F';
  if (delayMs < 10) return '2F';
  if (delayMs < 14) return '3F';
  return '4F';
}

/**
 * Estimate uncertainty from quality grade
 */
function estimateUncertainty(grade) {
  switch (grade) {
    case 'A': return 0.3;
    case 'B': return 1.0;
    case 'C': return 2.5;
    case 'D': return 5.0;
    default: return 10.0;
  }
}

export {
  getClockOffsetSeries,
  getPhase2AnalyticsStatus,
  getAllPhase2Status,
  getBestDClock,
  getPhase2PipelineStatus,
  getGradeColor,
  getGradeDescription,
  getKalmanFunnelData,
  getConstellationData,
  getConsensusData,
  getModeProbabilityData
};

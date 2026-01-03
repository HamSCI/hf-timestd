/**
 * Timing Analysis Helper Functions
 * 
 * Backend logic for timing analysis API endpoints
 */

import fs from 'fs';
import { join } from 'path';
import { channelNameToKey } from '../timestd-paths.js';

/**
 * Parse CSV file and return rows
 */
function parseTimingCSV(filePath) {
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
        record[header.trim()] = values[index] ? values[index].trim() : '';
      });
      records.push(record);
    }
    
    return { status: 'OK', records, count: records.length };
  } catch (err) {
    console.error(`Error parsing CSV ${filePath}:`, err);
    return { status: 'error', records: [], count: 0, error: err.message };
  }
}

/**
 * Parse JSON file
 */
function parseJSON(filePath) {
  try {
    if (!fs.existsSync(filePath)) {
      return { status: 'not_found', data: null };
    }
    
    const content = fs.readFileSync(filePath, 'utf-8');
    const data = JSON.parse(content);
    
    return { status: 'OK', data };
  } catch (err) {
    console.error(`Error parsing JSON ${filePath}:`, err);
    return { status: 'error', data: null, error: err.message };
  }
}

/**
 * Get primary (best) time reference across all channels
 */
async function getPrimaryTimeReference(paths, config) {
  const channels = config.recorder?.channels || [];
  const enabledChannels = channels.filter(ch => ch.enabled !== false);
  
  let bestReference = null;
  let bestScore = -1;
  
  for (const channel of enabledChannels) {
    const channelName = channel.description || `Channel ${channel.ssrc}`;
    const key = channelNameToKey(channelName);  // e.g., "wwv10" from "WWV 10 MHz"
    const stateFile = join(paths.getStateDir(), `analytics-${key}.json`);
    
    if (!fs.existsSync(stateFile)) continue;
    
    try {
      const state = JSON.parse(fs.readFileSync(stateFile, 'utf-8'));
      
      if (!state.time_snap || !state.time_snap.rtp_timestamp) continue;
      
      const source = state.time_snap.source || 'unknown';
      const confidence = state.time_snap.confidence || 0;
      const ageSeconds = Date.now() / 1000 - (state.time_snap.established_at || state.time_snap.utc_timestamp);
      
      // Calculate score
      let score = confidence * 100;
      
      // Prefer tone-locked sources
      if (source.includes('startup')) score += 50;
      else if (source === 'ntp') score += 10;
      
      // Penalize age
      score -= Math.min(ageSeconds / 60, 30); // Max 30 point penalty for age
      
      // Bonus for high SNR
      const snr = state.time_snap.detection_snr_db || 0;
      if (snr > 20) score += Math.min((snr - 20) * 0.5, 10);
      
      if (score > bestScore) {
        bestScore = score;
        const qualityDetails = getQualityDetails(source, ageSeconds, 0);
        bestReference = {
          source_channel: channelName,
          source_type: source,
          station: state.time_snap.station || 'unknown',
          quality: qualityDetails.quality,
          effective_quality: qualityDetails.effectiveQuality,
          base_reference: qualityDetails.baseReference,
          is_tone_based: qualityDetails.isToneBased,
          precision_ms: getPrecision(source, ageSeconds),
          precision_description: qualityDetails.precisionDescription,
          confidence: confidence,
          snr_db: state.time_snap.detection_snr_db || null,
          tone_frequency_hz: state.time_snap.tone_frequency || null,
          age_seconds: Math.round(ageSeconds),
          next_check_seconds: Math.max(0, 300 - ageSeconds), // Check every 5 min
          rtp_anchor: state.time_snap.rtp_timestamp,
          utc_anchor: state.time_snap.utc_timestamp,
          utc_anchor_iso: new Date(state.time_snap.utc_timestamp * 1000).toISOString()
        };
      }
    } catch (err) {
      console.error(`Error reading state for ${channelName}:`, err);
    }
  }
  
  if (!bestReference) {
    return {
      available: false,
      message: 'No time reference available'
    };
  }
  
  return {
    available: true,
    ...bestReference
  };
}

/**
 * Get system-wide timing health summary
 */
async function getTimingHealthSummary(paths, config) {
  const channels = config.recorder?.channels || [];
  const enabledChannels = channels.filter(ch => ch.enabled !== false);
  
  let toneLocked = 0;
  let ntpSynced = 0;
  let interpolated = 0;
  let wallClock = 0;
  
  const driftValues = [];
  const jitterValues = [];
  let transitionsLast24h = 0;
  
  const channelDetails = [];
  
  for (const channel of enabledChannels) {
    const channelName = channel.description || `Channel ${channel.ssrc}`;
    const cleanName = channelName.replace(/\s+/g, '_');
    
    // Try Phase 2 status first (new system)
    const phase2StatusFile = join(
      paths.getPhase2Dir(channelName),
      'status',
      'analytics-service-status.json'
    );
    
    const phase2Status = parseJSON(phase2StatusFile);
    if (phase2Status.status === 'OK' && phase2Status.data) {
      const ch = Object.values(phase2Status.data.channels || {})[0];
      if (ch && ch.d_clock_ms !== undefined) {
        // Use Phase 2 data
        const quality = ch.quality_grade === 'A' || ch.quality_grade === 'B' ? 'TONE_LOCKED' :
                       ch.quality_grade === 'C' ? 'INTERPOLATED' : 'WALL_CLOCK';
        
        if (quality === 'TONE_LOCKED') toneLocked++;
        else if (quality === 'INTERPOLATED') interpolated++;
        else wallClock++;
        
        const drift = ch.d_clock_ms || 0;
        // Estimate jitter from quality grade if uncertainty not available
        const jitter = ch.uncertainty_ms || (
          ch.quality_grade === 'A' ? 0.5 :
          ch.quality_grade === 'B' ? 1.0 :
          ch.quality_grade === 'C' ? 2.0 :
          ch.quality_grade === 'D' ? 5.0 : 10.0
        );
        if (!isNaN(drift)) driftValues.push(drift);
        if (!isNaN(jitter)) jitterValues.push(jitter);
        
        channelDetails.push({
          channel: channelName,
          quality,
          drift_ms: drift,
          jitter_ms: jitter,
          d_clock_ms: ch.d_clock_ms,
          station: ch.station,
          health_score: ch.quality_grade === 'A' ? 100 : 
                       ch.quality_grade === 'B' ? 80 :
                       ch.quality_grade === 'C' ? 60 :
                       ch.quality_grade === 'D' ? 40 : 20
        });
        continue; // Skip legacy timing file check
      }
    }
    
    // Fallback to legacy timing metrics
    const today = new Date().toISOString().split('T')[0].replace(/-/g, '');
    const metricsFile = join(
      paths.getTimingDir(channelName),
      `${cleanName}_timing_metrics_${today}.csv`
    );
    
    const metrics = parseTimingCSV(metricsFile);
    if (metrics.status !== 'OK' || metrics.records.length === 0) continue;
    
    // Get latest record
    const latest = metrics.records[metrics.records.length - 1];
    
    // Count by quality - handle both old and new naming
    const quality = latest.quality || 'WALL_CLOCK';
    if (quality === 'TONE_LOCKED') toneLocked++;
    else if (quality === 'NTP_SYNCED') ntpSynced++;
    else if (quality === 'INTERPOLATED' || quality === 'TONE_AGED' || quality === 'TONE_STABLE') {
      interpolated++;  // Count all tone-based interpolation together
    }
    else wallClock++;
    
    // Collect drift/jitter
    const drift = parseFloat(latest.drift_ms);
    const jitter = parseFloat(latest.jitter_ms);
    if (!isNaN(drift)) driftValues.push(drift);
    if (!isNaN(jitter)) jitterValues.push(jitter);
    
    channelDetails.push({
      channel: channelName,
      quality,
      drift_ms: drift,
      jitter_ms: jitter,
      health_score: parseInt(latest.health_score) || 0
    });
    
    // Count transitions
    const transitionsFile = join(
      paths.getTimingDir(channelName),
      `${cleanName}_timing_transitions_${today}.csv`
    );
    
    const transitions = parseJSON(transitionsFile);
    if (transitions.status === 'OK' && transitions.data.transitions) {
      const cutoff = Date.now() - (24 * 3600 * 1000);
      transitionsLast24h += transitions.data.transitions.filter(t => {
        return new Date(t.timestamp).getTime() > cutoff;
      }).length;
    }
  }
  
  // Calculate drift statistics
  const avgDrift = driftValues.length > 0
    ? driftValues.reduce((sum, v) => sum + Math.abs(v), 0) / driftValues.length
    : 0;
  
  const maxDrift = driftValues.length > 0
    ? Math.max(...driftValues.map(v => Math.abs(v)))
    : 0;
  
  // Filter out unrealistic jitter values (> 10000ms indicates data collection issue)
  const validJitter = jitterValues.filter(v => v < 10000);
  const avgJitter = validJitter.length > 0
    ? validJitter.reduce((sum, v) => sum + v, 0) / validJitter.length
    : 0;
  
  return {
    tone_locked_channels: toneLocked,
    ntp_synced_channels: ntpSynced,
    interpolated_channels: interpolated,
    wall_clock_channels: wallClock,
    total_channels: enabledChannels.length,
    tone_lock_percentage: (toneLocked / enabledChannels.length * 100).toFixed(1),
    drift: {
      average_ms: avgDrift.toFixed(3),
      max_ms: maxDrift.toFixed(3),
      range_ms: `${Math.min(...driftValues).toFixed(3)} to ${Math.max(...driftValues).toFixed(3)}`,
      quality: classifyDriftQuality(avgDrift)
    },
    jitter: {
      average_ms: avgJitter.toFixed(3),
      quality: classifyJitterQuality(avgJitter)
    },
    transitions: {
      last_24h: transitionsLast24h,
      stability: classifyStability(transitionsLast24h, enabledChannels.length)
    },
    channels: channelDetails
  };
}

/**
 * Get timing metrics for a specific channel
 * Spans multiple days if hours > 24 or crosses midnight
 */
async function getTimingMetrics(channel, date, hours, paths) {
  if (!channel) {
    throw new Error('Channel is required');
  }
  
  const cleanName = channel.replace(/\s+/g, '_');
  const cutoff = Date.now() - (hours * 3600 * 1000);
  
  // Calculate which dates we need to check (today and potentially yesterday)
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
  
  // Collect records from all relevant dates
  let allRecords = [];
  for (const dateStr of datesToCheck) {
    const metricsFile = join(
      paths.getTimingDir(channel),
      `${cleanName}_timing_metrics_${dateStr}.csv`
    );
    
    const data = parseTimingCSV(metricsFile);
    if (data.status === 'OK' && data.records) {
      allRecords.push(...data.records);
    }
  }
  
  if (allRecords.length === 0) {
    return { available: false, message: `No metrics for ${channel}` };
  }
  
  // Filter by time window
  const filtered = allRecords.filter(r => {
    return new Date(r.timestamp_utc).getTime() > cutoff;
  });
  
  // Sort by timestamp
  filtered.sort((a, b) => new Date(a.timestamp_utc) - new Date(b.timestamp_utc));
  
  return {
    available: true,
    channel,
    date: date || todayStr,
    hours,
    count: filtered.length,
    metrics: filtered.map(r => ({
      timestamp: r.timestamp_utc,
      source_type: r.source_type,
      quality: r.quality,
      snr_db: r.snr_db ? parseFloat(r.snr_db) : null,
      confidence: parseFloat(r.confidence),
      age_seconds: parseFloat(r.age_seconds),
      drift_ms: parseFloat(r.drift_ms),
      jitter_ms: parseFloat(r.jitter_ms),
      ntp_offset_ms: r.ntp_offset_ms ? parseFloat(r.ntp_offset_ms) : null,
      health_score: parseInt(r.health_score)
    }))
  };
}

/**
 * Get timing transitions
 */
async function getTimingTransitions(channel, hours, paths, config) {
  const channels = channel === 'all'
    ? config.recorder?.channels.filter(ch => ch.enabled !== false) || []
    : [config.recorder?.channels.find(ch => (ch.description || `Channel ${ch.ssrc}`) === channel)];
  
  const allTransitions = [];
  const today = new Date().toISOString().split('T')[0].replace(/-/g, '');
  const cutoff = Date.now() - (hours * 3600 * 1000);
  
  for (const ch of channels) {
    if (!ch) continue;
    const channelName = ch.description || `Channel ${ch.ssrc}`;
    const cleanName = channelName.replace(/\s+/g, '_');
    
    const transitionsFile = join(
      paths.getTimingDir(channelName),
      `${cleanName}_timing_transitions_${today}.json`
    );
    
    const data = parseJSON(transitionsFile);
    if (data.status === 'OK' && data.data.transitions) {
      const filtered = data.data.transitions.filter(t => {
        return new Date(t.timestamp).getTime() > cutoff;
      });
      allTransitions.push(...filtered);
    }
  }
  
  // Sort by timestamp descending (most recent first)
  allTransitions.sort((a, b) => new Date(b.timestamp) - new Date(a.timestamp));
  
  return {
    count: allTransitions.length,
    hours,
    transitions: allTransitions
  };
}

/**
 * Get timeline data for visualization
 */
async function getTimingTimeline(channel, hours, paths) {
  const metrics = await getTimingMetrics(channel, 
    new Date().toISOString().split('T')[0].replace(/-/g, ''), 
    hours, paths);
  
  if (!metrics.available) {
    return { available: false, message: metrics.message };
  }
  
  // Group consecutive records with same source/quality
  const segments = [];
  let currentSegment = null;
  
  for (const m of metrics.metrics) {
    if (!currentSegment || 
        currentSegment.source !== m.source_type ||
        currentSegment.quality !== m.quality) {
      
      if (currentSegment) segments.push(currentSegment);
      
      currentSegment = {
        start_time: m.timestamp,
        end_time: m.timestamp,
        source: m.source_type,
        quality: m.quality,
        snr_avg_db: m.snr_db || 0,
        snr_count: m.snr_db ? 1 : 0,
        confidence_avg: m.confidence,
        confidence_count: 1,
        drift_avg_ms: m.drift_ms,
        drift_count: 1
      };
    } else {
      // Extend current segment
      currentSegment.end_time = m.timestamp;
      if (m.snr_db) {
        currentSegment.snr_avg_db += m.snr_db;
        currentSegment.snr_count++;
      }
      currentSegment.confidence_avg += m.confidence;
      currentSegment.confidence_count++;
      currentSegment.drift_avg_ms += m.drift_ms;
      currentSegment.drift_count++;
    }
  }
  
  if (currentSegment) segments.push(currentSegment);
  
  // Calculate averages
  segments.forEach(seg => {
    seg.snr_avg_db = seg.snr_count > 0 ? seg.snr_avg_db / seg.snr_count : null;
    seg.confidence_avg = seg.confidence_avg / seg.confidence_count;
    seg.drift_avg_ms = seg.drift_avg_ms / seg.drift_count;
    delete seg.snr_count;
    delete seg.confidence_count;
    delete seg.drift_count;
  });
  
  return {
    available: true,
    channel: metrics.channel,
    hours,
    segments
  };
}

// Helper functions

function classifyQuality(source, ageSeconds) {
  // Tone-based sources: wwv_startup, wwv_verified, chu_startup, wwvh_verified, etc.
  const isToneBased = source.includes('startup') || source.includes('verified') ||
                      source.includes('wwv') || source.includes('chu');
  
  if (isToneBased && ageSeconds < 300) {
    return 'TONE_LOCKED';
  } else if (isToneBased && ageSeconds < 3600) {
    return 'TONE_AGED';  // Tone reference older than 5 min but < 1 hour
  } else if (source === 'ntp') {
    return 'NTP_SYNCED';
  } else {
    return 'WALL_CLOCK';
  }
}

/**
 * Get detailed quality info including base reference and estimated precision
 */
function getQualityDetails(source, ageSeconds, driftMs) {
  const quality = classifyQuality(source, ageSeconds);
  
  // Base reference - what timing is derived from
  let baseReference = 'unknown';
  if (source.includes('wwv')) baseReference = 'WWV tone';
  else if (source.includes('chu')) baseReference = 'CHU tone';
  else if (source === 'ntp') baseReference = 'NTP';
  else baseReference = 'System clock';
  
  // Estimate current precision based on source and age
  // Tone sources: start at ±1ms, degrade ~1ms/hour due to ADC drift
  // NTP: ±10ms constant
  // Wall clock: ±seconds
  let estimatedPrecisionMs;
  let precisionDescription;
  
  // Tone-based sources: wwv_startup, wwv_verified, chu_startup, etc.
  const isToneBased = source.includes('startup') || source.includes('verified') ||
                      source.includes('wwv') || source.includes('chu');
  
  if (isToneBased) {
    // Tone-based: degrades with age
    const hoursSincelock = ageSeconds / 3600;
    estimatedPrecisionMs = 1.0 + (hoursSincelock * 1.0); // ~1ms/hour drift
    if (estimatedPrecisionMs < 2) {
      precisionDescription = 'Excellent (fresh tone)';
    } else if (estimatedPrecisionMs < 5) {
      precisionDescription = 'Good (aging tone)';
    } else {
      precisionDescription = 'Degraded (stale tone)';
    }
  } else if (source === 'ntp') {
    estimatedPrecisionMs = 10.0;
    precisionDescription = 'Good (NTP)';
  } else {
    estimatedPrecisionMs = 1000.0;
    precisionDescription = 'Poor (unsynchronized)';
  }
  
  // Effective quality considers both nominal quality AND actual drift
  // If drift is excellent despite aged source, effective quality is still good
  let effectiveQuality = quality;
  if (quality === 'TONE_AGED' && Math.abs(driftMs) < 1.0) {
    effectiveQuality = 'TONE_STABLE';  // Aged but drift is excellent
  }
  
  return {
    quality,
    effectiveQuality,
    baseReference,
    estimatedPrecisionMs,
    precisionDescription,
    isToneBased,
    ageSeconds
  };
}

function getPrecision(source, ageSeconds = 0) {
  // Tone-based sources: wwv_startup, wwv_verified, chu_startup, etc.
  const isToneBased = source.includes('startup') || source.includes('verified') ||
                      source.includes('wwv') || source.includes('chu');
  
  if (isToneBased) {
    // Tone precision degrades ~1ms per hour
    const hoursSinceLock = ageSeconds / 3600;
    return Math.min(1.0 + hoursSinceLock, 50.0);  // Cap at 50ms
  }
  if (source === 'ntp') return 10.0;
  return 1000.0; // wall clock
}

function classifyDriftQuality(avgDrift) {
  if (avgDrift < 1) return 'excellent';
  if (avgDrift < 5) return 'good';
  if (avgDrift < 10) return 'fair';
  return 'poor';
}

function classifyJitterQuality(avgJitter) {
  if (avgJitter < 0.5) return 'excellent';
  if (avgJitter < 2) return 'good';
  if (avgJitter < 5) return 'fair';
  return 'poor';
}

function classifyStability(transitions, channelCount) {
  const transitionsPerChannel = transitions / channelCount;
  if (transitionsPerChannel < 2) return 'excellent';
  if (transitionsPerChannel < 5) return 'good';
  if (transitionsPerChannel < 10) return 'fair';
  return 'poor';
}

export {
  getPrimaryTimeReference,
  getTimingHealthSummary,
  getTimingMetrics,
  getTimingTransitions,
  getTimingTimeline,
  getQualityDetails,
  classifyQuality
};

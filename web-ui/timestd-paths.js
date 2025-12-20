/**
 * HF Time Standard Paths - JavaScript/Node.js Implementation
 * 
 * Centralized path management for hf-timestd data structures.
 * MUST stay synchronized with Python implementation in src/hf_timestd/paths.py
 * 
 * SYNC VERSION: 2025-12-08-v3-discovery-fix
 */

import { join, dirname } from 'path';
import { readFileSync, readdirSync, existsSync } from 'fs';
import { fileURLToPath } from 'url';

const __dirname = dirname(fileURLToPath(import.meta.url));

/**
 * Convert channel name to key format.
 * 
 * Examples:
 *   SHARED_10000 -> shared10000
 *   CHU_3330 -> chu3330
 */
function channelNameToKey(channelName) {
    // Handle canonical STATION_KILOHERTZ format
    if (channelName.includes('_')) {
        const parts = channelName.split('_');
        if (['SHARED', 'WWV', 'CHU'].includes(parts[0])) {
            return `${parts[0].toLowerCase()}${parts[1]}`;
        }
    }

    // Fallback: underscored lowercase
    return channelName.replace(/ /g, '_').replace(/_/g, '').toLowerCase();
}

/**
 * Convert channel name to directory format (pass-through for canonical format).
 * 
 * The canonical format is STATION_KILOHERTZ (e.g., SHARED_10000, CHU_3330).
 * This function passes through canonical format unchanged.
 * 
 * Examples:
 *   SHARED_10000 -> SHARED_10000 (pass-through)
 *   CHU_3330 -> CHU_3330 (pass-through)
 */
function channelNameToDir(channelName) {
    // Already in canonical STATION_KILOHERTZ format - pass through
    if (channelName.includes('_')) {
        const parts = channelName.split('_');
        if (parts.length === 2 && ['SHARED', 'WWV', 'CHU'].includes(parts[0]) && /^\d+$/.test(parts[1])) {
            return channelName;
        }
    }

    // Fallback: replace spaces with underscores
    return channelName.replace(/ /g, '_');
}

/**
 * Return directory name unchanged (canonical format is STATION_KILOHERTZ).
 * Matches Python dir_to_channel_name().
 */
function dirToChannelName(dirName) {
    return dirName;
}

/**
 * Convert canonical channel name to human-readable display format.
 * Matches Python channel_to_display_name().
 * 
 * @param {string} channelName - Canonical format (e.g., "SHARED_10000", "CHU_3330")
 * @returns {string} Display format (e.g., "SHARED 10 MHz", "CHU 3.33 MHz")
 */
function channelToDisplayName(channelName) {
    const parts = channelName.split('_');
    if (parts.length === 2 && /^\d+$/.test(parts[1])) {
        const station = parts[0];
        const khz = parseInt(parts[1], 10);
        const mhz = khz / 1000;
        // Format MHz: use integer if whole number, otherwise show decimals
        const mhzStr = Number.isInteger(mhz) ? mhz.toString() : mhz.toFixed(2).replace(/\.?0+$/, '');
        return `${station} ${mhzStr} MHz`;
    }

    return channelName.replace(/_/g, ' ');
}

/**
 * Central path manager for HF Time Standard data structures.
 */
class TimeStdPaths {
    /**
     * @param {string} dataRoot - Root data directory (e.g., /tmp/timestd-test)
     */
    constructor(dataRoot) {
        this.dataRoot = dataRoot;
    }

    /**
     * Convert channel name to directory format (Station_kHz).
     */
    channelNameToDir(channelName) {
        return channelNameToDir(channelName);
    }

    /**
     * Get the data root directory.
     * 
     * @returns {string} Path: {data_root}/
     */
    getDataRoot() {
        return this.dataRoot;
    }

    /**
     * Get tick windows directory for BCD analysis.
     * 
     * @param {string} channelName - Channel name
     * @returns {string} Path: {data_root}/phase2/{CHANNEL}/tick_windows/
     */
    getTickWindowsDir(channelName) {
        return join(this.getPhase2Dir(channelName), 'tick_windows');
    }

    /**
     * Get station ID 440Hz directory.
     * 
     * @param {string} channelName - Channel name
     * @returns {string} Path: {data_root}/phase2/{CHANNEL}/station_id_440hz/
     */
    getStationId440HzDir(channelName) {
        return join(this.getPhase2Dir(channelName), 'station_id_440hz');
    }

    /**
     * Get test signal directory (minutes 8 and 44).
     * 
     * @param {string} channelName - Channel name
     * @returns {string} Path: {data_root}/phase2/{CHANNEL}/test_signal/
     */
    getTestSignalDir(channelName) {
        return join(this.getPhase2Dir(channelName), 'test_signal');
    }

    /**
     * Get BCD discrimination directory.
     * 
     * @param {string} channelName - Channel name
     * @returns {string} Path: {data_root}/phase2/{CHANNEL}/bcd_discrimination/
     */
    getBcdDiscriminationDir(channelName) {
        return join(this.getPhase2Dir(channelName), 'bcd_discrimination');
    }

    /**
     * Get Doppler analysis directory.
     * 
     * @param {string} channelName - Channel name
     * @returns {string} Path: {data_root}/phase2/{CHANNEL}/doppler/
     */
    getDopplerDir(channelName) {
        return join(this.getPhase2Dir(channelName), 'doppler');
    }

    /**
     * Get audio tones directory (500/600 Hz + BCD intermodulation analysis).
     * 
     * @param {string} channelName - Channel name
     * @returns {string} Path: {data_root}/phase2/{CHANNEL}/audio_tones/
     */
    getAudioTonesDir(channelName) {
        return join(this.getPhase2Dir(channelName), 'audio_tones');
    }

    // ========================================================================
    // Phase 2 Analytics Paths (Per-channel analytical results)
    // These methods provide convenient aliases to Phase 2 paths
    // ========================================================================

    /**
     * Get discrimination directory (WWV/WWVH per-minute analysis).
     * 
     * @param {string} channelName - Channel name
     * @returns {string} Path: {data_root}/phase2/{CHANNEL}/discrimination/
     */
    getDiscriminationDir(channelName) {
        return join(this.getPhase2Dir(channelName), 'discrimination');
    }

    /**
     * Get tone detections directory (1000/1200 Hz timing tones).
     * 
     * @param {string} channelName - Channel name
     * @returns {string} Path: {data_root}/phase2/{CHANNEL}/tone_detections/
     */
    getToneDetectionsDir(channelName) {
        return join(this.getPhase2Dir(channelName), 'tone_detections');
    }

    /**
     * Get carrier analysis directory (amplitude, phase, Doppler).
     * 
     * @param {string} channelName - Channel name
     * @returns {string} Path: {data_root}/phase2/{CHANNEL}/carrier_analysis/
     */
    getCarrierAnalysisDir(channelName) {
        return join(this.getPhase2Dir(channelName), 'carrier_analysis');
    }

    /**
     * Get timing metrics directory (time_snap status, drift, transitions).
     * 
     * @param {string} channelName - Channel name
     * @returns {string} Path: {data_root}/phase2/{CHANNEL}/timing/
     */
    getTimingDir(channelName) {
        return join(this.getPhase2Dir(channelName), 'timing');
    }

    /**
     * Get Phase 2 state directory (per-channel state files).
     * 
     * @param {string} channelName - Channel name
     * @returns {string} Path: {data_root}/phase2/{CHANNEL}/state/
     */
    getPhase2StateDir(channelName) {
        return join(this.getPhase2Dir(channelName), 'state');
    }

    /**
     * Get Phase 2 status directory (per-channel status files).
     * Note: The analytics service writes to 'status/' subdirectory.
     * 
     * @param {string} channelName - Channel name
     * @returns {string} Path: {data_root}/phase2/{CHANNEL}/status/
     */
    getPhase2StatusDir(channelName) {
        return join(this.getPhase2Dir(channelName), 'status');
    }

    /**
     * Get analytics service status file (per-channel).
     * This is where the analytics_service writes its status.
     * 
     * @param {string} channelName - Channel name
     * @returns {string} Path: {data_root}/phase2/{CHANNEL}/status/analytics-service-status.json
     */
    getAnalyticsServiceStatusFileForChannel(channelName) {
        return join(this.getPhase2StatusDir(channelName), 'analytics-service-status.json');
    }

    /**
     * Get channel status file (per-channel status in Phase 2).
     * 
     * @param {string} channelName - Channel name
     * @returns {string} Path: {data_root}/phase2/{CHANNEL}/state/channel-status.json
     */
    getChannelStatusFile(channelName) {
        return join(this.getPhase2StateDir(channelName), 'channel-status.json');
    }

    // ========================================================================
    // State Paths (Service persistence)
    // ========================================================================

    /**
     * Get state directory.
     * 
     * @returns {string} Path: {data_root}/state/
     */
    getStateDir() {
        return join(this.dataRoot, 'state');
    }

    /**
     * Get analytics state file for a channel.
     * 
     * @param {string} channelName - Channel name
     * @returns {string} Path: {data_root}/state/analytics-{key}.json
     * 
     * Example: WWV 10 MHz -> analytics-wwv10.json
     */
    getAnalyticsStateFile(channelName) {
        const channelKey = channelNameToKey(channelName);
        return join(this.getStateDir(), `analytics-${channelKey}.json`);
    }

    /**
     * Get core recorder status file.
     * 
     * @returns {string} Path: {data_root}/status/core-recorder-status.json
     */
    getCoreStatusFile() {
        return join(this.getStatusDir(), 'core-recorder-status.json');
    }

    // ========================================================================
    // System Status Paths
    // ========================================================================

    /**
     * Get system status directory.
     * 
     * @returns {string} Path: {data_root}/status/
     */
    getStatusDir() {
        return join(this.dataRoot, 'status');
    }

    /**
     * Get analytics service status file.
     * 
     * @returns {string} Path: {data_root}/status/analytics-service-status.json
     */
    getAnalyticsServiceStatusFile() {
        return join(this.getStatusDir(), 'analytics-service-status.json');
    }

    /**
     * Get GPSDO monitor status file.
     * Written by analytics service GPSDOMonitor, read by web-ui.
     * 
     * @returns {string} Path: {data_root}/status/gpsdo_status.json
     */
    getGpsdoStatusFile() {
        return join(this.getStatusDir(), 'gpsdo_status.json');
    }

    /**
     * Get timing status file (primary time reference).
     * Written by analytics service, read by web-ui.
     * 
     * @returns {string} Path: {data_root}/status/timing_status.json
     */
    getTimingStatusFile() {
        return join(this.getStatusDir(), 'timing_status.json');
    }

    // ========================================================================
    // PHASE 1: RAW BUFFER (binary complex64 + JSON sidecars)
    // ========================================================================

    /**
     * Get raw buffer root directory.
     * 
     * @returns {string} Path: {data_root}/raw_buffer/
     */
    getRawBufferRoot() {
        return join(this.dataRoot, 'raw_buffer');
    }

    /**
     * Get raw buffer directory for a channel.
     * 
     * @param {string} channelName - Channel name (e.g., "WWV 10 MHz")
     * @returns {string} Path: {data_root}/raw_buffer/{CHANNEL}/
     */
    getRawBufferDir(channelName) {
        const channelDir = channelNameToDir(channelName);
        return join(this.getRawBufferRoot(), channelDir);
    }

    // ========================================================================
    // PHASE 2: ANALYTICAL ENGINE
    // ========================================================================

    /**
     * Get Phase 2 root directory.
     * 
     * @returns {string} Path: {data_root}/phase2/
     */
    getPhase2Root() {
        return join(this.dataRoot, 'phase2');
    }

    /**
     * Get Phase 2 directory for a channel.
     * 
     * @param {string} channelName - Channel name
     * @returns {string} Path: {data_root}/phase2/{CHANNEL}/
     */
    getPhase2Dir(channelName) {
        const channelDir = channelNameToDir(channelName);
        return join(this.getPhase2Root(), channelDir);
    }

    /**
     * Get clock offset series directory (D_clock time series).
     * 
     * @param {string} channelName - Channel name
     * @returns {string} Path: {data_root}/phase2/{CHANNEL}/clock_offset/
     */
    getClockOffsetDir(channelName) {
        return join(this.getPhase2Dir(channelName), 'clock_offset');
    }

    /**
     * Get Phase 2 discrimination directory.
     * 
     * @param {string} channelName - Channel name
     * @returns {string} Path: {data_root}/phase2/{CHANNEL}/discrimination/
     */
    getPhase2DiscriminationDir(channelName) {
        return join(this.getPhase2Dir(channelName), 'discrimination');
    }

    // Phase 3 products are handled externally.

    // ========================================================================
    // Discovery Methods
    // ========================================================================

    /**
     * Discover all channels from any available data source.
     * Checks raw_buffer/ (Phase 1) and phase2/ (Phase 2).
     * 
     * @returns {string[]} List of channel names (human-readable format)
     */
    discoverChannels() {
        const channelSet = new Set();

        // Non-channel directories to exclude
        const excludeDirs = ['status', 'metadata', 'state', 'logs', 'fusion', 'upload'];

        // Valid channel directory pattern: station_kilohertz format (e.g., SHARED_10000, CHU_3330)
        // This filters out old _MHz format directories and stray directories
        const isValidChannelDir = (name) => {
            return /^(SHARED|WWV|CHU)_\d+$/.test(name);
        };

        // Check raw_buffer/ (Phase 1)
        const rawBufferDir = this.getRawBufferRoot();
        if (existsSync(rawBufferDir)) {
            const entries = readdirSync(rawBufferDir, { withFileTypes: true });
            for (const entry of entries) {
                if (entry.isDirectory() && !excludeDirs.includes(entry.name) && isValidChannelDir(entry.name)) {
                    channelSet.add(dirToChannelName(entry.name));
                }
            }
        }

        // Check phase2/ (Phase 2) - analytics data may exist without raw archive
        const phase2Dir = this.getPhase2Root();
        if (existsSync(phase2Dir)) {
            const entries = readdirSync(phase2Dir, { withFileTypes: true });
            for (const entry of entries) {
                if (entry.isDirectory() && !excludeDirs.includes(entry.name) && isValidChannelDir(entry.name)) {
                    channelSet.add(dirToChannelName(entry.name));
                }
            }
        }

        return Array.from(channelSet).sort();
    }

    /**
     * Discover all channels with Phase 2 analytical data.
     * 
     * @returns {string[]} List of channel names (human-readable format)
     */
    discoverPhase2Channels() {
        const phase2Dir = this.getPhase2Root();

        if (!existsSync(phase2Dir)) {
            return [];
        }

        const excludeDirs = ['status', 'metadata', 'state', 'logs', 'fusion', 'upload'];
        const channels = [];
        const entries = readdirSync(phase2Dir, { withFileTypes: true });

        for (const entry of entries) {
            // Valid channel directory pattern: station_kilohertz format (e.g., SHARED_10000, CHU_3330)
            if (entry.isDirectory() &&
                !excludeDirs.includes(entry.name) &&
                /^(SHARED|WWV|CHU)_\d+$/.test(entry.name)) {
                channels.push(dirToChannelName(entry.name));
            }
        }

        return channels.sort();
    }

    discoverProductChannels() {
        return [];
    }
}

/**
 * Load TimeStdPaths from configuration file.
 * 
 * @param {string} configPath - Path to timestd-config.toml (default: ./config/timestd-config.toml)
 * @returns {TimeStdPaths} TimeStdPaths instance configured from TOML
 */
async function loadPathsFromConfig(configPath = null) {
    // Dynamic import to avoid breaking if toml not installed
    let toml;
    try {
        const tomlModule = await import('toml');
        toml = tomlModule.default || tomlModule;
    } catch (err) {
        throw new Error('toml package required: npm install toml');
    }

    if (!configPath) {
        // Default location
        configPath = join(__dirname, '..', 'config', 'timestd-config.toml');
    }

    if (!existsSync(configPath)) {
        throw new Error(`Config file not found: ${configPath}`);
    }

    const configContent = readFileSync(configPath, 'utf8');
    const config = toml.parse(configContent);

    // Determine data root based on mode
    const mode = (config.recorder && config.recorder.mode) || 'test';

    let dataRoot;
    if (mode === 'production') {
        dataRoot = (config.recorder && config.recorder.production_data_root) || '/var/lib/hf-timestd';
    } else {
        dataRoot = (config.recorder && config.recorder.test_data_root) || '/tmp/timestd-test';
    }

    return new TimeStdPaths(dataRoot);
}

export {
    TimeStdPaths,
    loadPathsFromConfig,
    channelNameToKey,
    channelNameToDir,
    dirToChannelName,
    channelToDisplayName
};

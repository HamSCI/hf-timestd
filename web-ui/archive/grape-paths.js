/**
 * GRAPE Paths - JavaScript/Node.js Implementation
 * 
 * Centralized path management for GRAPE data structures.
 * MUST stay synchronized with Python implementation in src/grape_recorder/paths.py
 * 
 * SYNC VERSION: 2025-12-08-v3-discovery-fix
 * 
 * Change History:
 *   2025-12-08-v3: Issue 2.2 fix - Python discover_channels() now matches JS
 *   2025-12-04-v2: Three-phase architecture paths
 *   2025-11-01-v1: Initial implementation
 * 
 * Three-Phase Architecture:
 *   data_root/
 *   ├── raw_archive/{CHANNEL}/               # PHASE 1: Immutable Raw Archive (DRF)
 *   │   └── {YYYYMMDD}/
 *   │       ├── {YYYY-MM-DDTHH}/
 *   │       │   └── rf@{ts}.h5               # 20 kHz complex64 IQ
 *   │       └── metadata/
 *   │
 *   ├── phase2/{CHANNEL}/                    # PHASE 2: Analytical Engine
 *   │   ├── clock_offset/                    # D_clock(t) time series
 *   │   ├── carrier_analysis/                # Amplitude, phase, Doppler
 *   │   ├── discrimination/                  # WWV/WWVH per-minute
 *   │   ├── tone_detections/                 # 1000/1200 Hz markers
 *   │   └── state/
 *   │
 *   ├── products/{CHANNEL}/                  # PHASE 3: Derived Products
 *   │   ├── decimated/                       # 10 Hz DRF time series
 *   │   ├── spectrograms/                    # PNG images
 *   │   └── psws_upload/                     # PSWS format files
 *   │
 *   ├── state/                               # Global state
 *   ├── status/                              # System status (gpsdo_status.json, etc.)
 *   │   ├── gpsdo_status.json                # GPSDO monitor state
 *   │   └── timing_status.json               # Primary time reference
 *   └── logs/
 *   
 * Legacy paths (archives/, analytics/) have been removed - using new three-phase architecture only.
 * 
 * Usage:
 *   import { GRAPEPaths, loadPathsFromConfig } from './grape-paths.js';
 *   
 *   // From config
 *   const paths = loadPathsFromConfig('./config/grape-config.toml');
 *   
 *   // Or explicit data root
 *   const paths = new GRAPEPaths('/tmp/grape-test');
 *   
 *   // Get paths
 *   const archiveDir = paths.getArchiveDir('WWV 10 MHz');
 *   const drfDir = paths.getDigitalRFDir('WWV 10 MHz');
 *   const specPath = paths.getSpectrogramPath('WWV 10 MHz', '20251115', 'decimated');
 */

import { join, dirname } from 'path';
import { readFileSync, readdirSync, existsSync } from 'fs';
import { fileURLToPath } from 'url';

const __dirname = dirname(fileURLToPath(import.meta.url));

/**
 * Convert channel name to key format.
 * 
 * Examples:
 *   WWV 10 MHz -> wwv10
 *   WWV 2.5 MHz -> wwv2.5
 *   CHU 3.33 MHz -> chu3.33
 */
function channelNameToKey(channelName) {
    const parts = channelName.split(' ');
    if (parts.length < 2) {
        // Fallback: underscored lowercase
        return channelName.replace(/ /g, '_').toLowerCase();
    }
    
    const station = parts[0].toLowerCase();  // wwv, chu
    const freq = parts[1];                   // 10, 2.5, 3.33
    
    return `${station}${freq}`;
}

/**
 * Convert channel name to directory format.
 * 
 * Examples:
 *   WWV 10 MHz -> WWV_10_MHz
 *   CHU 3.33 MHz -> CHU_3.33_MHz
 */
function channelNameToDir(channelName) {
    return channelName.replace(/ /g, '_');
}

/**
 * Convert directory name back to human-readable format.
 * 
 * Examples:
 *   WWV_10_MHz -> WWV 10 MHz
 */
function dirToChannelName(dirName) {
    return dirName.replace(/_/g, ' ');
}

/**
 * Central path manager for GRAPE data structures.
 */
class GRAPEPaths {
    /**
     * @param {string} dataRoot - Root data directory (e.g., /tmp/grape-test)
     */
    constructor(dataRoot) {
        this.dataRoot = dataRoot;
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
     * Get decimated data directory (10 Hz DRF from Phase 3 products).
     * 
     * @param {string} channelName - Channel name
     * @returns {string} Path: {data_root}/products/{CHANNEL}/decimated/
     */
    getDecimatedDir(channelName) {
        return this.getProductsDecimatedDir(channelName);
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
    // Spectrogram Paths (from Phase 3 Products)
    // ========================================================================
    
    /**
     * Get spectrogram path for a channel (from products directory).
     * 
     * @param {string} channelName - Channel name
     * @param {string} date - Date in YYYYMMDD format
     * @returns {string} Path: {data_root}/products/{CHANNEL}/spectrograms/{YYYYMMDD}_spectrogram.png
     */
    getSpectrogramPath(channelName, date) {
        return join(this.getProductsSpectrogramsDir(channelName), `${date}_spectrogram.png`);
    }
    
    /**
     * Get spectrograms directory for a channel.
     * 
     * @param {string} channelName - Channel name
     * @returns {string} Path: {data_root}/products/{CHANNEL}/spectrograms/
     */
    getSpectrogramsDir(channelName) {
        return this.getProductsSpectrogramsDir(channelName);
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
    // PHASE 1: RAW ARCHIVE (Immutable Digital RF)
    // ========================================================================
    
    /**
     * Get raw archive root directory.
     * 
     * @returns {string} Path: {data_root}/raw_archive/
     */
    getRawArchiveRoot() {
        return join(this.dataRoot, 'raw_archive');
    }
    
    /**
     * Get raw archive directory for a channel.
     * 
     * @param {string} channelName - Channel name (e.g., "WWV 10 MHz")
     * @returns {string} Path: {data_root}/raw_archive/{CHANNEL}/
     */
    getRawArchiveDir(channelName) {
        const channelDir = channelNameToDir(channelName);
        return join(this.getRawArchiveRoot(), channelDir);
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
    
    // ========================================================================
    // PHASE 3: DERIVED PRODUCTS
    // ========================================================================
    
    /**
     * Get products root directory.
     * 
     * @returns {string} Path: {data_root}/products/
     */
    getProductsRoot() {
        return join(this.dataRoot, 'products');
    }
    
    /**
     * Get products directory for a channel.
     * 
     * @param {string} channelName - Channel name
     * @returns {string} Path: {data_root}/products/{CHANNEL}/
     */
    getProductsDir(channelName) {
        const channelDir = channelNameToDir(channelName);
        return join(this.getProductsRoot(), channelDir);
    }
    
    /**
     * Get Phase 3 decimated directory (10 Hz DRF).
     * 
     * @param {string} channelName - Channel name
     * @returns {string} Path: {data_root}/products/{CHANNEL}/decimated/
     */
    getProductsDecimatedDir(channelName) {
        return join(this.getProductsDir(channelName), 'decimated');
    }
    
    /**
     * Get Phase 3 spectrograms directory.
     * 
     * @param {string} channelName - Channel name
     * @returns {string} Path: {data_root}/products/{CHANNEL}/spectrograms/
     */
    getProductsSpectrogramsDir(channelName) {
        return join(this.getProductsDir(channelName), 'spectrograms');
    }
    
    // ========================================================================
    // Discovery Methods
    // ========================================================================
    
    /**
     * Discover all channels from any available data source.
     * Checks raw_archive/ (Phase 1), phase2/ (Phase 2), and products/ (Phase 3).
     * 
     * @returns {string[]} List of channel names (human-readable format)
     */
    discoverChannels() {
        const channelSet = new Set();
        
        // Non-channel directories to exclude
        const excludeDirs = ['status', 'metadata', 'state', 'logs', 'fusion', 'upload'];
        
        // Valid channel name pattern: must start with WWV or CHU
        // This filters out stray directories like "2", "8", "E", "M", "w"
        const isValidChannelDir = (name) => {
            return name.startsWith('WWV') || name.startsWith('CHU');
        };
        
        // Check raw_archive/ (Phase 1)
        const rawArchiveDir = this.getRawArchiveRoot();
        if (existsSync(rawArchiveDir)) {
            const entries = readdirSync(rawArchiveDir, { withFileTypes: true });
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
        
        // Check products/ (Phase 3)
        const productsDir = this.getProductsRoot();
        if (existsSync(productsDir)) {
            const entries = readdirSync(productsDir, { withFileTypes: true });
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
            // Valid channel names must start with WWV or CHU
            if (entry.isDirectory() && 
                !excludeDirs.includes(entry.name) &&
                (entry.name.startsWith('WWV') || entry.name.startsWith('CHU'))) {
                channels.push(dirToChannelName(entry.name));
            }
        }
        
        return channels.sort();
    }
    
    /**
     * Discover all channels with Phase 3 products.
     * 
     * @returns {string[]} List of channel names (human-readable format)
     */
    discoverProductChannels() {
        const productsDir = this.getProductsRoot();
        
        if (!existsSync(productsDir)) {
            return [];
        }
        
        const channels = [];
        const entries = readdirSync(productsDir, { withFileTypes: true });
        
        for (const entry of entries) {
            if (entry.isDirectory()) {
                channels.push(dirToChannelName(entry.name));
            }
        }
        
        return channels.sort();
    }
}

/**
 * Load GRAPEPaths from configuration file.
 * 
 * @param {string} configPath - Path to grape-config.toml (default: ./config/grape-config.toml)
 * @returns {GRAPEPaths} GRAPEPaths instance configured from TOML
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
        configPath = join(__dirname, '..', 'config', 'grape-config.toml');
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
        dataRoot = (config.recorder && config.recorder.production_data_root) || '/var/lib/signal-recorder';
    } else {
        dataRoot = (config.recorder && config.recorder.test_data_root) || '/tmp/grape-test';
    }
    
    return new GRAPEPaths(dataRoot);
}

export {
    GRAPEPaths,
    loadPathsFromConfig,
    channelNameToKey,
    channelNameToDir,
    dirToChannelName
};

/**
 * HDF5 Reader Utility for hf-timestd Monitoring Server
 * 
 * Provides functions to read L1A and L2 data products from HDF5 files
 * with quality metadata extraction and CSV fallback support.
 */

import h5wasm from 'h5wasm';
import { readFileSync, existsSync } from 'fs';
import { join } from 'path';

// Initialize h5wasm module (async initialization required)
let h5wasmReady = false;
let File = null;
let FS = null;

async function initH5wasm() {
    if (!h5wasmReady) {
        const Module = await h5wasm.ready;
        File = h5wasm.File;
        FS = Module.FS;
        h5wasmReady = true;
    }
    return { File, FS };
}

/**
 * Read L2 timing measurements from HDF5 file
 * 
 * @param {string} hdf5Path - Path to HDF5 file
 * @param {object} options - Options for filtering/limiting data
 * @param {number} options.maxRecords - Maximum number of records to return
 * @param {string} options.minQualityGrade - Minimum quality grade (A/B/C/D)
 * @param {string} options.qualityFlag - Filter by quality flag (GOOD/MARGINAL/BAD)
 * @returns {Promise<object>} Parsed timing measurements with metadata
 */
export async function readL2TimingMeasurements(hdf5Path, options = {}) {
    try {
        await initH5wasm();

        if (!existsSync(hdf5Path)) {
            throw new Error(`HDF5 file not found: ${hdf5Path}`);
        }

        // Read file into buffer
        const buffer = readFileSync(hdf5Path);

        // Write to h5wasm virtual filesystem
        const vfsPath = '/temp_l2.h5';
        FS.writeFile(vfsPath, new Uint8Array(buffer));

        // Open HDF5 file with h5wasm
        const file = new File(vfsPath, 'r');

        try {
            // Read datasets
            const timestamps = file.get('timestamp_utc').value;
            const clockOffsets = file.get('clock_offset_ms').value;
            const uncertainties = file.get('uncertainty_ms').value;
            const expandedUncertainties = file.get('expanded_uncertainty_ms').value;
            const qualityGrades = file.get('quality_grade').value;
            const qualityFlags = file.get('quality_flag').value;
            const confidences = file.get('confidence').value;
            const stations = file.get('station').value;
            const discriminationMethods = file.get('discrimination_method').value;
            const discriminationConfidences = file.get('discrimination_confidence').value;

            // Optional fields (may not exist in all files)
            let snrDb = null;
            let dopplerHz = null;
            let propagationMode = null;

            try {
                snrDb = file.get('snr_db')?.value;
                dopplerHz = file.get('doppler_hz')?.value;
                propagationMode = file.get('propagation_mode')?.value;
            } catch (err) {
                // Optional fields may not exist
            }

            // Convert to array of objects
            const measurements = [];
            const numRecords = timestamps.length;

            for (let i = 0; i < numRecords; i++) {
                const grade = String.fromCharCode(qualityGrades[i]);
                const flag = decodeString(qualityFlags[i]);

                // Apply quality filters if specified
                if (options.minQualityGrade) {
                    const gradeOrder = { 'A': 0, 'B': 1, 'C': 2, 'D': 3 };
                    if (gradeOrder[grade] > gradeOrder[options.minQualityGrade]) {
                        continue;
                    }
                }

                if (options.qualityFlag && flag !== options.qualityFlag) {
                    continue;
                }

                const measurement = {
                    timestamp: decodeString(timestamps[i]),
                    clock_offset_ms: clockOffsets[i],
                    uncertainty_ms: uncertainties[i],
                    expanded_uncertainty_ms: expandedUncertainties[i],
                    quality_grade: grade,
                    quality_flag: flag,
                    confidence: confidences[i],
                    station: decodeString(stations[i]),
                    discrimination_method: decodeString(discriminationMethods[i]),
                    discrimination_confidence: discriminationConfidences[i]
                };

                // Add optional fields if available
                if (snrDb) measurement.snr_db = snrDb[i];
                if (dopplerHz) measurement.doppler_hz = dopplerHz[i];
                if (propagationMode) measurement.propagation_mode = decodeString(propagationMode[i]);

                measurements.push(measurement);

                // Limit records if specified
                if (options.maxRecords && measurements.length >= options.maxRecords) {
                    break;
                }
            }

            // Calculate statistics
            const validOffsets = measurements
                .map(m => m.clock_offset_ms)
                .filter(v => isFinite(v));

            const statistics = {
                count: measurements.length,
                total_records: numRecords,
                min: validOffsets.length > 0 ? Math.min(...validOffsets) : null,
                max: validOffsets.length > 0 ? Math.max(...validOffsets) : null,
                mean: validOffsets.length > 0 ? validOffsets.reduce((a, b) => a + b, 0) / validOffsets.length : null,
                std: validOffsets.length > 1 ? calculateStd(validOffsets) : null
            };

            // Calculate grade distribution
            const gradeDistribution = { A: 0, B: 0, C: 0, D: 0 };
            measurements.forEach(m => {
                if (gradeDistribution.hasOwnProperty(m.quality_grade)) {
                    gradeDistribution[m.quality_grade]++;
                }
            });

            return {
                measurements,
                statistics,
                grade_distribution: gradeDistribution,
                source: 'hdf5',
                file_path: hdf5Path,
                status: 'OK'
            };

        } finally {
            file.close();
            // Clean up virtual filesystem
            try {
                FS.unlink(vfsPath);
            } catch (err) {
                // Ignore cleanup errors
            }
        }

    } catch (error) {
        console.error('Error reading L2 HDF5 file:', error);
        throw error;
    }
}

/**
 * Read L1A channel observables from HDF5 file
 * 
 * @param {string} hdf5Path - Path to HDF5 file
 * @param {object} options - Options for filtering/limiting data
 * @param {number} options.maxRecords - Maximum number of records to return
 * @param {string} options.qualityFlag - Filter by quality flag (GOOD/MARGINAL/BAD)
 * @returns {Promise<object>} Parsed channel observables with metadata
 */
export async function readL1AChannelObservables(hdf5Path, options = {}) {
    try {
        await initH5wasm();

        if (!existsSync(hdf5Path)) {
            throw new Error(`HDF5 file not found: ${hdf5Path}`);
        }

        // Read file into buffer
        const buffer = readFileSync(hdf5Path);

        // Write to h5wasm virtual filesystem
        const vfsPath = '/temp_l1a.h5';
        FS.writeFile(vfsPath, new Uint8Array(buffer));

        // Open HDF5 file with h5wasm
        const file = new File(vfsPath, 'r');

        try {
            // Read required datasets
            const timestamps = file.get('timestamp_utc').value;
            const qualityFlags = file.get('quality_flag').value;
            const dataCompleteness = file.get('data_completeness').value;

            // Read optional observables (may not all be present)
            const datasets = {
                carrier_power_db: safeGetDataset(file, 'carrier_power_db'),
                carrier_snr_db: safeGetDataset(file, 'carrier_snr_db'),
                carrier_doppler_hz: safeGetDataset(file, 'carrier_doppler_hz'),
                doppler_std_hz: safeGetDataset(file, 'doppler_std_hz'),
                coherence_time_sec: safeGetDataset(file, 'coherence_time_sec'),
                phase_variance_rad: safeGetDataset(file, 'phase_variance_rad'),
                wwv_tone_500hz_db: safeGetDataset(file, 'wwv_tone_500hz_db'),
                wwv_tone_600hz_db: safeGetDataset(file, 'wwv_tone_600hz_db'),
                wwvh_tone_1200hz_db: safeGetDataset(file, 'wwvh_tone_1200hz_db'),
                wwvh_tone_1500hz_db: safeGetDataset(file, 'wwvh_tone_1500hz_db'),
                chu_tone_db: safeGetDataset(file, 'chu_tone_db')
            };

            // Convert to array of objects
            const records = [];
            const numRecords = timestamps.length;

            for (let i = 0; i < numRecords; i++) {
                const flag = decodeString(qualityFlags[i]);

                // Apply quality filter if specified
                if (options.qualityFlag && flag !== options.qualityFlag) {
                    continue;
                }

                const record = {
                    timestamp: decodeString(timestamps[i]),
                    quality_flag: flag,
                    data_completeness: dataCompleteness[i]
                };

                // Add all available observables
                for (const [key, dataset] of Object.entries(datasets)) {
                    if (dataset) {
                        const value = dataset[i];
                        // Only include finite values
                        if (isFinite(value)) {
                            record[key] = value;
                        }
                    }
                }

                records.push(record);

                // Limit records if specified
                if (options.maxRecords && records.length >= options.maxRecords) {
                    break;
                }
            }

            return {
                records,
                count: records.length,
                total_records: numRecords,
                source: 'hdf5',
                file_path: hdf5Path,
                status: 'OK'
            };

        } finally {
            file.close();
            // Clean up virtual filesystem
            try {
                FS.unlink(vfsPath);
            } catch (err) {
                // Ignore cleanup errors
            }
        }

    } catch (error) {
        console.error('Error reading L1A HDF5 file:', error);
        throw error;
    }
}

/**
 * Helper function to safely get a dataset (returns null if not found)
 */
function safeGetDataset(file, datasetName) {
    try {
        return file.get(datasetName)?.value;
    } catch (err) {
        return null;
    }
}

/**
 * Decode HDF5 string (handles both string and byte array formats)
 */
function decodeString(value) {
    if (typeof value === 'string') {
        return value;
    }
    if (value instanceof Uint8Array) {
        // Convert byte array to string, trim null bytes
        return String.fromCharCode(...value).replace(/\0/g, '').trim();
    }
    return String(value);
}

/**
 * Calculate standard deviation
 */
function calculateStd(values) {
    const mean = values.reduce((a, b) => a + b, 0) / values.length;
    const squaredDiffs = values.map(v => Math.pow(v - mean, 2));
    const variance = squaredDiffs.reduce((a, b) => a + b, 0) / values.length;
    return Math.sqrt(variance);
}

/**
 * Get HDF5 file path for L2 timing measurements
 * 
 * @param {object} paths - TimeStdPaths instance
 * @param {string} channelName - Channel name (e.g., "WWV 10 MHz")
 * @param {string} date - Date in YYYYMMDD format
 * @returns {string} Path to HDF5 file
 */
export function getL2TimingMeasurementsPath(paths, channelName, date) {
    const timingDir = paths.getTimingDir(channelName);
    return join(timingDir, `timing_measurements_${date}.h5`);
}

/**
 * Get HDF5 file path for L1A channel observables
 * 
 * @param {object} paths - TimeStdPaths instance
 * @param {string} channelName - Channel name (e.g., "WWV 10 MHz")
 * @param {string} date - Date in YYYYMMDD format
 * @returns {string} Path to HDF5 file
 */
export function getL1AChannelObservablesPath(paths, channelName, date) {
    const phase2Dir = paths.getPhase2Dir(channelName);
    return join(phase2Dir, `channel_observables_${date}.h5`);
}

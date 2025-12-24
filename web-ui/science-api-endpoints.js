// ============================================================================
// SCIENCE API ENDPOINTS - Phase 1
// Quality-first data access with complete metadata and provenance
// ============================================================================

/**
 * GET /api/v1/science/tec
 * Total Electron Content time series
 * 
 * Query params:
 *   station: WWV, WWVH, CHU, BPM (required)
 *   start: ISO 8601 timestamp (optional)
 *   end: ISO 8601 timestamp (optional)
 *   min_confidence: 0-1 (default: 0.0)
 *   min_frequencies: integer (default: 2)
 */
app.get('/api/v1/science/tec', async (req, res) => {
    try {
        const { station, start, end, min_confidence = 0.0, min_frequencies = 2 } = req.query;

        if (!station) {
            return res.status(400).json({
                error: {
                    code: 'MISSING_PARAMETER',
                    message: 'Station parameter is required. Valid stations: WWV, WWVH, CHU, BPM'
                }
            });
        }

        const validStations = ['WWV', 'WWVH', 'CHU', 'BPM'];
        if (!validStations.includes(station.toUpperCase())) {
            return res.status(400).json({
                error: {
                    code: 'INVALID_STATION',
                    message: `Station '${station}' not found. Valid stations: ${validStations.join(', ')}`
                }
            });
        }

        // Determine date range
        const now = new Date();
        const endDate = end ? new Date(end) : now;
        const startDate = start ? new Date(start) : new Date(endDate.getTime() - 24 * 3600 * 1000);

        // Read TEC CSV files
        const tecDir = join(dataRoot, 'phase2', 'science', 'tec');

        if (!fs.existsSync(tecDir)) {
            return res.json({
                metadata: {
                    station: station.toUpperCase(),
                    parameter: 'Total Electron Content',
                    units: 'TECU (10^16 electrons/m²)',
                    validation_status: 'pending',
                    message: 'TEC directory not found - Science Aggregator may not be running'
                },
                data: [],
                quality: { total_measurements: 0 },
                provenance: { data_source: 'hf-timestd Science Aggregator' }
            });
        }

        const tecFiles = fs.readdirSync(tecDir)
            .filter(f => f.startsWith('tec_') && f.endsWith('.csv'))
            .sort();

        let allData = [];

        for (const file of tecFiles) {
            try {
                const csvPath = join(tecDir, file);
                const csvContent = fs.readFileSync(csvPath, 'utf8');
                const records = csvParse(csvContent, {
                    columns: true,
                    skip_empty_lines: true
                });

                for (const row of records) {
                    if (row.station !== station.toUpperCase()) continue;

                    const timestamp = new Date(row.timestamp_utc);
                    if (timestamp < startDate || timestamp > endDate) continue;

                    const confidence = parseFloat(row.confidence);
                    const nFreq = parseInt(row.n_frequencies);

                    if (confidence < parseFloat(min_confidence)) continue;
                    if (nFreq < parseInt(min_frequencies)) continue;

                    // Parse frequencies
                    const freqList = row.frequencies_mhz ? row.frequencies_mhz.split(';').map(f => parseFloat(f)) : [];

                    // Quality flag
                    let qualityFlag = 'GOOD';
                    if (confidence < 0.7 || nFreq < 3) qualityFlag = 'MARGINAL';
                    if (confidence < 0.5 || nFreq < 2) qualityFlag = 'BAD';

                    allData.push({
                        timestamp: row.timestamp_utc,
                        minute_boundary: parseInt(row.minute_boundary),
                        tec_tecu: parseFloat(row.tec_tecu),
                        tec_electrons_m2: parseFloat(row.tec_tecu) * 1e16,
                        t_vacuum_error_ms: parseFloat(row.t_vacuum_error_ms),
                        confidence: confidence,
                        uncertainty_tecu: 5.0, // Conservative estimate pending validation
                        residuals_ms: parseFloat(row.residuals_ms),
                        n_frequencies: nFreq,
                        frequencies_mhz: freqList,
                        quality_flag: qualityFlag
                    });
                }
            } catch (err) {
                console.error(`Error reading TEC file ${file}:`, err.message);
            }
        }

        // Calculate quality summary
        const goodCount = allData.filter(d => d.quality_flag === 'GOOD').length;
        const marginalCount = allData.filter(d => d.quality_flag === 'MARGINAL').length;
        const badCount = allData.filter(d => d.quality_flag === 'BAD').length;
        const meanConfidence = allData.length > 0
            ? allData.reduce((sum, d) => sum + d.confidence, 0) / allData.length
            : 0;
        const meanNFreq = allData.length > 0
            ? allData.reduce((sum, d) => sum + d.n_frequencies, 0) / allData.length
            : 0;

        res.json({
            metadata: {
                station: station.toUpperCase(),
                parameter: 'Total Electron Content',
                units: 'TECU (10^16 electrons/m²)',
                coordinate_system: 'WGS84',
                time_system: 'UTC',
                cadence_seconds: 60,
                description: 'TEC estimated from multi-frequency HF propagation delay',
                accuracy_estimate: '±5-10 TECU (vs GPS TEC)',
                validation_status: 'pending',
                query: {
                    start: startDate.toISOString(),
                    end: endDate.toISOString(),
                    min_confidence: parseFloat(min_confidence),
                    min_frequencies: parseInt(min_frequencies)
                }
            },
            data: allData,
            quality: {
                total_measurements: allData.length,
                good_measurements: goodCount,
                marginal_measurements: marginalCount,
                bad_measurements: badCount,
                mean_confidence: parseFloat(meanConfidence.toFixed(3)),
                mean_n_frequencies: parseFloat(meanNFreq.toFixed(1)),
                data_completeness: allData.length > 0 ? parseFloat((goodCount / allData.length).toFixed(2)) : 0
            },
            provenance: {
                data_source: 'hf-timestd Science Aggregator',
                processing_version: '1.0.0',
                tec_estimator_version: '1.0.0',
                generated_at: new Date().toISOString(),
                station_location: {
                    latitude: config.station?.latitude,
                    longitude: config.station?.longitude,
                    callsign: config.station?.callsign
                }
            }
        });

    } catch (err) {
        console.error('TEC API Error:', err);
        res.status(500).json({
            error: {
                code: 'INTERNAL_ERROR',
                message: err.message,
                timestamp: new Date().toISOString()
            }
        });
    }
});

/**
 * GET /api/v1/science/group-delay
 * Multi-frequency group delay for TEC dispersion visualization
 * 
 * Query params:
 *   station: WWV, WWVH, CHU, BPM (required)
 *   timestamp: ISO 8601 timestamp for single minute (required)
 */
app.get('/api/v1/science/group-delay', async (req, res) => {
    try {
        const { station, timestamp } = req.query;

        if (!station || !timestamp) {
            return res.status(400).json({
                error: {
                    code: 'MISSING_PARAMETER',
                    message: 'Both station and timestamp parameters are required'
                }
            });
        }

        const targetTime = new Date(timestamp);
        const minuteBoundary = Math.floor(targetTime.getTime() / 1000 / 60) * 60;

        // Find all channels for this station
        const phase2Dir = join(dataRoot, 'phase2');
        const channels = fs.readdirSync(phase2Dir)
            .filter(d => {
                const stat = fs.statSync(join(phase2Dir, d));
                return stat.isDirectory() && d !== 'science' && d !== 'fusion';
            });

        const measurements = [];

        // Read clock_offset CSVs for each channel
        for (const channel of channels) {
            try {
                const dateStr = targetTime.toISOString().split('T')[0].replace(/-/g, '');
                const clockOffsetDir = join(phase2Dir, channel, 'clock_offset');
                const csvFiles = fs.readdirSync(clockOffsetDir)
                    .filter(f => f.includes(dateStr) && f.endsWith('.csv'));

                if (csvFiles.length === 0) continue;

                const csvPath = join(clockOffsetDir, csvFiles[0]);
                const csvContent = fs.readFileSync(csvPath, 'utf8');
                const records = csvParse(csvContent, {
                    columns: true,
                    skip_empty_lines: true
                });

                for (const row of records) {
                    if (parseInt(row.minute_boundary_utc) !== minuteBoundary) continue;
                    if (row.station !== station.toUpperCase()) continue;

                    measurements.push({
                        frequency_mhz: parseFloat(row.frequency_mhz),
                        frequency_hz: parseFloat(row.frequency_mhz) * 1e6,
                        toa_ms: parseFloat(row.clock_offset_ms),
                        uncertainty_ms: parseFloat(row.uncertainty_ms || 1.0),
                        snr_db: parseFloat(row.snr_db || 0),
                        quality_flag: row.quality_grade === 'A' || row.quality_grade === 'B' ? 'GOOD' : 'MARGINAL'
                    });
                }
            } catch (err) {
                // Skip channels with errors
            }
        }

        // Sort by frequency
        measurements.sort((a, b) => a.frequency_mhz - b.frequency_mhz);

        // Calculate linear fit if we have enough points
        let fit = null;
        if (measurements.length >= 2) {
            // Linear regression: toa = slope * (1/f²) + intercept
            const x = measurements.map(m => 1 / (m.frequency_hz ** 2));
            const y = measurements.map(m => m.toa_ms);

            const n = x.length;
            const sumX = x.reduce((a, b) => a + b, 0);
            const sumY = y.reduce((a, b) => a + b, 0);
            const sumXY = x.reduce((sum, xi, i) => sum + xi * y[i], 0);
            const sumX2 = x.reduce((sum, xi) => sum + xi * xi, 0);

            const slope = (n * sumXY - sumX * sumY) / (n * sumX2 - sumX * sumX);
            const intercept = (sumY - slope * sumX) / n;

            // TEC from slope: slope = 40.3 * TEC
            const tecTecu = slope / 40.3;

            // R²
            const yMean = sumY / n;
            const ssTotal = y.reduce((sum, yi) => sum + (yi - yMean) ** 2, 0);
            const ssResidual = y.reduce((sum, yi, i) => sum + (yi - (slope * x[i] + intercept)) ** 2, 0);
            const rSquared = 1 - (ssResidual / ssTotal);

            fit = {
                tec_tecu: parseFloat(tecTecu.toFixed(2)),
                t_vacuum_ms: parseFloat(intercept.toFixed(3)),
                slope: parseFloat(slope.toFixed(3)),
                intercept: parseFloat(intercept.toFixed(3)),
                r_squared: parseFloat(rSquared.toFixed(4)),
                residuals_rms_ms: parseFloat(Math.sqrt(ssResidual / n).toFixed(3))
            };
        }

        res.json({
            metadata: {
                station: station.toUpperCase(),
                parameter: 'Ionospheric Group Delay',
                units: 'milliseconds',
                description: 'Frequency-dependent propagation delay for TEC estimation'
            },
            data: {
                timestamp: targetTime.toISOString(),
                minute_boundary: minuteBoundary,
                measurements: measurements,
                fit: fit
            },
            provenance: {
                data_source: 'Phase 2 clock_offset CSVs',
                aggregation_method: 'Real-time multi-channel query'
            }
        });

    } catch (err) {
        console.error('Group Delay API Error:', err);
        res.status(500).json({
            error: {
                code: 'INTERNAL_ERROR',
                message: err.message
            }
        });
    }
});


/**
 * Advanced Timing Visualizations for hf-timestd
 * 
 * Four specialized scientific visualizations for timing analysis:
 * 1. KalmanFunnelChart - Clock stability convergence over time
 * 2. ConstellationRadar - Geographic timing error polar plot
 * 3. ProbabilityPeak - KDE consensus time visualization
 * 4. ModeProbabilityRidge - Propagation mode heatmap
 * 
 * All components use Plotly.js for rendering.
 */

// ============================================================================
// 1. KALMAN FUNNEL CHART - Clock Stability Convergence
// ============================================================================

class KalmanFunnelChart {
    /**
     * Visualizes clock stability over time with uncertainty bounds.
     * Shows high-variance measurements narrowing into tight lock.
     * 
     * @param {string} containerId - DOM element ID
     * @param {Object} options - Configuration options
     */
    constructor(containerId, options = {}) {
        this.container = document.getElementById(containerId);
        this.options = {
            timeWindowMinutes: options.timeWindowMinutes || 60,
            yAxisRange: options.yAxisRange || [-10, 10], // ±ms
            lockedColor: options.lockedColor || 'rgba(59, 130, 246, 0.3)',
            holdColor: options.holdColor || 'rgba(148, 163, 184, 0.3)',
            lineColor: options.lineColor || '#3b82f6',
            anomalyColor: options.anomalyColor || '#ef4444',
            ...options
        };
        this.data = [];
        this.chart = null;
    }

    /**
     * Update with new data points
     * @param {Array} points - [{timestamp, offset_ms, uncertainty_ms, status}]
     */
    update(points) {
        if (!points || points.length === 0) return;
        
        this.data = points;
        this.render();
    }

    render() {
        if (!this.container) return;

        // Use ISO strings to ensure Plotly interprets as UTC
        const timestamps = this.data.map(p => new Date(p.timestamp * 1000).toISOString());
        const offsets = this.data.map(p => p.offset_ms);
        const upperBound = this.data.map(p => p.offset_ms + p.uncertainty_ms);
        const lowerBound = this.data.map(p => p.offset_ms - p.uncertainty_ms);
        
        // Detect anomalies (points outside previous funnel)
        const anomalies = this.detectAnomalies();
        
        // Build traces
        const traces = [];
        
        // Confidence band (the "funnel")
        // We need to separate LOCKED and HOLD segments for different colors
        const segments = this.segmentByStatus();
        
        segments.forEach((segment, idx) => {
            const segmentTimes = segment.points.map(p => new Date(p.timestamp * 1000).toISOString());
            const segmentUpper = segment.points.map(p => p.offset_ms + p.uncertainty_ms);
            const segmentLower = segment.points.map(p => p.offset_ms - p.uncertainty_ms);
            
            // Upper bound line (invisible, for fill reference)
            traces.push({
                x: segmentTimes,
                y: segmentUpper,
                mode: 'lines',
                line: { width: 0 },
                showlegend: false,
                hoverinfo: 'skip',
                name: `upper_${idx}`
            });
            
            // Lower bound with fill to upper
            traces.push({
                x: segmentTimes,
                y: segmentLower,
                mode: 'lines',
                fill: 'tonexty',
                fillcolor: segment.status === 'LOCKED' ? this.options.lockedColor : this.options.holdColor,
                line: { width: 0 },
                showlegend: idx === 0,
                name: segment.status === 'LOCKED' ? 'Locked (±σ)' : 'Hold (±σ)',
                hoverinfo: 'skip'
            });
        });
        
        // The "truth" line (actual offset)
        traces.push({
            x: timestamps,
            y: offsets,
            mode: 'lines',
            name: 'Clock Offset',
            line: { color: this.options.lineColor, width: 2 },
            hovertemplate: '%{y:.3f} ms<br>%{x}<extra></extra>'
        });
        
        // Anomaly markers
        if (anomalies.length > 0) {
            traces.push({
                x: anomalies.map(a => new Date(a.timestamp * 1000).toISOString()),
                y: anomalies.map(a => a.offset_ms),
                mode: 'markers',
                name: 'Anomaly',
                marker: {
                    color: this.options.anomalyColor,
                    size: 10,
                    symbol: 'x'
                },
                hovertemplate: 'ANOMALY<br>%{y:.3f} ms<br>%{x}<extra></extra>'
            });
        }
        
        // Zero reference line
        traces.push({
            x: [timestamps[0], timestamps[timestamps.length - 1]],
            y: [0, 0],
            mode: 'lines',
            name: 'UTC Reference',
            line: { color: '#22c55e', width: 1, dash: 'dash' },
            hoverinfo: 'skip'
        });

        const layout = {
            title: {
                text: 'Clock Stability Convergence (Kalman Funnel)',
                font: { color: '#e0e0e0', size: 16 }
            },
            paper_bgcolor: 'transparent',
            plot_bgcolor: 'rgba(30, 41, 59, 0.5)',
            margin: { l: 60, r: 30, t: 50, b: 50 },
            xaxis: {
                title: { text: 'Time (UTC)', font: { color: '#94a3b8' } },
                gridcolor: '#334155',
                tickcolor: '#64748b',
                tickfont: { color: '#94a3b8' },
                type: 'date',
                // Adaptive tick format based on zoom level
                tickformatstops: [
                    { dtickrange: [null, 60000], value: '%H:%M:%S' },
                    { dtickrange: [60000, 3600000], value: '%H:%M' },
                    { dtickrange: [3600000, 86400000], value: '%H:%M' },
                    { dtickrange: [86400000, null], value: '%b %d\n%H:%M' }
                ]
            },
            yaxis: {
                title: { text: 'Offset from UTC (ms)', font: { color: '#94a3b8' } },
                range: this.options.yAxisRange,
                gridcolor: '#334155',
                tickcolor: '#64748b',
                tickfont: { color: '#94a3b8' },
                zeroline: true,
                zerolinecolor: '#22c55e',
                zerolinewidth: 1
            },
            legend: {
                font: { color: '#e0e0e0' },
                bgcolor: 'rgba(30, 41, 59, 0.8)',
                x: 0.02,
                y: 0.98
            },
            hovermode: 'x unified',
            dragmode: 'zoom'  // Enable drag-to-zoom by default
        };

        const config = {
            responsive: true,
            displayModeBar: true,
            modeBarButtonsToRemove: ['lasso2d', 'select2d', 'autoScale2d'],
            displaylogo: false,
            scrollZoom: true  // Enable scroll to zoom
        };

        if (this.chart) {
            Plotly.react(this.container, traces, layout, config);
        } else {
            Plotly.newPlot(this.container, traces, layout, config);
            this.chart = true;
        }
    }

    detectAnomalies() {
        const anomalies = [];
        for (let i = 1; i < this.data.length; i++) {
            const prev = this.data[i - 1];
            const curr = this.data[i];
            const prevUpper = prev.offset_ms + prev.uncertainty_ms;
            const prevLower = prev.offset_ms - prev.uncertainty_ms;
            
            if (curr.offset_ms > prevUpper || curr.offset_ms < prevLower) {
                anomalies.push(curr);
            }
        }
        return anomalies;
    }

    segmentByStatus() {
        const segments = [];
        let currentSegment = null;
        
        this.data.forEach(point => {
            const status = point.status || 'HOLD';
            if (!currentSegment || currentSegment.status !== status) {
                if (currentSegment && currentSegment.points.length > 0) {
                    // Add last point of previous segment to bridge
                    currentSegment = { status, points: [this.data[this.data.indexOf(point) - 1] || point] };
                } else {
                    currentSegment = { status, points: [] };
                }
                segments.push(currentSegment);
            }
            currentSegment.points.push(point);
        });
        
        return segments.filter(s => s.points.length > 0);
    }
}


// ============================================================================
// 2. CONSTELLATION RADAR - Geographic Timing Error Polar Plot
// ============================================================================

class ConstellationRadar {
    /**
     * Polar plot showing timing errors by station azimuth.
     * Center = Perfect Time (0 error), outer = high error.
     * Always shows vectors pointing toward CHU, WWV, and WWVH.
     * 
     * @param {string} containerId - DOM element ID
     * @param {Object} options - Configuration options
     */
    constructor(containerId, options = {}) {
        this.container = document.getElementById(containerId);
        this.options = {
            maxRadius: options.maxRadius || 20, // ms
            goodThreshold: options.goodThreshold || 1, // ms
            fairThreshold: options.fairThreshold || 5, // ms
            ...options
        };
        
        // Fixed station azimuths from receiver at EM38ww (38.9°N, 92.2°W)
        // Calculated using great circle bearings to transmitter sites
        this.stationVectors = {
            'WWV': { azimuth: 284, label: 'WWV', color: '#3b82f6' },    // Fort Collins, CO (WNW)
            'WWVH': { azimuth: 275, label: 'WWVH', color: '#8b5cf6' },  // Hawaii (W)
            'CHU': { azimuth: 57, label: 'CHU', color: '#22c55e' },     // Ottawa, Canada (NE)
            'BPM': { azimuth: 342, label: 'BPM', color: '#f97316' }     // Pucheng, China (NNW)
        };
        
        this.chart = null;
    }

    /**
     * Update with station data
     * @param {Object} data - {stations: [{name, azimuth_deg, error_ms, snr, active}]}
     */
    update(data) {
        const stations = data?.stations || [];
        this.render(stations);
    }

    render(stations) {
        if (!this.container) return;

        const traces = [];
        const maxR = this.options.maxRadius;
        
        // 1. Draw the three fixed station direction vectors (always visible)
        Object.entries(this.stationVectors).forEach(([key, vec]) => {
            // Direction line from center to edge
            traces.push({
                type: 'scatterpolar',
                mode: 'lines',
                r: [0, maxR],
                theta: [vec.azimuth, vec.azimuth],
                line: { 
                    color: vec.color, 
                    width: 2,
                    dash: 'dot'
                },
                name: vec.label,
                showlegend: true,
                hoverinfo: 'skip'
            });
            
            // Station label at the edge
            traces.push({
                type: 'scatterpolar',
                mode: 'text',
                r: [maxR * 1.08],
                theta: [vec.azimuth],
                text: [vec.label],
                textfont: { color: vec.color, size: 13, family: 'Arial Black' },
                showlegend: false,
                hoverinfo: 'skip'
            });
        });
        
        // 2. Group measurements by base station and place pucks on each vector line
        const activeStations = stations.filter(s => s.active !== false);
        
        // Group by base station (WWV, WWVH, CHU, BPM)
        const stationGroups = { 'WWV': [], 'WWVH': [], 'CHU': [], 'BPM': [] };
        
        activeStations.forEach(station => {
            const stationUpper = (station.base_station || station.name || '').toUpperCase();
            if (stationUpper.includes('WWVH')) {
                stationGroups['WWVH'].push(station);
            } else if (stationUpper.includes('WWV')) {
                stationGroups['WWV'].push(station);
            } else if (stationUpper.includes('CHU')) {
                stationGroups['CHU'].push(station);
            } else if (stationUpper.includes('BPM')) {
                stationGroups['BPM'].push(station);
            }
        });
        
        // ALWAYS place a puck for each station (4 pucks total: WWV, WWVH, CHU, BPM)
        Object.entries(this.stationVectors).forEach(([baseStation, vec]) => {
            const measurements = stationGroups[baseStation] || [];
            
            let r, errorColor, size, hoverText;
            
            if (measurements.length > 0) {
                // Use the best (lowest error) measurement for this station
                measurements.sort((a, b) => Math.abs(a.error_ms) - Math.abs(b.error_ms));
                const best = measurements[0];
                
                const error = Math.abs(best.error_ms);
                r = Math.min(error, maxR);
                
                // Color by error magnitude
                if (error < this.options.goodThreshold) {
                    errorColor = '#22c55e'; // Green - good
                } else if (error < this.options.fairThreshold) {
                    errorColor = '#eab308'; // Yellow - fair
                } else {
                    errorColor = '#ef4444'; // Red - poor/multipath
                }
                
                // Size based on SNR
                size = Math.max(14, Math.min(28, best.snr || 15));
                
                hoverText = `<b>${baseStation}</b><br>` +
                           `Channel: ${best.channel || best.name}<br>` +
                           `Error: ${best.error_ms.toFixed(2)} ms<br>` +
                           `SNR: ${(best.snr || 0).toFixed(1)} dB<extra></extra>`;
            } else {
                // No data - show placeholder puck at edge (grayed out)
                r = maxR * 0.95;
                errorColor = '#475569';  // Gray - no data
                size = 12;
                hoverText = `<b>${baseStation}</b><br>No data<extra></extra>`;
            }
            
            // Place puck ON the station's azimuth line
            traces.push({
                type: 'scatterpolar',
                mode: 'markers',
                r: [r],
                theta: [vec.azimuth],
                marker: {
                    size: size,
                    color: errorColor,
                    line: { color: vec.color, width: 3 },
                    symbol: 'circle'
                },
                name: baseStation,
                showlegend: false,
                hovertemplate: hoverText
            });
        });
        
        // 3. Center marker (Perfect UTC)
        traces.push({
            type: 'scatterpolar',
            mode: 'markers+text',
            r: [0],
            theta: [0],
            text: ['UTC'],
            textposition: 'bottom center',
            textfont: { color: '#22c55e', size: 11 },
            marker: {
                size: 10,
                color: '#22c55e',
                symbol: 'star'
            },
            showlegend: false,
            hoverinfo: 'skip'
        });

        const layout = {
            title: {
                text: 'Station Constellation (Timing Error by Azimuth)',
                font: { color: '#e0e0e0', size: 16 }
            },
            paper_bgcolor: 'transparent',
            plot_bgcolor: 'transparent',
            polar: {
                bgcolor: 'rgba(30, 41, 59, 0.5)',
                radialaxis: {
                    visible: true,
                    range: [0, maxR],
                    ticksuffix: 'ms',
                    tickfont: { color: '#94a3b8', size: 10 },
                    gridcolor: '#334155',
                    linecolor: '#475569'
                },
                angularaxis: {
                    tickfont: { color: '#94a3b8' },
                    gridcolor: '#334155',
                    linecolor: '#475569',
                    direction: 'clockwise',
                    rotation: 90 // North at top
                }
            },
            legend: {
                font: { color: '#e0e0e0', size: 11 },
                bgcolor: 'rgba(30, 41, 59, 0.8)',
                x: 0.02,
                y: 0.98
            },
            showlegend: true,
            margin: { l: 60, r: 60, t: 60, b: 60 }
        };

        if (this.chart) {
            Plotly.react(this.container, traces, layout, { responsive: true, displayModeBar: false });
        } else {
            Plotly.newPlot(this.container, traces, layout, { responsive: true, displayModeBar: false });
            this.chart = true;
        }
    }
}


// ============================================================================
// 3. PROBABILITY PEAK - KDE Consensus Time Visualization
// ============================================================================

class ProbabilityPeak {
    /**
     * Kernel Density Estimation showing consensus time.
     * Sharp peak = agreement, double-hump = mode ambiguity.
     * 
     * @param {string} containerId - DOM element ID
     * @param {Object} options - Configuration options
     */
    constructor(containerId, options = {}) {
        this.container = document.getElementById(containerId);
        this.options = {
            xRange: options.xRange || [-5, 5], // ms
            bandwidth: options.bandwidth || 0.3, // KDE bandwidth
            resolution: options.resolution || 200, // Points in curve
            ...options
        };
        this.chart = null;
    }

    /**
     * Update with offset estimates
     * @param {Array} estimates - [{source, offset}]
     */
    update(estimates) {
        if (!estimates || estimates.length === 0) return;
        this.render(estimates);
    }

    render(estimates) {
        if (!this.container) return;

        const offsets = estimates.map(e => e.offset);
        
        // Calculate KDE
        const kde = this.calculateKDE(offsets);
        
        // Find peaks for state detection
        const peaks = this.findPeaks(kde.y);
        const state = peaks.length > 1 ? 'SPLIT_BRAIN' : 'LOCKED';
        const stateColor = state === 'LOCKED' ? '#22c55e' : '#f97316';
        
        // Build traces
        const traces = [];
        
        // KDE curve with gradient fill
        traces.push({
            x: kde.x,
            y: kde.y,
            mode: 'lines',
            name: 'Probability Density',
            fill: 'tozeroy',
            fillcolor: state === 'LOCKED' 
                ? 'rgba(34, 197, 94, 0.3)' 
                : 'rgba(249, 115, 22, 0.3)',
            line: { 
                color: stateColor, 
                width: 3,
                shape: 'spline'
            },
            hovertemplate: 'Offset: %{x:.2f} ms<br>Density: %{y:.3f}<extra></extra>'
        });
        
        // Individual measurement markers at bottom
        traces.push({
            x: offsets,
            y: offsets.map(() => -0.02 * Math.max(...kde.y)),
            mode: 'markers',
            name: 'Measurements',
            marker: {
                size: 8,
                color: '#3b82f6',
                symbol: 'line-ns',
                line: { width: 2 }
            },
            hovertemplate: '%{x:.3f} ms<extra></extra>'
        });
        
        // UTC reference line
        traces.push({
            x: [0, 0],
            y: [0, Math.max(...kde.y) * 1.1],
            mode: 'lines',
            name: 'Expected UTC',
            line: { color: '#94a3b8', width: 2, dash: 'dash' },
            hoverinfo: 'skip'
        });
        
        // Peak markers
        peaks.forEach((peak, idx) => {
            traces.push({
                x: [kde.x[peak.index]],
                y: [peak.value],
                mode: 'markers+text',
                text: [`Peak ${idx + 1}`],
                textposition: 'top center',
                textfont: { color: '#e0e0e0', size: 10 },
                marker: {
                    size: 10,
                    color: stateColor,
                    symbol: 'diamond'
                },
                showlegend: false,
                hoverinfo: 'skip'
            });
        });

        const layout = {
            title: {
                text: `Consensus Time Distribution (${state === 'LOCKED' ? '✓ Locked' : '⚠ Split Brain'})`,
                font: { color: stateColor, size: 16 }
            },
            paper_bgcolor: 'transparent',
            plot_bgcolor: 'rgba(30, 41, 59, 0.5)',
            margin: { l: 60, r: 30, t: 50, b: 50 },
            xaxis: {
                title: { text: 'Offset from UTC (ms)', font: { color: '#94a3b8' } },
                range: this.options.xRange,
                gridcolor: '#334155',
                tickcolor: '#64748b',
                tickfont: { color: '#94a3b8' },
                zeroline: true,
                zerolinecolor: '#94a3b8'
            },
            yaxis: {
                title: { text: 'Probability Density', font: { color: '#94a3b8' } },
                gridcolor: '#334155',
                tickcolor: '#64748b',
                tickfont: { color: '#94a3b8' },
                rangemode: 'tozero'
            },
            legend: {
                font: { color: '#e0e0e0' },
                bgcolor: 'rgba(30, 41, 59, 0.8)',
                x: 0.02,
                y: 0.98
            },
            annotations: [{
                x: 0,
                y: -0.12,
                xref: 'x',
                yref: 'paper',
                text: 'Expected UTC',
                showarrow: false,
                font: { color: '#94a3b8', size: 11 }
            }]
        };

        if (this.chart) {
            Plotly.react(this.container, traces, layout, { responsive: true, displayModeBar: false });
        } else {
            Plotly.newPlot(this.container, traces, layout, { responsive: true, displayModeBar: false });
            this.chart = true;
        }
    }

    calculateKDE(data) {
        const [xMin, xMax] = this.options.xRange;
        const n = this.options.resolution;
        const h = this.options.bandwidth;
        
        const x = [];
        const y = [];
        
        for (let i = 0; i < n; i++) {
            const xi = xMin + (xMax - xMin) * (i / (n - 1));
            x.push(xi);
            
            // Gaussian kernel
            let density = 0;
            for (const d of data) {
                const u = (xi - d) / h;
                density += Math.exp(-0.5 * u * u) / (h * Math.sqrt(2 * Math.PI));
            }
            y.push(density / data.length);
        }
        
        return { x, y };
    }

    findPeaks(y) {
        const peaks = [];
        const threshold = Math.max(...y) * 0.3; // 30% of max
        
        for (let i = 1; i < y.length - 1; i++) {
            if (y[i] > y[i-1] && y[i] > y[i+1] && y[i] > threshold) {
                peaks.push({ index: i, value: y[i] });
            }
        }
        
        // Filter peaks that are too close together
        const filtered = [];
        for (const peak of peaks) {
            if (filtered.length === 0 || 
                Math.abs(peak.index - filtered[filtered.length-1].index) > this.options.resolution * 0.1) {
                filtered.push(peak);
            }
        }
        
        return filtered;
    }
}


// ============================================================================
// 4. MODE PROBABILITY RIDGE - Propagation Mode Heatmap
// ============================================================================

class ModeProbabilityRidge {
    /**
     * Ridgeline/heatmap showing propagation mode probabilities.
     * 
     * @param {string} containerId - DOM element ID
     * @param {Object} options - Configuration options
     */
    constructor(containerId, options = {}) {
        this.container = document.getElementById(containerId);
        this.options = {
            xRange: options.xRange || [0, 20], // delay ms
            modeOrder: options.modeOrder || ['Ground', '1E', '1F', '2F', '3F', '4F'],
            gaussianWidth: options.gaussianWidth || 0.8, // ms
            ...options
        };
        this.chart = null;
    }

    /**
     * Update with mode probability data
     * @param {Object} data - {candidates: [{mode, delay_ms, probability}], measured_delay}
     */
    update(data) {
        if (!data || !data.candidates) return;
        this.render(data);
    }

    render(data) {
        if (!this.container) return;

        const traces = [];
        const modes = this.options.modeOrder;
        
        // Create x-axis points
        const xPoints = [];
        for (let x = this.options.xRange[0]; x <= this.options.xRange[1]; x += 0.1) {
            xPoints.push(x);
        }
        
        // Build ridgeline for each mode
        modes.forEach((mode, modeIdx) => {
            const candidate = data.candidates.find(c => c.mode === mode);
            
            if (candidate) {
                // Calculate gaussian curve for this mode
                const yValues = xPoints.map(x => {
                    const u = (x - candidate.delay_ms) / this.options.gaussianWidth;
                    return candidate.probability * Math.exp(-0.5 * u * u);
                });
                
                // Color based on probability
                const opacity = Math.max(0.3, candidate.probability);
                const color = this.getProbabilityColor(candidate.probability);
                
                traces.push({
                    x: xPoints,
                    y: yValues.map(v => v + modeIdx), // Offset by mode index for ridgeline effect
                    mode: 'lines',
                    fill: 'tozeroy',
                    fillcolor: color.replace('1)', `${opacity})`),
                    line: { color: color, width: 2 },
                    name: `${mode} (${(candidate.probability * 100).toFixed(0)}%)`,
                    hovertemplate: `${mode}<br>Delay: %{x:.1f} ms<br>P: ${(candidate.probability * 100).toFixed(0)}%<extra></extra>`
                });
            } else {
                // Empty placeholder for this mode
                traces.push({
                    x: [this.options.xRange[0], this.options.xRange[1]],
                    y: [modeIdx, modeIdx],
                    mode: 'lines',
                    line: { color: 'rgba(100, 116, 139, 0.3)', width: 1, dash: 'dot' },
                    name: `${mode} (inactive)`,
                    showlegend: false,
                    hoverinfo: 'skip'
                });
            }
        });
        
        // Measured delay vertical line
        if (data.measured_delay !== undefined) {
            traces.push({
                x: [data.measured_delay, data.measured_delay],
                y: [-0.2, modes.length + 0.5],
                mode: 'lines',
                name: `Measured (${data.measured_delay.toFixed(1)} ms)`,
                line: { color: '#f43f5e', width: 3 },
                hoverinfo: 'skip'
            });
        }

        const layout = {
            title: {
                text: 'Propagation Mode Probability',
                font: { color: '#e0e0e0', size: 16 }
            },
            paper_bgcolor: 'transparent',
            plot_bgcolor: 'rgba(30, 41, 59, 0.5)',
            margin: { l: 80, r: 30, t: 50, b: 50 },
            xaxis: {
                title: { text: 'Propagation Delay (ms)', font: { color: '#94a3b8' } },
                range: this.options.xRange,
                gridcolor: '#334155',
                tickcolor: '#64748b',
                tickfont: { color: '#94a3b8' }
            },
            yaxis: {
                title: { text: 'Mode', font: { color: '#94a3b8' } },
                tickmode: 'array',
                tickvals: modes.map((_, i) => i),
                ticktext: modes,
                gridcolor: '#334155',
                tickcolor: '#64748b',
                tickfont: { color: '#94a3b8' },
                range: [-0.5, modes.length - 0.5]
            },
            legend: {
                font: { color: '#e0e0e0' },
                bgcolor: 'rgba(30, 41, 59, 0.8)',
                x: 1.02,
                y: 0.98
            },
            showlegend: true
        };

        if (this.chart) {
            Plotly.react(this.container, traces, layout, { responsive: true, displayModeBar: false });
        } else {
            Plotly.newPlot(this.container, traces, layout, { responsive: true, displayModeBar: false });
            this.chart = true;
        }
    }

    getProbabilityColor(p) {
        if (p >= 0.7) return 'rgba(34, 197, 94, 1)';  // Green - high confidence
        if (p >= 0.4) return 'rgba(59, 130, 246, 1)'; // Blue - medium
        if (p >= 0.2) return 'rgba(234, 179, 8, 1)';  // Yellow - low
        return 'rgba(148, 163, 184, 1)';              // Gray - very low
    }
}


// ============================================================================
// EXPORT
// ============================================================================

if (typeof module !== 'undefined' && module.exports) {
    module.exports = {
        KalmanFunnelChart,
        ConstellationRadar,
        ProbabilityPeak,
        ModeProbabilityRidge
    };
}

// Also attach to window for browser usage
if (typeof window !== 'undefined') {
    window.KalmanFunnelChart = KalmanFunnelChart;
    window.ConstellationRadar = ConstellationRadar;
    window.ProbabilityPeak = ProbabilityPeak;
    window.ModeProbabilityRidge = ModeProbabilityRidge;
}

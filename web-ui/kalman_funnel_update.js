// Multi-station Kalman Funnel update and render methods
// Replace the update() and render() methods in KalmanFunnelChart class

update(data) {
    if (!data || !data.station_data) return;

    this.stationData = data.station_data;
    this.render();
}

render() {
    if (!this.container) return;

    const traces = [];

    // Station colors matching constellation radar
    const stationColors = {
        'WWV': '#3b82f6',    // Blue
        'WWVH': '#8b5cf6',   // Purple
        'CHU': '#22c55e',    // Green
        'BPM': '#f97316'     // Orange
    };

    // Create a trace for each station
    for (const [station, points] of Object.entries(this.stationData)) {
        if (points.length === 0) continue;

        const timestamps = points.map(p => new Date(p.timestamp * 1000).toISOString());
        const offsets = points.map(p => p.offset_ms);
        const upperBound = points.map(p => p.offset_ms + p.uncertainty_ms);
        const lowerBound = points.map(p => p.offset_ms - p.uncertainty_ms);

        // Upper bound (invisible, for fill reference)
        traces.push({
            x: timestamps,
            y: upperBound,
            mode: 'lines',
            line: { width: 0 },
            showlegend: false,
            hoverinfo: 'skip',
            name: `${station}_upper`
        });

        // Lower bound with fill
        traces.push({
            x: timestamps,
            y: lowerBound,
            mode: 'lines',
            fill: 'tonexty',
            fillcolor: stationColors[station].replace(')', ', 0.2)').replace('rgb', 'rgba'),
            line: { width: 0 },
            showlegend: false,
            hoverinfo: 'skip',
            name: `${station}_lower`
        });

        // Station measurement line
        traces.push({
            x: timestamps,
            y: offsets,
            mode: 'lines+markers',
            name: station,
            line: { color: stationColors[station], width: 2 },
            marker: { size: 4, color: stationColors[station] },
            hovertemplate: `<b>${station}</b><br>%{y:.2f} ms<br>%{x}<extra></extra>`
        });
    }

    // Zero reference line
    if (traces.length > 0) {
        const allTimestamps = Object.values(this.stationData)
            .flat()
            .map(p => new Date(p.timestamp * 1000).toISOString())
            .sort();

        if (allTimestamps.length > 0) {
            traces.push({
                x: [allTimestamps[0], allTimestamps[allTimestamps.length - 1]],
                y: [0, 0],
                mode: 'lines',
                name: 'UTC Reference',
                line: { color: '#94a3b8', width: 1, dash: 'dash' },
                hoverinfo: 'skip'
            });
        }
    }

    const layout = {
        title: {
            text: 'Multi-Station Clock Convergence',
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
            tickformatstops: [
                { dtickrange: [null, 60000], value: '%H:%M:%S' },
                { dtickrange: [60000, 3600000], value: '%H:%M' },
                { dtickrange: [3600000, 86400000], value: '%H:%M' },
                { dtickrange: [86400000, null], value: '%b %d\\n%H:%M' }
            ]
        },
        yaxis: {
            title: { text: 'Offset from UTC (ms)', font: { color: '#94a3b8' } },
            autorange: true,
            gridcolor: '#334155',
            tickcolor: '#64748b',
            tickfont: { color: '#94a3b8' },
            zeroline: true,
            zerolinecolor: '#94a3b8',
            zerolinewidth: 1
        },
        legend: {
            font: { color: '#e0e0e0' },
            bgcolor: 'rgba(30, 41, 59, 0.8)',
            x: 0.02,
            y: 0.98
        },
        hovermode: 'x unified',
        dragmode: 'zoom'
    };

    const config = {
        responsive: true,
        displayModeBar: true,
        modeBarButtonsToRemove: ['lasso2d', 'select2d', 'autoScale2d'],
        displaylogo: false,
        scrollZoom: true
    };

    if (this.chart) {
        Plotly.react(this.container, traces, layout, config);
    } else {
        Plotly.newPlot(this.container, traces, layout, config);
        this.chart = true;
    }
}

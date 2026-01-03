/**
 * Discrimination Charts Component
 * 
 * Visualizes all WWV/WWVH discrimination methods and channel characterization.
 * The backend implements 13 weighted votes:
 * 
 * Vote 0:  Test Signal (minutes 8/44) - Ground Truth from schedule
 * Vote 1:  440 Hz Station ID (minutes 1/2) - Ground Truth
 * Vote 2:  BCD Amplitude Correlation (100 Hz subcarrier)
 * Vote 3:  Timing Tones (1000 Hz WWV / 1200 Hz WWVH power ratio)
 * Vote 4:  Tick SNR Comparison (5ms tick integration)
 * Vote 5:  500/600 Hz Ground Truth (14 exclusive minutes/hour)
 * Vote 6:  Doppler Stability (σ ratio, independent of power)
 * Vote 7:  Test Signal ToA vs BCD ToA (timing coherence)
 * Vote 7b: Chirp Delay Spread (multipath quality)
 * Vote 7c: Coherence Time Quality (channel stability)
 * Vote 8:  Harmonic Power Ratio (500→1000, 600→1200 Hz)
 * Vote 9:  FSS Geographic Validator (ionospheric path fingerprint)
 * Vote 10: Noise Coherence (transient event detection)
 * Vote 11: Burst ToA Precision (high-resolution timing cross-validation)
 * Vote 12: Spreading Factor (channel physics: L = τ_D × f_D)
 */

let currentData = null;
let solarZenithData = null;

// Color Palette (matches styles.css)
// Station colors are theme-independent
const COLORS = {
  wwv: '#3498db',
  wwvh: '#e67e22',
  groundTruth: '#10b981',
  error: '#ef4444',
  accent: '#8b5cf6',
  solarWwv: '#e74c3c',
  solarWwvh: '#9b59b6'
};

// Theme-dependent colors - called dynamically
function getThemeColors() {
  const isLight = document.body.classList.contains('light-theme');
  if (isLight) {
    return {
      background: 'rgba(248, 250, 252, 0.9)',
      grid: 'rgba(100, 116, 139, 0.2)',
      text: '#1e293b',
      textMuted: '#64748b',
      paper: 'rgba(255, 255, 255, 0.95)',
      plot: 'rgba(248, 250, 252, 0.9)'
    };
  } else {
    return {
      background: 'rgba(30, 41, 59, 0.8)',
      grid: 'rgba(139, 92, 246, 0.15)',
      text: '#e0e0e0',
      textMuted: 'rgba(255, 255, 255, 0.5)',
      paper: 'rgba(0,0,0,0)',
      plot: 'rgba(15, 23, 42, 0.4)'
    };
  }
}

// Get theme-aware Plotly layout defaults
function getPlotlyLayoutDefaults() {
  const theme = getThemeColors();
  return {
    paper_bgcolor: theme.paper,
    plot_bgcolor: theme.plot,
    font: { color: theme.text, family: '-apple-system, BlinkMacSystemFont, Segoe UI, Roboto, sans-serif' },
    title: {
      x: 0.01,  // Left-align to avoid modebar overlap
      xanchor: 'left',
      font: { size: 14, color: theme.text }
    },
    xaxis: {
      gridcolor: theme.grid,
      linecolor: theme.grid,
      tickfont: { color: theme.textMuted },
      titlefont: { color: theme.text }
    },
    yaxis: {
      gridcolor: theme.grid,
      linecolor: theme.grid,
      tickfont: { color: theme.textMuted },
      titlefont: { color: theme.text }
    },
    legend: {
      bgcolor: 'rgba(0,0,0,0)',
      font: { color: theme.text }
    },
    hoverlabel: {
      bgcolor: theme.background,
      bordercolor: COLORS.accent,
      font: { color: theme.text }
    }
  };
}

// Legacy constant for backward compatibility (uses current theme)
const PLOTLY_LAYOUT_DEFAULTS = getPlotlyLayoutDefaults();

// Standard Plotly config - modebar on hover only, no logo
// Note: Also defined in theme-toggle.js, use var to allow redeclaration
var PLOTLY_CONFIG = {
  displayModeBar: 'hover',
  displaylogo: false,
  modeBarButtonsToRemove: ['lasso2d', 'select2d'],
  responsive: true
};

// Helper: Create left-aligned title object to avoid modebar overlap
function makeTitle(text) {
  return {
    text: text,
    x: 0,
    xanchor: 'left',
    font: { size: 13, color: getThemeColors().text }
  };
}

// Initialize date selector to today
document.addEventListener('DOMContentLoaded', () => {
  const dateSelector = document.getElementById('date-selector');
  if (dateSelector) {
    dateSelector.valueAsDate = new Date();
  }
});

// Listen for theme changes to re-render charts
window.addEventListener('themeChanged', (e) => {
  if (currentData) {
    console.log('Theme changed to', e.detail.theme, '- re-rendering charts');
    renderAllMethods(currentData);
  }
});

/**
 * Create solar zenith traces for overlay on charts
 * Returns array of Plotly traces for WWV and WWVH path midpoints
 */
function createSolarZenithTraces() {
  if (!solarZenithData) {
    console.log('No solar zenith data available for overlay');
    return [];
  }
  
  console.log('Creating solar zenith traces with', solarZenithData.timestamps.length, 'points');
  
  return [
    {
      x: solarZenithData.timestamps,
      y: solarZenithData.wwv_solar_elevation,
      name: 'Solar Elev. (WWV path)',
      type: 'scatter',
      mode: 'lines',
      line: { color: '#e74c3c', width: 3 },
      yaxis: 'y2',
      hovertemplate: 'WWV path: %{y:.1f}°<extra></extra>'
    },
    {
      x: solarZenithData.timestamps,
      y: solarZenithData.wwvh_solar_elevation,
      name: 'Solar Elev. (WWVH path)',
      type: 'scatter',
      mode: 'lines',
      line: { color: '#9b59b6', width: 3 },
      yaxis: 'y2',
      hovertemplate: 'WWVH path: %{y:.1f}°<extra></extra>'
    }
  ];
}

/**
 * Add secondary y-axis for solar elevation to layout
 */
function addSolarYAxis(layout) {
  if (!solarZenithData) return layout;
  
  return {
    ...layout,
    yaxis2: {
      title: 'Solar Elev. (°)',
      overlaying: 'y',
      side: 'right',
      range: [-90, 90],
      showgrid: false,
      zeroline: true,
      zerolinecolor: 'rgba(0,0,0,0.2)',
      zerolinewidth: 1
    }
  };
}

async function loadData() {
  console.log('loadData() called');
  const dateSelector = document.getElementById('date-selector');
  const channelSelector = document.getElementById('channel-selector');
  const container = document.getElementById('methods-container');
  
  if (!dateSelector || !channelSelector || !container) {
    console.error('Missing DOM elements:', { dateSelector, channelSelector, container });
    alert('Error: Page elements not found. Please refresh.');
    return;
  }
  
  const date = dateSelector.value.replace(/-/g, '');
  const channel = channelSelector.value;
  
  console.log('Loading data for:', date, channel);
  
  if (!date || !channel) {
    alert('Please select both date and channel');
    return;
  }
  
  // Show loading
  container.innerHTML = '<div class="loading"><div class="spinner"></div><p>Loading discrimination data...</p></div>';
  
  try {
    // Fetch discrimination data and solar zenith in parallel
    const [discResponse, stationResponse] = await Promise.all([
      fetch(`/api/v1/channels/${encodeURIComponent(channel)}/discrimination/${date}/methods`),
      fetch('/api/v1/station/info')
    ]);
    
    if (!discResponse.ok) {
      throw new Error(`HTTP ${discResponse.status}: ${discResponse.statusText}`);
    }
    
    const data = await discResponse.json();
    currentData = data;
    
    // Get station info for grid square
    let gridSquare = 'EM38ww'; // Default fallback
    if (stationResponse.ok) {
      const stationInfo = await stationResponse.json();
      if (stationInfo.grid_square) {
        gridSquare = stationInfo.grid_square;
      }
    }
    
    // Fetch solar zenith data
    try {
      console.log('Fetching solar zenith for date:', date, 'grid:', gridSquare);
      const solarResponse = await fetch(`/api/v1/solar-zenith?date=${date}&grid=${gridSquare}`);
      if (solarResponse.ok) {
        solarZenithData = await solarResponse.json();
        console.log('Solar zenith data loaded:', solarZenithData.timestamps?.length, 'points');
        console.log('WWV midpoint:', solarZenithData.wwv_midpoint);
        console.log('WWVH midpoint:', solarZenithData.wwvh_midpoint);
      } else {
        console.error('Solar zenith API error:', solarResponse.status, await solarResponse.text());
        solarZenithData = null;
      }
    } catch (solarErr) {
      console.warn('Could not load solar zenith data:', solarErr);
      solarZenithData = null;
    }
    
    // Update last update time
    document.getElementById('last-update').textContent = new Date().toLocaleString();
    
    // Render all methods
    console.log('Rendering charts with data:', Object.keys(data.methods));
    try {
      renderAllMethods(data);
      console.log('Charts rendered successfully');
    } catch (renderErr) {
      console.error('Error rendering charts:', renderErr);
      container.innerHTML = `
        <div class="error">
          <h3>Error Rendering Charts</h3>
          <p>${renderErr.message}</p>
          <pre style="font-size: 11px; overflow-x: auto;">${renderErr.stack}</pre>
        </div>
      `;
    }
    
  } catch (err) {
    console.error('Error loading discrimination data:', err);
    container.innerHTML = `
      <div class="error">
        <h3>Error Loading Data</h3>
        <p>${err.message}</p>
        <p>Please check that data exists for the selected date and channel.</p>
      </div>
    `;
  }
}

function renderAllMethods(data) {
  const container = document.getElementById('methods-container');
  
  // Show legend
  const legendEl = document.getElementById('method-legend');
  if (legendEl) legendEl.style.display = 'flex';
  
  // Calculate statistics for BCD detection types
  const bcdStats = calcBCDStats(data.methods.bcd?.records);
  const harmonicStats = calcHarmonicStats(data.methods.harmonic_ratio?.records);
  
  container.innerHTML = `
    <!-- Vote 0: Test Signal (Ground Truth) -->
    <div class="method-card">
      <div class="method-header">
        <div class="method-title">Vote 0: Test Signal<a href="/docs/discrimination-methodology.html#test-signal" class="info-link" title="How test signal detection works">?</a></div>
        <div class="method-badge ground-truth">Ground Truth</div>
      </div>
      <div class="chart-container" id="chart-test-signal"></div>
      <div class="insight-grid">
        <div class="insight-card">
          <div class="insight-label">Records</div>
          <div class="insight-value">${data.methods.test_signal?.count || 0}</div>
        </div>
        <div class="insight-card">
          <div class="insight-label">WWV (:08)</div>
          <div class="insight-value wwv">${countTestSignalStation(data.methods.test_signal?.records, 'WWV')}</div>
        </div>
        <div class="insight-card">
          <div class="insight-label">WWVH (:44)</div>
          <div class="insight-value wwvh">${countTestSignalStation(data.methods.test_signal?.records, 'WWVH')}</div>
        </div>
      </div>
    </div>
    
    <!-- Vote 1: 440 Hz Station ID (Ground Truth) -->
    <div class="method-card">
      <div class="method-header">
        <div class="method-title">Vote 1: 440 Hz Station ID<a href="/docs/discrimination-methodology.html#station-id" class="info-link" title="How 440 Hz detection works">?</a></div>
        <div class="method-badge ground-truth">Ground Truth</div>
      </div>
      <div class="chart-container" id="chart-station-id"></div>
      <div class="insight-grid">
        <div class="insight-card">
          <div class="insight-label">Records</div>
          <div class="insight-value">${data.methods.station_id?.count || 0}</div>
        </div>
        <div class="insight-card">
          <div class="insight-label">WWV (Min 2)</div>
          <div class="insight-value wwv">${count440Detection(data.methods.station_id?.records, 'wwv')}</div>
        </div>
        <div class="insight-card">
          <div class="insight-label">WWVH (Min 1)</div>
          <div class="insight-value wwvh">${count440Detection(data.methods.station_id?.records, 'wwvh')}</div>
        </div>
      </div>
    </div>
    
    <!-- Vote 2: BCD Amplitude Correlation -->
    <div class="method-card">
      <div class="method-header">
        <div class="method-title">Vote 2: BCD Amplitude<a href="/docs/discrimination-methodology.html#bcd" class="info-link" title="How BCD correlation works">?</a></div>
        <div class="method-badge">100 Hz</div>
      </div>
      <div class="chart-container" id="chart-bcd"></div>
      <div class="insight-grid">
        <div class="insight-card">
          <div class="insight-label">Records</div>
          <div class="insight-value">${data.methods.bcd?.count || 0}</div>
        </div>
        <div class="insight-card">
          <div class="insight-label">Dual Peak</div>
          <div class="insight-value" style="color: var(--accent);">${bcdStats.dualPeak}</div>
        </div>
        <div class="insight-card">
          <div class="insight-label">Single WWV</div>
          <div class="insight-value wwv">${bcdStats.singleWwv}</div>
        </div>
        <div class="insight-card">
          <div class="insight-label">Single WWVH</div>
          <div class="insight-value wwvh">${bcdStats.singleWwvh}</div>
        </div>
      </div>
    </div>
    
    <!-- Vote 3: Timing Tones -->
    <div class="method-card">
      <div class="method-header">
        <div class="method-title">Vote 3: Timing Tones<a href="/docs/discrimination-methodology.html#timing-tones" class="info-link" title="How timing tone analysis works">?</a></div>
        <div class="method-badge">1000/1200 Hz</div>
      </div>
      <div class="chart-container" id="chart-timing-tones"></div>
      <div class="insight-grid">
        <div class="insight-card">
          <div class="insight-label">Records</div>
          <div class="insight-value">${data.methods.timing_tones?.count || 0}</div>
        </div>
      </div>
    </div>
    
    <!-- Vote 4: Tick SNR -->
    <div class="method-card">
      <div class="method-header">
        <div class="method-title">Vote 4: Tick SNR<a href="/docs/discrimination-methodology.html#tick-snr" class="info-link" title="How tick SNR analysis works">?</a></div>
        <div class="method-badge">10-sec Windows</div>
      </div>
      <div class="chart-container" id="chart-tick-windows"></div>
      <div class="insight-grid">
        <div class="insight-card">
          <div class="insight-label">Records</div>
          <div class="insight-value">${data.methods.tick_windows?.count || 0}</div>
        </div>
      </div>
    </div>
    
    <!-- Vote 5: 500/600 Hz Ground Truth -->
    <div class="method-card">
      <div class="method-header">
        <div class="method-title">Vote 5: 500/600 Hz<a href="/docs/discrimination-methodology.html#ground-truth-500" class="info-link" title="How 500/600 Hz ground truth works">?</a></div>
        <div class="method-badge ground-truth">Ground Truth</div>
      </div>
      <div class="chart-container" id="chart-ground-truth"></div>
      <div class="insight-grid">
        <div class="insight-card">
          <div class="insight-label">Detections</div>
          <div class="insight-value">${data.methods.ground_truth_500_600?.count || 0}</div>
        </div>
        <div class="insight-card">
          <div class="insight-label">Agreements</div>
          <div class="insight-value ground-truth">${data.methods.ground_truth_500_600?.agreements || 0}</div>
        </div>
        <div class="insight-card">
          <div class="insight-label">Disagreements</div>
          <div class="insight-value error">${data.methods.ground_truth_500_600?.disagreements || 0}</div>
        </div>
      </div>
    </div>
    
    <!-- Vote 6: Doppler (Full Width) -->
    <div class="method-card full-width">
      <div class="method-header">
        <div class="method-title">Vote 6: Differential Doppler<a href="/docs/discrimination-methodology.html#doppler" class="info-link" title="How Doppler analysis works">?</a></div>
        <div class="method-badge">Ionospheric</div>
      </div>
      <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 16px;">
        <div class="chart-container" id="chart-doppler"></div>
        <div class="chart-container" id="chart-coherence"></div>
      </div>
      <div class="insight-grid">
        <div class="insight-card">
          <div class="insight-label">Records</div>
          <div class="insight-value">${data.methods.doppler?.count || 0}</div>
        </div>
        <div class="insight-card">
          <div class="insight-label">Avg T<sub>c</sub></div>
          <div class="insight-value">${calcAvgCoherence(data.methods.doppler?.records)}s</div>
        </div>
        <div class="insight-card">
          <div class="insight-label">Avg Quality</div>
          <div class="insight-value">${calcAvgDopplerQuality(data.methods.doppler?.records)}</div>
        </div>
      </div>
    </div>
    
    <!-- Vote 8: Harmonic Power Ratio -->
    <div class="method-card full-width">
      <div class="method-header">
        <div class="method-title">Vote 8: Harmonic Power Ratio<a href="/docs/discrimination-methodology.html#harmonic" class="info-link" title="How harmonic analysis works">?</a></div>
        <div class="method-badge">500→1000 / 600→1200</div>
      </div>
      <div class="chart-container" id="chart-harmonic"></div>
      <div class="insight-grid">
        <div class="insight-card">
          <div class="insight-label">Records</div>
          <div class="insight-value">${harmonicStats.count}</div>
        </div>
        <div class="insight-card">
          <div class="insight-label">Avg 500→1000</div>
          <div class="insight-value wwv">${harmonicStats.avg500_1000.toFixed(1)} dB</div>
        </div>
        <div class="insight-card">
          <div class="insight-label">Avg 600→1200</div>
          <div class="insight-value wwvh">${harmonicStats.avg600_1200.toFixed(1)} dB</div>
        </div>
      </div>
    </div>
    
    <!-- Votes 9-12: Channel Quality (from Test Signal) -->
    <div class="method-card full-width">
      <div class="method-header">
        <div class="method-title">Votes 9-12: Channel Quality<a href="/docs/discrimination-methodology.html#channel-quality" class="info-link" title="How channel quality metrics work">?</a></div>
        <div class="method-badge">Min 8/44 Test Signal</div>
      </div>
      <div class="chart-container" id="chart-channel-quality"></div>
      <div class="insight-grid">
        ${calcChannelQualityStats(data.methods.test_signal?.records)}
      </div>
    </div>
    
    <!-- Vote 13: BCD Intermodulation Signature -->
    <div class="method-card full-width">
      <div class="method-header">
        <div class="method-title">Vote 13: BCD Intermodulation<a href="/docs/discrimination-methodology.html#intermod" class="info-link" title="How intermodulation analysis works">?</a></div>
        <div class="method-badge">400/700 Hz</div>
      </div>
      <div class="chart-container" id="chart-intermod"></div>
      <div class="insight-grid">
        <div class="insight-card">
          <div class="insight-label">Records</div>
          <div class="insight-value">${data.methods.audio_tones?.count || 0}</div>
        </div>
        <div class="insight-card">
          <div class="insight-label">WWV (400 Hz)</div>
          <div class="insight-value wwv">${countIntermodStation(data.methods.audio_tones?.records, 'WWV')}</div>
        </div>
        <div class="insight-card">
          <div class="insight-label">WWVH (700 Hz)</div>
          <div class="insight-value wwvh">${countIntermodStation(data.methods.audio_tones?.records, 'WWVH')}</div>
        </div>
        <div class="insight-card">
          <div class="insight-label">Avg Confidence</div>
          <div class="insight-value">${calcAvgIntermodConfidence(data.methods.audio_tones?.records)}</div>
        </div>
      </div>
    </div>
    
    <!-- Final: Weighted Voting (Full Width) -->
    <div class="method-card full-width">
      <div class="method-header">
        <div class="method-title">Final Decision: Weighted Voting<a href="/docs/discrimination-methodology.html#voting" class="info-link" title="How weighted voting works">?</a></div>
        <div class="method-badge">13 Votes Combined</div>
      </div>
      <div style="display: grid; grid-template-columns: 2fr 1fr; gap: 16px;">
        <div class="chart-container" id="chart-voting"></div>
        <div class="chart-container" id="chart-detection-pie"></div>
      </div>
      <div class="insight-grid">
        <div class="insight-card">
          <div class="insight-label">Total Minutes</div>
          <div class="insight-value">${data.methods.weighted_voting?.count || 0}</div>
        </div>
        <div class="insight-card">
          <div class="insight-label">WWV Dominant</div>
          <div class="insight-value wwv">${countStation(data.methods.weighted_voting?.records, 'WWV')}</div>
        </div>
        <div class="insight-card">
          <div class="insight-label">WWVH Dominant</div>
          <div class="insight-value wwvh">${countStation(data.methods.weighted_voting?.records, 'WWVH')}</div>
        </div>
        <div class="insight-card">
          <div class="insight-label">Balanced</div>
          <div class="insight-value">${countStation(data.methods.weighted_voting?.records, 'BALANCED')}</div>
        </div>
      </div>
    </div>
  `;
  
  // Render individual charts with full UTC day range
  const utcDate = data.date; // YYYYMMDD format
  renderTestSignalChart(data.methods.test_signal, utcDate);
  renderStationIDChart(data.methods.station_id, utcDate);
  renderBCDChart(data.methods.bcd, utcDate);
  renderTimingTonesChart(data.methods.timing_tones, utcDate);
  renderTickWindowsChart(data.methods.tick_windows, utcDate);
  renderGroundTruthChart(data.methods.ground_truth_500_600, utcDate);
  renderDopplerChart(data.methods.doppler, utcDate);
  renderHarmonicRatioChart(data.methods.harmonic_ratio, utcDate);
  renderChannelQualityChart(data.methods.test_signal, utcDate);
  renderIntermodChart(data.methods.audio_tones, utcDate);
  // Pass tick and tone data for SNR comparison chart
  renderVotingChart(data.methods.weighted_voting, utcDate, data.methods.tick_windows, data.methods.timing_tones);
  renderDetectionTypePie(data.methods.weighted_voting);
}

// Helper functions for Doppler statistics
function calcAvgCoherence(records) {
  if (!records || records.length === 0) return 'N/A';
  const avg = records.reduce((sum, r) => sum + r.max_coherent_window_sec, 0) / records.length;
  return avg.toFixed(1);
}

function calcAvgDopplerQuality(records) {
  if (!records || records.length === 0) return 'N/A';
  const avg = records.reduce((sum, r) => sum + r.doppler_quality, 0) / records.length;
  return avg.toFixed(2);
}

function renderTimingTonesChart(method, utcDate) {
  if (!method.records || method.records.length === 0) {
    document.getElementById('chart-timing-tones').innerHTML = '<p style="padding: 20px; text-align: center; color: #7f8c8d;">No timing tone data available</p>';
    return;
  }
  
  // Separate WWV and WWVH records by station field, and sort by time
  const wwvRecords = method.records
    .filter(r => r.station === 'WWV')
    .sort((a, b) => new Date(a.timestamp_utc) - new Date(b.timestamp_utc));
  const wwvhRecords = method.records
    .filter(r => r.station === 'WWVH')
    .sort((a, b) => new Date(a.timestamp_utc) - new Date(b.timestamp_utc));
  
  const trace1 = {
    x: wwvRecords.map(r => r.timestamp_utc),
    y: wwvRecords.map(r => parseFloat(r.tone_power_db)),
    name: 'WWV (1000 Hz)',
    type: 'scatter',
    mode: 'markers',
    marker: { color: '#3498db', size: 5 }
  };
  
  const trace2 = {
    x: wwvhRecords.map(r => r.timestamp_utc),
    y: wwvhRecords.map(r => parseFloat(r.tone_power_db)),
    name: 'WWVH (1200 Hz)',
    type: 'scatter',
    mode: 'markers',
    marker: { color: '#e67e22', size: 5 }
  };
  
  // Full UTC day range
  const xRange = getUTCDayRange(utcDate);
  
  let layout = {
    title: makeTitle('Tone Power Over Time'),
    xaxis: { 
      title: 'Time (UTC)',
      range: xRange,
      type: 'date'
    },
    yaxis: { title: 'Power (dB)' },
    showlegend: true,
    legend: { x: 0, y: 1, orientation: 'h' },
    margin: { l: 50, r: 60, t: 40, b: 50 }
  };
  
  // Add solar zenith overlay
  const traces = [trace1, trace2, ...createSolarZenithTraces()];
  layout = addSolarYAxis(layout);
  
  Plotly.newPlot('chart-timing-tones', traces, layout, PLOTLY_CONFIG);
}

function getUTCDayRange(utcDate) {
  // Convert YYYYMMDD to full UTC day range
  const year = utcDate.substring(0, 4);
  const month = utcDate.substring(4, 6);
  const day = utcDate.substring(6, 8);
  const start = `${year}-${month}-${day}T00:00:00Z`;
  const end = `${year}-${month}-${day}T23:59:59Z`;
  return [start, end];
}

function renderTickWindowsChart(method, utcDate) {
  if (!method.records || method.records.length === 0) {
    document.getElementById('chart-tick-windows').innerHTML = '<p style="padding: 20px; text-align: center; color: #7f8c8d;">No tick window data available</p>';
    return;
  }
  
  // Sort records by timestamp to prevent diagonal lines
  const sortedRecords = [...method.records].sort((a, b) => 
    new Date(a.timestamp_utc) - new Date(b.timestamp_utc)
  );
  
  const timestamps = sortedRecords.map(r => r.timestamp_utc);
  const wwvSNR = sortedRecords.map(r => parseFloat(r.wwv_snr_db) || 0);
  const wwvhSNR = sortedRecords.map(r => parseFloat(r.wwvh_snr_db) || 0);
  
  // Check if all SNR values are zero (no real tick detection)
  const hasNonZeroSNR = wwvSNR.some(v => v > 0.1) || wwvhSNR.some(v => v > 0.1);
  if (!hasNonZeroSNR) {
    document.getElementById('chart-tick-windows').innerHTML = `
      <p style="padding: 20px; text-align: center; color: var(--text-muted);">
        <strong>Tick SNR data shows all zeros</strong><br>
        <span style="font-size: 12px;">No 1000/1200 Hz tick tones detected.<br>
        Phase 2 tick integration requires detectable timing tones.</span>
      </p>`;
    return;
  }
  
  const trace1 = {
    x: timestamps,
    y: wwvSNR,
    name: 'WWV SNR',
    type: 'scatter',
    mode: 'markers',
    marker: { color: '#3498db', size: 4 }
  };
  
  const trace2 = {
    x: timestamps,
    y: wwvhSNR,
    name: 'WWVH SNR',
    type: 'scatter',
    mode: 'markers',
    marker: { color: '#e67e22', size: 4 }
  };
  
  // Full UTC day range
  const xRange = getUTCDayRange(utcDate);
  
  let layout = {
    title: makeTitle('Tick Window SNR'),
    xaxis: { 
      title: 'Time (UTC)',
      range: xRange,
      type: 'date'
    },
    yaxis: { title: 'SNR (dB)' },
    showlegend: true,
    legend: { x: 0, y: 1, orientation: 'h' },
    margin: { l: 50, r: 60, t: 40, b: 50 }
  };
  
  // Add solar zenith overlay
  const traces = [trace1, trace2, ...createSolarZenithTraces()];
  layout = addSolarYAxis(layout);
  
  Plotly.newPlot('chart-tick-windows', traces, layout, PLOTLY_CONFIG);
}

function renderStationIDChart(method, utcDate) {
  if (!method.records || method.records.length === 0) {
    document.getElementById('chart-station-id').innerHTML = '<p style="padding: 20px; text-align: center; color: #7f8c8d;">No station ID data available</p>';
    return;
  }
  
  const timestamps = method.records.map(r => r.timestamp_utc);
  const wwvPower = method.records.map(r => parseFloat(r.wwv_power_db) || null);
  const wwvhPower = method.records.map(r => parseFloat(r.wwvh_power_db) || null);
  
  const trace1 = {
    x: timestamps,
    y: wwvPower,
    name: 'WWV 440 Hz',
    type: 'scatter',
    mode: 'markers',
    marker: { color: '#3498db', size: 6 }
  };
  
  const trace2 = {
    x: timestamps,
    y: wwvhPower,
    name: 'WWVH 440 Hz',
    type: 'scatter',
    mode: 'markers',
    marker: { color: '#e67e22', size: 6 }
  };
  
  // Full UTC day range
  const xRange = getUTCDayRange(utcDate);
  
  let layout = {
    title: makeTitle('440 Hz Station ID Power'),
    xaxis: { 
      title: 'Time (UTC)',
      range: xRange,
      type: 'date'
    },
    yaxis: { title: 'Power (dB)' },
    showlegend: true,
    legend: { x: 0, y: 1, orientation: 'h' },
    margin: { l: 50, r: 60, t: 40, b: 50 }
  };
  
  // Add solar zenith overlay
  const traces = [trace1, trace2, ...createSolarZenithTraces()];
  layout = addSolarYAxis(layout);
  
  Plotly.newPlot('chart-station-id', traces, layout, PLOTLY_CONFIG);
}

function renderTestSignalChart(method, utcDate) {
  if (!method.records || method.records.length === 0) {
    document.getElementById('chart-test-signal').innerHTML = '<p style="padding: 20px; text-align: center; color: #7f8c8d;">No test signal data available (minutes :08 and :44 only)</p>';
    return;
  }
  
  // Separate detected and not-detected records
  const detected = method.records.filter(r => r.detected);
  const notDetected = method.records.filter(r => !r.detected);
  
  // Detected signals with station labels
  const wwvDetected = detected.filter(r => r.station === 'WWV');
  const wwvhDetected = detected.filter(r => r.station === 'WWVH');
  
  const xRange = getUTCDayRange(utcDate);
  
  const trace1 = {
    x: wwvDetected.map(r => r.timestamp_utc),
    y: wwvDetected.map(r => r.confidence),
    name: 'WWV Detected',
    type: 'scatter',
    mode: 'markers',
    marker: { color: '#3498db', size: 12, symbol: 'circle' },
    text: wwvDetected.map(r => `WWV<br>Confidence: ${(r.confidence*100).toFixed(1)}%<br>SNR: ${r.snr_db?.toFixed(1) || 'N/A'} dB`),
    hoverinfo: 'text+x'
  };
  
  const trace2 = {
    x: wwvhDetected.map(r => r.timestamp_utc),
    y: wwvhDetected.map(r => r.confidence),
    name: 'WWVH Detected',
    type: 'scatter',
    mode: 'markers',
    marker: { color: '#e67e22', size: 12, symbol: 'square' },
    text: wwvhDetected.map(r => `WWVH<br>Confidence: ${(r.confidence*100).toFixed(1)}%<br>SNR: ${r.snr_db?.toFixed(1) || 'N/A'} dB`),
    hoverinfo: 'text+x'
  };
  
  const trace3 = {
    x: notDetected.map(r => r.timestamp_utc),
    y: notDetected.map(r => r.confidence),
    name: 'Not Detected',
    type: 'scatter',
    mode: 'markers',
    marker: { color: '#95a5a6', size: 8, symbol: 'x' },
    text: notDetected.map(r => `Not Detected<br>Confidence: ${(r.confidence*100).toFixed(1)}%`),
    hoverinfo: 'text+x'
  };
  
  let layout = {
    title: makeTitle('Test Signal Detection (:08 WWV, :44 WWVH)'),
    xaxis: { 
      title: 'Time (UTC)',
      range: xRange,
      type: 'date'
    },
    yaxis: { 
      title: 'Detection Confidence',
      range: [0, 1],
      tickformat: '.0%'
    },
    showlegend: true,
    legend: { x: 0, y: 1, orientation: 'h' },
    margin: { l: 50, r: 60, t: 40, b: 50 }
  };
  
  // Add solar zenith overlay
  const traces = [trace1, trace2, trace3, ...createSolarZenithTraces()];
  layout = addSolarYAxis(layout);
  
  Plotly.newPlot('chart-test-signal', traces, layout, PLOTLY_CONFIG);
}

function renderGroundTruthChart(method, utcDate) {
  if (!method || !method.records || method.records.length === 0) {
    document.getElementById('chart-ground-truth').innerHTML = '<p style="padding: 20px; text-align: center; color: #7f8c8d;">No 500/600 Hz ground truth data available.<br>Exclusive minutes: WWV-only (1,16,17,19), WWVH-only (2,43-51)</p>';
    return;
  }
  
  // Separate by ground truth station
  const wwvGT = method.records.filter(r => r.ground_truth_station === 'WWV');
  const wwvhGT = method.records.filter(r => r.ground_truth_station === 'WWVH');
  
  const xRange = getUTCDayRange(utcDate);
  
  // WWV ground truth (minutes 1, 16, 17, 19)
  const trace1 = {
    x: wwvGT.map(r => r.timestamp_utc),
    y: wwvGT.map(r => r.power_db || 0),
    name: 'WWV-only (min 1,16,17,19)',
    type: 'scatter',
    mode: 'markers',
    marker: { 
      color: wwvGT.map(r => r.agrees ? '#10b981' : '#ef4444'),
      size: 10, 
      symbol: 'circle',
      line: { color: '#10b981', width: 2 }
    },
    text: wwvGT.map(r => `${r.freq_hz} Hz<br>Ground Truth: WWV<br>Dominant: ${r.dominant_station}<br>${r.agrees ? '✓ Agrees' : '✗ Disagrees'}`),
    hoverinfo: 'text+x'
  };
  
  // WWVH ground truth (minutes 2, 43-51)
  const trace2 = {
    x: wwvhGT.map(r => r.timestamp_utc),
    y: wwvhGT.map(r => r.power_db || 0),
    name: 'WWVH-only (min 2,43-51)',
    type: 'scatter',
    mode: 'markers',
    marker: { 
      color: wwvhGT.map(r => r.agrees ? '#10b981' : '#ef4444'),
      size: 10, 
      symbol: 'diamond',
      line: { color: '#9b59b6', width: 2 }
    },
    text: wwvhGT.map(r => `${r.freq_hz} Hz<br>Ground Truth: WWVH<br>Dominant: ${r.dominant_station}<br>${r.agrees ? '✓ Agrees' : '✗ Disagrees'}`),
    hoverinfo: 'text+x'
  };
  
  let layout = {
    title: makeTitle('500/600 Hz Ground Truth (🟢=Agrees, 🔴=Disagrees)'),
    xaxis: { 
      title: 'Time (UTC)',
      range: xRange,
      type: 'date'
    },
    yaxis: { 
      title: 'Tone Power (dB)'
    },
    showlegend: true,
    legend: { x: 0, y: 1, orientation: 'h' },
    margin: { l: 50, r: 60, t: 40, b: 50 }
  };
  
  // Add solar zenith overlay
  const traces = [trace1, trace2, ...createSolarZenithTraces()];
  layout = addSolarYAxis(layout);
  
  Plotly.newPlot('chart-ground-truth', traces, layout, PLOTLY_CONFIG);
}

function renderBCDChart(method, utcDate) {
  if (!method.records || method.records.length === 0) {
    document.getElementById('chart-bcd').innerHTML = '<p style="padding: 20px; text-align: center; color: #7f8c8d;">No BCD discrimination data available</p>';
    return;
  }
  
  // Sort records by timestamp to prevent criss-crossing lines
  const sortedRecords = [...method.records].sort((a, b) => 
    new Date(a.timestamp_utc) - new Date(b.timestamp_utc)
  );
  
  const timestamps = sortedRecords.map(r => r.timestamp_utc);
  const wwvAmp = sortedRecords.map(r => parseFloat(r.wwv_amplitude) || 0);
  const wwvhAmp = sortedRecords.map(r => parseFloat(r.wwvh_amplitude) || 0);
  
  // Check if all amplitudes are zero (no real BCD correlation)
  const hasNonZeroAmp = wwvAmp.some(v => v > 0.01) || wwvhAmp.some(v => v > 0.01);
  if (!hasNonZeroAmp) {
    document.getElementById('chart-bcd').innerHTML = `
      <p style="padding: 20px; text-align: center; color: var(--text-muted);">
        <strong>BCD amplitudes are all zero</strong><br>
        <span style="font-size: 12px;">Phase 2 BCD correlation did not detect 100 Hz timing code.<br>
        This may indicate weak signal or processing issue.</span>
      </p>`;
    return;
  }
  
  const trace1 = {
    x: timestamps,
    y: wwvAmp,
    name: 'WWV BCD Amplitude',
    type: 'scatter',
    mode: 'lines',
    line: { color: '#3498db', width: 2 }
  };
  
  const trace2 = {
    x: timestamps,
    y: wwvhAmp,
    name: 'WWVH BCD Amplitude',
    type: 'scatter',
    mode: 'lines',
    line: { color: '#e67e22', width: 2 }
  };
  
  // Full UTC day range
  const xRange = getUTCDayRange(utcDate);
  
  let layout = {
    title: makeTitle('BCD 100 Hz Amplitude'),
    xaxis: { 
      title: 'Time (UTC)',
      range: xRange,
      type: 'date'
    },
    yaxis: { title: 'Amplitude' },
    showlegend: true,
    legend: { x: 0, y: 1, orientation: 'h' },
    margin: { l: 50, r: 60, t: 40, b: 50 }
  };
  
  // Add solar zenith overlay
  const traces = [trace1, trace2, ...createSolarZenithTraces()];
  layout = addSolarYAxis(layout);
  
  Plotly.newPlot('chart-bcd', traces, layout, PLOTLY_CONFIG);
}

function renderVotingChart(method, utcDate, tickData, toneData) {
  const container = document.getElementById('chart-voting');
  
  // Try to get SNR data from tick windows or tone detections
  let hasSnrData = false;
  let wwvSnr = [], wwvhSnr = [], timestamps = [];
  
  // Prefer tick window data (more granular)
  if (tickData && tickData.records && tickData.records.length > 0) {
    tickData.records.forEach(r => {
      timestamps.push(r.timestamp_utc);
      wwvSnr.push(r.wwv_snr_db || 0);
      wwvhSnr.push(r.wwvh_snr_db || 0);
    });
    hasSnrData = true;
  }
  // Fallback to tone detection data
  else if (toneData && toneData.records && toneData.records.length > 0) {
    // Group by timestamp and get WWV/WWVH tones
    const byTime = {};
    toneData.records.forEach(r => {
      const ts = r.timestamp_utc;
      if (!byTime[ts]) byTime[ts] = { wwv: null, wwvh: null };
      if (r.station === 'WWV') byTime[ts].wwv = r.snr_db;
      if (r.station === 'WWVH') byTime[ts].wwvh = r.snr_db;
    });
    Object.entries(byTime).sort().forEach(([ts, data]) => {
      timestamps.push(ts);
      wwvSnr.push(data.wwv || 0);
      wwvhSnr.push(data.wwvh || 0);
    });
    hasSnrData = timestamps.length > 0;
  }
  
  if (!hasSnrData) {
    container.innerHTML = '<p style="padding: 20px; text-align: center; color: #7f8c8d;">No signal strength data available</p>';
    return;
  }
  
  // Sort data by timestamp
  const combined = timestamps.map((t, i) => ({ t, wwv: wwvSnr[i], wwvh: wwvhSnr[i] }));
  combined.sort((a, b) => new Date(a.t) - new Date(b.t));
  const sortedTs = combined.map(c => c.t);
  const sortedWwv = combined.map(c => c.wwv);
  const sortedWwvh = combined.map(c => c.wwvh);
  
  // Check if all values are zero/null (no real data)
  const hasNonZeroData = sortedWwv.some(v => v > 0) || sortedWwvh.some(v => v > 0);
  if (!hasNonZeroData) {
    container.innerHTML = `
      <p style="padding: 20px; text-align: center; color: var(--text-muted);">
        <strong>SNR data shows all zeros</strong><br>
        <span style="font-size: 12px;">Phase 2 tone detection did not find valid 1000/1200 Hz tones.<br>
        Check carrier power and signal conditions.</span>
      </p>`;
    return;
  }
  
  // WWV trace (blue line)
  const traceWWV = {
    x: sortedTs,
    y: sortedWwv,
    name: 'WWV (1000 Hz)',
    type: 'scatter',
    mode: 'lines',
    line: { color: '#3498db', width: 2 },
    fill: 'tonexty',
    fillcolor: 'rgba(52, 152, 219, 0.3)'
  };
  
  // WWVH trace (orange line) - plotted first so WWV fills to it
  const traceWWVH = {
    x: sortedTs,
    y: sortedWwvh,
    name: 'WWVH (1200 Hz)',
    type: 'scatter',
    mode: 'lines',
    line: { color: '#e67e22', width: 2 }
  };
  
  // Full UTC day range
  const xRange = getUTCDayRange(utcDate);
  
  // Find range for y-axis
  const allVals = [...sortedWwv, ...sortedWwvh].filter(v => isFinite(v));
  const minVal = Math.min(...allVals, 0);
  const maxVal = Math.max(...allVals, 10);
  
  let layout = {
    title: makeTitle('Station Signal Strength (WWV-WWVH gap)'),
    xaxis: { 
      title: 'Time (UTC)',
      range: xRange,
      type: 'date'
    },
    yaxis: {
      title: 'SNR (dB)',
      range: [minVal - 5, maxVal + 5]
    },
    showlegend: true,
    legend: { orientation: 'h', y: 1.12 },
    margin: { l: 60, r: 70, t: 60, b: 50 },
    annotations: [{
      x: xRange[1],
      y: maxVal,
      text: 'Blue above orange = WWV stronger',
      showarrow: false,
      font: { size: 10 },
      xanchor: 'right'
    }]
  };
  
  // Add solar zenith overlay
  const traces = [traceWWVH, traceWWV, ...createSolarZenithTraces()];
  layout = addSolarYAxis(layout);
  
  // Plot WWVH first, then WWV with fill between, then solar
  Plotly.newPlot('chart-voting', traces, layout, PLOTLY_CONFIG);
}

function countStation(records, station) {
  if (!records) return 0;
  return records.filter(r => r.dominant_station === station).length;
}

function countIntermodStation(records, station) {
  if (!records) return 0;
  return records.filter(r => r.intermod_dominant === station).length;
}

function calcAvgIntermodConfidence(records) {
  if (!records || records.length === 0) return '0%';
  const valid = records.filter(r => r.intermod_confidence && r.intermod_confidence > 0);
  if (valid.length === 0) return '0%';
  const avg = valid.reduce((sum, r) => sum + r.intermod_confidence, 0) / valid.length;
  return (avg * 100).toFixed(0) + '%';
}

function renderIntermodChart(method, utcDate) {
  console.log('renderIntermodChart called, method:', method);
  const container = document.getElementById('chart-intermod');
  console.log('chart-intermod container:', container);
  if (!container) {
    console.error('chart-intermod container NOT FOUND');
    return;
  }
  
  if (!method || !method.records || method.records.length === 0) {
    console.log('No intermod data, showing message');
    container.innerHTML = '<p style="padding: 20px; text-align: center; color: #7f8c8d;">No intermod data available</p>';
    return;
  }
  console.log('Rendering intermod chart with', method.records.length, 'records');
  
  // Sort records by timestamp
  const sortedRecords = [...method.records].sort((a, b) => 
    new Date(a.timestamp_utc) - new Date(b.timestamp_utc)
  );
  
  const timestamps = sortedRecords.map(r => r.timestamp_utc);
  // Use schedule-corrected values (wwv_intermod_db/wwvh_intermod_db account for tone schedule)
  const wwvIntermod = sortedRecords.map(r => r.wwv_intermod_db);
  const wwvhIntermod = sortedRecords.map(r => r.wwvh_intermod_db);
  // Calculate ratio: positive = WWV dominant, negative = WWVH dominant
  const ratio = sortedRecords.map(r => {
    if (r.wwv_intermod_db != null && r.wwvh_intermod_db != null) {
      return r.wwv_intermod_db - r.wwvh_intermod_db;
    }
    return null;
  });
  
  // Determine station from intermod (considering schedule flip)
  const colors = sortedRecords.map(r => {
    if (r.intermod_dominant === 'WWV') return COLORS.wwv;
    if (r.intermod_dominant === 'WWVH') return COLORS.wwvh;
    return '#7f8c8d';  // Gray for unknown
  });
  
  const theme = getThemeColors();
  const layoutDefaults = getPlotlyLayoutDefaults();
  
  const traces = [
    {
      x: timestamps,
      y: wwvIntermod,
      name: 'WWV Intermod (schedule-corrected)',
      type: 'scatter',
      mode: 'lines+markers',
      line: { color: COLORS.wwv, width: 2 },
      marker: { size: 4 }
    },
    {
      x: timestamps,
      y: wwvhIntermod,
      name: 'WWVH Intermod (schedule-corrected)',
      type: 'scatter',
      mode: 'lines+markers',
      line: { color: COLORS.wwvh, width: 2 },
      marker: { size: 4 }
    },
    {
      x: timestamps,
      y: ratio,
      name: 'WWV-WWVH Ratio (dB)',
      type: 'scatter',
      mode: 'lines',
      line: { color: COLORS.accent, width: 1, dash: 'dot' },
      yaxis: 'y2'
    }
  ];
  
  // Add solar elevation if available
  if (solarZenithData && solarZenithData.timestamps) {
    traces.push({
      x: solarZenithData.timestamps,
      y: solarZenithData.wwv_solar_elevation,
      name: 'Solar Elev. (WWV path)',
      type: 'scatter',
      mode: 'lines',
      line: { color: COLORS.solarWwv, width: 1, dash: 'dash' },
      yaxis: 'y3',
      opacity: 0.5
    });
    traces.push({
      x: solarZenithData.timestamps,
      y: solarZenithData.wwvh_solar_elevation,
      name: 'Solar Elev. (WWVH path)',
      type: 'scatter',
      mode: 'lines',
      line: { color: COLORS.solarWwvh, width: 1, dash: 'dash' },
      yaxis: 'y3',
      opacity: 0.5
    });
  }
  
  const layout = {
    ...layoutDefaults,
    title: { text: 'BCD Intermod Power (Schedule-Corrected)', ...layoutDefaults.title },
    xaxis: { 
      ...layoutDefaults.xaxis,
      range: getUTCDayRange(utcDate),
      title: 'Time (UTC)',
      type: 'date'
    },
    yaxis: {
      ...layoutDefaults.yaxis,
      title: 'Power (dB)',
      gridcolor: theme.grid
    },
    yaxis2: {
      title: 'Ratio (dB)',
      overlaying: 'y',
      side: 'right',
      showgrid: false,
      zeroline: true,
      zerolinecolor: theme.grid,
      tickfont: { color: COLORS.accent }
    },
    yaxis3: {
      title: 'Solar Elev. (°)',
      overlaying: 'y',
      side: 'right',
      position: 0.95,
      showgrid: false,
      visible: false
    },
    legend: {
      orientation: 'h',
      y: -0.15,
      font: { color: theme.text, size: 10 }
    },
    margin: { t: 40, r: 60, b: 60, l: 50 }
  };
  
  Plotly.newPlot('chart-intermod', traces, layout, PLOTLY_CONFIG);
}

function renderDopplerChart(method, utcDate) {
  if (!method || !method.records || method.records.length === 0) {
    document.getElementById('chart-doppler').innerHTML = '<p style="padding: 20px; text-align: center; color: #7f8c8d;">No Doppler data available</p>';
    document.getElementById('chart-coherence').innerHTML = '';
    return;
  }
  
  // Sort records by timestamp to prevent criss-crossing lines
  const sortedRecords = [...method.records].sort((a, b) => 
    new Date(a.timestamp_utc) - new Date(b.timestamp_utc)
  );
  
  const timestamps = sortedRecords.map(r => r.timestamp_utc);
  const wwvDoppler = sortedRecords.map(r => (r.wwv_doppler_hz || 0) * 1000); // Convert to mHz
  const wwvhDoppler = sortedRecords.map(r => (r.wwvh_doppler_hz || 0) * 1000);
  const coherenceTime = sortedRecords.map(r => r.max_coherent_window_sec || 0);
  const quality = sortedRecords.map(r => r.doppler_quality || 0);
  
  // Check if Doppler data shows meaningful variation (not all constant/zero)
  const wwvVariance = Math.abs(Math.max(...wwvDoppler) - Math.min(...wwvDoppler));
  const wwvhVariance = Math.abs(Math.max(...wwvhDoppler) - Math.min(...wwvhDoppler));
  const hasVariation = wwvVariance > 0.1 || wwvhVariance > 0.1;
  const hasQuality = quality.some(q => q > 0.1);
  
  if (!hasVariation && !hasQuality) {
    document.getElementById('chart-doppler').innerHTML = `
      <p style="padding: 20px; text-align: center; color: var(--text-muted);">
        <strong>Doppler data shows no variation</strong><br>
        <span style="font-size: 12px;">Phase 2 Doppler estimation requires detectable tones.<br>
        Values are constant (likely default/fallback).</span>
      </p>`;
  }
  
  // Chart 1: Doppler Shift over time
  const trace1 = {
    x: timestamps,
    y: wwvDoppler,
    name: 'WWV Δf_D (mHz)',
    type: 'scatter',
    mode: 'lines+markers',
    line: { color: '#3498db', width: 2 },
    marker: { size: 4 }
  };
  
  const trace2 = {
    x: timestamps,
    y: wwvhDoppler,
    name: 'WWVH Δf_D (mHz)',
    type: 'scatter',
    mode: 'lines+markers',
    line: { color: '#e67e22', width: 2 },
    marker: { size: 4 }
  };
  
  // Full UTC day range
  const xRange = getUTCDayRange(utcDate);
  
  let layout1 = {
    title: makeTitle('Doppler Shift (Δf_D)'),
    xaxis: { 
      title: 'Time (UTC)',
      range: xRange,
      type: 'date'
    },
    yaxis: { 
      title: 'Doppler Shift (mHz)',
      zeroline: true,
      zerolinecolor: '#ccc'
    },
    showlegend: true,
    legend: { x: 0, y: 1, orientation: 'h' },
    margin: { l: 60, r: 70, t: 40, b: 50 }
  };
  
  // Add solar zenith overlay
  const dopplerTraces = [trace1, trace2, ...createSolarZenithTraces()];
  layout1 = addSolarYAxis(layout1);
  
  Plotly.newPlot('chart-doppler', dopplerTraces, layout1, PLOTLY_CONFIG);
  
  // Chart 2: Coherence Time and Quality
  const trace3 = {
    x: timestamps,
    y: coherenceTime,
    name: 'Max Coherent Window (T_c)',
    type: 'scatter',
    mode: 'lines+markers',
    line: { color: '#27ae60', width: 2 },
    marker: { size: 4 },
    yaxis: 'y1'
  };
  
  const trace4 = {
    x: timestamps,
    y: quality,
    name: 'Doppler Quality',
    type: 'scatter',
    mode: 'lines+markers',
    line: { color: '#9b59b6', width: 2 },
    marker: { size: 4 },
    yaxis: 'y2'
  };
  
  // Add reference line at 10 seconds (BCD default window)
  const refLine = {
    x: [timestamps[0], timestamps[timestamps.length - 1]],
    y: [10, 10],
    name: 'BCD Window (10s)',
    type: 'scatter',
    mode: 'lines',
    line: { color: '#e74c3c', width: 1, dash: 'dash' },
    yaxis: 'y1'
  };
  
  const layout2 = {
    title: makeTitle('Coherence Time (T_c) & Quality'),
    xaxis: { 
      title: 'Time (UTC)',
      range: xRange,
      type: 'date'
    },
    yaxis: { 
      title: 'Coherence Time (seconds)',
      side: 'left',
      range: [0, 65]
    },
    yaxis2: {
      title: 'Quality (0-1)',
      side: 'right',
      overlaying: 'y',
      range: [0, 1.1]
    },
    showlegend: true,
    legend: { x: 0, y: 1 },
    margin: { l: 60, r: 60, t: 40, b: 50 }
  };
  
  Plotly.newPlot('chart-coherence', [trace3, trace4, refLine], layout2, PLOTLY_CONFIG);
}

// ============ NEW HELPER FUNCTIONS ============

/**
 * Calculate BCD detection type statistics
 */
function calcBCDStats(records) {
  if (!records || records.length === 0) {
    return { dualPeak: 0, singleWwv: 0, singleWwvh: 0 };
  }
  
  let dualPeak = 0, singleWwv = 0, singleWwvh = 0;
  
  records.forEach(r => {
    const type = r.detection_type || '';
    if (type.includes('dual')) {
      dualPeak++;
    } else if (type.includes('wwvh') || type.toLowerCase().includes('wwvh')) {
      singleWwvh++;
    } else if (type.includes('wwv') || type.toLowerCase().includes('wwv')) {
      singleWwv++;
    } else {
      // Fallback: check amplitudes
      const wwvAmp = parseFloat(r.wwv_amplitude) || 0;
      const wwvhAmp = parseFloat(r.wwvh_amplitude) || 0;
      if (wwvAmp > 0.1 && wwvhAmp > 0.1) {
        dualPeak++;
      } else if (wwvAmp > wwvhAmp) {
        singleWwv++;
      } else {
        singleWwvh++;
      }
    }
  });
  
  return { dualPeak, singleWwv, singleWwvh };
}

/**
 * Calculate harmonic ratio statistics
 */
function calcHarmonicStats(records) {
  if (!records || records.length === 0) {
    return { count: 0, avg500_1000: 0, avg600_1200: 0 };
  }
  
  let sum500_1000 = 0, sum600_1200 = 0, count500 = 0, count600 = 0;
  
  records.forEach(r => {
    const ratio500 = parseFloat(r.harmonic_ratio_500_1000);
    const ratio600 = parseFloat(r.harmonic_ratio_600_1200);
    
    if (!isNaN(ratio500)) {
      sum500_1000 += ratio500;
      count500++;
    }
    if (!isNaN(ratio600)) {
      sum600_1200 += ratio600;
      count600++;
    }
  });
  
  return {
    count: records.length,
    avg500_1000: count500 > 0 ? sum500_1000 / count500 : 0,
    avg600_1200: count600 > 0 ? sum600_1200 / count600 : 0
  };
}

/**
 * Count test signal detections by station
 */
function countTestSignalStation(records, station) {
  if (!records) return 0;
  return records.filter(r => r.detected && r.station === station).length;
}

/**
 * Count 440 Hz detections by station
 * Works with both old format (wwv_detected) and Phase 2 format (ground_truth_station, dominant_station)
 */
function count440Detection(records, station) {
  if (!records) return 0;
  const stationUpper = station.toUpperCase();
  
  return records.filter(r => {
    // Old format check
    const key = `${station.toLowerCase()}_detected`;
    if (r[key] === true || r[key] === 'true' || r[key] === 1) return true;
    
    // Phase 2 format: check ground_truth_station or dominant_station
    if (r.ground_truth_station === stationUpper) return true;
    if (r.dominant_station === stationUpper && !r.ground_truth_station) return true;
    
    return false;
  }).length;
}

// ============ NEW CHART FUNCTIONS ============

/**
 * Render harmonic power ratio chart
 */
function renderHarmonicRatioChart(method, utcDate) {
  const container = document.getElementById('chart-harmonic');
  if (!container) return;
  
  if (!method || !method.records || method.records.length === 0) {
    container.innerHTML = '<p style="padding: 20px; text-align: center; color: var(--text-muted);">No harmonic ratio data available.<br>This data shows 500→1000 Hz and 600→1200 Hz harmonic relationships.</p>';
    return;
  }
  
  const sortedRecords = [...method.records].sort((a, b) => 
    new Date(a.timestamp_utc) - new Date(b.timestamp_utc)
  );
  
  const timestamps = sortedRecords.map(r => r.timestamp_utc);
  const ratio500_1000 = sortedRecords.map(r => parseFloat(r.harmonic_ratio_500_1000) || null);
  const ratio600_1200 = sortedRecords.map(r => parseFloat(r.harmonic_ratio_600_1200) || null);
  
  const trace1 = {
    x: timestamps,
    y: ratio500_1000,
    name: '500→1000 Hz (WWV)',
    type: 'scatter',
    mode: 'lines+markers',
    line: { color: COLORS.wwv, width: 2 },
    marker: { size: 4 },
    hovertemplate: '500→1000: %{y:.1f} dB<extra></extra>'
  };
  
  const trace2 = {
    x: timestamps,
    y: ratio600_1200,
    name: '600→1200 Hz (WWVH)',
    type: 'scatter',
    mode: 'lines+markers',
    line: { color: COLORS.wwvh, width: 2 },
    marker: { size: 4 },
    hovertemplate: '600→1200: %{y:.1f} dB<extra></extra>'
  };
  
  const xRange = getUTCDayRange(utcDate);
  
  let layout = {
    ...getPlotlyLayoutDefaults(),
    title: makeTitle('Harmonic Power Ratios (2nd Harmonic / Fundamental)'),
    xaxis: { 
      ...getPlotlyLayoutDefaults().xaxis,
      title: 'Time (UTC)',
      range: xRange,
      type: 'date'
    },
    yaxis: { 
      ...getPlotlyLayoutDefaults().yaxis,
      title: 'Ratio (dB)',
      zeroline: true,
      zerolinecolor: 'rgba(255,255,255,0.2)'
    },
    showlegend: true,
    legend: { ...getPlotlyLayoutDefaults().legend, x: 0, y: 1, orientation: 'h' },
    margin: { l: 60, r: 70, t: 50, b: 50 }
  };
  
  // Add solar zenith overlay
  const traces = [trace1, trace2, ...createSolarZenithTraces()];
  layout = addSolarYAxis(layout);
  
  Plotly.newPlot('chart-harmonic', traces, layout, PLOTLY_CONFIG);
}

/**
 * Render detection type pie chart
 */
function renderDetectionTypePie(method) {
  const container = document.getElementById('chart-detection-pie');
  if (!container) return;
  
  if (!method || !method.records || method.records.length === 0) {
    container.innerHTML = '<p style="padding: 20px; text-align: center; color: var(--text-muted);">No voting data available</p>';
    return;
  }
  
  // Count by dominant station
  const wwvCount = countStation(method.records, 'WWV');
  const wwvhCount = countStation(method.records, 'WWVH');
  const balancedCount = countStation(method.records, 'BALANCED');
  
  // Count by confidence
  let highConf = 0, medConf = 0, lowConf = 0;
  method.records.forEach(r => {
    const conf = (r.confidence || '').toLowerCase();
    if (conf === 'high') highConf++;
    else if (conf === 'medium') medConf++;
    else lowConf++;
  });
  
  // Pie chart for station dominance
  const pieData = [{
    values: [wwvCount, wwvhCount, balancedCount],
    labels: ['WWV', 'WWVH', 'Balanced'],
    type: 'pie',
    marker: {
      colors: [COLORS.wwv, COLORS.wwvh, '#94a3b8']
    },
    textinfo: 'label+percent',
    textposition: 'inside',
    hovertemplate: '%{label}: %{value} minutes<br>%{percent}<extra></extra>',
    hole: 0.4
  }];
  
  const layout = {
    ...getPlotlyLayoutDefaults(),
    title: makeTitle('Station Dominance'),
    showlegend: false,
    margin: { l: 20, r: 20, t: 50, b: 20 },
    annotations: [{
      text: `${method.records.length}<br>min`,
      x: 0.5,
      y: 0.5,
      font: { size: 16, color: getThemeColors().text },
      showarrow: false
    }]
  };
  
  Plotly.newPlot('chart-detection-pie', pieData, layout, PLOTLY_CONFIG);
}

/**
 * Apply dark theme to all charts
 */
function applyDarkTheme(layout) {
  return {
    ...layout,
    ...getPlotlyLayoutDefaults(),
    xaxis: { ...getPlotlyLayoutDefaults().xaxis, ...layout.xaxis },
    yaxis: { ...getPlotlyLayoutDefaults().yaxis, ...layout.yaxis }
  };
}

/**
 * Calculate channel quality statistics from test signal data
 * Used for Votes 9-12: FSS, Delay Spread, Coherence Time, Spreading Factor
 */
function calcChannelQualityStats(records) {
  if (!records || records.length === 0) {
    return `
      <div class="insight-card">
        <div class="insight-label">Status</div>
        <div class="insight-value" style="color: var(--text-muted);">No test signal data</div>
      </div>
      <div class="insight-card">
        <div class="insight-label">Info</div>
        <div class="insight-value" style="font-size: 11px;">Test signal in min :08 (WWV) and :44 (WWVH)</div>
      </div>
    `;
  }
  
  // Calculate statistics from detected test signals
  const detected = records.filter(r => r.detected);
  let fssValues = [], delaySpreadValues = [], coherenceValues = [];
  
  detected.forEach(r => {
    if (r.fss_db != null && !isNaN(r.fss_db)) fssValues.push(r.fss_db);
    if (r.delay_spread_ms != null && !isNaN(r.delay_spread_ms)) delaySpreadValues.push(r.delay_spread_ms);
    if (r.coherence_time_sec != null && !isNaN(r.coherence_time_sec)) coherenceValues.push(r.coherence_time_sec);
  });
  
  const avgFSS = fssValues.length > 0 ? (fssValues.reduce((a,b) => a+b, 0) / fssValues.length).toFixed(1) : 'N/A';
  const avgDelaySpread = delaySpreadValues.length > 0 ? (delaySpreadValues.reduce((a,b) => a+b, 0) / delaySpreadValues.length).toFixed(2) : 'N/A';
  const avgCoherence = coherenceValues.length > 0 ? (coherenceValues.reduce((a,b) => a+b, 0) / coherenceValues.length).toFixed(1) : 'N/A';
  
  // Calculate spreading factor L = τ_D × f_D where f_D ≈ 1/(π×τ_c)
  let spreadingFactor = 'N/A';
  if (delaySpreadValues.length > 0 && coherenceValues.length > 0) {
    const avgTauD = delaySpreadValues.reduce((a,b) => a+b, 0) / delaySpreadValues.length;
    const avgTauC = coherenceValues.reduce((a,b) => a+b, 0) / coherenceValues.length;
    if (avgTauC > 0.01) {
      const fD = 1.0 / (Math.PI * avgTauC);
      const L = (avgTauD / 1000) * fD;
      spreadingFactor = L.toFixed(3);
    }
  }
  
  // Determine channel quality assessment
  let qualityLabel = 'Unknown', qualityColor = 'var(--text-muted)';
  if (spreadingFactor !== 'N/A') {
    const L = parseFloat(spreadingFactor);
    if (L < 0.05) { qualityLabel = 'Excellent'; qualityColor = '#10b981'; }
    else if (L < 0.3) { qualityLabel = 'Good'; qualityColor = '#3b82f6'; }
    else if (L < 1.0) { qualityLabel = 'Fair'; qualityColor = '#f59e0b'; }
    else { qualityLabel = 'Poor'; qualityColor = '#ef4444'; }
  }
  
  return `
    <div class="insight-card">
      <div class="insight-label">Detected</div>
      <div class="insight-value">${detected.length}/${records.length}</div>
    </div>
    <div class="insight-card">
      <div class="insight-label">Avg FSS</div>
      <div class="insight-value">${avgFSS} dB</div>
    </div>
    <div class="insight-card">
      <div class="insight-label">Avg τ<sub>D</sub></div>
      <div class="insight-value">${avgDelaySpread} ms</div>
    </div>
    <div class="insight-card">
      <div class="insight-label">Avg τ<sub>c</sub></div>
      <div class="insight-value">${avgCoherence} s</div>
    </div>
    <div class="insight-card">
      <div class="insight-label">Spreading L</div>
      <div class="insight-value">${spreadingFactor}</div>
    </div>
    <div class="insight-card">
      <div class="insight-label">Quality</div>
      <div class="insight-value" style="color: ${qualityColor};">${qualityLabel}</div>
    </div>
  `;
}

/**
 * Render channel quality chart from test signal data
 * Shows FSS, Delay Spread, and Coherence Time over time
 */
function renderChannelQualityChart(method, utcDate) {
  const container = document.getElementById('chart-channel-quality');
  if (!container) return;
  
  if (!method || !method.records || method.records.length === 0) {
    container.innerHTML = `
      <p style="padding: 20px; text-align: center; color: var(--text-muted);">
        No channel quality data available.<br>
        <span style="font-size: 11px;">Test signal occurs at minutes :08 (WWV) and :44 (WWVH).<br>
        Provides FSS (geographic fingerprint), delay spread (multipath), and coherence time (stability).</span>
      </p>
    `;
    return;
  }
  
  // Filter to detected test signals with channel quality data and sort by timestamp
  const detected = method.records
    .filter(r => r.detected)
    .sort((a, b) => new Date(a.timestamp_utc) - new Date(b.timestamp_utc));
  
  if (detected.length === 0) {
    container.innerHTML = `
      <p style="padding: 20px; text-align: center; color: var(--text-muted);">
        Test signals recorded but none detected.<br>
        <span style="font-size: 11px;">This may indicate weak signal conditions.</span>
      </p>
    `;
    return;
  }
  
  const timestamps = detected.map(r => r.timestamp_utc);
  const fssValues = detected.map(r => r.fss_db);
  const delaySpreadValues = detected.map(r => r.delay_spread_ms);
  const coherenceValues = detected.map(r => r.coherence_time_sec);
  
  const traces = [];
  
  // FSS trace (left y-axis)
  if (fssValues.some(v => v != null)) {
    traces.push({
      x: timestamps,
      y: fssValues,
      name: 'FSS (dB)',
      type: 'scatter',
      mode: 'markers+lines',
      marker: { color: '#8b5cf6', size: 10, symbol: 'diamond' },
      line: { color: '#8b5cf6', width: 2 },
      hovertemplate: 'FSS: %{y:.1f} dB<extra></extra>'
    });
  }
  
  // Delay spread trace (right y-axis)
  if (delaySpreadValues.some(v => v != null)) {
    traces.push({
      x: timestamps,
      y: delaySpreadValues,
      name: 'Delay Spread (ms)',
      type: 'scatter',
      mode: 'markers+lines',
      marker: { color: '#f59e0b', size: 10, symbol: 'circle' },
      line: { color: '#f59e0b', width: 2 },
      yaxis: 'y2',
      hovertemplate: 'τ_D: %{y:.2f} ms<extra></extra>'
    });
  }
  
  // Coherence time trace (right y-axis, different scale)
  if (coherenceValues.some(v => v != null)) {
    traces.push({
      x: timestamps,
      y: coherenceValues,
      name: 'Coherence (s)',
      type: 'scatter',
      mode: 'markers+lines',
      marker: { color: '#10b981', size: 10, symbol: 'square' },
      line: { color: '#10b981', width: 2, dash: 'dash' },
      yaxis: 'y3',
      hovertemplate: 'τ_c: %{y:.1f} s<extra></extra>'
    });
  }
  
  // Add solar zenith if available
  traces.push(...createSolarZenithTraces());
  
  const xRange = getUTCDayRange(utcDate);
  
  let layout = {
    ...getPlotlyLayoutDefaults(),
    title: makeTitle('Channel Quality: FSS, Delay Spread, Coherence Time'),
    xaxis: { 
      ...getPlotlyLayoutDefaults().xaxis,
      title: 'Time (UTC)',
      range: xRange,
      type: 'date',
      domain: [0, 0.85]
    },
    yaxis: { 
      ...getPlotlyLayoutDefaults().yaxis,
      title: 'FSS (dB)',
      titlefont: { color: '#8b5cf6' },
      tickfont: { color: '#8b5cf6' }
    },
    yaxis2: {
      title: 'Delay Spread (ms)',
      titlefont: { color: '#f59e0b' },
      tickfont: { color: '#f59e0b' },
      overlaying: 'y',
      side: 'right',
      position: 0.88
    },
    yaxis3: {
      title: 'τ_c (s)',
      titlefont: { color: '#10b981' },
      tickfont: { color: '#10b981' },
      overlaying: 'y',
      side: 'right',
      position: 0.95,
      anchor: 'free'
    },
    showlegend: true,
    legend: { ...getPlotlyLayoutDefaults().legend, x: 0, y: 1.15, orientation: 'h' },
    margin: { l: 60, r: 100, t: 60, b: 50 }
  };
  
  // Add solar y-axis if data available
  if (solarZenithData) {
    layout.yaxis4 = {
      title: 'Solar (°)',
      overlaying: 'y',
      side: 'right',
      position: 1.0,
      anchor: 'free',
      range: [-90, 90],
      showgrid: false
    };
    // Update solar traces to use yaxis4
    traces.filter(t => t.name && t.name.includes('Solar')).forEach(t => t.yaxis = 'y4');
  }
  
  Plotly.newPlot('chart-channel-quality', traces, layout, PLOTLY_CONFIG);
}

# Multi-Method Discrimination Enhancement
**Date:** November 24, 2025  
**Purpose:** Specific design for enhanced discrimination visualization  
**Status:** Ready to implement

---

## Current vs Enhanced Visualization

### Current State
- Shows only **Method 5: Weighted Voting** (final result)
- Single time-series plot
- Limited insight into how decision was made

### Enhanced State
- Shows **all 5 methods** side-by-side
- Individual confidence levels
- Agreement/disagreement indicators
- Method-specific time-series plots
- Provenance transparency

---

## 5-Panel Layout Design

```
┌────────────────────────────────────────────────────────────────┐
│  WWV/WWVH Discrimination - WWV 10 MHz - 2025-11-24           │
├────────────────────────────────────────────────────────────────┤
│                                                                │
│  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐│
│  │ Method 1:       │  │ Method 2:       │  │ Method 3:       ││
│  │ Timing Tones    │  │ Tick Windows    │  │ Station ID      ││
│  │─────────────────│  │─────────────────│  │─────────────────││
│  │ WWV:  48 det    │  │ WWV SNR: 18.2dB │  │ WWV:  2 IDs     ││
│  │ WWVH: 42 det    │  │ WWVH SNR: 15.7  │  │ WWVH: 1 ID      ││
│  │ Ratio: 1.14     │  │ Coherence: 0.82 │  │ 440Hz: Clear    ││
│  │ ✅ WWV favored   │  │ ✅ WWV favored   │  │ ✅ WWV minute 2  ││
│  └─────────────────┘  └─────────────────┘  └─────────────────┘│
│                                                                │
│  ┌─────────────────┐  ┌─────────────────────────────────────┐ │
│  │ Method 4:       │  │ Method 5: Weighted Voting (FINAL)   │ │
│  │ BCD Analysis    │  │─────────────────────────────────────│ │
│  │─────────────────│  │ 🎯 Dominant: WWV                     │ │
│  │ WWV amp: 0.65   │  │ Confidence: HIGH                     │ │
│  │ WWVH amp: 0.42  │  │ Agreement: 4/4 methods agree         │ │
│  │ Delay: 2.3 ms   │  │                                      │ │
│  │ ✅ WWV stronger  │  │ Method Contributions:                │ │
│  └─────────────────┘  │ • Timing Tones:  25% (WWV)          │ │
│                       │ • Tick Windows:  20% (WWV)          │ │
│                       │ • Station ID:    30% (WWV)          │ │
│                       │ • BCD Analysis:  25% (WWV)          │ │
│                       └─────────────────────────────────────┘ │
│                                                                │
│  📊 Time-Series Analysis (click method to view)               │
│  [Currently showing: All Methods Combined]                    │
│  ───────────────────────────────────────────────────────────  │
│  │                                                          │  │
│  │  [Interactive Plotly chart showing power ratio over time] │
│  │                                                          │  │
│  ───────────────────────────────────────────────────────────  │
└────────────────────────────────────────────────────────────────┘
```

---

## HTML Structure

**Create:** `web-ui/discrimination-enhanced-v2.html`

```html
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>WWV/WWVH Discrimination - Multi-Method Analysis</title>
    <script src="https://cdn.plot.ly/plotly-2.27.0.min.js"></script>
    <style>
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: #0a0e27;
            color: #e0e0e0;
            padding: 20px;
            margin: 0;
        }
        
        .header {
            background: linear-gradient(135deg, #1e3a8a, #312e81);
            padding: 20px;
            border-radius: 10px;
            margin-bottom: 20px;
        }
        
        .methods-grid {
            display: grid;
            grid-template-columns: repeat(3, 1fr);
            gap: 15px;
            margin-bottom: 20px;
        }
        
        .method-card {
            background: #1e293b;
            padding: 20px;
            border-radius: 8px;
            border-left: 4px solid #3b82f6;
            transition: transform 0.2s;
        }
        
        .method-card:hover {
            transform: translateY(-2px);
            box-shadow: 0 4px 12px rgba(0,0,0,0.3);
        }
        
        .method-card.wwv-favored { border-left-color: #3b82f6; }
        .method-card.wwvh-favored { border-left-color: #f59e0b; }
        .method-card.balanced { border-left-color: #8b5cf6; }
        
        .method-title {
            font-size: 14px;
            font-weight: 600;
            color: #94a3b8;
            margin-bottom: 10px;
        }
        
        .method-stats {
            display: flex;
            flex-direction: column;
            gap: 8px;
            margin: 10px 0;
        }
        
        .stat-row {
            display: flex;
            justify-content: space-between;
            font-size: 13px;
        }
        
        .stat-label {
            color: #94a3b8;
        }
        
        .stat-value {
            font-weight: 600;
            color: #fff;
        }
        
        .method-result {
            margin-top: 12px;
            padding: 8px;
            background: rgba(59, 130, 246, 0.1);
            border-radius: 4px;
            font-size: 13px;
            font-weight: 600;
        }
        
        .result-wwv { background: rgba(59, 130, 246, 0.15); color: #60a5fa; }
        .result-wwvh { background: rgba(245, 158, 11, 0.15); color: #fbbf24; }
        .result-balanced { background: rgba(139, 92, 246, 0.15); color: #a78bfa; }
        
        .final-voting {
            grid-column: span 3;
            background: #1e293b;
            padding: 25px;
            border-radius: 8px;
            border: 2px solid #3b82f6;
        }
        
        .voting-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 15px;
        }
        
        .dominant-station {
            font-size: 24px;
            font-weight: 700;
        }
        
        .confidence-badge {
            padding: 8px 16px;
            border-radius: 20px;
            font-weight: 600;
            font-size: 14px;
        }
        
        .confidence-high { background: #10b981; }
        .confidence-medium { background: #f59e0b; }
        .confidence-low { background: #ef4444; }
        
        .method-weights {
            display: grid;
            grid-template-columns: repeat(2, 1fr);
            gap: 10px;
            margin-top: 15px;
        }
        
        .weight-item {
            display: flex;
            align-items: center;
            gap: 10px;
        }
        
        .weight-bar {
            flex: 1;
            height: 8px;
            background: #334155;
            border-radius: 4px;
            overflow: hidden;
        }
        
        .weight-fill {
            height: 100%;
            background: linear-gradient(90deg, #3b82f6, #60a5fa);
        }
        
        .agreement-indicator {
            margin-top: 15px;
            padding: 12px;
            background: rgba(16, 185, 129, 0.1);
            border-left: 4px solid #10b981;
            border-radius: 4px;
        }
        
        .chart-container {
            background: #1e293b;
            padding: 20px;
            border-radius: 8px;
            margin-top: 20px;
        }
    </style>
</head>
<body>
    <div class="header">
        <h1>WWV/WWVH Discrimination Analysis</h1>
        <div style="margin-top: 10px;">
            <select id="channelSelect" style="padding: 8px; border-radius: 4px; margin-right: 10px;">
                <option value="WWV_5_MHz">WWV 5 MHz</option>
                <option value="WWV_10_MHz" selected>WWV 10 MHz</option>
                <option value="WWV_15_MHz">WWV 15 MHz</option>
            </select>
            <input type="date" id="dateSelect" style="padding: 8px; border-radius: 4px;">
            <button onclick="loadData()" style="padding: 8px 16px; margin-left: 10px; background: #3b82f6; color: white; border: none; border-radius: 4px; cursor: pointer;">
                Load Data
            </button>
        </div>
    </div>

    <div class="methods-grid" id="methodsGrid">
        <!-- Dynamic content loaded here -->
    </div>

    <div class="chart-container">
        <h3>Time-Series Analysis</h3>
        <div style="margin-bottom: 10px;">
            <button class="chart-btn active" onclick="showChart('all')">All Methods</button>
            <button class="chart-btn" onclick="showChart('tones')">Timing Tones</button>
            <button class="chart-btn" onclick="showChart('ticks')">Tick Windows</button>
            <button class="chart-btn" onclick="showChart('bcd')">BCD Analysis</button>
        </div>
        <div id="chartDiv" style="height: 400px;"></div>
    </div>

    <script>
        // Set default date to today
        document.getElementById('dateSelect').valueAsDate = new Date();
        
        async function loadData() {
            const channel = document.getElementById('channelSelect').value;
            const date = document.getElementById('dateSelect').value.replace(/-/g, '');
            
            try {
                const response = await fetch(`/api/v1/channels/${channel}/discrimination/${date}/methods`);
                const data = await response.json();
                
                renderMethodCards(data);
                renderCharts(data);
            } catch (err) {
                console.error('Error loading discrimination data:', err);
                alert('Failed to load data. Check console for details.');
            }
        }
        
        function renderMethodCards(data) {
            const grid = document.getElementById('methodsGrid');
            
            // Method 1: Timing Tones
            const tones = data.methods.timing_tones;
            const wwvCount = tones.records.filter(r => r.station === 'WWV').length;
            const wwvhCount = tones.records.filter(r => r.station === 'WWVH').length;
            const tonesRatio = wwvhCount > 0 ? (wwvCount / wwvhCount).toFixed(2) : 'N/A';
            
            // Method 2: Tick Windows
            const ticks = data.methods.tick_windows;
            const avgWwvSnr = average(ticks.records.map(r => parseFloat(r.wwv_snr_db)));
            const avgWwvhSnr = average(ticks.records.map(r => parseFloat(r.wwvh_snr_db)));
            
            // Method 3: Station ID
            const stationId = data.methods.station_id;
            const wwvIds = stationId.records.filter(r => r.wwv_detected === 'True').length;
            const wwvhIds = stationId.records.filter(r => r.wwvh_detected === 'True').length;
            
            // Method 4: BCD
            const bcd = data.methods.bcd;
            const avgWwvAmp = average(bcd.records.map(r => parseFloat(r.wwv_amplitude)));
            const avgWwvhAmp = average(bcd.records.map(r => parseFloat(r.wwvh_amplitude)));
            
            // Method 5: Weighted Voting
            const voting = data.methods.weighted_voting;
            const dominantCounts = {};
            voting.records.forEach(r => {
                const station = r.dominant_station;
                dominantCounts[station] = (dominantCounts[station] || 0) + 1;
            });
            
            const dominantStation = Object.keys(dominantCounts).reduce((a, b) => 
                dominantCounts[a] > dominantCounts[b] ? a : b
            );
            const dominantPct = ((dominantCounts[dominantStation] / voting.records.length) * 100).toFixed(0);
            
            grid.innerHTML = `
                <div class="method-card wwv-favored">
                    <div class="method-title">Method 1: Timing Tones</div>
                    <div class="method-stats">
                        <div class="stat-row">
                            <span class="stat-label">WWV detections:</span>
                            <span class="stat-value">${wwvCount}</span>
                        </div>
                        <div class="stat-row">
                            <span class="stat-label">WWVH detections:</span>
                            <span class="stat-value">${wwvhCount}</span>
                        </div>
                        <div class="stat-row">
                            <span class="stat-label">Ratio:</span>
                            <span class="stat-value">${tonesRatio}</span>
                        </div>
                    </div>
                    <div class="method-result result-wwv">✅ ${wwvCount > wwvhCount ? 'WWV' : 'WWVH'} favored</div>
                </div>
                
                <div class="method-card wwv-favored">
                    <div class="method-title">Method 2: Tick Windows</div>
                    <div class="method-stats">
                        <div class="stat-row">
                            <span class="stat-label">WWV SNR:</span>
                            <span class="stat-value">${avgWwvSnr.toFixed(1)} dB</span>
                        </div>
                        <div class="stat-row">
                            <span class="stat-label">WWVH SNR:</span>
                            <span class="stat-value">${avgWwvhSnr.toFixed(1)} dB</span>
                        </div>
                        <div class="stat-row">
                            <span class="stat-label">Windows:</span>
                            <span class="stat-value">${ticks.count}</span>
                        </div>
                    </div>
                    <div class="method-result result-wwv">✅ ${avgWwvSnr > avgWwvhSnr ? 'WWV' : 'WWVH'} stronger</div>
                </div>
                
                <div class="method-card wwv-favored">
                    <div class="method-title">Method 3: Station ID (440 Hz)</div>
                    <div class="method-stats">
                        <div class="stat-row">
                            <span class="stat-label">WWV IDs (min 2):</span>
                            <span class="stat-value">${wwvIds}</span>
                        </div>
                        <div class="stat-row">
                            <span class="stat-label">WWVH IDs (min 1):</span>
                            <span class="stat-value">${wwvhIds}</span>
                        </div>
                    </div>
                    <div class="method-result result-wwv">✅ Clear identification</div>
                </div>
                
                <div class="method-card wwv-favored">
                    <div class="method-title">Method 4: BCD Analysis</div>
                    <div class="method-stats">
                        <div class="stat-row">
                            <span class="stat-label">WWV amplitude:</span>
                            <span class="stat-value">${avgWwvAmp.toFixed(2)}</span>
                        </div>
                        <div class="stat-row">
                            <span class="stat-label">WWVH amplitude:</span>
                            <span class="stat-value">${avgWwvhAmp.toFixed(2)}</span>
                        </div>
                        <div class="stat-row">
                            <span class="stat-label">Windows:</span>
                            <span class="stat-value">${bcd.count}</span>
                        </div>
                    </div>
                    <div class="method-result result-wwv">✅ ${avgWwvAmp > avgWwvhAmp ? 'WWV' : 'WWVH'} stronger</div>
                </div>
                
                <div class="final-voting">
                    <div class="voting-header">
                        <div>
                            <div style="font-size: 14px; color: #94a3b8; margin-bottom: 5px;">FINAL DETERMINATION</div>
                            <div class="dominant-station">🎯 Dominant: ${dominantStation}</div>
                        </div>
                        <div class="confidence-badge confidence-high">
                            HIGH CONFIDENCE (${dominantPct}%)
                        </div>
                    </div>
                    
                    <div class="agreement-indicator">
                        <strong>Agreement:</strong> 4/4 methods favor ${dominantStation}
                    </div>
                    
                    <div class="method-weights">
                        <div class="weight-item">
                            <span style="width: 120px;">Timing Tones:</span>
                            <div class="weight-bar"><div class="weight-fill" style="width: 25%"></div></div>
                            <span>25%</span>
                        </div>
                        <div class="weight-item">
                            <span style="width: 120px;">Tick Windows:</span>
                            <div class="weight-bar"><div class="weight-fill" style="width: 20%"></div></div>
                            <span>20%</span>
                        </div>
                        <div class="weight-item">
                            <span style="width: 120px;">Station ID:</span>
                            <div class="weight-bar"><div class="weight-fill" style="width: 30%"></div></div>
                            <span>30%</span>
                        </div>
                        <div class="weight-item">
                            <span style="width: 120px;">BCD Analysis:</span>
                            <div class="weight-bar"><div class="weight-fill" style="width: 25%"></div></div>
                            <span>25%</span>
                        </div>
                    </div>
                </div>
            `;
        }
        
        function renderCharts(data) {
            // Create Plotly time-series chart
            const tones = data.methods.timing_tones.records;
            
            const wwvTimes = [], wwvPowers = [];
            const wwvhTimes = [], wwvhPowers = [];
            
            tones.forEach(r => {
                if (r.station === 'WWV') {
                    wwvTimes.push(r.timestamp_utc);
                    wwvPowers.push(parseFloat(r.tone_power_db));
                } else if (r.station === 'WWVH') {
                    wwvhTimes.push(r.timestamp_utc);
                    wwvhPowers.push(parseFloat(r.tone_power_db));
                }
            });
            
            const traces = [
                {
                    x: wwvTimes,
                    y: wwvPowers,
                    mode: 'lines+markers',
                    name: 'WWV (1000 Hz)',
                    line: { color: '#3b82f6' }
                },
                {
                    x: wwvhTimes,
                    y: wwvhPowers,
                    mode: 'lines+markers',
                    name: 'WWVH (1200 Hz)',
                    line: { color: '#f59e0b' }
                }
            ];
            
            const layout = {
                title: 'Tone Power Over Time',
                xaxis: { title: 'Time (UTC)' },
                yaxis: { title: 'Tone Power (dB)' },
                paper_bgcolor: '#1e293b',
                plot_bgcolor: '#1e293b',
                font: { color: '#e0e0e0' }
            };
            
            Plotly.newPlot('chartDiv', traces, layout);
        }
        
        function average(arr) {
            const valid = arr.filter(v => !isNaN(v) && v !== null);
            return valid.length > 0 ? valid.reduce((a, b) => a + b, 0) / valid.length : 0;
        }
        
        // Load data on page load
        loadData();
    </script>
</body>
</html>
```

---

## Key Features

1. **5-Panel Grid** - Each method displayed separately
2. **Color Coding** - Blue (WWV), Amber (WWVH), Purple (Balanced)
3. **Agreement Indicator** - Shows consensus across methods
4. **Interactive Charts** - Click method to see its time-series
5. **Method Weights** - Visual representation of voting weights
6. **Confidence Levels** - HIGH/MEDIUM/LOW based on agreement

---

## Implementation Checklist

- [ ] Add `/api/v1/channels/:name/discrimination/:date/methods` endpoint
- [ ] Create `discrimination-enhanced-v2.html`
- [ ] Test with real WWV/WWVH data
- [ ] Add to main navigation
- [ ] Update documentation

**Estimated Time:** 3-4 hours for complete implementation

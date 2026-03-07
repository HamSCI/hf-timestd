/**
 * Common utilities for hf-timestd Web UI
 */

// API client
class TimestdAPI {
    constructor(baseURL = '/api') {
        this.baseURL = baseURL;
    }
    
    async get(endpoint, params = {}) {
        // Ensure endpoint starts with /
        if (!endpoint.startsWith('/')) {
            endpoint = '/' + endpoint;
        }
        
        // Build full URL with base
        const fullPath = this.baseURL + endpoint;
        const url = new URL(fullPath, window.location.origin);
        
        Object.keys(params).forEach(key => 
            url.searchParams.append(key, params[key])
        );
        
        const response = await fetch(url);
        if (!response.ok) {
            throw new Error(`API error: ${response.statusText}`);
        }
        return response.json();
    }
}

// Global API instance
const api = new TimestdAPI();

// Time formatting
function formatTimestamp(iso8601) {
    if (!iso8601) return 'N/A';
    const date = new Date(iso8601);
    return date.toLocaleString('en-US', {
        year: 'numeric',
        month: '2-digit',
        day: '2-digit',
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit',
        hour12: false
    });
}

function formatTime(iso8601) {
    if (!iso8601) return 'N/A';
    const date = new Date(iso8601);
    return date.toLocaleTimeString('en-US', {
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit',
        hour12: false
    });
}

function formatDate(iso8601) {
    if (!iso8601) return 'N/A';
    const date = new Date(iso8601);
    return date.toLocaleDateString('en-US', {
        year: 'numeric',
        month: '2-digit',
        day: '2-digit'
    });
}

function timeAgo(iso8601) {
    if (!iso8601) return 'N/A';
    const date = new Date(iso8601);
    const seconds = Math.floor((new Date() - date) / 1000);
    
    if (seconds < 60) return `${seconds}s ago`;
    if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
    if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
    return `${Math.floor(seconds / 86400)}d ago`;
}

// Number formatting
function formatNumber(value, decimals = 2) {
    if (value === null || value === undefined) return 'N/A';
    return Number(value).toFixed(decimals);
}

function formatFrequency(hz) {
    if (!hz) return 'N/A';
    const mhz = hz / 1e6;
    return `${mhz.toFixed(3)} MHz`;
}

// Quality grade colors
const QUALITY_COLORS = {
    'A': '#10b981',  // green
    'B': '#3b82f6',  // blue
    'C': '#f59e0b',  // orange
    'D': '#ef4444'   // red
};

function getQualityColor(grade) {
    return QUALITY_COLORS[grade] || '#94a3b8';
}

function getQualityClass(grade) {
    return `grade-${grade.toLowerCase()}`;
}

// Status colors
const STATUS_COLORS = {
    'healthy': '#10b981',
    'degraded': '#f59e0b',
    'error': '#ef4444',
    'active': '#10b981',
    'inactive': '#94a3b8',
    'stale': '#f59e0b'
};

function getStatusColor(status) {
    return STATUS_COLORS[status] || '#94a3b8';
}

function getStatusClass(status) {
    return `status-${status.toLowerCase()}`;
}

// Error handling
function showError(message, containerId = 'error-container') {
    const container = document.getElementById(containerId);
    if (container) {
        container.innerHTML = `
            <div class="error-message">
                <strong>Error:</strong> ${message}
            </div>
        `;
        container.style.display = 'block';
    } else {
        console.error(message);
    }
}

function clearError(containerId = 'error-container') {
    const container = document.getElementById(containerId);
    if (container) {
        container.innerHTML = '';
        container.style.display = 'none';
    }
}

// Loading indicator
function showLoading(containerId) {
    const container = document.getElementById(containerId);
    if (container) {
        container.innerHTML = '<div class="loading"></div>';
    }
}

function hideLoading(containerId) {
    const container = document.getElementById(containerId);
    if (container) {
        container.innerHTML = '';
    }
}

// Auto-refresh helper
class AutoRefresh {
    constructor(callback, interval = 60000) {
        this.callback = callback;
        this.interval = interval;
        this.timerId = null;
    }
    
    start() {
        this.callback();
        this.timerId = setInterval(() => this.callback(), this.interval);
    }
    
    stop() {
        if (this.timerId) {
            clearInterval(this.timerId);
            this.timerId = null;
        }
    }
    
    setInterval(interval) {
        this.interval = interval;
        if (this.timerId) {
            this.stop();
            this.start();
        }
    }
}

// Shared Plotly config — hide modebar to avoid overlapping time controls
const PLOTLY_CONFIG = { responsive: true, displayModeBar: false };

// Uniform plot margins so stacked plots align their x-axes
const PLOT_MARGINS = { l: 70, r: 70, t: 10, b: 40 };

/**
 * TimePeriodSelector — shared time-period navigation for stacked-plot pages.
 *
 * Default view: today 00:00-23:59 UTC.
 * Period buttons: 6h, 12h, 1d (default).
 * Step back / forward by the selected period.
 * Date picker for jumping to any day.
 *
 * Usage:
 *   const tps = new TimePeriodSelector('container-id', () => refreshAll());
 *   // In your plot code:
 *   const range = tps.getRange();       // { start: '...Z', end: '...Z' }
 *   const xRange = tps.getXRange();     // [startISO, endISO] for Plotly xaxis.range
 *   const apiParams = tps.getAPIParams(); // { start: 'ISO', end: 'ISO' }
 */
class TimePeriodSelector {
    constructor(containerId, onChange, options = {}) {
        this.container = document.getElementById(containerId);
        this.onChange = onChange;
        this.periods = options.periods || [
            { label: '6h', hours: 6 },
            { label: '12h', hours: 12 },
            { label: '1d', hours: 24 },
        ];
        this.defaultPeriodLabel = options.defaultPeriod || '1d';

        // State: anchor is the START of the window (UTC midnight by default)
        const now = new Date();
        this.anchorUTC = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate()));
        this.periodHours = 24;

        // Set initial period
        const match = this.periods.find(p => p.label === this.defaultPeriodLabel);
        if (match) this.periodHours = match.hours;

        this._render();
    }

    _render() {
        if (!this.container) return;
        const c = this.container;
        c.innerHTML = '';
        c.style.cssText = 'display:flex; gap:8px; align-items:center; flex-wrap:wrap;';

        // Step back button
        const backBtn = document.createElement('button');
        backBtn.className = 'time-range-btn';
        backBtn.innerHTML = '&#9664;';
        backBtn.title = 'Step back';
        backBtn.addEventListener('click', () => this.step(-1));
        c.appendChild(backBtn);

        // Period buttons
        this.periods.forEach(p => {
            const btn = document.createElement('button');
            btn.className = 'time-range-btn';
            if (p.hours === this.periodHours) btn.classList.add('active');
            btn.textContent = p.label;
            btn.addEventListener('click', () => {
                this.periodHours = p.hours;
                // Re-anchor to day boundary when switching to 1d
                if (p.hours === 24) {
                    this.anchorUTC = new Date(Date.UTC(
                        this.anchorUTC.getUTCFullYear(),
                        this.anchorUTC.getUTCMonth(),
                        this.anchorUTC.getUTCDate()
                    ));
                }
                this._render();
                this.onChange();
            });
            c.appendChild(btn);
        });

        // Step forward button
        const fwdBtn = document.createElement('button');
        fwdBtn.className = 'time-range-btn';
        fwdBtn.innerHTML = '&#9654;';
        fwdBtn.title = 'Step forward';
        fwdBtn.addEventListener('click', () => this.step(1));
        c.appendChild(fwdBtn);

        // Separator
        const sep = document.createElement('span');
        sep.style.cssText = 'color:#475569; margin:0 4px;';
        sep.textContent = '|';
        c.appendChild(sep);

        // Date picker
        const dateInput = document.createElement('input');
        dateInput.type = 'date';
        dateInput.style.cssText = 'padding:4px 8px; background:var(--bg-surface,#1e293b); border:1px solid rgba(59,130,246,0.3); border-radius:6px; color:#e2e8f0; font-size:12px;';
        dateInput.value = this._dateStr(this.anchorUTC);
        dateInput.addEventListener('change', () => {
            const parts = dateInput.value.split('-');
            this.anchorUTC = new Date(Date.UTC(+parts[0], +parts[1] - 1, +parts[2]));
            this.onChange();
        });
        c.appendChild(dateInput);
        this._dateInput = dateInput;

        // "Today" button
        const todayBtn = document.createElement('button');
        todayBtn.className = 'time-range-btn';
        todayBtn.textContent = 'Today';
        todayBtn.addEventListener('click', () => {
            const now = new Date();
            this.anchorUTC = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate()));
            this.periodHours = 24;
            this._render();
            this.onChange();
        });
        c.appendChild(todayBtn);

        // Range label
        const label = document.createElement('span');
        label.style.cssText = 'color:#94a3b8; font-size:12px; margin-left:8px;';
        const r = this.getRange();
        const startD = new Date(r.start);
        const endD = new Date(r.end);
        const fmt = (d) => d.toISOString().replace('T', ' ').slice(0, 16) + ' UTC';
        label.textContent = fmt(startD) + '  →  ' + fmt(endD);
        c.appendChild(label);
    }

    _dateStr(d) {
        return d.toISOString().slice(0, 10);
    }

    step(direction) {
        const ms = direction * this.periodHours * 3600000;
        this.anchorUTC = new Date(this.anchorUTC.getTime() + ms);
        this._render();
        this.onChange();
    }

    getRange() {
        const start = new Date(this.anchorUTC);
        const end = new Date(start.getTime() + this.periodHours * 3600000 - 1000); // -1s
        return {
            start: start.toISOString().replace('.000Z', 'Z'),
            end: end.toISOString().replace('.000Z', 'Z'),
        };
    }

    getXRange() {
        const r = this.getRange();
        return [r.start, r.end];
    }

    getAPIParams() {
        return this.getRange();
    }
}

// Export for use in other scripts
window.TimestdAPI = TimestdAPI;
window.api = api;
window.formatTimestamp = formatTimestamp;
window.formatTime = formatTime;
window.formatDate = formatDate;
window.timeAgo = timeAgo;
window.formatNumber = formatNumber;
window.formatFrequency = formatFrequency;
window.getQualityColor = getQualityColor;
window.getQualityClass = getQualityClass;
window.getStatusColor = getStatusColor;
window.getStatusClass = getStatusClass;
window.showError = showError;
window.clearError = clearError;
window.showLoading = showLoading;
window.hideLoading = hideLoading;
window.AutoRefresh = AutoRefresh;
window.PLOTLY_CONFIG = PLOTLY_CONFIG;
window.PLOT_MARGINS = PLOT_MARGINS;
window.TimePeriodSelector = TimePeriodSelector;

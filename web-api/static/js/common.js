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

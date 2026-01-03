/**
 * Timing Status Widget - Real-time timing quality display
 * 
 * Usage:
 *   <div id="timingStatus"></div>
 *   <script src="components/timing-status-widget.js"></script>
 *   <script>new TimingStatusWidget('timingStatus');</script>
 */

class TimingStatusWidget {
  constructor(containerId, options = {}) {
    this.container = document.getElementById(containerId);
    this.options = {
      updateInterval: options.updateInterval || 10000, // 10 seconds
      showAdoptions: options.showAdoptions !== false,  // Default true
      compact: options.compact || false
    };
    
    if (!this.container) {
      console.error(`TimingStatusWidget: Container '${containerId}' not found`);
      return;
    }
    
    this.render({ loading: true });
    this.update();
    
    // Auto-refresh
    this.intervalId = setInterval(() => this.update(), this.options.updateInterval);
  }
  
  destroy() {
    if (this.intervalId) {
      clearInterval(this.intervalId);
    }
  }
  
  async update() {
    try {
      const response = await fetch('/api/v1/timing/status');
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      
      const data = await response.json();
      this.render(data);
    } catch (err) {
      console.error('TimingStatusWidget: Update failed:', err);
      this.renderError(err.message);
    }
  }
  
  render(data) {
    if (data.loading) {
      this.container.innerHTML = this.renderLoading();
      return;
    }
    
    const statusColors = {
      TONE_LOCKED: { bg: '#10b981', label: 'TONE-LOCKED', desc: 'GPS-quality timing' },
      NTP_SYNCED: { bg: '#f59e0b', label: 'NTP-SYNCED', desc: 'Network time' },
      INTERPOLATED: { bg: '#fb923c', label: 'INTERPOLATED', desc: 'Aged reference' },
      WALL_CLOCK: { bg: '#ef4444', label: 'WALL-CLOCK', desc: 'Fallback mode' }
    };
    
    const status = statusColors[data.overall_status] || statusColors.WALL_CLOCK;
    
    this.container.innerHTML = `
      <div style="
        background: linear-gradient(135deg, #1e3a8a 0%, #312e81 100%);
        border-radius: 10px;
        padding: 20px;
        color: #e0e0e0;
        box-shadow: 0 4px 6px rgba(0, 0, 0, 0.3);
      ">
        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 15px;">
          <div style="font-size: 18px; font-weight: 600; color: #fff;">⏱️ Timing Status</div>
          <div style="
            display: inline-flex;
            align-items: center;
            gap: 8px;
            padding: 8px 16px;
            border-radius: 20px;
            font-size: 14px;
            font-weight: 600;
            background: ${status.bg};
            color: #fff;
          ">
            <div style="
              width: 10px;
              height: 10px;
              background: currentColor;
              border-radius: 50%;
              animation: pulse 2s infinite;
            "></div>
            <span>${status.label}</span>
          </div>
        </div>
        
        ${data.primary_reference ? this.renderReference(data) : this.renderNoReference()}
        ${data.best_d_clock ? this.renderDClock(data.best_d_clock) : ''}
        ${this.renderChannelBreakdown(data)}
        ${this.options.showAdoptions && data.recent_adoptions?.length > 0 ? this.renderAdoptions(data.recent_adoptions) : ''}
      </div>
      
      <style>
        @keyframes pulse {
          0%, 100% { opacity: 1; transform: scale(1); }
          50% { opacity: 0.6; transform: scale(0.9); }
        }
      </style>
    `;
  }
  
  renderReference(data) {
    const ref = data.primary_reference;
    return `
      <div style="
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
        gap: 15px;
        margin-top: 15px;
      ">
        <div>
          <div style="font-size: 11px; color: #94a3b8; text-transform: uppercase; margin-bottom: 4px;">Reference</div>
          <div style="font-size: 16px; font-weight: 600; color: #fff;">${ref.channel}</div>
        </div>
        <div>
          <div style="font-size: 11px; color: #94a3b8; text-transform: uppercase; margin-bottom: 4px;">Station</div>
          <div style="font-size: 16px; font-weight: 600; color: #fff;">${ref.station}</div>
        </div>
        <div>
          <div style="font-size: 11px; color: #94a3b8; text-transform: uppercase; margin-bottom: 4px;">Precision</div>
          <div style="font-size: 16px; font-weight: 600; color: #fff;">±${data.precision_estimate_ms} ms</div>
        </div>
        <div>
          <div style="font-size: 11px; color: #94a3b8; text-transform: uppercase; margin-bottom: 4px;">Age</div>
          <div style="font-size: 16px; font-weight: 600; color: #fff;">${this.formatAge(ref.age_seconds)}</div>
        </div>
        <div>
          <div style="font-size: 11px; color: #94a3b8; text-transform: uppercase; margin-bottom: 4px;">Confidence</div>
          <div style="font-size: 16px; font-weight: 600; color: #fff;">${(ref.confidence * 100).toFixed(0)}%</div>
        </div>
        <div>
          <div style="font-size: 11px; color: #94a3b8; text-transform: uppercase; margin-bottom: 4px;">UTC Time</div>
          <div style="font-size: 13px; font-weight: 600; color: #fff;">${this.formatUTC(ref.time_snap_utc)}</div>
        </div>
      </div>
    `;
  }
  
  renderNoReference() {
    return `
      <div style="
        margin-top: 15px;
        padding: 12px;
        background: rgba(239, 68, 68, 0.1);
        border-left: 4px solid #ef4444;
        border-radius: 4px;
        color: #fca5a5;
      ">
        ⚠️ No timing reference available
      </div>
    `;
  }
  
  renderChannelBreakdown(data) {
    const breakdown = data.channel_breakdown;
    return `
      <div style="
        margin-top: 15px;
        padding-top: 15px;
        border-top: 1px solid rgba(255, 255, 255, 0.2);
        font-size: 14px;
      ">
        <strong>Channel Status:</strong>
        <span style="color: #10b981; margin-left: 8px;">🟢 ${breakdown.tone_locked} tone-locked</span>
        <span style="color: #f59e0b; margin-left: 8px;">🟡 ${breakdown.ntp_synced} NTP-synced</span>
        ${breakdown.interpolated > 0 ? `<span style="color: #fb923c; margin-left: 8px;">🟠 ${breakdown.interpolated} interpolated</span>` : ''}
        ${breakdown.wall_clock > 0 ? `<span style="color: #ef4444; margin-left: 8px;">🔴 ${breakdown.wall_clock} wall-clock</span>` : ''}
      </div>
    `;
  }
  
  renderDClock(dClock) {
    const gradeColors = { 'A': '#22c55e', 'B': '#3b82f6', 'C': '#eab308', 'D': '#f97316', 'X': '#ef4444' };
    const gradeColor = gradeColors[dClock.quality_grade] || '#94a3b8';
    const dClockStr = `${dClock.d_clock_ms >= 0 ? '+' : ''}${dClock.d_clock_ms.toFixed(3)} ms`;
    
    return `
      <div style="
        margin-top: 15px;
        padding: 12px;
        background: rgba(59, 130, 246, 0.1);
        border-radius: 6px;
        border-left: 4px solid ${gradeColor};
      ">
        <div style="display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 10px;">
          <div>
            <div style="font-size: 11px; color: #94a3b8; text-transform: uppercase;">D_clock (Phase 2)</div>
            <div style="font-size: 20px; font-weight: 700; color: ${gradeColor}; font-family: monospace;">
              ${dClockStr}
            </div>
            <div style="font-size: 11px; color: #94a3b8;">Channel: ${dClock.channel}</div>
          </div>
          <div style="display: flex; gap: 16px; text-align: center;">
            <div>
              <div style="font-size: 18px; font-weight: 600; color: ${gradeColor};">${dClock.quality_grade}</div>
              <div style="font-size: 10px; color: #94a3b8;">Grade</div>
            </div>
            <div>
              <div style="font-size: 18px; font-weight: 600; color: #e0e0e0;">${dClock.station || '—'}</div>
              <div style="font-size: 10px; color: #94a3b8;">Station</div>
            </div>
            ${dClock.snr_db ? `
            <div>
              <div style="font-size: 18px; font-weight: 600; color: #e0e0e0;">${dClock.snr_db.toFixed(1)}</div>
              <div style="font-size: 10px; color: #94a3b8;">SNR dB</div>
            </div>
            ` : ''}
          </div>
        </div>
      </div>
    `;
  }
  
  renderAdoptions(adoptions) {
    const adoptionsHTML = adoptions.slice(0, 3).map(adoption => `
      <div style="
        display: flex;
        align-items: center;
        gap: 8px;
        padding: 6px 0;
        border-bottom: 1px solid rgba(255, 255, 255, 0.1);
        font-size: 12px;
      ">
        <span style="font-size: 16px;">✅</span>
        <div style="flex: 1; color: #cbd5e1;">
          <strong>${adoption.channel}:</strong> ${adoption.reason}
          ${adoption.improvement_ms ? ` (improved ${adoption.improvement_ms.toFixed(1)} ms)` : ''}
        </div>
        <div style="color: #94a3b8; font-size: 11px;">${this.formatRelativeTime(adoption.timestamp)}</div>
      </div>
    `).join('');
    
    return `
      <div style="
        margin-top: 15px;
        padding: 12px;
        background: rgba(0, 0, 0, 0.2);
        border-radius: 6px;
      ">
        <div style="font-weight: 600; margin-bottom: 8px; font-size: 13px;">Recent Time_Snap Adoptions</div>
        ${adoptionsHTML}
      </div>
    `;
  }
  
  renderLoading() {
    return `
      <div style="
        background: linear-gradient(135deg, #1e3a8a 0%, #312e81 100%);
        border-radius: 10px;
        padding: 40px;
        text-align: center;
        color: #94a3b8;
      ">
        Loading timing status...
      </div>
    `;
  }
  
  renderError(message) {
    this.container.innerHTML = `
      <div style="
        background: linear-gradient(135deg, #7f1d1d 0%, #991b1b 100%);
        border-radius: 10px;
        padding: 20px;
        color: #fca5a5;
      ">
        <strong>⚠️ Error loading timing status:</strong> ${message}
      </div>
    `;
  }
  
  formatAge(seconds) {
    if (seconds < 60) return `${seconds}s ago`;
    if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
    return `${Math.floor(seconds / 3600)}h ago`;
  }
  
  formatUTC(isoString) {
    try {
      return new Date(isoString).toISOString().replace('T', ' ').slice(0, 19);
    } catch {
      return isoString;
    }
  }
  
  formatRelativeTime(isoString) {
    try {
      const seconds = Math.floor((Date.now() - new Date(isoString)) / 1000);
      return this.formatAge(seconds);
    } catch {
      return 'unknown';
    }
  }
}

// Export for module usage
if (typeof module !== 'undefined' && module.exports) {
  module.exports = TimingStatusWidget;
}

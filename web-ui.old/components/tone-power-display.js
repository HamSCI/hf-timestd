/**
 * Tone Power Display Component - Shows 1000/1200 Hz tone powers
 * 
 * Usage:
 *   <div id="tonePowers"></div>
 *   <script src="components/tone-power-display.js"></script>
 *   <script>new TonePowerDisplay('tonePowers');</script>
 */

class TonePowerDisplay {
  constructor(containerId, options = {}) {
    this.container = document.getElementById(containerId);
    this.options = {
      updateInterval: options.updateInterval || 30000, // 30 seconds
      showChannelNames: options.showChannelNames !== false,
      compact: options.compact || false
    };
    
    if (!this.container) {
      console.error(`TonePowerDisplay: Container '${containerId}' not found`);
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
      const response = await fetch('/api/v1/tones/current');
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      
      const data = await response.json();
      this.render(data);
    } catch (err) {
      console.error('TonePowerDisplay: Update failed:', err);
      this.renderError(err.message);
    }
  }
  
  render(data) {
    if (data.loading) {
      this.container.innerHTML = this.renderLoading();
      return;
    }
    
    const channelsHTML = data.channels.map(ch => this.renderChannel(ch)).join('');
    
    this.container.innerHTML = `
      <div style="
        background: #1e293b;
        border-radius: 10px;
        padding: 20px;
        color: #e0e0e0;
      ">
        <div style="
          font-size: 18px;
          font-weight: 600;
          color: #fff;
          margin-bottom: 15px;
          display: flex;
          justify-content: space-between;
          align-items: center;
        ">
          <span>📊 Tone Power Levels</span>
          <span style="font-size: 12px; color: #94a3b8; font-weight: normal;">
            Updated: ${this.formatTime(data.last_updated)}
          </span>
        </div>
        
        <div style="display: flex; flex-direction: column; gap: 15px;">
          ${channelsHTML}
        </div>
      </div>
      
      <style>
        .tone-bar-fill {
          transition: width 0.3s ease;
        }
      </style>
    `;
  }
  
  renderChannel(channel) {
    if (channel.status !== 'OK' || channel.tone_1000_hz_db === null) {
      return `
        <div style="
          padding: 12px;
          background: #0f172a;
          border-radius: 6px;
          border-left: 4px solid #64748b;
        ">
          <div style="font-weight: 600; margin-bottom: 4px;">${channel.channel}</div>
          <div style="color: #94a3b8; font-size: 13px;">No tone data available</div>
        </div>
      `;
    }
    
    // Calculate bar widths (normalize to -20 to +40 dB range)
    const wwvPercent = Math.min(100, Math.max(0, ((channel.tone_1000_hz_db + 20) / 60) * 100));
    const wwvhPercent = channel.tone_1200_hz_db !== null 
      ? Math.min(100, Math.max(0, ((channel.tone_1200_hz_db + 20) / 60) * 100))
      : 0;
    
    const hasWWVH = channel.tone_1200_hz_db !== null;
    const dominantStation = hasWWVH && channel.tone_1200_hz_db > channel.tone_1000_hz_db ? 'WWVH' : 'WWV';
    
    return `
      <div style="
        padding: 15px;
        background: #0f172a;
        border-radius: 6px;
        border-left: 4px solid ${dominantStation === 'WWV' ? '#3b82f6' : '#f59e0b'};
      ">
        ${this.options.showChannelNames ? `
          <div style="
            font-weight: 600;
            margin-bottom: 10px;
            font-size: 15px;
            display: flex;
            justify-content: space-between;
            align-items: center;
          ">
            <span>${channel.channel}</span>
            ${hasWWVH ? `
              <span style="
                font-size: 12px;
                padding: 4px 8px;
                background: ${dominantStation === 'WWV' ? '#1e40af' : '#92400e'};
                border-radius: 4px;
              ">
                ${dominantStation} stronger (${Math.abs(channel.ratio_db).toFixed(1)} dB)
              </span>
            ` : ''}
          </div>
        ` : ''}
        
        <div style="display: flex; flex-direction: column; gap: 8px;">
          <!-- 1000 Hz (WWV) -->
          <div style="display: flex; align-items: center; gap: 8px;">
            <div style="width: 80px; font-size: 12px; color: #94a3b8;">1000 Hz</div>
            <div style="
              flex: 1;
              height: 20px;
              background: #1e293b;
              border-radius: 4px;
              position: relative;
              overflow: hidden;
            ">
              <div class="tone-bar-fill" style="
                height: 100%;
                width: ${wwvPercent}%;
                background: linear-gradient(90deg, #3b82f6, #60a5fa);
              "></div>
            </div>
            <div style="
              width: 70px;
              text-align: right;
              font-weight: 600;
              color: #60a5fa;
              font-size: 14px;
            ">
              ${channel.tone_1000_hz_db.toFixed(1)} dB
            </div>
          </div>
          
          ${hasWWVH ? `
          <!-- 1200 Hz (WWVH) -->
          <div style="display: flex; align-items: center; gap: 8px;">
            <div style="width: 80px; font-size: 12px; color: #94a3b8;">1200 Hz</div>
            <div style="
              flex: 1;
              height: 20px;
              background: #1e293b;
              border-radius: 4px;
              position: relative;
              overflow: hidden;
            ">
              <div class="tone-bar-fill" style="
                height: 100%;
                width: ${wwvhPercent}%;
                background: linear-gradient(90deg, #f59e0b, #fbbf24);
              "></div>
            </div>
            <div style="
              width: 70px;
              text-align: right;
              font-weight: 600;
              color: #fbbf24;
              font-size: 14px;
            ">
              ${channel.tone_1200_hz_db.toFixed(1)} dB
            </div>
          </div>
          ` : ''}
        </div>
      </div>
    `;
  }
  
  renderLoading() {
    return `
      <div style="
        background: #1e293b;
        border-radius: 10px;
        padding: 40px;
        text-align: center;
        color: #94a3b8;
      ">
        Loading tone power data...
      </div>
    `;
  }
  
  renderError(message) {
    this.container.innerHTML = `
      <div style="
        background: #7f1d1d;
        border-radius: 10px;
        padding: 20px;
        color: #fca5a5;
      ">
        <strong>⚠️ Error loading tone powers:</strong> ${message}
      </div>
    `;
  }
  
  formatTime(isoString) {
    try {
      const date = new Date(isoString);
      return date.toLocaleTimeString();
    } catch {
      return 'unknown';
    }
  }
}

// Export for module usage
if (typeof module !== 'undefined' && module.exports) {
  module.exports = TonePowerDisplay;
}

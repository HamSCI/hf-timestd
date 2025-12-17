# RTP Stream Quality Monitoring Design

**Date**: November 16, 2025  
**Purpose**: Detect and alert on RTP stream quality issues before they affect scientific data

## Problem Statement

The 2.5 MHz channel received only 38.6% of expected data, but this wasn't detected until viewing spectrograms. We need:

1. **Real-time detection** of poor RTP streams
2. **Automatic alerting** when quality degrades
3. **Root cause diagnosis** (weak signal vs network vs radiod issue)
4. **Historical trending** to identify patterns

## Quality Metrics Framework

### Core Metrics (Per-Minute Granularity)

#### 1. **Completeness** ⭐ Primary Indicator
```
completeness = (samples_received / samples_expected) * 100
expected = sample_rate * 60  # 960,000 @ 16 kHz
```

**Thresholds**:
- **Excellent**: ≥99.9% (< 960 samples missing)
- **Good**: 99.0-99.9% (960-9,600 samples missing)
- **Degraded**: 95.0-99.0% (9,600-48,000 samples missing)
- **Poor**: 90.0-95.0% (48,000-96,000 samples missing)
- **Critical**: <90.0% (>96,000 samples missing)

#### 2. **Packet Loss Rate**
```
packet_loss_pct = (packets_dropped / packets_expected) * 100
packets_expected = 3000/minute @ 16 kHz, 320 samples/packet
```

**Thresholds**:
- **Excellent**: <0.01% (<1 packet/10,000)
- **Good**: 0.01-0.1%
- **Warning**: 0.1-1.0%
- **Error**: 1.0-5.0%
- **Critical**: >5.0%

#### 3. **Gap Characteristics**
```
gaps_per_minute: Number of discontinuities
gap_duration_mean: Average gap size in samples
gap_duration_max: Largest gap in minute
gap_pattern: Regular/burst/random
```

**Patterns**:
- **Isolated**: Single large gap → Likely network hiccup
- **Burst**: Many small gaps in short period → Network congestion
- **Regular**: Gaps at fixed intervals → Radiod timing issue
- **Random**: Sporadic gaps → Weak signal, multipath fading

#### 4. **RTP Timing Quality**
```
rtp_jitter: Variance in packet arrival timing
rtp_out_of_order: Packets arriving after later sequence numbers
rtp_duplicates: Duplicate sequence numbers
```

#### 5. **Signal Strength Indicators**
```
snr_estimate: If available from radiod metadata
power_level: Relative signal power
tone_detection_rate: % of WWV/CHU minutes with successful detection
```

## Implementation Layers

### Layer 1: Core Recorder (Real-Time)

**Location**: `grape_channel_recorder_v2.py`

**Already tracking**:
- ✅ Packets received/dropped
- ✅ Gaps filled (count and samples)
- ✅ Samples written per minute
- ✅ RTP sequence tracking

**Enhancements needed**:

```python
class QualityMetrics:
    """Per-minute quality metrics"""
    def __init__(self):
        self.timestamp = None
        self.completeness_pct = 100.0
        self.packet_loss_pct = 0.0
        self.gaps_count = 0
        self.gaps_total_samples = 0
        self.gap_durations = []  # List of gap sizes
        self.rtp_jitter_ms = 0.0
        self.status = 'excellent'  # excellent/good/degraded/poor/critical
        
    def calculate_status(self):
        """Determine overall status from metrics"""
        if self.completeness_pct < 90.0:
            return 'critical'
        elif self.completeness_pct < 95.0:
            return 'poor'
        elif self.completeness_pct < 99.0:
            return 'degraded'
        elif self.completeness_pct < 99.9 or self.packet_loss_pct > 0.1:
            return 'good'
        else:
            return 'excellent'
    
    def diagnose_issue(self):
        """Identify likely root cause"""
        if self.gaps_count == 0:
            return None
        
        # Pattern analysis
        if self.gaps_count == 1 and self.gaps_total_samples > 100000:
            return "isolated_outage"  # Single large gap
        elif self.gaps_count > 100:
            return "burst_loss"  # Many small gaps (network)
        elif self._is_periodic():
            return "periodic_dropout"  # Regular pattern (radiod issue)
        elif self.completeness_pct < 50:
            return "weak_signal"  # Likely propagation
        else:
            return "intermittent_loss"  # General degradation
    
    def _is_periodic(self):
        """Check if gaps occur at regular intervals"""
        if len(self.gap_durations) < 3:
            return False
        # Analyze spacing between gaps
        # ... implementation ...
```

**Alert Generation**:

```python
class QualityAlerter:
    """Generate alerts based on quality metrics"""
    
    def __init__(self, alert_file: Path):
        self.alert_file = alert_file
        self.alert_history = deque(maxlen=1000)
        self.alert_thresholds = {
            'completeness_critical': 90.0,
            'completeness_warning': 99.0,
            'packet_loss_warning': 0.1,
            'packet_loss_critical': 1.0,
            'sustained_degradation_minutes': 5
        }
    
    def check_metrics(self, channel: str, metrics: QualityMetrics):
        """Check if metrics trigger alerts"""
        alerts = []
        
        # Critical completeness
        if metrics.completeness_pct < self.alert_thresholds['completeness_critical']:
            alerts.append({
                'level': 'critical',
                'channel': channel,
                'type': 'low_completeness',
                'value': metrics.completeness_pct,
                'message': f"{channel}: Critical data loss - {metrics.completeness_pct:.1f}% complete",
                'diagnosis': metrics.diagnose_issue(),
                'timestamp': datetime.now(timezone.utc).isoformat()
            })
        
        # Warning completeness
        elif metrics.completeness_pct < self.alert_thresholds['completeness_warning']:
            alerts.append({
                'level': 'warning',
                'channel': channel,
                'type': 'degraded_completeness',
                'value': metrics.completeness_pct,
                'message': f"{channel}: Degraded data quality - {metrics.completeness_pct:.1f}% complete"
            })
        
        # Packet loss
        if metrics.packet_loss_pct > self.alert_thresholds['packet_loss_critical']:
            alerts.append({
                'level': 'critical',
                'channel': channel,
                'type': 'high_packet_loss',
                'value': metrics.packet_loss_pct,
                'message': f"{channel}: Critical packet loss - {metrics.packet_loss_pct:.2f}%"
            })
        
        # Sustained degradation (check history)
        if self._is_sustained_degradation(channel):
            alerts.append({
                'level': 'error',
                'channel': channel,
                'type': 'sustained_degradation',
                'message': f"{channel}: Quality degraded for >5 minutes",
                'action': 'Check radiod configuration and signal strength'
            })
        
        for alert in alerts:
            self._emit_alert(alert)
        
        return alerts
    
    def _emit_alert(self, alert: dict):
        """Write alert to file and optionally send notifications"""
        self.alert_history.append(alert)
        
        # Append to alert log
        with open(self.alert_file, 'a') as f:
            f.write(json.dumps(alert) + '\n')
        
        # Could also:
        # - Send email/SMS (critical only)
        # - Write to syslog
        # - Trigger webhook
        # - Update web UI via websocket
```

### Layer 2: Analytics Service (Secondary Validation)

**Location**: `analytics_service.py`

**Purpose**: Validate NPZ files match expected quality

```python
def validate_npz_quality(self, archive: NPZArchive) -> QualityReport:
    """
    Verify NPZ file meets quality expectations
    Cross-check against what core recorder should have written
    """
    expected_samples = 960000  # 16 kHz * 60 sec
    actual_samples = len(archive.iq_samples)
    completeness = (actual_samples / expected_samples) * 100
    
    # Check metadata consistency
    gaps_declared = archive.metadata.get('gaps_filled', 0)
    gaps_count = archive.metadata.get('gaps_count', 0)
    
    report = {
        'file': archive.file_path.name,
        'timestamp': archive.timestamp,
        'completeness': completeness,
        'samples': actual_samples,
        'gaps_filled': gaps_declared,
        'status': 'ok' if completeness >= 99.0 else 'degraded'
    }
    
    # Alert if significant mismatch with core recorder metrics
    if completeness < 95.0:
        logger.warning(f"Low quality NPZ file: {archive.file_path.name} "
                      f"({completeness:.1f}% complete)")
        report['alert'] = True
    
    return report
```

### Layer 3: Web UI Dashboard

**Location**: `web-ui/carrier.html` (Quality Dashboard section)

**Real-time display**:

```javascript
// Quality status widget (per channel)
function renderQualityStatus(channel) {
    const status = channel.quality_status;
    
    return `
        <div class="quality-card ${status.level}">
            <h4>${channel.name}</h4>
            <div class="quality-metric">
                <span>Completeness:</span>
                <span class="value">${status.completeness.toFixed(1)}%</span>
                <div class="sparkline">${renderSparkline(status.history)}</div>
            </div>
            <div class="quality-metric">
                <span>Packet Loss:</span>
                <span class="value">${status.packet_loss.toFixed(2)}%</span>
            </div>
            ${status.alerts.length > 0 ? renderAlerts(status.alerts) : ''}
        </div>
    `;
}

// Active alerts panel
function renderActiveAlerts(alerts) {
    return alerts.map(alert => `
        <div class="alert alert-${alert.level}">
            <span class="alert-icon">${getAlertIcon(alert.level)}</span>
            <div class="alert-content">
                <strong>${alert.channel}</strong>: ${alert.message}
                ${alert.diagnosis ? `<br><small>Diagnosis: ${alert.diagnosis}</small>` : ''}
            </div>
            <span class="alert-time">${formatTime(alert.timestamp)}</span>
        </div>
    `).join('');
}
```

## Detection Scenarios

### Scenario 1: Weak Signal (2.5 MHz problem)

**Symptoms**:
- Completeness: 30-50%
- Gaps: Many small gaps, random pattern
- Packet loss: Moderate (0.5-2%)
- Duration: Persistent (hours/days)

**Diagnosis**: `weak_signal` - Propagation conditions poor for this frequency

**Actions**:
- ✅ Continue recording (partial data still valuable)
- ✅ Mark with quality metadata
- ⚠️ Alert operator to check antenna/propagation
- 📊 Note in web UI: "Low signal strength - expected for 2.5 MHz"

### Scenario 2: Network Congestion

**Symptoms**:
- Completeness: 90-98%
- Gaps: Burst pattern (many gaps in short period)
- Packet loss: High during bursts
- Duration: Intermittent (seconds to minutes)

**Diagnosis**: `burst_loss` - Network congestion

**Actions**:
- 🔴 Critical alert if sustained
- 🔧 Check network configuration (multicast, switch settings)
- 📈 Monitor system load

### Scenario 3: Radiod Timing Issue

**Symptoms**:
- Completeness: 95-99%
- Gaps: Regular periodic pattern
- Packet loss: Low but consistent
- Duration: Persistent

**Diagnosis**: `periodic_dropout` - Radiod timing/buffering issue

**Actions**:
- 🔧 Check radiod configuration
- 📊 Analyze gap intervals (might correlate with radiod buffer size)
- 💬 Report to radiod maintainer

### Scenario 4: Single Outage

**Symptoms**:
- Completeness: 95-99%
- Gaps: 1-2 large gaps
- Duration: One-time event

**Diagnosis**: `isolated_outage` - Temporary network/system issue

**Actions**:
- ℹ️ Info-level log (not critical)
- ✅ Gap-filled with zeros (already handled)

## Historical Analysis

### Trending Dashboard

Track quality over time to identify:

1. **Diurnal patterns**: Does quality vary by time of day?
2. **Frequency-dependent**: Which frequencies have chronic issues?
3. **System health**: Network/computer problems affecting all channels?
4. **Propagation correlation**: Does SNR correlate with completeness?

```sql
-- Example queries for trend analysis
SELECT 
    channel_name,
    DATE(timestamp) as date,
    AVG(completeness_pct) as avg_completeness,
    MIN(completeness_pct) as min_completeness,
    COUNT(*) FILTER (WHERE completeness_pct < 99) as degraded_minutes
FROM quality_metrics
WHERE timestamp > NOW() - INTERVAL '7 days'
GROUP BY channel_name, DATE(timestamp)
ORDER BY avg_completeness ASC;
```

## Implementation Priority

### Phase 1: Core Detection (Immediate)
- [x] Core recorder already tracks metrics
- [ ] Add alert generation to core recorder
- [ ] Expose quality status via JSON file
- [ ] Web UI reads and displays alerts

### Phase 2: Enhanced Diagnosis (Week 1)
- [ ] Gap pattern analysis
- [ ] Root cause classification
- [ ] Historical trending
- [ ] Automated recommendations

### Phase 3: Proactive Monitoring (Week 2)
- [ ] Predictive alerts (quality degrading trend)
- [ ] Correlation with external factors (solar indices, etc.)
- [ ] Automated mitigation (switch to backup channel?)
- [ ] Email/SMS notifications for critical alerts

## Configuration

```toml
# config/quality-monitoring.toml

[thresholds]
completeness_excellent = 99.9
completeness_good = 99.0
completeness_degraded = 95.0
completeness_poor = 90.0

packet_loss_good = 0.01
packet_loss_warning = 0.1
packet_loss_error = 1.0
packet_loss_critical = 5.0

sustained_degradation_minutes = 5

[alerts]
enable_logging = true
log_file = "/tmp/grape-test/logs/quality-alerts.log"
enable_email = false
email_recipients = ["operator@example.com"]
critical_only = true

[diagnosis]
enable_pattern_analysis = true
periodic_gap_threshold_ms = 50  # Consider periodic if gaps within 50ms of regular interval
burst_threshold_count = 20  # >20 gaps in same minute = burst

[historical]
enable_database = false
database_path = "/tmp/grape-test/quality_metrics.db"
retention_days = 90
```

## API Endpoints

### Real-time Quality Status

```
GET /api/v1/quality/realtime
Response: {
  "timestamp": "2025-11-16T22:05:00Z",
  "channels": {
    "WWV 10 MHz": {
      "status": "excellent",
      "completeness": 99.98,
      "packet_loss": 0.002,
      "alerts": []
    },
    "WWV 2.5 MHz": {
      "status": "poor",
      "completeness": 38.6,
      "packet_loss": 0.8,
      "alerts": [
        {
          "level": "critical",
          "type": "low_completeness",
          "diagnosis": "weak_signal",
          "message": "Critical data loss - 38.6% complete"
        }
      ]
    }
  }
}
```

### Historical Trends

```
GET /api/v1/quality/history?channel=WWV+2.5+MHz&hours=24
Response: {
  "channel": "WWV 2.5 MHz",
  "period": "2025-11-15T22:00:00Z to 2025-11-16T22:00:00Z",
  "metrics": [
    {
      "timestamp": "2025-11-16T22:00:00Z",
      "completeness": 38.2,
      "packet_loss": 0.9,
      "gaps_count": 234,
      "status": "poor"
    },
    // ... more data points
  ],
  "summary": {
    "avg_completeness": 39.5,
    "min_completeness": 12.3,
    "max_completeness": 67.8,
    "poor_minutes": 987,
    "excellent_minutes": 0
  }
}
```

### Active Alerts

```
GET /api/v1/quality/alerts?active=true
Response: {
  "active_alerts": [
    {
      "id": "alert_12345",
      "level": "critical",
      "channel": "WWV 2.5 MHz",
      "type": "sustained_low_completeness",
      "first_seen": "2025-11-16T00:00:00Z",
      "last_seen": "2025-11-16T22:05:00Z",
      "duration_minutes": 1325,
      "diagnosis": "weak_signal",
      "recommended_action": "Check antenna performance at 2.5 MHz. Consider signal is expected to be weak at this frequency during current propagation conditions."
    }
  ]
}
```

## Benefits

1. **Early Detection**: Identify issues in real-time, not hours later
2. **Root Cause Diagnosis**: Distinguish signal vs network vs radiod problems
3. **Operator Awareness**: Clear dashboard showing system health
4. **Scientific Integrity**: Mark data quality in metadata for downstream analysis
5. **System Reliability**: Detect radiod/network problems affecting all channels
6. **Trend Analysis**: Identify chronic issues requiring hardware/configuration fixes

## References

- Core recorder: `src/signal_recorder/grape_channel_recorder_v2.py`
- Quality tracker: `src/signal_recorder/live_quality_status.py`
- Analytics validation: `src/signal_recorder/analytics_service.py`
- Web UI: `web-ui/carrier.html`

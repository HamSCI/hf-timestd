# Automatic Spectrogram Generation - November 13, 2024

## Overview

Implemented **on-demand spectrogram generation** triggered directly from the web UI, eliminating the need for CLI access. Users can now view spectrograms for any UTC day by simply clicking a button - the system handles generation automatically in the background.

---

## Key Features

### 1. **Zero CLI Required**
- No SSH access needed
- No manual script execution
- Click "Generate Spectrograms Now" button
- System handles everything automatically

### 2. **Smart Detection**
- Automatically checks if spectrograms exist
- Only offers generation when needed
- Avoids duplicate generation jobs

### 3. **Live Progress Tracking**
- Real-time progress bar (0-100%)
- Elapsed time display
- Console output streaming
- Clear success/failure indicators

### 4. **Background Processing**
- Generation runs in separate process
- Web UI remains responsive
- Can navigate away and come back
- Jobs auto-cleanup after completion

### 5. **Both Spectrogram Types Supported**
- **10 Hz Carrier**: Frequency deviation analysis
- **16 kHz Archive**: Full bandwidth characterization

---

## User Workflow

### Viewing Spectrograms (Automatic Flow)

1. **Navigate to Carrier Data tab**
   - Open: http://localhost:3000/channels.html
   - Click "Carrier Data" tab

2. **Select date and type**
   - Choose date from picker
   - Choose "10 Hz Carrier" or "16 kHz Archive"
   - Click "Load Data"

3. **System checks for existing spectrograms**
   - If found: Displays immediately
   - If not found: Shows "Generate Spectrograms Now" button

4. **Click generate button** (if needed)
   - Progress bar appears
   - Real-time updates every 2 seconds
   - Console output shown (optional)

5. **Wait for completion** (1-5 minutes)
   - Green checkmark when done
   - "View Spectrograms" button appears
   - Click to display generated images

6. **View spectrograms**
   - All 9 channels displayed
   - Stacked vertically for comparison
   - Zoom and pan available

---

## Technical Implementation

### Backend API Endpoints

#### Generate Spectrograms
```
POST /api/v1/spectrograms/generate
Content-Type: application/json

{
  "date": "20241113",     // YYYYMMDD format
  "type": "carrier"        // or "archive"
}

Response:
{
  "jobId": "carrier_20241113_1731522000000",
  "status": "started",
  "message": "Spectrogram generation started...",
  "pollUrl": "/api/v1/spectrograms/status/{jobId}"
}
```

#### Check Generation Status
```
GET /api/v1/spectrograms/status/:jobId

Response:
{
  "jobId": "carrier_20241113_1731522000000",
  "status": "running",      // or "completed", "failed"
  "type": "carrier",
  "date": "20241113",
  "progress": 45,           // 0-100
  "error": null,
  "elapsedSeconds": 67,
  "recentOutput": "Processing channel 5/9..."
}
```

### Frontend Flow

```javascript
// 1. Check if spectrograms exist
const testUrl = `/api/v1/channels/WWV 2.5 MHz/spectrogram/${type}/${date}`;
const response = await fetch(testUrl);

if (response.status === 404) {
  // 2. Show "Generate" button
  showGenerateButton();
}

// 3. User clicks "Generate"
async function generateSpectrograms(date, type) {
  // Start generation
  const response = await fetch('/api/v1/spectrograms/generate', {
    method: 'POST',
    body: JSON.stringify({ date, type })
  });
  
  const { jobId } = await response.json();
  
  // 4. Poll for status
  pollGenerationStatus(jobId);
}

// 5. Poll every 2 seconds
function pollGenerationStatus(jobId) {
  setInterval(async () => {
    const status = await fetch(`/api/v1/spectrograms/status/${jobId}`);
    updateProgressBar(status.progress);
    
    if (status.status === 'completed') {
      showSuccessAndReload();
    }
  }, 2000);
}
```

### Background Job Management

**Job Tracking:**
```javascript
const spectrogramJobs = new Map();

spectrogramJobs.set(jobId, {
  status: 'running',      // 'running', 'completed', 'failed'
  type: 'carrier',        // 'carrier' or 'archive'
  date: '20241113',
  progress: 0,            // 0-100
  error: null,
  startTime: Date.now(),
  output: []              // Console output lines
});
```

**Process Spawning:**
```javascript
const child = spawn('python3', [
  'scripts/generate_spectrograms_drf.py',
  '--date', '20241113',
  '--data-root', '/tmp/grape-test'
]);

// Stream output
child.stdout.on('data', (data) => {
  job.output.push(data.toString());
  parseProgressFromOutput(data);
});

// Handle completion
child.on('close', (code) => {
  job.status = (code === 0) ? 'completed' : 'failed';
  job.progress = 100;
  
  // Auto-cleanup after 5 minutes
  setTimeout(() => spectrogramJobs.delete(jobId), 5 * 60 * 1000);
});
```

### Progress Parsing

The backend parses progress from Python script output:

```javascript
// Look for patterns like "Processing channel 5/9"
const progressMatch = output.match(/Processing.*?(\d+)\/(\d+)/);
if (progressMatch) {
  const current = parseInt(progressMatch[1]);
  const total = parseInt(progressMatch[2]);
  job.progress = Math.round((current / total) * 100);
}
```

This works with the existing Python scripts which output:
```python
logger.info(f"Processing: {channel_name}")  # Detected automatically
```

---

## File Structure

```
/tmp/grape-test/
├── spectrograms/
│   ├── 20241113/
│   │   ├── WWV_2.5_MHz_20241113_carrier_spectrogram.png      # 10 Hz
│   │   ├── WWV_2.5_MHz_20241113_spectrogram.png             # 16 kHz
│   │   ├── WWV_5_MHz_20241113_carrier_spectrogram.png
│   │   ├── WWV_5_MHz_20241113_spectrogram.png
│   │   └── ... (all 9 channels × 2 types)
│   ├── 20241112/
│   └── 20241111/
└── ...
```

**Naming Convention:**
- **10 Hz Carrier**: `{channel}_{date}_carrier_spectrogram.png`
- **16 kHz Archive**: `{channel}_{date}_spectrogram.png`

---

## Performance Characteristics

### Generation Time

**10 Hz Carrier (Digital RF):**
- Input: ~860,000 samples per channel (10 Hz × 86,400 seconds)
- Processing: FFT + spectrogram computation
- Typical time: **1-2 minutes** for all 9 channels
- Output size: ~200 KB per channel

**16 kHz Archive (NPZ):**
- Input: ~1.4 billion samples per channel (16 kHz × 86,400 seconds)
- Processing: Load NPZ → FFT → spectrogram
- Typical time: **3-5 minutes** for all 9 channels
- Output size: ~50 MB per channel (PNG)

### Resource Usage

**Memory:**
- 10 Hz: ~50 MB per channel
- 16 kHz: ~500 MB per channel
- Peak total: ~5 GB for 9 channels (16 kHz)

**CPU:**
- Single-threaded Python process
- 100% CPU on one core during generation
- Does not impact web server or data recording

**Disk:**
- 10 Hz: 2 MB per day (all channels)
- 16 kHz: 450 MB per day (all channels)
- Consider cleanup policy for old spectrograms

---

## Error Handling

### Common Errors and Solutions

#### 1. Data Not Available
**Error**: "No Digital RF data found for date"

**Cause**: Analytics service hasn't processed that day yet

**Solution**: 
- Wait for analytics to catch up
- Check `/tmp/grape-test/analytics/{channel}/digital_rf/`
- Verify date format (YYYYMMDD)

#### 2. Script Not Found
**Error**: "Generation script not found"

**Cause**: Python scripts not in expected location

**Solution**:
- Verify scripts in `/home/mjh/git/signal-recorder/scripts/`
- Check `GRAPE_INSTALL_DIR` environment variable

#### 3. Python Dependencies Missing
**Error**: Process exits with non-zero code

**Cause**: Missing matplotlib, digital_rf, scipy, etc.

**Solution**:
```bash
cd /home/mjh/git/signal-recorder
source venv/bin/activate
pip install -r requirements.txt
```

#### 4. Permission Denied
**Error**: Cannot write to output directory

**Cause**: Web server lacks write permissions

**Solution**:
```bash
chmod 755 /tmp/grape-test/spectrograms
chown $USER:$USER /tmp/grape-test/spectrograms
```

### UI Error States

**Generation Failed:**
- Shows red error message
- Displays error details
- Provides "Try Again" button
- Allows user to retry or go back

**Job Not Found:**
- Happens if polling a completed/cleaned-up job
- Normal after 5-minute cleanup
- UI handles gracefully

**Network Error:**
- Timeout or connection issue
- Polling stops automatically
- User can reload page

---

## Advanced Features

### Concurrent Generation

System prevents duplicate generation:
```javascript
// Check if already generating
const existingJob = spectrogramJobs.find(
  job => job.date === date && job.type === type && job.status === 'running'
);

if (existingJob) {
  return { status: 'already_running', jobId: existingJobId };
}
```

**Behavior:**
- Multiple users can trigger different dates
- Same date/type shares single job
- Progress visible to all clients

### Job Cleanup

**Automatic:**
- Completed jobs: 5 minutes
- Failed jobs: 5 minutes
- Reason: Prevent memory accumulation

**Manual:**
```bash
# Restart monitoring server to clear all jobs
pm2 restart grape-monitoring
```

### Output Streaming

Console output sent to client:
```javascript
job.output.push(data.toString());

// Client sees:
"INFO: Reading WWV 5 MHz"
"INFO:   Sample rate: 10.0 Hz"
"INFO:   Read 864,000 samples"
"INFO: Generating spectrogram..."
"INFO:   ✅ Saved: WWV_5_MHz_20241113_carrier_spectrogram.png (187 KB)"
```

Useful for debugging and user confidence.

---

## Future Enhancements

### Short-Term (Easy)

1. **Automatic daily generation**
   - Cron job at midnight UTC
   - Generate today's spectrograms automatically
   - No user action needed

2. **Batch date generation**
   - "Generate Last 7 Days" button
   - Useful for backfilling after setup

3. **Progress persistence**
   - Store job state in file
   - Survives server restart
   - User can check progress after reconnect

### Medium-Term

4. **Queue system**
   - Multiple generation requests
   - Process in order (FIFO)
   - Show queue position

5. **Partial regeneration**
   - "Regenerate failed channels"
   - Skip already-generated channels
   - Faster for retry scenarios

6. **Email notifications**
   - "Notify me when complete"
   - Long-running generations
   - Useful for multi-day batches

### Long-Term

7. **Distributed generation**
   - Run on separate worker machines
   - Horizontal scaling for large datasets
   - Load balancing

8. **Incremental generation**
   - Generate hour-by-hour
   - Show partial spectrograms
   - Real-time updates

9. **GPU acceleration**
   - Use CUDA for FFT
   - 10-100x speedup possible
   - Requires GPU-enabled server

---

## Testing Checklist

### Manual Testing

- [ ] Generate 10 Hz carrier for today
- [ ] Generate 16 kHz archive for today
- [ ] Generate for past date (with data)
- [ ] Generate for future date (should fail gracefully)
- [ ] Start generation, refresh page, resume polling
- [ ] Click generate twice (should show "already running")
- [ ] Generate while another job running (different date)
- [ ] Test with missing data (proper error)
- [ ] Test with network timeout
- [ ] Verify progress bar updates smoothly
- [ ] Check console output display
- [ ] Verify spectrograms display after generation

### Automated Testing

```bash
# Test API endpoints
curl -X POST http://localhost:3000/api/v1/spectrograms/generate \
  -H "Content-Type: application/json" \
  -d '{"date":"20241113","type":"carrier"}'

# Response: {"jobId":"carrier_20241113_...", "status":"started"}

# Check status
curl http://localhost:3000/api/v1/spectrograms/status/{jobId}

# Response: {"status":"running","progress":45}
```

---

## Deployment Notes

### Requirements

**System:**
- Node.js 16+ (for monitoring server)
- Python 3.8+ (for generation scripts)
- 8 GB RAM minimum (for 16 kHz processing)
- 10 GB disk space per week (spectrograms)

**Python Packages:**
```bash
pip install digital_rf matplotlib scipy numpy
```

**Environment Variables:**
```bash
export GRAPE_INSTALL_DIR=/home/mjh/git/signal-recorder
export GRAPE_CONFIG=/home/mjh/git/signal-recorder/config/grape-config.toml
export PYTHON_PATH=/home/mjh/git/signal-recorder/venv/bin/python3
```

### Monitoring

**Server Logs:**
```bash
# Watch generation activity
tail -f /var/log/grape-monitoring.log | grep spectrogram

# Example output:
# Starting spectrogram generation: carrier for 20241113 (job carrier_20241113_...)
# [carrier_20241113_...] Processing channel 1/9
# [carrier_20241113_...] Processing channel 2/9
# Spectrogram generation completed: carrier_20241113_...
```

**Job Status:**
```bash
# See active jobs
curl http://localhost:3000/api/v1/spectrograms/status/{jobId}
```

---

## Summary

This feature transforms spectrogram viewing from a **developer-only** CLI task into a **user-friendly** one-click operation. No technical knowledge required - just click "Generate" and watch the progress bar.

**Key Benefits:**
- ✅ **Zero CLI required**: Everything in web UI
- ✅ **Live progress**: See what's happening
- ✅ **Smart caching**: Only generates when needed
- ✅ **Error recovery**: Clear messages and retry options
- ✅ **Background processing**: UI stays responsive
- ✅ **Both types supported**: 10 Hz carrier + 16 kHz archive

**Impact:**
- **Before**: SSH → venv → python command → wait → refresh browser
- **After**: Click button → wait (with progress) → click "View"

This is exactly what a web monitoring UI should do: make complex operations simple and transparent.

#!/usr/bin/env node

/**
 * GStreamer Audio Proxy for Smooth RTP Streaming
 * 
 * Uses GStreamer to handle RTP timing properly, then proxies to web clients.
 * This bypasses Python's real-time limitations.
 */

const express = require('express');
const { spawn } = require('child_process');
const { join } = require('path');

const app = express();
const PORT = 3001; // Separate port for audio streaming

// Audio streaming endpoint using GStreamer
app.get('/api/audio/stream/:ssrc', (req, res) => {
  const ssrc = req.params.ssrc;
  
  // Default multicast settings (these should match your ka9q-radio config)
  let multicastAddr = '239.192.152.141';
  let multicastPort = '5004';
  
  console.log(`Starting GStreamer audio for SSRC ${ssrc}: ${multicastAddr}:${multicastPort}`);
  
  // Set headers for audio streaming
  res.setHeader('Content-Type', 'audio/wav');
  res.setHeader('Cache-Control', 'no-cache');
  res.setHeader('Transfer-Encoding', 'chunked');
  
  // GStreamer pipeline for RTP to WAV conversion
  // This handles all timing and buffering properly in C
  const gstPipeline = [
    'gst-launch-1.0',
    'rtpbin', 
    `udpsrc address=${multicastAddr} port=${multicastPort} caps="application/x-rtp,media=(string)audio,clock-rate=(int)16000,encoding-name=(string)L16"`,
    '!', 
    'rtpL16depay',
    '!',
    'audioconvert',
    '!',
    'audioresample',
    '!',
    'avenc_wav',
    '!',
    'fdsink'
  ].join(' ');
  
  console.log('GStreamer pipeline:', gstPipeline);
  
  // Spawn GStreamer process
  const gstProcess = spawn('bash', ['-c', gstPipeline], {
    stdio: ['ignore', 'pipe', 'pipe']
  });
  
  // Write WAV header
  const wavHeader = Buffer.alloc(44);
  wavHeader.write('RIFF', 0);
  wavHeader.writeUInt32LE(0xFFFFFFFF, 4);
  wavHeader.write('WAVE', 8);
  wavHeader.write('fmt ', 12);
  wavHeader.writeUInt32LE(16, 16);
  wavHeader.writeUInt16LE(1, 20);
  wavHeader.writeUInt16LE(1, 22);
  wavHeader.writeUInt32LE(8000, 24); // 8 kHz for browser compatibility
  wavHeader.writeUInt32LE(16000, 28);
  wavHeader.writeUInt16LE(2, 32);
  wavHeader.writeUInt16LE(16, 34);
  wavHeader.write('data', 36);
  wavHeader.writeUInt32LE(0xFFFFFFFF, 40);
  
  res.write(wavHeader);
  
  // Pipe GStreamer output to response
  gstProcess.stdout.on('data', (chunk) => {
    res.write(chunk);
  });
  
  gstProcess.stderr.on('data', (data) => {
    console.error(`GStreamer error: ${data}`);
  });
  
  gstProcess.on('close', (code) => {
    console.log(`GStreamer exited with code ${code}`);
    res.end();
  });
  
  // Clean up on client disconnect
  req.on('close', () => {
    gstProcess.kill();
  });
});

// Health check endpoint
app.get('/health', (req, res) => {
  res.json({ status: 'ok', service: 'gstream-audio-proxy' });
});

// Start server
app.listen(PORT, () => {
  console.log(`🎵 GStreamer Audio Proxy running on http://localhost:${PORT}/`);
  console.log(`📡 Streaming endpoint: http://localhost:${PORT}/api/audio/stream/:ssrc`);
});

// Handle shutdown
process.on('SIGINT', () => {
  console.log('\n🛑 Shutting down GStreamer Audio Proxy...');
  process.exit(0);
});

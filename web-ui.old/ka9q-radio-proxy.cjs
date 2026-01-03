#!/usr/bin/env node

/**
 * ka9q-radio Audio Proxy
 * 
 * Implements the same approach as ka9q-web:
 * 1. Send control commands to radiod to request audio
 * 2. Receive raw RTP packets from radiod output
 * 3. Forward to web clients via HTTP streaming
 * 
 * This bypasses Python's real-time limitations by using radiod's native streaming.
 */

const express = require('express');
const dgram = require('dgram');
const { spawn } = require('child_process');
const { EventEmitter } = require('events');

const app = express();
const PORT = 3001;

// RTP constants from ka9q-web
const RTP_MIN_SIZE = 12;
const CMD = 1;
const OUTPUT_SSRC = 2;
const RADIO_FREQUENCY = 3;
const COMMAND_TAG = 4;
const PRESET = 5;

class Ka9qRadioProxy extends EventEmitter {
  constructor() {
    super();
    this.controlSocket = null;
    this.audioSocket = null;
    this.sessionId = 1000;
    this.activeStreams = new Map();
    
    this.init();
  }

  init() {
    // Create control socket (connects to radiod control)
    this.controlSocket = dgram.createSocket('udp4');
    this.controlSocket.bind(() => {
      console.log(`Control socket bound to ${this.controlSocket.address().port}`);
    });

    // Create audio socket (receives RTP from radiod)
    this.audioSocket = dgram.createSocket('udp4');
    this.audioSocket.bind(5004, '0.0.0.0', () => {
      console.log(`Audio socket listening on 5004`);
      this.setupAudioReception();
    });

    // Don't connect control socket - we'll send to multicast address directly
    console.log('Control socket ready for radiod commands');
  }

  setupAudioReception() {
    this.audioSocket.on('message', (msg, rinfo) => {
      // Parse RTP header to get SSRC
      if (msg.length < RTP_MIN_SIZE) return;

      // Extract SSRC from RTP header (bytes 8-11)
      const ssrc = msg.readUInt32BE(8);
      
      // Forward to any active streams for this SSRC
      const stream = this.activeStreams.get(ssrc);
      if (stream && stream.response) {
        // Send raw RTP packet
        stream.response.write(msg);
      }
    });
  }

  startAudioStream(ssrc, frequency = 10000000) {
    console.log(`Starting audio stream for SSRC ${ssrc} at ${frequency} Hz`);
    
    // Send control command to radiod to start audio output
    const command = this.buildAudioCommand(ssrc, frequency);
    
    return new Promise((resolve, reject) => {
      this.controlSocket.send(command, 5006, '239.192.152.141', (err) => {
        if (err) {
          console.error('Failed to send control command:', err);
          reject(err);
        } else {
          console.log(`Audio command sent for SSRC ${ssrc}`);
          
          // Create stream object
          const stream = {
            ssrc,
            frequency,
            response: null,
            active: true
          };
          
          this.activeStreams.set(ssrc, stream);
          resolve(stream);
        }
      });
    });
  }

  stopAudioStream(ssrc) {
    console.log(`Stopping audio stream for SSRC ${ssrc}`);
    
    const stream = this.activeStreams.get(ssrc);
    if (stream) {
      stream.active = false;
      if (stream.response) {
        stream.response.end();
      }
      this.activeStreams.delete(ssrc);
    }
  }

  buildAudioCommand(ssrc, frequency) {
    // Build binary command using ka9q-web protocol
    const buffer = Buffer.alloc(128);
    let offset = 0;

    // Command byte
    buffer.writeUInt8(CMD, offset++);
    
    // Radio frequency
    buffer.writeUInt8(RADIO_FREQUENCY, offset++);
    buffer.writeUInt8(8, offset++); // 8-byte double
    buffer.writeDoubleBE(frequency, offset);
    offset += 8;
    
    // Output SSRC
    buffer.writeUInt8(OUTPUT_SSRC, offset++);
    buffer.writeUInt8(4, offset++); // 4-byte int
    buffer.writeUInt32BE(ssrc, offset);
    offset += 4;
    
    // Command tag
    buffer.writeUInt8(COMMAND_TAG, offset++);
    buffer.writeUInt8(4, offset++); // 4-byte int
    buffer.writeUInt32BE(Math.floor(Math.random() * 0xFFFFFFFF), offset);
    offset += 4;
    
    // End of list
    buffer.writeUInt8(0, offset++); // EOL
    
    return buffer.slice(0, offset);
  }

  setStreamResponse(ssrc, response) {
    const stream = this.activeStreams.get(ssrc);
    if (stream) {
      stream.response = response;
    }
  }
}

// Create proxy instance
const radioProxy = new Ka9qRadioProxy();

// Audio streaming endpoint
app.get('/api/audio/stream/:ssrc', async (req, res) => {
  const ssrc = parseInt(req.params.ssrc);
  
  console.log(`Audio stream request for SSRC ${ssrc}`);
  
  // Set headers for RTP streaming
  res.setHeader('Content-Type', 'application/octet-stream');
  res.setHeader('Cache-Control', 'no-cache');
  res.setHeader('Transfer-Encoding', 'chunked');
  
  try {
    // Start audio stream with radiod
    await radioProxy.startAudioStream(ssrc);
    
    // Register response with proxy
    radioProxy.setStreamResponse(ssrc, res);
    
    // Handle client disconnect
    req.on('close', () => {
      console.log(`Client disconnected for SSRC ${ssrc}`);
      radioProxy.stopAudioStream(ssrc);
    });
    
    // Handle timeout
    req.on('timeout', () => {
      console.log(`Request timeout for SSRC ${ssrc}`);
      radioProxy.stopAudioStream(ssrc);
    });
    
  } catch (error) {
    console.error('Failed to start audio stream:', error);
    res.status(500).json({ error: 'Failed to start audio stream' });
  }
});

// Health check
app.get('/health', (req, res) => {
  res.json({ 
    status: 'ok', 
    service: 'ka9q-radio-proxy',
    activeStreams: radioProxy.activeStreams.size
  });
});

// Start server
app.listen(PORT, () => {
  console.log(`🎵 ka9q-radio Audio Proxy running on http://localhost:${PORT}/`);
  console.log(`📡 Streaming endpoint: http://localhost:${PORT}/api/audio/stream/:ssrc`);
  console.log(`🔗 Using radiod control: 239.192.152.141:5006`);
  console.log(`🎧 Audio reception: 0.0.0.0:5004`);
});

// Handle shutdown
process.on('SIGINT', () => {
  console.log('\n🛑 Shutting down ka9q-radio Audio Proxy...');
  
  // Stop all active streams
  for (const [ssrc] of radioProxy.activeStreams) {
    radioProxy.stopAudioStream(ssrc);
  }
  
  // Close sockets
  if (radioProxy.controlSocket) radioProxy.controlSocket.close();
  if (radioProxy.audioSocket) radioProxy.audioSocket.close();
  
  process.exit(0);
});

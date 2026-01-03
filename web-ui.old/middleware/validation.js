/**
 * Input Validation Middleware
 * 
 * Provides validation functions for all user inputs to prevent injection attacks
 */

/**
 * Validate ham radio callsign format
 * Must be 3-8 alphanumeric characters
 */
export function validateCallsign(callsign) {
  if (!callsign || typeof callsign !== 'string') return false;
  if (callsign.length < 3 || callsign.length > 8) return false;
  return /^[A-Z0-9]+$/i.test(callsign);
}

/**
 * Validate SSRC (Synchronization Source Identifier)
 * Must be a positive 32-bit unsigned integer
 */
export function validateSSRC(ssrc) {
  const num = parseInt(ssrc, 10);
  return Number.isInteger(num) && num > 0 && num <= 0xFFFFFFFF;
}

/**
 * Validate frequency in Hz
 * Must be positive number in HF range (0-30 MHz)
 */
export function validateFrequency(freq) {
  const num = parseFloat(freq);
  return !isNaN(num) && num > 0 && num < 30e6;
}

/**
 * Validate process ID
 * Must be a positive integer less than system maximum
 */
export function validatePID(pid) {
  const num = parseInt(pid, 10);
  return Number.isInteger(num) && num > 0 && num < 32768;
}

/**
 * Validate grid square format (Maidenhead locator)
 * Format: AA00aa (e.g., EM38ww)
 */
export function validateGridSquare(grid) {
  if (!grid || typeof grid !== 'string') return false;
  if (grid.length < 4 || grid.length > 8) return false;
  // Basic format: two letters, two digits, optionally two more letters
  return /^[A-R]{2}[0-9]{2}([a-x]{2})?$/i.test(grid);
}

/**
 * Sanitize callsign - remove all non-alphanumeric characters
 */
export function sanitizeCallsign(callsign) {
  if (!callsign || typeof callsign !== 'string') return '';
  return callsign.replace(/[^A-Z0-9]/gi, '').toUpperCase();
}

/**
 * Sanitize filename - remove path traversal attempts
 */
export function sanitizeFilename(filename) {
  if (!filename || typeof filename !== 'string') return '';
  // Remove path separators and special characters
  return filename.replace(/[^a-z0-9._-]/gi, '');
}

/**
 * Validate configuration ID format
 */
export function validateConfigId(id) {
  if (!id || typeof id !== 'string') return false;
  // Allow alphanumeric and hyphens
  return /^[a-z0-9-]+$/i.test(id);
}

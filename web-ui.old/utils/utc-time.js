/**
 * UTC Time Formatting Utilities
 * 
 * All displays should use UTC for scientific consistency.
 * This module provides formatting functions that always output UTC.
 */

/**
 * Format a date/timestamp as UTC time string (HH:MM:SS)
 * @param {Date|number|string} input - Date object, Unix timestamp (ms or s), or ISO string
 * @returns {string} Time in HH:MM:SS UTC format
 */
function formatUTCTime(input) {
  const date = toDate(input);
  if (!date || isNaN(date.getTime())) return '--:--:--';
  
  return date.toISOString().substring(11, 19);
}

/**
 * Format a date/timestamp as UTC time with milliseconds (HH:MM:SS.mmm)
 * @param {Date|number|string} input - Date object, Unix timestamp, or ISO string
 * @returns {string} Time in HH:MM:SS.mmm UTC format
 */
function formatUTCTimeMs(input) {
  const date = toDate(input);
  if (!date || isNaN(date.getTime())) return '--:--:--.---';
  
  return date.toISOString().substring(11, 23);
}

/**
 * Format a date/timestamp as UTC date string (YYYY-MM-DD)
 * @param {Date|number|string} input - Date object, Unix timestamp, or ISO string
 * @returns {string} Date in YYYY-MM-DD UTC format
 */
function formatUTCDate(input) {
  const date = toDate(input);
  if (!date || isNaN(date.getTime())) return '----/--/--';
  
  return date.toISOString().substring(0, 10);
}

/**
 * Format a date/timestamp as UTC datetime (YYYY-MM-DD HH:MM:SS)
 * @param {Date|number|string} input - Date object, Unix timestamp, or ISO string
 * @returns {string} Datetime in YYYY-MM-DD HH:MM:SS UTC format
 */
function formatUTCDateTime(input) {
  const date = toDate(input);
  if (!date || isNaN(date.getTime())) return '----/--/-- --:--:--';
  
  return date.toISOString().replace('T', ' ').substring(0, 19) + ' UTC';
}

/**
 * Format a date/timestamp as short UTC datetime (MM-DD HH:MM)
 * @param {Date|number|string} input - Date object, Unix timestamp, or ISO string
 * @returns {string} Datetime in MM-DD HH:MM UTC format
 */
function formatUTCShort(input) {
  const date = toDate(input);
  if (!date || isNaN(date.getTime())) return '--/-- --:--';
  
  const iso = date.toISOString();
  return iso.substring(5, 10) + ' ' + iso.substring(11, 16);
}

/**
 * Format current time as UTC
 * @returns {string} Current time in HH:MM:SS UTC format
 */
function nowUTC() {
  return formatUTCTime(new Date());
}

/**
 * Format current datetime as UTC
 * @returns {string} Current datetime in YYYY-MM-DD HH:MM:SS UTC format
 */
function nowUTCDateTime() {
  return formatUTCDateTime(new Date());
}

/**
 * Convert input to Date object
 * Handles: Date objects, Unix timestamps (seconds or milliseconds), ISO strings
 */
function toDate(input) {
  if (!input) return null;
  
  if (input instanceof Date) return input;
  
  if (typeof input === 'number') {
    // Detect seconds vs milliseconds (timestamps after year 2001 in ms are > 1e12)
    if (input < 1e12) {
      return new Date(input * 1000); // Unix seconds
    }
    return new Date(input); // Milliseconds
  }
  
  if (typeof input === 'string') {
    return new Date(input);
  }
  
  return null;
}

/**
 * Get UTC hours from a date
 * @param {Date|number|string} input
 * @returns {number} Hour (0-23) in UTC
 */
function getUTCHour(input) {
  const date = toDate(input);
  return date ? date.getUTCHours() : 0;
}

/**
 * Format hour for display (e.g., "14:00 UTC")
 * @param {number} hour - Hour (0-23)
 * @returns {string} Formatted hour string
 */
function formatHourUTC(hour) {
  return `${hour.toString().padStart(2, '0')}:00 UTC`;
}

/**
 * Format a duration in milliseconds
 * @param {number} ms - Duration in milliseconds
 * @returns {string} Human-readable duration
 */
function formatDuration(ms) {
  if (ms < 1000) return `${ms}ms`;
  if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`;
  if (ms < 3600000) return `${Math.floor(ms / 60000)}m ${Math.floor((ms % 60000) / 1000)}s`;
  return `${Math.floor(ms / 3600000)}h ${Math.floor((ms % 3600000) / 60000)}m`;
}

// Export for ES modules
if (typeof module !== 'undefined' && module.exports) {
  module.exports = {
    formatUTCTime,
    formatUTCTimeMs,
    formatUTCDate,
    formatUTCDateTime,
    formatUTCShort,
    nowUTC,
    nowUTCDateTime,
    toDate,
    getUTCHour,
    formatHourUTC,
    formatDuration
  };
}

// Also attach to window for browser use
if (typeof window !== 'undefined') {
  window.UTCTime = {
    formatTime: formatUTCTime,
    formatTimeMs: formatUTCTimeMs,
    formatDate: formatUTCDate,
    formatDateTime: formatUTCDateTime,
    formatShort: formatUTCShort,
    now: nowUTC,
    nowDateTime: nowUTCDateTime,
    getHour: getUTCHour,
    formatHour: formatHourUTC,
    formatDuration: formatDuration
  };
}

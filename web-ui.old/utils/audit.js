/**
 * Audit Logging Utility
 * 
 * Logs all security-relevant events for compliance and forensics
 */

import winston from 'winston';
import { join } from 'path';
import fs from 'fs';

// Ensure log directory exists
const logDir = '/var/log/hf-timestd';
try {
  fs.mkdirSync(logDir, { recursive: true, mode: 0o750 });
} catch (err) {
  console.warn(`Could not create log directory ${logDir}, using /tmp`);
}

const logFile = fs.existsSync(logDir) 
  ? join(logDir, 'audit.log')
  : '/tmp/hf-timestd-audit.log';

/**
 * Audit logger instance
 */
export const auditLogger = winston.createLogger({
  level: 'info',
  format: winston.format.combine(
    winston.format.timestamp(),
    winston.format.errors({ stack: true }),
    winston.format.json()
  ),
  transports: [
    new winston.transports.File({ 
      filename: logFile,
      maxsize: 10 * 1024 * 1024, // 10MB
      maxFiles: 5,
      tailable: true
    }),
    new winston.transports.Console({
      format: winston.format.combine(
        winston.format.colorize(),
        winston.format.simple()
      ),
      level: 'warn' // Only show warnings and errors in console
    })
  ]
});

/**
 * Log a security event
 * 
 * @param {string} event - Event type (e.g., 'LOGIN_SUCCESS', 'DAEMON_START')
 * @param {object} user - User object with id, username
 * @param {object} details - Additional event details
 */
export function auditLog(event, user, details = {}) {
  auditLogger.info({
    event,
    userId: user?.id,
    username: user?.username,
    timestamp: new Date().toISOString(),
    ...details
  });
}

/**
 * Log authentication events
 */
export function logAuth(event, username, success, details = {}) {
  auditLogger.info({
    event,
    username,
    success,
    timestamp: new Date().toISOString(),
    ...details
  });
}

/**
 * Log configuration changes
 */
export function logConfigChange(action, user, configId, details = {}) {
  auditLogger.info({
    event: 'CONFIG_CHANGE',
    action,
    userId: user?.id,
    username: user?.username,
    configId,
    timestamp: new Date().toISOString(),
    ...details
  });
}

/**
 * Log daemon control actions
 */
export function logDaemonControl(action, user, pid, details = {}) {
  auditLogger.info({
    event: 'DAEMON_CONTROL',
    action,
    userId: user?.id,
    username: user?.username,
    pid,
    timestamp: new Date().toISOString(),
    ...details
  });
}

/**
 * Log security violations
 */
export function logSecurityViolation(violation, user, details = {}) {
  auditLogger.warn({
    event: 'SECURITY_VIOLATION',
    violation,
    userId: user?.id,
    username: user?.username,
    timestamp: new Date().toISOString(),
    ...details
  });
}

console.log(`Audit logging initialized: ${logFile}`);

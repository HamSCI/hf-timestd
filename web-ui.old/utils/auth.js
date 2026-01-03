/**
 * Authentication Utilities
 * 
 * Provides secure authentication using bcrypt and JWT
 */

import bcrypt from 'bcrypt';
import jwt from 'jsonwebtoken';
import crypto from 'crypto';
import fs from 'fs';
import { join } from 'path';
import { fileURLToPath } from 'url';

const __dirname = join(fileURLToPath(import.meta.url), '../..');

// JWT secret - loaded from env or generated
const JWT_SECRET_FILE = join(__dirname, 'data', 'jwt-secret.txt');

/**
 * Get or generate JWT secret
 */
export function getJWTSecret() {
  try {
    // Try to load existing secret
    if (fs.existsSync(JWT_SECRET_FILE)) {
      return fs.readFileSync(JWT_SECRET_FILE, 'utf8').trim();
    }
  } catch (err) {
    console.warn('Could not load JWT secret, generating new one');
  }
  
  // Generate new secret
  const secret = crypto.randomBytes(64).toString('hex');
  
  try {
    // Save for persistence across restarts
    fs.mkdirSync(join(__dirname, 'data'), { recursive: true });
    fs.writeFileSync(JWT_SECRET_FILE, secret, { mode: 0o600 }); // Readable only by owner
    console.log('Generated new JWT secret');
  } catch (err) {
    console.error('Could not save JWT secret:', err.message);
  }
  
  return secret;
}

const JWT_SECRET = getJWTSecret();

/**
 * Hash a password using bcrypt
 */
export async function hashPassword(password) {
  const saltRounds = 10;
  return await bcrypt.hash(password, saltRounds);
}

/**
 * Verify a password against a hash
 */
export async function verifyPassword(password, hash) {
  try {
    return await bcrypt.compare(password, hash);
  } catch (err) {
    console.error('Password verification error:', err);
    return false;
  }
}

/**
 * Generate a JWT token
 */
export function generateToken(user, expiresIn = '8h') {
  return jwt.sign(
    {
      id: user.id,
      username: user.username,
      role: user.role || 'user'
    },
    JWT_SECRET,
    { expiresIn }
  );
}

/**
 * Verify a JWT token
 */
export function verifyToken(token) {
  try {
    return jwt.verify(token, JWT_SECRET);
  } catch (err) {
    return null;
  }
}

/**
 * Express middleware to require authentication
 */
export function requireAuth(req, res, next) {
  const authHeader = req.headers.authorization;
  
  if (!authHeader || !authHeader.startsWith('Bearer ')) {
    return res.status(401).json({ error: 'Authentication required' });
  }

  const token = authHeader.substring(7);
  const decoded = verifyToken(token);
  
  if (!decoded) {
    return res.status(401).json({ error: 'Invalid or expired token' });
  }
  
  req.user = decoded;
  next();
}

/**
 * Create default admin user if none exists
 */
export async function ensureDefaultAdmin(usersFile) {
  try {
    let users = [];
    
    // Try to load existing users
    if (fs.existsSync(usersFile)) {
      const data = fs.readFileSync(usersFile, 'utf8');
      users = JSON.parse(data);
    }
    
    // Check if admin already exists
    const adminExists = users.some(u => u.username === 'admin');
    
    if (!adminExists) {
      console.log('Creating default admin user...');
      console.log('⚠️  IMPORTANT: Change the default password immediately!');
      console.log('   Username: admin');
      console.log('   Password: admin');
      
      const passwordHash = await hashPassword('admin');
      
      users.push({
        id: 'admin-' + Date.now(),
        username: 'admin',
        passwordHash: passwordHash,
        role: 'admin',
        createdAt: new Date().toISOString()
      });
      
      fs.mkdirSync(join(usersFile, '..'), { recursive: true });
      fs.writeFileSync(usersFile, JSON.stringify(users, null, 2), { mode: 0o600 });
      
      return true;
    }
    
    return false;
  } catch (err) {
    console.error('Error ensuring default admin:', err);
    return false;
  }
}

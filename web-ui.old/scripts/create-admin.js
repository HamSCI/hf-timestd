#!/usr/bin/env node
/**
 * Create Admin User Script
 * 
 * Creates a secure admin user with a custom password
 * Usage: node scripts/create-admin.js <username> <password>
 */

import bcrypt from 'bcrypt';
import fs from 'fs';
import { join } from 'path';
import { fileURLToPath } from 'url';
import readline from 'readline';

const __dirname = join(fileURLToPath(import.meta.url), '../..');
const usersFile = join(__dirname, 'data', 'users.json');

async function createAdmin(username, password) {
  try {
    // Load existing users
    let users = [];
    if (fs.existsSync(usersFile)) {
      const data = fs.readFileSync(usersFile, 'utf8');
      users = JSON.parse(data);
    }
    
    // Check if user already exists
    const existingUser = users.find(u => u.username === username);
    if (existingUser) {
      console.error(`Error: User '${username}' already exists`);
      console.log('To reset password, delete the user from data/users.json first');
      process.exit(1);
    }
    
    // Validate password strength
    if (password.length < 8) {
      console.error('Error: Password must be at least 8 characters');
      process.exit(1);
    }
    
    console.log('Hashing password...');
    const passwordHash = await bcrypt.hash(password, 10);
    
    // Create new user
    const newUser = {
      id: `user-${Date.now()}`,
      username: username,
      passwordHash: passwordHash,
      role: 'admin',
      createdAt: new Date().toISOString()
    };
    
    users.push(newUser);
    
    // Save users file
    fs.mkdirSync(join(usersFile, '..'), { recursive: true });
    fs.writeFileSync(usersFile, JSON.stringify(users, null, 2), { mode: 0o600 });
    
    console.log(`✓ Admin user '${username}' created successfully`);
    console.log(`  User ID: ${newUser.id}`);
    console.log(`  Role: admin`);
    console.log(`  Created: ${newUser.createdAt}`);
    console.log('');
    console.log('You can now log in with these credentials.');
    
  } catch (err) {
    console.error('Error creating admin user:', err.message);
    process.exit(1);
  }
}

// Get credentials from command line or prompt
const args = process.argv.slice(2);

if (args.length === 2) {
  // Username and password provided
  const [username, password] = args;
  createAdmin(username, password);
} else if (args.length === 0) {
  // Interactive mode
  const rl = readline.createInterface({
    input: process.stdin,
    output: process.stdout
  });
  
  console.log('=== Create Admin User ===\n');
  
  rl.question('Username: ', (username) => {
    rl.question('Password (min 8 characters): ', (password) => {
      rl.question('Confirm password: ', async (confirm) => {
        rl.close();
        
        if (password !== confirm) {
          console.error('Error: Passwords do not match');
          process.exit(1);
        }
        
        await createAdmin(username, password);
      });
    });
  });
} else {
  console.log('Usage: node create-admin.js [username] [password]');
  console.log('');
  console.log('If no arguments provided, will prompt interactively.');
  console.log('');
  console.log('Examples:');
  console.log('  node create-admin.js admin SecurePassword123');
  console.log('  node create-admin.js    # Interactive mode');
  process.exit(1);
}

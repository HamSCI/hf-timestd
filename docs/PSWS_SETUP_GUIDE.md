# PSWS Authentication and Upload Setup Guide

## Overview

The HamSCI PSWS (Personal Space Weather Station) network requires SSH key-based authentication for uploading GRAPE Digital RF data. This guide explains the complete setup process.

## Quick Reference

| Item | Description | Example |
|------|-------------|---------|
| **SITE_ID** | Your PSWS station identifier | `S000171` |
| **TOKEN** | Password for initial SSH key upload | (from PSWS admin page) |
| **INSTRUMENT_ID** | Instrument number within your site | `172` |
| **SSH Key** | Private key for authentication | `~/.ssh/psws_key` |

## PSWS Server Details

- **Server URL**: `pswsnetwork.eng.ua.edu`
- **Registration Portal**: `pswsnetwork.caps.ua.edu`
- **Protocol**: SFTP over SSH (port 22)
- **Authentication**: SSH public key

## Step-by-Step Setup

### Step 1: Create PSWS Account

1. Navigate to https://pswsnetwork.caps.ua.edu/
2. Create a new user account
3. Log in to your account dashboard

### Step 2: Create a Site

In your PSWS account dashboard:

1. **Create a new "Site"**
   - Each site represents a physical location/station
   - You will be assigned a **SITE_ID** (e.g., `S000171`)
   
2. **Record the TOKEN**
   - When you create the site, you'll receive a **TOKEN** (password)
   - This is displayed in the PSWS admin page for your site
   - **Copy this exactly** - no extra spaces or missing characters

### Step 3: Add an Instrument

For each site, add an instrument:

1. Select instrument type (e.g., "rx888", "grape")
2. You will be assigned an **INSTRUMENT_ID** (e.g., `172`)
3. Record this INSTRUMENT_ID

**Result**: You now have:
- **SITE_ID**: `S000NNN` (e.g., `S000171`)
- **TOKEN**: The password for this site
- **INSTRUMENT_ID**: A number (e.g., `172`)

### Step 4: Generate SSH Key Pair

Generate a dedicated SSH key for PSWS uploads:

```bash
# Generate SSH key pair for PSWS (as the timestd user)
sudo -u timestd ssh-keygen -t ed25519 -f /home/timestd/.ssh/psws_key -N "" -C "PSWS upload key"

# Set correct permissions
sudo chmod 600 /home/timestd/.ssh/psws_key
sudo chmod 644 /home/timestd/.ssh/psws_key.pub
sudo chown timestd:timestd /home/timestd/.ssh/psws_key*
```

This creates:
- Private key: `/home/timestd/.ssh/psws_key`
- Public key: `/home/timestd/.ssh/psws_key.pub`

### Step 5: Upload Public Key to PSWS

Copy your SSH public key to the PSWS server using your SITE_ID and TOKEN:

```bash
# Replace S000171 with your actual SITE_ID
sudo -u timestd ssh-copy-id -i /home/timestd/.ssh/psws_key.pub S000171@pswsnetwork.eng.ua.edu
```

When prompted for password, enter your **TOKEN** from the PSWS admin page.

You should see: `Number of key(s) added: 1`

### Step 6: Test Authentication

Verify SSH key authentication is working:

```bash
# Test SFTP connection (should connect without password)
sudo -u timestd sftp -i /home/timestd/.ssh/psws_key -o BatchMode=yes S000171@pswsnetwork.eng.ua.edu <<< "quit"
```

If it asks for a password, authentication setup failed - check your TOKEN and try Step 5 again.

### Step 7: Configure hf-timestd

Edit `/etc/hf-timestd/timestd-config.toml`:

```toml
[station]
callsign = "YOUR_CALLSIGN"           # e.g., "W1ABC"
grid_square = "YOUR_GRID"            # e.g., "FN31pr"
id = "YOUR_SITE_ID"                  # e.g., "S000171"
instrument_id = "YOUR_INSTRUMENT_ID" # e.g., "172"

[uploader]
enabled = true
protocol = "sftp"

[uploader.sftp]
host = "pswsnetwork.eng.ua.edu"
user = "YOUR_SITE_ID"                # Same as station.id
ssh_key = "/home/timestd/.ssh/psws_key"
bandwidth_limit_kbps = 100           # Optional: limit upload speed
```

### Step 8: Enable Daily Upload

The grape-daily.service already includes package and upload steps. Ensure it's enabled:

```bash
sudo systemctl enable --now grape-daily.timer
```

## Manual Upload

To manually upload a specific date:

```bash
# Package the data
sudo -u timestd hf-timestd grape package --date 2026-01-20 --callsign AC0G --grid EM38ww

# Upload (dry-run first)
sudo -u timestd hf-timestd grape upload --date 2026-01-20 --dry-run

# Actual upload
sudo -u timestd hf-timestd grape upload --date 2026-01-20
```

## Troubleshooting

### Authentication Issues

**Problem**: `ssh-copy-id` rejects the password

**Solutions**:
1. Verify SITE_ID is correct (check PSWS admin page)
2. Copy TOKEN again carefully (no extra spaces)
3. Try typing the TOKEN manually instead of pasting

**Problem**: SFTP asks for password after `ssh-copy-id` succeeded

**Solutions**:
1. Verify key path is correct in config
2. Check key permissions: `ls -la /home/timestd/.ssh/psws_key`
3. Test with verbose output: `sftp -v -i /home/timestd/.ssh/psws_key S000171@pswsnetwork.eng.ua.edu`

### Upload Issues

**Problem**: Upload fails with permission denied

**Solutions**:
1. Verify authentication is working (Step 6)
2. Check SITE_ID matches your account
3. Ensure config file has correct `[uploader.sftp]` settings

**Problem**: Files upload but don't appear in PSWS

**Solutions**:
1. Check that trigger directory was created
2. Verify INSTRUMENT_ID matches your PSWS configuration
3. Check Digital RF format is valid

## Security Best Practices

1. **Dedicated key**: Use a separate SSH key for PSWS (not your personal key)
2. **Restrict permissions**: `chmod 600` on private key
3. **Service user**: Run uploads as `timestd` user, not root
4. **Config permissions**: `chmod 640 /etc/hf-timestd/timestd-config.toml`
5. **Never commit keys**: Add `.ssh/` to `.gitignore`

## Upload Log Location

Upload status is logged to the systemd journal:

```bash
# View recent upload logs
journalctl -u grape-daily.service -n 50

# Follow live
journalctl -u grape-daily.service -f
```

## References

- [HamSCI GRAPE Project](https://hamsci.org/grape)
- [PSWS Network Portal](https://pswsnetwork.caps.ua.edu/)
- [Digital RF Format](https://github.com/MITHaystack/digital_rf)

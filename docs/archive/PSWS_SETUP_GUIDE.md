# PSWS Authentication and Upload Setup Guide

## Overview

The HamSCI PSWS (Personal Space Weather Station) network requires SSH key-based authentication for uploading GRAPE Digital RF data. This guide explains the complete setup process based on the wsprdaemon implementation.

## PSWS Server Details

- **Server URL**: `pswsnetwork.eng.ua.edu` (production)
- **Alternate URL**: `pswsnetwork.caps.ua.edu` (registration portal)
- **Protocol**: SFTP over SSH (port 22)
- **Authentication**: SSH public key authentication

## Prerequisites

1. **PSWS Account**: You must create an account at https://pswsnetwork.caps.ua.edu/
2. **SSH Key Pair**: Your system needs an SSH public/private key pair
3. **Network Access**: Your system must be able to reach the PSWS server on port 22

## Step-by-Step Setup Process

### Step 1: Create PSWS Account

1. Navigate to https://pswsnetwork.caps.ua.edu/
2. Create a new user account
3. Log in to your account dashboard

### Step 2: Create a Site

In your PSWS account dashboard:

1. **Create a new "Site"**
   - Each site represents a physical location/station
   - You will be assigned a **SITE_ID** in the format `S000NNN` (e.g., `S000987`)
   - Choose a descriptive name (e.g., "AC0G_B1" for callsign AC0G, building 1)
   
2. **Record the TOKEN**
   - When you create the site, you'll receive a **TOKEN** (password)
   - This token is displayed in the PSWS admin page for your site
   - **IMPORTANT**: Copy this token exactly - no extra spaces or missing characters
   - The token functions as the password for SSH authentication

### Step 3: Add an Instrument

For each site, add an instrument:

1. Select instrument type (e.g., "rx888", "magnetometer")
2. You will be assigned an **INSTRUMENT_ID** (typically a number like `0`, `1`, `2`, etc.)
3. Record this INSTRUMENT_ID

**Result**: You now have:
- **SITE_ID**: `S000NNN` (e.g., `S000987`)
- **TOKEN**: The password for this site
- **INSTRUMENT_ID**: `0` (or another number)

### Step 4: Configure Your Station

Create or edit your configuration file with the PSWS credentials:

**For signal-recorder**, edit `config/grape-production.toml`:

```toml
[station]
callsign = "AC0G"
grid_square = "EM38ww"
id = "S000987"              # Your SITE_ID
instrument_id = "0"         # Your INSTRUMENT_ID
description = "GRAPE station with RX888 MkII"

[psws]
server = "pswsnetwork.eng.ua.edu"
site_id = "S000987"         # Your SITE_ID
instrument_id = "0"         # Your INSTRUMENT_ID
enabled = true
```

**For wsprdaemon**, edit `wsprdaemon.conf`:

```bash
GRAPE_PSWS_ID="S000987_0"   # Format: SITE_ID_INSTRUMENT_ID
```

### Step 5: Generate SSH Key Pair (If Needed)

Check if you already have SSH keys:

```bash
ls -la ~/.ssh/*.pub
```

If no public keys exist, generate a new key pair:

```bash
# Generate SSH key pair (press Enter to accept defaults)
ssh-keygen -t rsa -b 4096 -C "your_email@example.com"

# When prompted:
# - File location: Press Enter (default: ~/.ssh/id_rsa)
# - Passphrase: Press Enter for no passphrase (or enter one if desired)
```

This creates:
- Private key: `~/.ssh/id_rsa`
- Public key: `~/.ssh/id_rsa.pub`

### Step 6: Upload Public Key to PSWS

This is the **critical authentication step**. You need to copy your SSH public key to the PSWS server using your SITE_ID and TOKEN.

```bash
# Replace S000987 with your actual SITE_ID
ssh-copy-id -o ConnectTimeout=5 -f S000987@pswsnetwork.eng.ua.edu
```

**What happens:**

1. The command will prompt: `S000987@pswsnetwork.eng.ua.edu's password:`
2. **Enter your TOKEN** (the password from the PSWS admin page)
3. The command uploads your public key to the server
4. You should see: `Number of key(s) added: 1`

**Common Issues:**

- **Wrong token**: Most common error - copy/paste the token carefully
- **Extra spaces**: Don't include leading/trailing spaces in the token
- **Wrong SITE_ID**: Verify you're using the correct SITE_ID format
- **Network timeout**: Check firewall rules and network connectivity

### Step 7: Test Authentication

Verify that SSH key authentication is working:

```bash
# Test SFTP connection (should connect without password)
sftp -o ConnectTimeout=5 -b /dev/null S000987@pswsnetwork.eng.ua.edu

# Expected output: "Connected to pswsnetwork.eng.ua.edu"
# If it asks for a password, authentication setup failed
```

**For wsprdaemon users**, there's an alias:

```bash
wdssp  # Tests PSWS auto-login
```

### Step 8: Test Network Connectivity

Before attempting uploads, verify you can reach the PSWS server:

```bash
# Test SSH port connectivity (should complete in ~2 seconds)
nc -vz -w 2 pswsnetwork.eng.ua.edu 22

# Expected output: "Connection to pswsnetwork.eng.ua.edu 22 port [tcp/ssh] succeeded!"
```

## Upload Process

### Directory Structure on PSWS Server

When you upload, files go to your site's home directory on the PSWS server:

```
/home/S000987/                          # Your SITE_ID home directory
├── AC0G_EM38ww/                        # Station directory
│   └── RX888@S000987_0/                # Receiver@SITE_INSTRUMENT
│       └── OBS2024-10-24T00-00/        # Observation timestamp
│           └── ch0/                    # Channel
│               ├── drf_*.h5            # Digital RF data files
│               └── metadata/
│                   └── metadata_*.h5
└── cOBS2024-10-24T00-00_#0_#2024-10-25T00-05/  # Trigger directory
```

### Trigger Directory

The PSWS server processes data when it sees a **trigger directory**. The format is:

```
c<dataset_name>_#<instrument_id>_#<timestamp>
```

Example:
```
cOBS2024-10-24T00-00_#0_#2024-10-25T00-05
```

This tells the PSWS server:
- Process the dataset `OBS2024-10-24T00-00`
- For instrument ID `0`
- Upload completed at `2024-10-25T00-05`

### Manual Upload Example

```bash
# Navigate to your Digital RF dataset directory
cd /mnt/grape-data/archive/20241024/AC0G_EM38ww

# Create SFTP command file
cat > /tmp/sftp_upload.txt <<EOF
put -r .
mkdir cOBS2024-10-24T00-00_#0_#$(date -u +%Y-%m-%dT%H-%M)
EOF

# Upload via SFTP (with 100 KB/s bandwidth limit)
sftp -l 100 -b /tmp/sftp_upload.txt S000987@pswsnetwork.eng.ua.edu
```

### Automated Upload Script

For signal-recorder, create an upload script:

```bash
#!/bin/bash
# upload_to_psws.sh

SITE_ID="S000987"
INSTRUMENT_ID="0"
PSWS_SERVER="pswsnetwork.eng.ua.edu"
DATA_DIR="/mnt/grape-data/archive"

# Get yesterday's date
DATE=$(date -u -d "yesterday" +%Y%m%d)

# Find dataset directory
DATASET_DIR=$(find ${DATA_DIR}/${DATE} -type d -name "OBS*" | head -1)

if [[ -z "${DATASET_DIR}" ]]; then
    echo "No dataset found for ${DATE}"
    exit 1
fi

# Create trigger directory name
TRIGGER_DIR="c$(basename ${DATASET_DIR})_#${INSTRUMENT_ID}_#$(date -u +%Y-%m-%dT%H-%M)"

# Create SFTP commands
echo "put -r .
mkdir ${TRIGGER_DIR}" > /tmp/sftp_cmds.txt

# Change to dataset parent directory
cd "$(dirname ${DATASET_DIR})"

# Upload
sftp -l 100 -b /tmp/sftp_cmds.txt ${SITE_ID}@${PSWS_SERVER}

echo "Upload complete: ${TRIGGER_DIR}"
```

Make it executable and run:

```bash
chmod +x upload_to_psws.sh
./upload_to_psws.sh
```

## Troubleshooting

### Authentication Issues

**Problem**: `ssh-copy-id` asks for password but rejects it

**Solutions**:
1. Verify SITE_ID is correct (check PSWS admin page)
2. Copy TOKEN again from PSWS admin page (avoid copy/paste errors)
3. Try typing the TOKEN manually instead of pasting
4. Check for hidden characters or spaces

**Problem**: SFTP asks for password after `ssh-copy-id` succeeded

**Solutions**:
1. Verify public key was uploaded: `ssh-add -L`
2. Check SSH agent is running: `eval $(ssh-agent)`
3. Add key to agent: `ssh-add ~/.ssh/id_rsa`
4. Test with verbose output: `sftp -v S000987@pswsnetwork.eng.ua.edu`

### Network Issues

**Problem**: Connection timeout

**Solutions**:
1. Check firewall rules: `sudo iptables -L -n | grep 22`
2. Verify DNS resolution: `nslookup pswsnetwork.eng.ua.edu`
3. Test with longer timeout: `nc -vz -w 10 pswsnetwork.eng.ua.edu 22`
4. Try alternate server: `pswsnetwork.caps.ua.edu`

**Problem**: Connection refused

**Solutions**:
1. Verify server is up (check HamSCI status page)
2. Check if SSH service is running on server
3. Contact HamSCI support

### Upload Issues

**Problem**: Upload fails with permission denied

**Solutions**:
1. Verify authentication is working (Step 7)
2. Check SITE_ID matches your account
3. Ensure you're uploading to correct directory structure

**Problem**: Files upload but don't process

**Solutions**:
1. Verify trigger directory was created
2. Check trigger directory name format is correct
3. Verify INSTRUMENT_ID matches your PSWS configuration
4. Check Digital RF format is valid: `drf verify <dataset_dir>`

### Verification

**Problem**: How do I know if upload succeeded?

**Solutions**:
1. Check SFTP output for errors
2. Log in to PSWS web interface and check for your data
3. Look for processing confirmation emails from PSWS
4. Check local logs for upload completion markers

## Security Best Practices

### SSH Key Management

1. **Use strong keys**: RSA 4096-bit or Ed25519
2. **Protect private keys**: `chmod 600 ~/.ssh/id_rsa`
3. **Use passphrases**: Add passphrase protection to private keys
4. **Backup keys**: Keep secure backup of private key
5. **Rotate keys**: Change keys periodically (annually)

### Token Security

1. **Never commit tokens to git**: Add to `.gitignore`
2. **Limit token exposure**: Only store in configuration files
3. **Restrict file permissions**: `chmod 600 config/*.toml`
4. **Don't share tokens**: Each site should have unique token

### Network Security

1. **Use firewall rules**: Restrict outbound SSH to PSWS server only
2. **Monitor uploads**: Log all upload attempts
3. **Bandwidth limiting**: Use `-l` flag with SFTP to limit bandwidth
4. **Verify server**: Check SSH fingerprint on first connection

## Automation

### Systemd Timer for Daily Uploads

Create `/etc/systemd/system/grape-upload.timer`:

```ini
[Unit]
Description=Daily GRAPE data upload to PSWS
Requires=grape-upload.service

[Timer]
OnCalendar=daily
OnCalendar=*-*-* 00:05:00
Persistent=true

[Install]
WantedBy=timers.target
```

Create `/etc/systemd/system/grape-upload.service`:

```ini
[Unit]
Description=Upload GRAPE data to PSWS
After=network.target

[Service]
Type=oneshot
User=grape
Group=grape
ExecStart=/home/grape/signal-recorder/upload_to_psws.sh
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable grape-upload.timer
sudo systemctl start grape-upload.timer
sudo systemctl list-timers grape-upload.timer
```

### Cron Job Alternative

Add to crontab (`crontab -e`):

```bash
# Upload GRAPE data daily at 00:05 UTC
5 0 * * * /home/grape/signal-recorder/upload_to_psws.sh >> /var/log/grape-upload.log 2>&1
```

## Integration with signal-recorder

The signal-recorder can be extended to include automatic PSWS upload:

```python
# In src/signal_recorder/psws_uploader.py

import subprocess
from pathlib import Path
from datetime import datetime, timezone

class PSWSUploader:
    def __init__(self, site_id: str, instrument_id: str, 
                 server: str = "pswsnetwork.eng.ua.edu"):
        self.site_id = site_id
        self.instrument_id = instrument_id
        self.server = server
    
    def upload_dataset(self, dataset_dir: Path) -> bool:
        """Upload Digital RF dataset to PSWS server"""
        
        # Create trigger directory name
        dataset_name = dataset_dir.name
        timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H-%M')
        trigger_dir = f"c{dataset_name}_#{self.instrument_id}_#{timestamp}"
        
        # Create SFTP commands
        sftp_cmds = f"put -r .\nmkdir {trigger_dir}\n"
        
        # Execute SFTP upload
        cmd = [
            'sftp', '-l', '100',  # 100 KB/s bandwidth limit
            '-b', '/dev/stdin',
            f"{self.site_id}@{self.server}"
        ]
        
        try:
            result = subprocess.run(
                cmd,
                input=sftp_cmds.encode(),
                cwd=dataset_dir.parent,
                capture_output=True,
                timeout=3600  # 1 hour timeout
            )
            
            return result.returncode == 0
            
        except Exception as e:
            print(f"Upload failed: {e}")
            return False
```

## References

- [HamSCI GRAPE Project](https://hamsci.org/grape)
- [PSWS Network Portal](https://pswsnetwork.caps.ua.edu/)
- [Digital RF Format](https://github.com/MITHaystack/digital_rf)
- [wsprdaemon GRAPE Implementation](https://github.com/rrobinett/wsprdaemon)
- [wsprdaemon Groups.io Discussion](https://groups.io/g/wsprdaemon/message/3319)

## Support

For PSWS authentication issues:

1. Check this guide's troubleshooting section
2. Verify all steps were completed correctly
3. Test with manual SFTP commands
4. Contact HamSCI GRAPE team via [HamSCI website](https://hamsci.org/contact)
5. Post on [wsprdaemon Groups.io](https://groups.io/g/wsprdaemon)

For signal-recorder specific issues:

1. Check logs: `/tmp/grape_recorder_test.log`
2. Review configuration files
3. Verify Digital RF format: `drf verify <dataset>`
4. Consult main documentation: `docs/GRAPE_DIGITAL_RF_RECORDER.md`


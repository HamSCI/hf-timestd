# NASA Earthdata Account Setup for VTEC Service

The VTEC service downloads Differential Code Bias (DCB) correction files from NASA's CDDIS archive to improve ionospheric measurements. This requires a free NASA Earthdata account.

## Why This Is Needed

DCB corrections improve VTEC accuracy by accounting for satellite and receiver biases. Without these corrections, the system assumes zero bias, which degrades accuracy.

## Setup Steps

### 1. Create NASA Earthdata Account

1. Go to <https://urs.earthdata.nasa.gov/users/new>
2. Fill out the registration form
3. Verify your email address
4. **Important**: Request access to CDDIS data:
   - Log in to <https://urs.earthdata.nasa.gov/>
   - Go to "Applications" → "Authorized Apps"
   - Add "NASA GESDISC DATA ARCHIVE"

### 2. Configure Authentication

The VTEC service runs as the `timestd` user and needs credentials in `/home/timestd/.netrc`.

**Option A: During Installation (Recommended)**

The install script will prompt for your credentials and create the file automatically.

**Option B: Manual Setup**

```bash
# Create timestd home directory if it doesn't exist
sudo mkdir -p /home/timestd

# Create .netrc file
sudo tee /home/timestd/.netrc > /dev/null << 'EOF'
machine urs.earthdata.nasa.gov
login YOUR_EARTHDATA_USERNAME
password YOUR_EARTHDATA_PASSWORD
EOF

# Set correct permissions (CRITICAL - .netrc must be 600)
sudo chown timestd:timestd /home/timestd/.netrc
sudo chmod 600 /home/timestd/.netrc
```

### 3. Verify Setup

After restarting the VTEC service, check the logs:

```bash
sudo systemctl restart timestd-vtec
sudo journalctl -u timestd-vtec -n 50 | grep -E "DCB|bias|Download"
```

You should see:

```
Downloading CAS0OPSRAP_YYYYDDD0000_01D_01D_DCB.BIA.gz...
Unzipping...
Parsed XXXX bias entries.
Loaded XXXX bias entries.
```

## Troubleshooting

### "Unzip failed: Not a gzipped file"

This means authentication failed. Check:

1. Credentials are correct in `.netrc`
2. File permissions are exactly `600` (not `644` or `640`)
3. File is owned by `timestd:timestd`
4. You've authorized CDDIS access in your Earthdata account

### Empty DCB Files (0 bytes)

If files in `/var/lib/timestd/data/dcb/` are 0 bytes:

```bash
# Remove empty files
sudo rm /var/lib/timestd/data/dcb/*.BIA

# Restart service to re-download
sudo systemctl restart timestd-vtec
```

### Service Works Without DCB

The VTEC service will run without DCB corrections, but accuracy is reduced. Logs will show:

```
Failed to download DCB file. VTEC accuracy will be degraded (0 bias assumed).
```

## Security Notes

- The `.netrc` file contains your password in plain text
- **Must** have `600` permissions (owner read/write only)
- Consider using a dedicated Earthdata account for this system
- Rotate password periodically

## Alternative: Disable DCB Downloads

If you don't want to set up NASA Earthdata, you can disable DCB downloads:

```toml
# In /etc/hf-timestd/timestd-config.toml
[gnss_vtec]
enabled = true
download_dcb = false  # Disable DCB downloads
```

The system will work but with reduced VTEC accuracy.

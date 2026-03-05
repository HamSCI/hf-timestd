# NASA Earthdata Account Setup

Several hf-timestd services download data from NASA's CDDIS archive:

- **IONEX** (Global Ionosphere Maps) — used by the ionospheric model
- **DCB** (Differential Code Bias) — used by the VTEC service

Both require a free NASA Earthdata account.

## Why This Is Needed

IONEX provides global TEC maps for ionospheric delay modeling. DCB corrections
improve VTEC accuracy by accounting for satellite and receiver biases. Without
credentials, these downloads fail and the system falls back to parametric models.

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

Credentials are stored in a netrc file at `/etc/hf-timestd/earthdata-netrc`.
This is the system-wide location used by all hf-timestd services.

**Option A: During Installation (Recommended)**

The install script will prompt for your credentials and create the file automatically.

**Option B: Manual Setup**

```bash
# Create the credential file
sudo tee /etc/hf-timestd/earthdata-netrc > /dev/null << 'EOF'
machine urs.earthdata.nasa.gov
login YOUR_EARTHDATA_USERNAME
password YOUR_EARTHDATA_PASSWORD
EOF

# Set correct ownership and permissions (CRITICAL - must be 600)
sudo chown timestd:timestd /etc/hf-timestd/earthdata-netrc
sudo chmod 600 /etc/hf-timestd/earthdata-netrc
```

**Credential Lookup Order:**

The system checks for credentials in this order:
1. `NETRC` environment variable (set automatically by systemd units)
2. `/etc/hf-timestd/earthdata-netrc` (system-wide, recommended)
3. `~/.netrc` (user home directory, fallback for interactive/dev use)

### 3. Verify Setup

After creating the credential file, restart the affected services:

```bash
sudo systemctl restart timestd-vtec
sudo systemctl start timestd-ionex-download
```

Check the logs:

```bash
# VTEC/DCB downloads
sudo journalctl -u timestd-vtec -n 50 | grep -E "DCB|bias|Download"

# IONEX downloads
sudo journalctl -u timestd-ionex -n 50
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

This means authentication failed — CDDIS returned an HTML login page
instead of the binary file. Check:

1. Credentials are correct in `/etc/hf-timestd/earthdata-netrc`
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

### Services Work Without Credentials

Both services degrade gracefully without CDDIS credentials:

- **VTEC**: Runs but assumes zero DCB bias (reduced accuracy)
- **Ionospheric model**: Falls back to parametric IRI model (no IONEX)

Logs will show warnings like:

```
Failed to download DCB file. VTEC accuracy will be degraded (0 bias assumed).
```

## Security Notes

- The credential file contains your password in plain text
- **Must** have `600` permissions (owner read/write only)
- Stored in `/etc/hf-timestd/` alongside other system config
- Consider using a dedicated Earthdata account for this system
- Rotate password periodically

## Alternative: Disable CDDIS Downloads

If you don't want to set up NASA Earthdata, you can disable downloads:

```toml
# In /etc/hf-timestd/timestd-config.toml
[gnss_vtec]
enabled = true
download_dcb = false  # Disable DCB downloads
```

The system will work but with reduced ionospheric correction accuracy.

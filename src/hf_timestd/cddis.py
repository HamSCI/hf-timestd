import os
import datetime
import requests
import gzip
import shutil
import glob
import logging

try:
    from hf_timestd.cddis_auth import get_cddis_session
    _HAS_CDDIS_AUTH = True
except ImportError:
    _HAS_CDDIS_AUTH = False

logger = logging.getLogger(__name__)

class CDDISDownloader:
    def __init__(self, output_dir="data/dcb"):
        # Ensure output_dir is absolute or relative to CWD correctly.
        # Ideally, we should use a path from the project structure, but user asked for "data/dcb".
        self.output_dir = output_dir
        self.base_url = "https://cddis.nasa.gov/archive/gnss/products/bias"
        
        if not os.path.exists(self.output_dir):
            os.makedirs(self.output_dir)

    def get_doy_date(self, days_ago=2):
        """
        Calculates the date and Day of Year (DOY) for 'n' days ago.
        Latency = 2 days ensures the 'Rapid' file is available.
        """
        target_date = datetime.datetime.utcnow() - datetime.timedelta(days=days_ago)
        year = target_date.strftime("%Y")
        doy = target_date.strftime("%j") # Day of Year (001-366)
        return year, doy

    def download_latest_rapid_dcb(self, max_days_ago=10):
        """
        Downloads the latest available CAS (Multi-GNSS) Rapid Bias file.
        Searches backwards from 2 days ago up to max_days_ago.
        """
        for days_ago in range(2, max_days_ago + 1):
            year, doy = self.get_doy_date(days_ago=days_ago)
            # Format found in directory listing: CAS0OPSRAP_20250010000_01D_01D_DCB.BIA.gz
            # Changed MGX to OPS, and BSX to BIA
            filename = f"CAS0OPSRAP_{year}{doy}0000_01D_01D_DCB.BIA.gz"
            url = f"{self.base_url}/{year}/{filename}"
            
            print(f"Checking for file from {days_ago} days ago: {filename}...")
            
            local_path = os.path.join(self.output_dir, filename)
            unzipped_path = local_path.replace(".gz", "")

            if os.path.exists(unzipped_path):
                print(f"File {filename} already exists locally.")
                return unzipped_path

            try:
                if _HAS_CDDIS_AUTH:
                    session = get_cddis_session()
                else:
                    session = requests.Session()
                with session:
                    r = session.get(url, stream=True)
                    
                    if r.status_code == 200:
                        # Check if we got an HTML login page instead of binary
                        if 'text/html' in r.headers.get('Content-Type', ''):
                             # This means auth failed or manual redirect needed, but usually requests handles it.
                             # If we are here, it might be a 404 disguised as 200 login page (unlikely) 
                             # or we need to debug auth.
                             print("Response content-type is HTML, likely login page or error.")
                             print(f"Preview: {r.text[:200]}")
                             pass

                        # If content length is small (<1KB), it's suspicious for a gz file
                        if len(r.content) < 1000 and 'text/html' in r.headers.get('Content-Type', ''):
                             print("File too small/HTML. Skipping.")
                             continue

                        print(f"Downloading {filename}...")
                        with open(local_path, 'wb') as f:
                            for chunk in r.iter_content(chunk_size=8192):
                                f.write(chunk)
                        
                        print("Unzipping...")
                        try:
                            with gzip.open(local_path, 'rb') as f_in:
                                with open(unzipped_path, 'wb') as f_out:
                                    shutil.copyfileobj(f_in, f_out)
                            os.remove(local_path)
                            return unzipped_path
                        except Exception as e:
                            print(f"Unzip failed: {e}")
                            if os.path.exists(local_path): os.remove(local_path)
                            continue

                    elif r.status_code == 404:
                        print(f"File not found (404) for DOY {doy}. Trying previous day...")
                        continue
                    else:
                         print(f"Failed with status {r.status_code}. Response: {r.text[:100]}")
                         continue

            except Exception as e:
                print(f"Error checking {url}: {e}")
                continue
                
        print("Could not find any recent bias files.")
        return None

    def parse_biases(self, file_path):
        """
        Parses the BSX file to extract DCBs.
        Returns a dictionary: { (PRN, Signal1, Signal2): BiasInSeconds, ... }
        And potentially converted to meters if needed.
        """
        biases = {}
        c = 299792458.0 # Speed of light in m/s
        
        print(f"Parsing DCB file: {file_path}")
        
        try:
            with open(file_path, 'r') as f:
                in_bias_block = False
                for line in f:
                    if "+BIAS/SOLUTION" in line:
                        in_bias_block = True
                        continue
                    if "-BIAS/SOLUTION" in line:
                        in_bias_block = False
                        break
                    
                    if in_bias_block:
                        if line.startswith("*"): continue # Comment
                        parts = line.split()
                        if len(parts) < 8: continue
                        
                        # Format: DSB SVN PRN [STATION] OBS1 OBS2 START END UNIT VALUE STD
                        # Example: DSB G080 G01 C1C C1W 2025:357:00000 ... ns -0.7070 0.0360
                        
                        bias_type = parts[0]
                        if bias_type != "DSB": continue
                        
                        # Robust parsing by looking for 'ns'
                        if 'ns' not in parts: continue
                        unit_idx = parts.index('ns')
                        
                        # Value is after unit
                        try:
                            value_str = parts[unit_idx + 1]
                            value_ns = float(value_str)
                            
                            # Backtrack to find observables and PRN
                            # Usually: ... OBS1 OBS2 START END ns ...
                            # So OBS2 is unit_idx - 2, OBS1 is unit_idx - 3
                            # TIMESTAMPS are unit_idx - 1 (END) and unit_idx - 2 (START) ??
                            # Let's count back from 'ns':
                            # ns at i
                            # END at i-1
                            # START at i-2
                            # OBS2 at i-3
                            # OBS1 at i-4
                            # PRN at i-5 (usually)
                            
                            obs2 = parts[unit_idx - 3]
                            obs1 = parts[unit_idx - 4]
                            prn = parts[unit_idx - 5]
                            
                            # Sanity check PRN format (e.g., G01, E01, C01)
                            if len(prn) != 3 or prn[0] not in "GRECJ":
                                # Maybe STATION was present?
                                # If STATION is present, indices shift. 
                                # But we only care about Satellite biases (STATION empty)
                                # If STATION is present (e.g. CAS1), it's a receiver bias.
                                # Check if PRN looks like a station ID (e.g. 4 chars)?
                                # Actually, user wants Satellite biases.
                                # Satellite PRN is usually 3 chars (e.g. G01).   
                                pass

                            # Convert to meters
                            bias_meters = value_ns * 1e-9 * c
                            
                            key = (prn, obs1, obs2)
                            biases[key] = bias_meters
                            
                        except (ValueError, IndexError):
                            continue
                            
            print(f"Parsed {len(biases)} bias entries.")
            return biases
            
        except FileNotFoundError:
            print(f"File not found: {file_path}")
            return {}

# --- Usage Example ---
if __name__ == "__main__":
    downloader = CDDISDownloader()
    dcb_file = downloader.download_latest_rapid_dcb()
    
    if dcb_file:
        biases = downloader.parse_biases(dcb_file)
        
        # Example lookup for G01 C1C-C2W (L1 C/A - L2 P)
        target_pair = ('G01', 'C1C', 'C2W') 
        # Note: BSX may store C1C-C2W or similar.
        # User said: "Look for C1C (L1 C/A) and C2W (L2 P-code) or C2L pairs."
        
        if target_pair in biases:
            print(f"Found bias for {target_pair}: {biases[target_pair]:.4f} meters")
        else:
            # Try to find any G01 pair
            found = [k for k in biases.keys() if k[0] == 'G01']
            if found:
                print(f"Found biases for G01: {found[:5]}...")
            else:
                print("No biases found for G01.")

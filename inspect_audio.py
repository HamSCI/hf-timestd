
import numpy as np
import sys
import os
import zstandard as zstd
from scipy.fft import fft, fftfreq

def analyze_data(data, original_filename):
    print(f"Analyzing {original_filename}...")
    print(f"Shape: {data.shape}")
    print(f"Type: {data.dtype}")
    
    if data.size == 0:
        print("Empty data")
        return False

    # Assume it's complex IQ
    if np.iscomplexobj(data):
        mag = np.abs(data)
        # Remove DC
        mag = mag - np.mean(mag)
        
        # FFT of first 1 second (assume 24000 samples for buffer sizing)
        n_fft = min(len(mag), 24000)
        segment = mag[:n_fft]
        
        # Apply Window
        segment = segment * np.hanning(len(segment))
        
        # FFT
        yf = fft(segment)
        xf_24k = fftfreq(n_fft, 1/24000)
        xf_16k = fftfreq(n_fft, 1/16000)
        xf_12k = fftfreq(n_fft, 1/12000)
        
        # Find Peak (ignore DC and very low freq)
        # Only look at positive frequencies
        pos_mask = xf_24k > 50 
        
        # Find index of max in this region
        # Re-slice yf to match mask
        yf_pos = np.abs(yf[pos_mask])
        if len(yf_pos) == 0:
             print("No significant signal found")
             return True

        peak_idx_local = np.argmax(yf_pos)
        peak_mag = yf_pos[peak_idx_local]
        
        # Map back to frequencies
        freqs_24k = xf_24k[pos_mask]
        freqs_16k = xf_16k[pos_mask]
        freqs_12k = xf_12k[pos_mask]
        
        peak_freq_24k = freqs_24k[peak_idx_local]
        peak_freq_16k = freqs_16k[peak_idx_local]
        peak_freq_12k = freqs_12k[peak_idx_local]
        
        print(f"Peak Magnitude: {peak_mag:.2f}")
        print(f"Peak Freq (if 24ksps): {peak_freq_24k:.1f} Hz")
        print(f"Peak Freq (if 16ksps): {peak_freq_16k:.1f} Hz")
        print(f"Peak Freq (if 12ksps): {peak_freq_12k:.1f} Hz")

        # Check magnitudes at specific frequencies (24ksps basis)
        idx_1000 = int(1000 * n_fft / 24000)
        idx_600 = int(600 * n_fft / 24000)
        
        mag_1000 = np.mean(np.abs(yf[idx_1000-5:idx_1000+5]))
        mag_600 = np.mean(np.abs(yf[idx_600-5:idx_600+5]))
        
        print(f"Mag @ 1000 Hz: {mag_1000:.2f}")
        print(f"Mag @ 600 Hz: {mag_600:.2f}")
        print(f"Ratio 1000/600: {mag_1000/mag_600:.3f}")


        # Check for 1000 Hz specifically (WWV tick)
        # We expect a tone at 1000 Hz.
        # If the file is 16ksps but played at 24ksps, 1000Hz -> 1500Hz.
        # If the file is 12ksps but played at 24ksps, 1000Hz -> 2000Hz.
        
        return True
    else:
        print("Data is not complex.")
        return False

def analyze_zst_file(filepath):
    try:
        dctx = zstd.ZstdDecompressor()
        with open(filepath, 'rb') as ifh:
            # Read all (careful with RAM, but minutes are small ~10MB)
            compressed = ifh.read()
            raw_bytes = dctx.decompress(compressed)
            # JSON said complex64
            data = np.frombuffer(raw_bytes, dtype=np.complex64)
            return analyze_data(data, filepath)
    except Exception as e:
        print(f"Error analyzing ZST {filepath}: {e}")
        return False

def analyze_npy_file(filepath):
    try:
        data = np.load(filepath)
        return analyze_data(data, filepath)
    except Exception as e:
        print(f"Error analyzing NPY {filepath}: {e}")
        return False

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 inspect_audio.py <file>")
        sys.exit(1)
        
    filepath = sys.argv[1]
    if filepath.endswith(".zst"):
        analyze_zst_file(filepath)
    elif filepath.endswith(".npy"):
        analyze_npy_file(filepath)
    else:
        print("Unknown file extension")

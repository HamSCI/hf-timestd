
import zstandard as zstd
import numpy as np
import sys
from pathlib import Path

def test_read(path_str):
    path = Path(path_str)
    if not path.exists():
        print(f"File not found: {path}")
        return

    print(f"Reading {path}...")
    try:
        with open(path, 'rb') as f:
            dctx = zstd.ZstdDecompressor()
            decompressed = dctx.decompress(f.read())
            
        iq_samples = np.frombuffer(decompressed, dtype=np.complex64)
        print(f"Read {len(iq_samples)} samples")
        
        peak = np.max(np.abs(iq_samples)) if len(iq_samples) > 0 else 0.0
        print(f"Peak amplitude: {peak}")
        
        if peak == 0.0:
            print("FAIL: Peak is 0.0")
        else:
            print("SUCCESS: Valid data")
            print(f"First 5 samples: {iq_samples[:5]}")
            
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    test_read("/var/lib/timestd/raw_buffer/SHARED_10000/20260112/1768255620.bin.zst")

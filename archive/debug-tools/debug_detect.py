
import logging
import sys
import numpy as np
import zstandard as zstd
from pathlib import Path

# Setup logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger("hf_timestd")

# Import ToneDetector
sys.path.append("/home/mjh/git/hf-timestd/src")
from hf_timestd.core.tone_detector import MultiStationToneDetector

def test_detect(path_str):
    path = Path(path_str)
    print(f"Loading {path}...")
    
    with open(path, 'rb') as f:
        dctx = zstd.ZstdDecompressor()
        decompressed = dctx.decompress(f.read())
    
    iq_samples = np.frombuffer(decompressed, dtype=np.complex64)
    print(f"Loaded {len(iq_samples)} samples. Max Amp: {np.max(np.abs(iq_samples))}")

    # Create Detector for WWV 10 MHz
    detector = MultiStationToneDetector("WWV_10_MHz", sample_rate=24000)
    
    # Process
    # timestamp passed to detector is buffer midpoint
    timestamp = 1768255620.0 + 30.0
    
    detector.process_samples(
        timestamp=timestamp,
        samples=iq_samples,
        rtp_timestamp=0
    )

if __name__ == "__main__":
    test_detect("/var/lib/timestd/raw_buffer/SHARED_10000/20260112/1768255620.bin.zst")

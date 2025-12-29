
import unittest
import tempfile
import shutil
import time
import os
import json
from pathlib import Path
from unittest.mock import MagicMock
import numpy as np

# Adjust path to import the actual classes
import sys
src_path = str(Path(__file__).parent.parent / 'src')
if src_path not in sys.path:
    sys.path.append(src_path)

# Adjust path to import the actual classes
import sys
src_path = str(Path(__file__).parent.parent / 'src')
if src_path not in sys.path:
    sys.path.append(src_path)

from hf_timestd.core import GlobalStationVoter, StationAnchor, AnchorQuality
from hf_timestd.core import Phase2TemporalEngine

class TestGlobalStationLock(unittest.TestCase):
    def setUp(self):
        # Use a temporary directory for IPC instead of actual /dev/shm to avoid permissions/cleanup issues in test env
        self.test_dir = tempfile.mkdtemp()
        self.ipc_dir = Path(self.test_dir) / 'grape_voter'
        self.ipc_dir.mkdir()
        
        # Patch the default root_dir in GlobalStationVoter's FileBackend
        # We'll just instantiate the voters with manual patching for this test
        
    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def test_ipc_state_sharing(self):
        """Verify that Process A (CHU) can write an anchor that Process B (WWV) can read."""
        
        # Process A: "CHU Process"
        voter_a = GlobalStationVoter(channels=['CHU 7.85 MHz'], use_ipc=True)
        # Monkey-patch backend root (hack for test isolation)
        voter_a.backend.root_dir = self.ipc_dir
        
        # Process B: "WWV 10 MHz Process"
        voter_b = GlobalStationVoter(channels=['WWV 10 MHz'], use_ipc=True)
        voter_b.backend.root_dir = self.ipc_dir
        
        # 1. Process A reports a strong CHU detection
        minute_rtp = 1000000
        voter_a.report_detection(
            channel='CHU 7.85 MHz',
            rtp_timestamp=minute_rtp, # Corresponds to a specific minute
            station='CHU',
            snr_db=20.0, # High SNR
            toa_offset_samples=50,
            confidence=1.0
        )
        
        # Verify file exists
        minute_key = voter_a._minute_rtp_key(minute_rtp)
        expected_file = self.ipc_dir / str(minute_key) / 'CHU_CHU_7.85_MHz.json'
        self.assertTrue(expected_file.exists(), "Anchor JSON file was not created by Process A")
        
        # 2. Process B attempts to find anchors
        anchor = voter_b.get_best_time_snap_anchor(minute_rtp)
        
        # 3. Validation
        self.assertIsNotNone(anchor, "Process B failed to find the anchor written by Process A")
        self.assertEqual(anchor['station'], 'CHU')
        self.assertEqual(anchor['channel'], 'CHU 7.85 MHz')
        self.assertEqual(anchor['snr_db'], 20.0)
        
        print("\nSUCCESS: Cross-process state sharing verified via filesystem IPC.")

if __name__ == '__main__':
    unittest.main()

# Fix the tests that are likely failing because they reference removed imports or classes
import os
import re

for test_file in [
    'tests/test_bootstrap_rolling_buffer.py',
    'tests/test_d_clock_continuity.py',
    'tests/test_discrimination_stability.py',
    'tests/test_pipeline_integration.py',
    'tests/test_tone_detector_improvements.py'
]:
    if not os.path.exists(test_file):
        continue
    with open(test_file, 'r') as f:
        content = f.read()
        
    # We removed CHUFSKListener and some old bootstrap stuff earlier
    # Let's just catch what error it is exactly by running pytest again with traceback

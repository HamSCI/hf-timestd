import re

with open('src/hf_timestd/cli.py', 'r') as f:
    content = f.read()

# Add logic in CLI to apply _expand_channel_groups equivalent for PhaseEngine
# Wait, `_expand_channel_groups` is already a function here.
# Let's inspect `_expand_channel_groups`.


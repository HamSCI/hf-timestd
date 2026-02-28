import re

with open('src/hf_timestd/core/metrology_service.py', 'r') as f:
    content = f.read()

# Make sure we import CHUFSKResult at the top
if 'CHUFSKResult' not in content:
    content = content.replace("from hf_timestd.data_product_registry import DataProductRegistry", "from hf_timestd.data_product_registry import DataProductRegistry\nfrom hf_timestd.core.chu_fsk_decoder import CHUFSKResult")

# Find the spot to update L2 fsk json writer in MetrologyService
# Wait, FSK JSON writes usually go to /dev/shm. MetrologyService processes L2 to HDF5.
# Let's see if MetrologyService has a writer for chu_fsk yet.


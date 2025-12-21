
#!/bin/bash
set -e
# Uninstall multiple times to clear any layers
/opt/hf-timestd/venv/bin/pip uninstall -y hf-timestd || true
/opt/hf-timestd/venv/bin/pip uninstall -y hf-timestd || true

# Install in editable mode
/opt/hf-timestd/venv/bin/pip install -e /home/mjh/git/hf-timestd
echo "Clean Re-installation complete."

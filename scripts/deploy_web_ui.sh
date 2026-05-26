#!/bin/bash
# Deploy web UI changes to bee1 server

set -e

echo "Deploying web UI enhancements to bee1..."

# Copy updated files to bee1
echo "Copying updated files..."
scp web-api/services/propagation_service.py bee1:/tmp/
scp web-api/static/propagation.html bee1:/tmp/
scp web-api/static/metrology.html bee1:/tmp/

# Deploy on bee1
echo "Installing files on bee1..."
ssh bee1 << 'EOF'
    # Backup existing files
    sudo cp /opt/git/sigmond/hf-timestd/venv/lib/python3.11/site-packages/hf_timestd/web-api/services/propagation_service.py \
        /opt/git/sigmond/hf-timestd/venv/lib/python3.11/site-packages/hf_timestd/web-api/services/propagation_service.py.backup
    sudo cp /opt/git/sigmond/hf-timestd/venv/lib/python3.11/site-packages/hf_timestd/web-api/static/propagation.html \
        /opt/git/sigmond/hf-timestd/venv/lib/python3.11/site-packages/hf_timestd/web-api/static/propagation.html.backup
    sudo cp /opt/git/sigmond/hf-timestd/venv/lib/python3.11/site-packages/hf_timestd/web-api/static/metrology.html \
        /opt/git/sigmond/hf-timestd/venv/lib/python3.11/site-packages/hf_timestd/web-api/static/metrology.html.backup
    
    # Install new files
    sudo cp /tmp/propagation_service.py /opt/git/sigmond/hf-timestd/venv/lib/python3.11/site-packages/hf_timestd/web-api/services/
    sudo cp /tmp/propagation.html /opt/git/sigmond/hf-timestd/venv/lib/python3.11/site-packages/hf_timestd/web-api/static/
    sudo cp /tmp/metrology.html /opt/git/sigmond/hf-timestd/venv/lib/python3.11/site-packages/hf_timestd/web-api/static/
    
    # Clean up temp files
    rm /tmp/propagation_service.py /tmp/propagation.html /tmp/metrology.html
    
    # Restart web UI service
    sudo systemctl restart timestd-web-ui
    
    echo "Deployment complete!"
    echo "Checking service status..."
    systemctl status timestd-web-ui --no-pager | head -10
EOF

echo ""
echo "✅ Deployment complete!"
echo ""
echo "Next steps:"
echo "1. Monitor logs: ssh bee1 'sudo journalctl -u timestd-web-ui -f'"
echo "2. Test propagation API: curl 'http://bee1:8000/propagation/conditions' | jq"
echo "3. Test TEC API: curl 'http://bee1:8000/propagation/tec?start=-7d&end=now' | jq"
echo "4. Open browser: http://bee1:8000/static/metrology.html"
echo "5. Open browser: http://bee1:8000/static/propagation.html"

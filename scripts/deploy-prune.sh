#!/bin/bash
# deploy-prune.sh — Install timestd-prune timer and run an initial prune
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

echo "Installing prune-old-data.sh..."
cp "$REPO_ROOT/scripts/prune-old-data.sh" /usr/local/bin/prune-old-data.sh
chmod 755 /usr/local/bin/prune-old-data.sh

echo "Installing systemd units..."
cp "$REPO_ROOT/systemd/timestd-prune.service" /etc/systemd/system/
cp "$REPO_ROOT/systemd/timestd-prune.timer" /etc/systemd/system/

systemctl daemon-reload
systemctl enable --now timestd-prune.timer

echo "Timer status:"
systemctl status timestd-prune.timer --no-pager

echo ""
echo "Running initial prune (dry-run first)..."
/usr/local/bin/prune-old-data.sh --dry-run

echo ""
read -p "Apply prune? (yes/no): " CONFIRM
if [[ "$CONFIRM" == "yes" ]]; then
    /usr/local/bin/prune-old-data.sh
else
    echo "Skipped live prune. Run manually: sudo /usr/local/bin/prune-old-data.sh"
fi

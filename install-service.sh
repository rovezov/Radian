#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_FILE="$SCRIPT_DIR/radian-ui.service"

if [ ! -f "$SERVICE_FILE" ]; then
  echo "Error: radian-ui.service not found in $SCRIPT_DIR"
  exit 1
fi

echo "Installing Radian UI service..."
echo "Make sure you've edited radian-ui.service with your username and paths first!"
echo ""

sudo cp "$SERVICE_FILE" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable radian-ui
sudo systemctl start radian-ui
sudo systemctl status radian-ui

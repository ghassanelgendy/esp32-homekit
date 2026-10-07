#!/usr/bin/env bash
set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "=== Installing ESP32 Room USB Bridge on this PC ==="

# Check python3 and pyserial
if ! python3 -c "import serial" 2>/dev/null; then
    echo "[*] Installing pyserial..."
    pip3 install pyserial || sudo apt-get install -y python3-serial
fi

# Ensure user is in dialout group for serial access
if ! groups | grep -q "dialout"; then
    echo "[!] Adding $USER to dialout group for serial port access..."
    sudo usermod -a -G dialout "$USER"
    echo "[!] Please re-login or run 'newgrp dialout' if permission is denied."
fi

# Copy script to home or keep here
DEST="$HOME/esp_room_bridge.py"
cp "$DIR/esp_room_bridge.py" "$DEST"
chmod +x "$DEST"

# Setup systemd user service
SERVICE_DIR="$HOME/.config/systemd/user"
mkdir -p "$SERVICE_DIR"

cat <<EOF > "$SERVICE_DIR/esp-room-bridge.service"
[Unit]
Description=ESP32 Room USB Bridge
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 -u $DEST
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable esp-room-bridge.service
systemctl --user restart esp-room-bridge.service

sleep 1
systemctl --user status esp-room-bridge.service --no-pager

echo ""
echo "=== ESP32 Room Bridge installed and running on port 8881! ==="

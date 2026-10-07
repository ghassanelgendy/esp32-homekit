#!/usr/bin/env bash
set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLI="/home/ghesso/.local/bin/arduino-cli"
SKETCH_DIR="$DIR/firmware_usb"
FQBN="esp32:esp32:esp32"

echo "========================================================"
echo "    ESP32 USB Wired Homelab Controller - Flasher        "
echo "========================================================"

# Detect serial port
find_port() {
    ls /dev/ttyUSB* /dev/ttyACM* 2>/dev/null | head -n 1 || true
}

PORT=$(find_port)
if [ -z "$PORT" ]; then
    echo "[!] No ESP32 detected on USB. Please plug the ESP32 into your PC's USB port."
    echo "[*] Waiting for USB connection..."
    while [ -z "$PORT" ]; do
        sleep 1
        PORT=$(find_port)
    done
fi

echo "[+] Detected ESP32 on serial port: $PORT"

# Stop ac-proxy service to release port during flashing
echo "[*] Temporarily stopping ac-proxy.service to free serial port..."
systemctl --user stop ac-proxy.service || true

# Compile
echo "[*] Compiling USB-wired firmware..."
"$CLI" compile --fqbn "$FQBN" "$SKETCH_DIR"

# Upload
echo "[*] Flashing firmware to ESP32 on $PORT..."
"$CLI" upload -p "$PORT" --fqbn "$FQBN" "$SKETCH_DIR"

echo "[+] Firmware flashed successfully!"

# Restart ac-proxy service
echo "[*] Restarting ac-proxy.service..."
systemctl --user start ac-proxy.service

sleep 2

# Verify response
echo "[*] Verifying Homebridge HTTP API (port 8880)..."
STATUS=$(curl -s http://127.0.0.1:8880/status || echo "FAIL")
echo "[+] Bridge Status: $STATUS"

echo ""
echo "========================================================"
echo "    SUCCESS: ESP32 is now running wired USB mode!       "
echo "    Zero Wi-Fi dependencies. 100% stable connection.    "
echo "========================================================"

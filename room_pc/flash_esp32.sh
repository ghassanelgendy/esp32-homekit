#!/usr/bin/env bash
set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN="$DIR/bin/firmware_usb.ino.merged.bin"

echo "=== ESP32 USB Firmware Flasher ==="

# Check for esptool
if ! command -v esptool.py &>/dev/null && ! python3 -m esptool &>/dev/null; then
    echo "[*] Installing esptool..."
    pip3 install esptool || sudo apt-get install -y esptool
fi

ESPTOOL_CMD="esptool.py"
if ! command -v esptool.py &>/dev/null; then
    ESPTOOL_CMD="python3 -m esptool"
fi

# Detect serial port
find_port() {
    ls /dev/ttyUSB* /dev/ttyACM* 2>/dev/null | head -n 1 || true
}

PORT=$(find_port)
if [ -z "$PORT" ]; then
    echo "[!] No ESP32 detected on USB. Please plug the ESP32 into this PC's USB port."
    echo "[*] Waiting for USB connection..."
    while [ -z "$PORT" ]; do
        sleep 1
        PORT=$(find_port)
    done
fi

echo "[+] Detected ESP32 on $PORT"
echo "[*] Flashing merged firmware image..."
$ESPTOOL_CMD --chip esp32 --port "$PORT" --baud 921600 write_flash 0x0 "$BIN"

echo ""
echo "=== ESP32 successfully flashed with USB-only firmware! ==="

#!/usr/bin/env python3
"""
esp_serial_service.py - Resilient background ESP32 serial monitor service.
Appends all serial output with timestamp, detects WiFi drops, and immediately
flushes and syncs to disk (so even on PC crashes, no log entries are lost).
"""

import sys
import os
import time
import datetime
import re
import signal
import glob
from typing import Optional

try:
    import serial
    import serial.tools.list_ports
except ImportError:
    print("[ERROR] pyserial is required. Install with: pip install pyserial", file=sys.stderr)
    sys.exit(1)

LOG_FILE = "/home/ghesso/homekit/logs/LOGSMAIN.log"
DROP_LOG = "/home/ghesso/homekit/logs/esp32_drops_events.log"
BAUD_RATE = 115200

KNOWN_PATTERNS = [
    "cp210", "ch340", "ch341", "ftdi", "usb serial", "usb-serial",
    "uart", "esp32", "silicon labs", "wch.cn"
]

def find_esp_port() -> Optional[str]:
    fs_devs = sorted(glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*"))
    if fs_devs:
        return fs_devs[0]
    for p in serial.tools.list_ports.comports():
        desc = (p.description or "").lower()
        hwid = (p.hwid or "").lower()
        mfg = (p.manufacturer or "").lower()
        combined = f"{desc} {hwid} {mfg}"
        for pattern in KNOWN_PATTERNS:
            if pattern in combined:
                return p.device
    return None

def format_ts() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

def write_and_sync(f, text: str):
    f.write(text)
    f.flush()
    try:
        os.fsync(f.fileno())
    except Exception:
        pass

def main():
    os.makedirs("/home/ghesso/homekit/logs", exist_ok=True)
    
    running = True
    def handle_signal(sig, frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    log_f = open(LOG_FILE, "a", encoding="utf-8", buffering=1)
    drop_f = open(DROP_LOG, "a", encoding="utf-8", buffering=1)

    init_msg = f"\n=== [SERVICE STARTED] {format_ts()} (Logging to {LOG_FILE}) ===\n"
    print(init_msg.strip())
    write_and_sync(log_f, init_msg)

    last_waiting_msg = 0
    ser = None

    while running:
        port = find_esp_port()
        if not port:
            now = time.time()
            if now - last_waiting_msg > 10:
                print(f"[{format_ts()}] [WAITING] ESP32 USB device not detected. Waiting...")
                last_waiting_msg = now
            time.sleep(2)
            continue

        try:
            ser = serial.Serial(port=port, baudrate=BAUD_RATE, timeout=2.0)
            # Clear old accumulated backpressure in kernel buffer
            ser.reset_input_buffer()
            conn_msg = f"[{format_ts()}] [CONNECTED] Serial port {port} opened at {BAUD_RATE} baud\n"
            print(conn_msg.strip())
            write_and_sync(log_f, conn_msg)

            buffer = bytearray()
            while running:
                raw = ser.read(ser.in_waiting or 1)
                if not raw:
                    continue
                buffer.extend(raw)

                while b"\n" in buffer:
                    line_bytes, buffer = buffer.split(b"\n", 1)
                    line = line_bytes.decode("utf-8", errors="replace").rstrip("\r")
                    if not line:
                        continue
                    ts = format_ts()
                    out_line = f"[{ts}] {line}\n"
                    
                    # Print to standard output
                    print(out_line, end="")
                    # Write and fsync immediately so crashes cannot lose data
                    write_and_sync(log_f, out_line)

                    # Check for disconnect or reboot events
                    line_lower = line.lower()
                    if (re.search(r"wifi.*disconnect", line_lower)
                            or "wl_disconnected" in line_lower
                            or "fast re-attempting" in line_lower
                            or "wifi link down" in line_lower):
                        drop_entry = f"[{ts}] [DROP_ALERT] {line}\n"
                        write_and_sync(drop_f, drop_entry)
                    elif ("rst:0x" in line_lower or "ets jun" in line_lower 
                            or "reset reason" in line_lower):
                        reboot_entry = f"[{ts}] [REBOOT_ALERT] {line}\n"
                        write_and_sync(drop_f, reboot_entry)

        except (serial.SerialException, OSError) as e:
            err_msg = f"[{format_ts()}] [SERIAL_ERROR] Port error on {port}: {e}\n"
            print(err_msg.strip())
            write_and_sync(log_f, err_msg)
            time.sleep(3)
        finally:
            if ser and ser.is_open:
                try:
                    ser.close()
                except Exception:
                    pass
            ser = None

    stop_msg = f"=== [SERVICE STOPPED] {format_ts()} ===\n"
    print(stop_msg.strip())
    write_and_sync(log_f, stop_msg)
    log_f.close()
    drop_f.close()

if __name__ == "__main__":
    main()

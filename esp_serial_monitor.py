#!/usr/bin/env python3
"""
esp_serial_monitor.py - Continuous ESP32 serial monitor & Wi-Fi drop tracker.
Logs all serial messages with millisecond timestamps for 12 hours (configurable),
tracks disconnect events/reasons, and handles automatic reconnection if disconnected.
"""

import sys
import os
import time
import datetime
import re
import json
import signal
import argparse
from typing import Optional

try:
    import serial
    import serial.tools.list_ports
except ImportError:
    print("[ERROR] pyserial is required. Install with: pip install pyserial", file=sys.stderr)
    sys.exit(1)

# ESP32 USB-to-UART known hardware IDs or patterns
KNOWN_PATTERNS = [
    "cp210", "ch340", "ch341", "ftdi", "usb serial", "usb-serial",
    "uart", "esp32", "silicon labs", "wch.cn"
]

def find_esp_port() -> Optional[str]:
    """Scan available serial ports for likely ESP32 USB bridges."""
    import glob
    # Priority 1: Check filesystem /dev/ttyUSB* and /dev/ttyACM* nodes directly
    fs_devs = sorted(glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*"))
    if fs_devs:
        return fs_devs[0]

    ports = list(serial.tools.list_ports.comports())
    
    # Priority 2: Match by description / hwid / manufacturer
    for p in ports:
        desc = (p.description or "").lower()
        hwid = (p.hwid or "").lower()
        mfg = (p.manufacturer or "").lower()
        combined = f"{desc} {hwid} {mfg}"
        for pattern in KNOWN_PATTERNS:
            if pattern in combined:
                return p.device

    return None

def format_ts() -> str:
    now = datetime.datetime.now()
    return now.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

def main():
    parser = argparse.ArgumentParser(description="ESP32 12-Hour Serial Drop Monitor")
    parser.add_argument("--port", type=str, default=None, help="Serial port (e.g. /dev/ttyUSB0). If omitted, auto-detected.")
    parser.add_argument("--baud", type=int, default=115200, help="Baud rate (default: 115200)")
    parser.add_argument("--hours", type=float, default=12.0, help="Duration to run in hours (default: 12)")
    parser.add_argument("--logfile", type=str, default=None, help="Output log file path")
    parser.add_argument("--summary", type=str, default=None, help="Output summary JSON file path")
    args = parser.parse_args()

    os.makedirs("/home/ghesso/homekit/logs", exist_ok=True)
    start_ts_str = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    
    log_path = args.logfile or f"/home/ghesso/homekit/logs/esp32_drops_{start_ts_str}.log"
    summary_path = args.summary or f"/home/ghesso/homekit/logs/esp32_summary_{start_ts_str}.json"

    duration_seconds = int(args.hours * 3600)
    end_time = time.time() + duration_seconds

    stats = {
        "start_time": datetime.datetime.now().isoformat(),
        "target_hours": args.hours,
        "total_lines": 0,
        "drop_events": 0,
        "reconnect_events": 0,
        "disconnect_reasons": {},
        "boot_resets": 0,
        "drop_log": []
    }

    running = True

    def handle_signal(sig, frame):
        nonlocal running
        print(f"\n[{format_ts()}] [SHUTDOWN] Interrupted by signal {sig}. Exiting gracefully...")
        running = False

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    print(f"==================================================")
    print(f" ESP32 SERIAL MONITOR & DROP TRACKER")
    print(f" Duration:       {args.hours} hours (ends ~{datetime.datetime.fromtimestamp(end_time).strftime('%Y-%m-%d %H:%M:%S')})")
    print(f" Baud rate:      {args.baud}")
    print(f" Log file:       {log_path}")
    print(f" Summary file:   {summary_path}")
    print(f" Auto-detect:    {'Disabled (' + args.port + ')' if args.port else 'Enabled'}")
    print(f"==================================================")

    log_file = open(log_path, "a", encoding="utf-8", buffering=1)
    log_file.write(f"=== Session started at {format_ts()} ===\n")
    log_file.flush()

    last_waiting_log = 0
    ser = None

    try:
        while running and time.time() < end_time:
            # 1. Resolve port
            port = args.port or find_esp_port()
            if not port:
                now = time.time()
                if now - last_waiting_log > 5:
                    print(f"[{format_ts()}] [WAITING] ESP32 not detected. Waiting for device to plug in...")
                    last_waiting_log = now
                time.sleep(1)
                continue

            # 2. Try opening serial connection
            try:
                print(f"[{format_ts()}] [CONNECTING] Opening {port} at {args.baud} baud...")
                ser = serial.Serial(port=port, baudrate=args.baud, timeout=1.0)
                print(f"[{format_ts()}] [CONNECTED] Serial port {port} opened successfully! Monitoring output...")
                log_file.write(f"[{format_ts()}] [SYSTEM] Connected to {port} @ {args.baud}\n")
                log_file.flush()

                buffer = bytearray()
                while running and time.time() < end_time:
                    try:
                        raw = ser.read(ser.in_waiting or 1)
                        if not raw:
                            continue
                        buffer.extend(raw)

                        while b"\n" in buffer:
                            line_bytes, buffer = buffer.split(b"\n", 1)
                            line = line_bytes.decode("utf-8", errors="replace").rstrip("\r")
                            stats["total_lines"] += 1
                            ts = format_ts()

                            formatted_line = f"[{ts}] {line}"
                            print(formatted_line)
                            log_file.write(formatted_line + "\n")
                            log_file.flush()

                            # Detect Wi-Fi disconnect events
                            line_lower = line.lower()
                            is_wifi_disconnect = bool(
                                re.search(r"wifi.*disconnect", line_lower)
                                or "wl_disconnected" in line_lower
                                or "fast re-attempting" in line_lower
                            )
                            if is_wifi_disconnect:
                                stats["drop_events"] += 1
                                reason_match = re.search(r"reason(?:\s+code)?[:=\s]+(\d+)", line, re.IGNORECASE)
                                reason_str = reason_match.group(1) if reason_match else "unknown"
                                stats["disconnect_reasons"][reason_str] = stats["disconnect_reasons"].get(reason_str, 0) + 1
                                drop_record = {"timestamp": ts, "reason": reason_str, "line": line}
                                stats["drop_log"].append(drop_record)
                                print(f"[{ts}] >>> [ALERT] Wi-Fi drop detected! Total drops: {stats['drop_events']} (Reason: {reason_str})")

                            # Detect reconnection events
                            elif "got ip" in line_lower or "associated to ap" in line_lower or "connection fully established" in line_lower:
                                stats["reconnect_events"] += 1
                                print(f"[{ts}] >>> [INFO] Wi-Fi connected/restored! Total drops recorded: {stats['drop_events']}")

                            # Detect ESP32 boot resets
                            elif "rst:0x" in line_lower or "ets jun" in line_lower or "homekit controller booting" in line_lower or "reset reason core" in line_lower:
                                stats["boot_resets"] += 1
                                print(f"[{ts}] >>> [ALERT] ESP32 reboot detected! Total boots: {stats['boot_resets']}")

                    except serial.SerialException as se:
                        print(f"[{format_ts()}] [DISCONNECTED] Serial exception: {se}. Re-scanning in 2s...")
                        log_file.write(f"[{format_ts()}] [SYSTEM] Serial exception: {se}\n")
                        log_file.flush()
                        break

            except serial.SerialException as e:
                err_str = str(e)
                now = time.time()
                if now - last_waiting_log > 4:
                    if "permission denied" in err_str.lower():
                        print(f"[{format_ts()}] [PERMISSION DENIED] Cannot access {port}!")
                        print(f"[{format_ts()}] Fix with: sudo chmod 666 {port}   (or run monitor with sudo)")
                    else:
                        print(f"[{format_ts()}] [CONNECT ERROR] Failed opening {port}: {e}")
                    last_waiting_log = now
                time.sleep(2)
            finally:
                if ser and ser.is_open:
                    try:
                        ser.close()
                    except Exception:
                        pass
                ser = None

    finally:
        end_iso = datetime.datetime.now().isoformat()
        stats["end_time"] = end_iso
        stats["status"] = "completed" if time.time() >= end_time else "stopped_early"

        # Write summary JSON
        try:
            with open(summary_path, "w", encoding="utf-8") as sf:
                json.dump(stats, sf, indent=2)
            print(f"\n[{format_ts()}] [SUMMARY] Saved summary JSON to {summary_path}")
        except Exception as e:
            print(f"Error saving summary JSON: {e}", file=sys.stderr)

        log_file.write(f"=== Session ended at {format_ts()} ===\n")
        log_file.write(f"Total lines: {stats['total_lines']}, Total drops: {stats['drop_events']}, Resets: {stats['boot_resets']}\n")
        log_file.close()

        print(f"\n==================== SESSION STATS ====================")
        print(f" Status:           {stats['status']}")
        print(f" Total drops:      {stats['drop_events']}")
        print(f" Disconnect codes: {stats['disconnect_reasons']}")
        print(f" Reboots/Resets:   {stats['boot_resets']}")
        print(f" Total lines logged: {stats['total_lines']}")
        print(f" Log saved to:     {log_path}")
        print(f"=======================================================")

if __name__ == "__main__":
    main()

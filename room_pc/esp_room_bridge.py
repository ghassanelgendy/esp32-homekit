#!/usr/bin/env python3
"""
esp_room_bridge.py - Lightweight USB Serial to HTTP Bridge for the Room PC.
Runs on the Room PC where the ESP32 is physically connected via USB cable.
Exposes an HTTP server on port 8881 that forwards commands to the ESP32 via Serial.
Zero Wi-Fi dependencies on the ESP32.
"""

import sys
import os
import time
import glob
import json
import threading
from typing import Optional, Dict, Any
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
import urllib.parse

try:
    import serial
    import serial.tools.list_ports
except ImportError:
    print("[ERROR] pyserial is required. Install with: pip install pyserial", file=sys.stderr)
    sys.exit(1)

PORT = 8882
BAUD_RATE = 115200

KNOWN_PATTERNS = [
    "cp210", "ch340", "ch341", "ftdi", "usb serial", "usb-serial",
    "uart", "esp32", "silicon labs", "wch.cn"
]

class SerialManager:
    def __init__(self, port: Optional[str] = None):
        self.preferred_port = port
        self.ser: Optional[serial.Serial] = None
        self.lock = threading.Lock()
        self.connected = False
        self.current_port = None
        self.running = True

    def find_port(self) -> Optional[str]:
        if self.preferred_port and os.path.exists(self.preferred_port):
            return self.preferred_port
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

    def connect(self) -> bool:
        port = self.find_port()
        if not port:
            return False
        try:
            ser = serial.Serial()
            ser.port = port
            ser.baudrate = BAUD_RATE
            ser.timeout = 1.0
            ser.dtr = False
            ser.rts = False
            ser.open()
            ser.reset_input_buffer()
            self.ser = ser
            self.current_port = port
            self.connected = True
            print(f"[Room Bridge] Connected to ESP32 on {port} at {BAUD_RATE} baud.")
            time.sleep(0.1)
            return True
        except Exception as e:
            self.ser = None
            self.connected = False
            return False

    def close(self):
        if self.ser:
            try:
                self.ser.close()
            except Exception:
                pass
            self.ser = None
        self.connected = False

    def send_cmd(self, cmd: str, timeout: float = 2.0) -> str:
        with self.lock:
            if not self.connected or not self.ser or not self.ser.is_open:
                if not self.connect():
                    raise ConnectionError("ESP32 USB Serial not connected")
            try:
                self.ser.reset_input_buffer()
                self.ser.write(f"{cmd.strip()}\n".encode("utf-8"))
                self.ser.flush()

                start = time.time()
                while time.time() - start < timeout:
                    line = self.ser.readline().decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    if line.startswith("[EVENT]") or line.startswith("[ESP32]"):
                        print(f"[ESP32 Event] {line}")
                        continue
                    return line
                raise TimeoutError(f"Serial command '{cmd}' timed out")
            except (serial.SerialException, OSError) as e:
                print(f"[Room Bridge] Serial error on {self.current_port}: {e}")
                self.close()
                raise e

manager = SerialManager()

class RoomBridgeHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def send_text(self, text: str, status: int = 200):
        try:
            self.send_response(status)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(str(text).encode("utf-8"))
        except (BrokenPipeError, ConnectionResetError):
            pass

    def send_json(self, data: Any, status: int = 200):
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            if isinstance(data, (dict, list)):
                self.wfile.write(json.dumps(data).encode("utf-8"))
            else:
                self.wfile.write(str(data).encode("utf-8"))
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        # Health check
        if path == "/ping":
            self.send_text("PONG")
            return

        try:
            # 1. Status queries
            if path == "/status":
                res = manager.send_cmd("STATUS")
                self.send_json(res)
                return

            if path == "/lamps/status":
                res = manager.send_cmd("LAMPS")
                self.send_json(res)
                return

            if path == "/led/status":
                res = manager.send_cmd("LED")
                self.send_json(res)
                return

            # 2. AC Controls
            if path == "/targetTemperature":
                val = query.get("value", ["24"])[0]
                res = manager.send_cmd(f"SET ac_temp {val}")
                self.send_text(res)
                return

            if path == "/targetHeatingCoolingState":
                val = query.get("value", ["0"])[0]
                force = " 1" if query.get("force", ["0"])[0] == "1" else ""
                res = manager.send_cmd(f"SET ac_state {val}{force}")
                self.send_text(res)
                return

            if path == "/ac/mode":
                res = manager.send_cmd("SET ac_mode")
                self.send_text(res)
                return

            if path == "/ac/mode/status":
                res = manager.send_cmd("GET ac_mode")
                self.send_text(res)
                return

            if path == "/ac/fan":
                res = manager.send_cmd("SET ac_fan")
                self.send_text(res)
                return

            if path == "/ac/fan/status":
                res = manager.send_cmd("GET ac_fan")
                self.send_text(res)
                return

            if path == "/ac/fan/speed":
                val = query.get("value", ["33"])[0]
                res = manager.send_cmd(f"SET ac_fan_speed {val}")
                self.send_text(res)
                return

            if path == "/ac/fan/active":
                self.send_text("1")
                return

            if path.startswith("/ac/swing"):
                if path.endswith("/status"):
                    res = manager.send_cmd("GET ac_swing")
                elif path.endswith("/on"):
                    res = manager.send_cmd("SET ac_swing 1")
                elif path.endswith("/off"):
                    res = manager.send_cmd("SET ac_swing 0")
                else:
                    res = manager.send_cmd("SET ac_swing")
                self.send_text(res)
                return

            if path.startswith("/ac/eco"):
                if path.endswith("/status"):
                    res = manager.send_cmd("GET ac_eco")
                elif path.endswith("/on"):
                    res = manager.send_cmd("SET ac_eco 1")
                elif path.endswith("/off"):
                    res = manager.send_cmd("SET ac_eco 0")
                else:
                    res = manager.send_cmd("SET ac_eco")
                self.send_text(res)
                return

            if path == "/ac/timer/status":
                res = manager.send_cmd("GET ac_timer")
                self.send_text(res)
                return

            if path == "/ac/timer/on":
                res = manager.send_cmd("SET ac_timer_on")
                self.send_text(res)
                return

            if path == "/ac/timer/off":
                res = manager.send_cmd("SET ac_timer_off")
                self.send_text(res)
                return

            if path == "/ac/timer":
                val = query.get("value", ["0"])[0]
                res = manager.send_cmd(f"SET ac_timer {val}")
                self.send_text(res)
                return

            # 3. Lamp / Relay Controls: /lamp/<name>/<on|off|status>
            if path.startswith("/lamp/"):
                parts = path.strip("/").split("/")
                if len(parts) == 3:
                    lamp_name = parts[1]
                    action = parts[2]
                    if action == "status":
                        res = manager.send_cmd(f"GET lamp {lamp_name}")
                    else:
                        val = "1" if action == "on" else "0"
                        res = manager.send_cmd(f"SET lamp {lamp_name} {val}")
                    self.send_text(res)
                    return

            # 4. LED Strip Controls
            if path == "/led/on":
                res = manager.send_cmd("SET led_power 1")
                self.send_text(res)
                return

            if path == "/led/off":
                res = manager.send_cmd("SET led_power 0")
                self.send_text(res)
                return

            if path == "/led/hue":
                val = query.get("value", ["0"])[0]
                res = manager.send_cmd(f"SET led_hue {val}")
                self.send_text(res)
                return

            if path == "/led/saturation":
                val = query.get("value", ["0"])[0]
                res = manager.send_cmd(f"SET led_sat {val}")
                self.send_text(res)
                return

            if path == "/led/brightness":
                val = query.get("value", ["100"])[0]
                res = manager.send_cmd(f"SET led_bri {val}")
                self.send_text(res)
                return

            if path == "/led/mode":
                res = manager.send_cmd("SET led_mode")
                self.send_text(res)
                return

            if path == "/led/mode/status":
                res = manager.send_cmd("GET led_mode")
                self.send_text(res)
                return

            if path == "/led/speed":
                res = manager.send_cmd("SET led_speed")
                self.send_text(res)
                return

            if path == "/led/speed/status":
                res = manager.send_cmd("GET led_speed")
                self.send_text(res)
                return

            if path == "/led/timer/status":
                res = manager.send_cmd("GET led_timer")
                self.send_text(res)
                return

            if path == "/led/timer":
                val = query.get("value", ["0"])[0]
                res = manager.send_cmd(f"SET led_timer {val}")
                self.send_text(res)
                return

            # 5. Raw Commands
            if path == "/send_raw_power":
                res = manager.send_cmd("SET ac_raw_power")
                self.send_text(res)
                return

            if path == "/raw_temp_steps":
                target = query.get("target", ["24"])[0]
                res = manager.send_cmd(f"SET ac_raw_temp_steps {target}")
                self.send_text(res)
                return

            if path == "/set_state_raw":
                st = query.get("state", ["0"])[0]
                res = manager.send_cmd(f"SET ac_state_raw {st}")
                self.send_text(res)
                return

            if path == "/set_state_memory":
                temp = query.get("temp", ["24"])[0]
                st = query.get("state", ["0"])[0]
                res = manager.send_cmd(f"SET ac_state_mem {temp} {st}")
                self.send_text(res)
                return

            self.send_error(404, "Endpoint not found")

        except Exception as e:
            self.send_error(500, str(e))

class ReusableThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True

def main():
    print(f"=================================================")
    print(f"  ESP32 Room USB-to-HTTP Bridge                  ")
    print(f"  Listening on: http://0.0.0.0:{PORT}            ")
    print(f"=================================================")

    # Initial connect attempt
    manager.connect()

    server = ReusableThreadingHTTPServer(("0.0.0.0", PORT), RoomBridgeHandler)
    server.serve_forever()

if __name__ == "__main__":
    main()


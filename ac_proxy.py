#!/usr/bin/env python3
"""
ac_proxy.py - Homebridge HTTP Bridge for ESP32 Room Controller.
Bridges Homebridge (on this server) to the ESP32 wired via USB to the Room PC (192.168.1.110:8881).
Also supports local USB connection as direct fallback.
Zero Wi-Fi dependencies on the ESP32.
"""

import sys
import os
import time
import datetime
import json
import glob
import re
import signal
import threading
from typing import Optional, Dict, Any
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
import urllib.request
import urllib.parse

# Configuration
PORT = 8880
ROOM_BRIDGE_URL = os.environ.get("ROOM_BRIDGE_URL", "http://192.168.1.110:8882")
DATA_DIR = "/data" if os.path.isdir("/data") else os.path.join(os.path.dirname(__file__), "ac_proxy_data")
os.makedirs(DATA_DIR, exist_ok=True)
LOGS_DIR = "/logs" if os.path.isdir("/logs") else os.path.join(os.path.dirname(__file__), "logs")
os.makedirs(LOGS_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOGS_DIR, "LOGSMAIN.log")

STATE_FILE = os.path.join(DATA_DIR, "ac_last_temp.txt")
FAN_LEVEL_STATE_FILE = os.path.join(DATA_DIR, "ac_last_fan_level.txt")
SWAP_STATE_FILE = os.path.join(DATA_DIR, "ac_swap_state.json")

def format_ts() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

def write_and_sync(filepath: str, text: str):
    try:
        with open(filepath, "a", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            try:
                os.fsync(f.fileno())
            except Exception:
                pass
    except Exception as e:
        print(f"[Log Error] {e}", file=sys.stderr)

# Global State
lock = threading.Lock()
is_calibrating = False
active_target_temp = 24
fan_speed_level = 3
virtual_state_override = None

# AC / Fan Alternate Swap state
swap_active = False
swap_interval_minutes = 30.0
AC_PHASE_MIN_MINUTES = 15.0
AC_PHASE_MAX_MINUTES = 45.0
AC_PHASE_RATIO = 1.0 / 6.0
swap_thread = None
swap_stop_event = threading.Event()
swap_update_event = threading.Event()
swap_state_lock = threading.Lock()

def save_swap_state(active, interval_minutes):
    with swap_state_lock:
        try:
            tmp_path = f"{SWAP_STATE_FILE}.{os.getpid()}.{threading.get_ident()}.tmp"
            with open(tmp_path, "w") as f:
                json.dump({"active": bool(active), "interval_minutes": float(interval_minutes)}, f)
            os.replace(tmp_path, SWAP_STATE_FILE)
        except Exception as e:
            print(f"[AC Proxy] Error saving swap state: {e}")

def load_swap_state():
    global swap_interval_minutes
    try:
        if os.path.exists(SWAP_STATE_FILE):
            with open(SWAP_STATE_FILE, "r") as f:
                data = json.load(f)
                swap_interval_minutes = float(data.get("interval_minutes", 30.0))
                return bool(data.get("active", False))
    except Exception as e:
        print(f"[AC Proxy] Error loading swap state: {e}")
    return False

def get_ac_phase_minutes():
    return max(AC_PHASE_MIN_MINUTES, min(AC_PHASE_MAX_MINUTES, swap_interval_minutes * AC_PHASE_RATIO))

def load_last_temp():
    global active_target_temp
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r") as f:
                val = int(f.read().strip())
                if 16 <= val <= 30:
                    active_target_temp = val
    except Exception:
        pass
    return active_target_temp

def save_last_temp(temp):
    global active_target_temp
    try:
        t = int(float(temp))
        if 16 <= t <= 30:
            active_target_temp = t
            with open(STATE_FILE, "w") as f:
                f.write(str(t))
    except Exception:
        pass

def load_last_fan_level():
    global fan_speed_level
    try:
        if os.path.exists(FAN_LEVEL_STATE_FILE):
            with open(FAN_LEVEL_STATE_FILE, "r") as f:
                val = int(f.read().strip())
                if 1 <= val <= 3:
                    fan_speed_level = val
    except Exception:
        pass
    return fan_speed_level

def save_fan_level(level):
    global fan_speed_level
    try:
        lvl = int(level)
        if 1 <= lvl <= 3:
            fan_speed_level = lvl
            with open(FAN_LEVEL_STATE_FILE, "w") as f:
                f.write(str(lvl))
    except Exception:
        pass

load_last_temp()
load_last_fan_level()

# =========================================================================
#  COMMUNICATION LAYER (Local USB Serial Primary + Room PC Bridge Fallback)
# =========================================================================

try:
    import serial
    import serial.tools.list_ports
    HAS_SERIAL = True
except ImportError:
    HAS_SERIAL = False

last_serial_cmd_time = 0.0

class LocalSerialManager:
    def __init__(self):
        self.ser = None
        self.lock = threading.Lock()
        self.connected = False
        self.port = None

    def find_port(self):
        by_id = glob.glob("/dev/serial/by-id/usb-Silicon_Labs*") + glob.glob("/dev/serial/by-id/*")
        if by_id:
            try:
                return os.path.realpath(by_id[0])
            except Exception:
                pass
        devs = sorted(glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*"))
        return devs[0] if devs else None

    def connect(self):
        if not HAS_SERIAL:
            return False
        port = self.find_port()
        if not port:
            self.close()
            return False
        if self.connected and self.ser and self.ser.is_open and self.port == port:
            return True
        try:
            # Disable HUPCL (hangup-on-close) so DTR does not drop
            os.system(f"stty -F {port} -hupcl 2>/dev/null")
            ser = serial.Serial()
            ser.port = port
            ser.baudrate = 115200
            ser.timeout = 0.6
            ser.write_timeout = 0.6
            ser.dtr = False
            ser.rts = False
            ser.open()
            time.sleep(0.1)
            ser.reset_input_buffer()
            self.ser = ser
            self.port = port
            self.connected = True
            print(f"[{format_ts()}] [LOCAL USB] Connected to ESP32 on {port} directly!")
            write_and_sync(LOG_FILE, f"[{format_ts()}] [LOCAL USB] Connected to ESP32 on {port} directly!\n")
            return True
        except Exception as e:
            self.close()
            return False

    def close(self):
        if self.ser:
            try:
                self.ser.close()
            except Exception:
                pass
            self.ser = None
        self.connected = False
        self.port = None

    def send_cmd(self, cmd: str, timeout: float = 0.8, retries: int = 2) -> str:
        global last_serial_cmd_time
        with self.lock:
            last_serial_cmd_time = time.time()
            if not self.connect():
                raise ConnectionError("No local USB ESP32 connected")
            for attempt in range(retries):
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
                            write_and_sync(LOG_FILE, f"[{format_ts()}] {line}\n")
                            continue
                        return line
                    if attempt < retries - 1:
                        time.sleep(0.05)
                        continue
                    # Command timed out: do NOT close port to prevent ESP32 reboot!
                    print(f"[{format_ts()}] [Serial Timeout] Command '{cmd}' timed out after {timeout}s")
                    return "ERR_TIMEOUT"
                except (serial.SerialException, OSError) as e:
                    if attempt < retries - 1:
                        time.sleep(0.1)
                        if self.connect():
                            continue
                    print(f"[{format_ts()}] [Serial Disconnect] Port error: {e}")
                    self.close()
                    raise e

local_serial = LocalSerialManager()

# Status Cache in Memory
cache: Dict[str, Any] = {
    "status": {
        "targetTemperature": float(active_target_temp),
        "currentTemperature": float(active_target_temp),
        "targetHeatingCoolingState": 0,
        "currentHeatingCoolingState": 0
    },
    "lamps": {"main": False, "fairy": False, "fan": False, "backlight": False},
    "led": {"power": False, "brightness": 100, "hue": 0, "saturation": 0, "mode": 1, "speed": 1},
    "ac_swing": "0",
    "ac_eco": "0",
    "ac_mode": "1",
    "ac_fan": "3",
    "ac_timer": "0",
    "led_timer": "0"
}

is_backend_online = False

def send_to_backend(path: str, timeout: float = 2.0) -> bytes:
    """ Sends command to Local USB Serial if connected, else forwards to Room PC bridge """
    global is_backend_online

    # 1. Check if ESP32 is plugged directly into this server
    if local_serial.connect():
        is_backend_online = True
        return execute_serial_for_path(path).encode("utf-8")

    # If local USB port exists, avoid hanging for 2s on offline LAN bridge
    if local_serial.find_port():
        is_backend_online = False
        raise ConnectionError("Local ESP32 USB port detected but failed to connect")

    # 2. Otherwise route to Room PC Bridge over LAN
    url = f"{ROOM_BRIDGE_URL}{path}"
    try:
        req = urllib.request.Request(url, headers={"Connection": "close"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read()
            is_backend_online = True
            return data
    except Exception as e:
        is_backend_online = False
        raise e

def execute_serial_for_path(path: str) -> str:
    """ Maps HTTP path to direct serial command when ESP32 is plugged locally """
    parsed = urllib.parse.urlparse(path)
    p = parsed.path
    q = urllib.parse.parse_qs(parsed.query)

    if p == "/status":
        return local_serial.send_cmd("STATUS")
    elif p == "/lamps/status":
        return local_serial.send_cmd("LAMPS")
    elif p == "/led/status":
        return local_serial.send_cmd("LED")
    elif p == "/targetTemperature":
        val = q.get("value", ["24"])[0]
        return local_serial.send_cmd(f"SET ac_temp {val}")
    elif p == "/targetHeatingCoolingState":
        val = q.get("value", ["0"])[0]
        force = " 1" if q.get("force", ["0"])[0] == "1" else ""
        return local_serial.send_cmd(f"SET ac_state {val}{force}")
    elif p == "/ac/mode":
        return local_serial.send_cmd("SET ac_mode")
    elif p == "/ac/mode/status":
        return local_serial.send_cmd("GET ac_mode")
    elif p == "/ac/fan":
        return local_serial.send_cmd("SET ac_fan")
    elif p == "/ac/fan/status":
        return local_serial.send_cmd("GET ac_fan")
    elif p == "/ac/fan/speed":
        val = q.get("value", ["33"])[0]
        return local_serial.send_cmd(f"SET ac_fan_speed {val}")
    elif p == "/ac/fan/active":
        return "1"
    elif p.startswith("/ac/swing"):
        if p.endswith("/status"): return local_serial.send_cmd("GET ac_swing")
        elif p.endswith("/on"): return local_serial.send_cmd("SET ac_swing 1")
        elif p.endswith("/off"): return local_serial.send_cmd("SET ac_swing 0")
        return local_serial.send_cmd("SET ac_swing")
    elif p.startswith("/ac/eco"):
        if p.endswith("/status"): return local_serial.send_cmd("GET ac_eco")
        elif p.endswith("/on"): return local_serial.send_cmd("SET ac_eco 1")
        elif p.endswith("/off"): return local_serial.send_cmd("SET ac_eco 0")
        return local_serial.send_cmd("SET ac_eco")
    elif p == "/ac/timer/status":
        return local_serial.send_cmd("GET ac_timer")
    elif p == "/ac/timer/on":
        return local_serial.send_cmd("SET ac_timer_on")
    elif p == "/ac/timer/off":
        return local_serial.send_cmd("SET ac_timer_off")
    elif p == "/ac/timer":
        val = q.get("value", ["0"])[0]
        return local_serial.send_cmd(f"SET ac_timer {val}")
    elif p.startswith("/lamp/"):
        parts = p.strip("/").split("/")
        if len(parts) == 3:
            name, act = parts[1], parts[2]
            if act == "status": return local_serial.send_cmd(f"GET lamp {name}")
            val = "1" if act == "on" else "0"
            return local_serial.send_cmd(f"SET lamp {name} {val}")
    elif p == "/led/on": return local_serial.send_cmd("SET led_power 1")
    elif p == "/led/off": return local_serial.send_cmd("SET led_power 0")
    elif p == "/led/hue":
        val = q.get("value", ["0"])[0]
        return local_serial.send_cmd(f"SET led_hue {val}")
    elif p == "/led/saturation":
        val = q.get("value", ["0"])[0]
        return local_serial.send_cmd(f"SET led_sat {val}")
    elif p == "/led/brightness":
        val = q.get("value", ["100"])[0]
        return local_serial.send_cmd(f"SET led_bri {val}")
    elif p == "/led/mode": return local_serial.send_cmd("SET led_mode")
    elif p == "/led/mode/status": return local_serial.send_cmd("GET led_mode")
    elif p == "/led/speed": return local_serial.send_cmd("SET led_speed")
    elif p == "/led/speed/status": return local_serial.send_cmd("GET led_speed")
    elif p == "/led/timer/status": return local_serial.send_cmd("GET led_timer")
    elif p == "/led/timer":
        val = q.get("value", ["0"])[0]
        return local_serial.send_cmd(f"SET led_timer {val}")
    elif p == "/send_raw_power": return local_serial.send_cmd("SET ac_raw_power")
    elif p == "/raw_temp_steps":
        t = q.get("target", ["24"])[0]
        return local_serial.send_cmd(f"SET ac_raw_temp_steps {t}")
    elif p == "/set_state_raw":
        s = q.get("state", ["0"])[0]
        return local_serial.send_cmd(f"SET ac_state_raw {s}")
    elif p == "/set_state_memory":
        t, s = q.get("temp", ["24"])[0], q.get("state", ["0"])[0]
        return local_serial.send_cmd(f"SET ac_state_mem {t} {s}")

    return "OK"

def background_poller():
    global is_backend_online
    last_err_time = 0
    time.sleep(2.0)
    while True:
        try:
            # Yield to active user commands
            if time.time() - last_serial_cmd_time < 2.0:
                time.sleep(1.0)
                continue

            if not is_calibrating:
                # 1. Fetch /lamps/status
                try:
                    raw_lp = send_to_backend("/lamps/status", timeout=0.8).decode("utf-8")
                    if raw_lp.startswith("{"):
                        data = json.loads(raw_lp)
                        for k, v in data.items():
                            cache["lamps"][k] = bool(v)
                except Exception:
                    pass

                # 2. Fetch /status (AC)
                try:
                    raw_st = send_to_backend("/status", timeout=0.8).decode("utf-8")
                    if raw_st.startswith("{"):
                        data = json.loads(raw_st)
                        if virtual_state_override is None:
                            cache["status"] = data
                except Exception:
                    pass

                # 3. Fetch /led/status
                try:
                    raw_ld = send_to_backend("/led/status", timeout=0.8).decode("utf-8")
                    if raw_ld.startswith("{"):
                        cache["led"] = json.loads(raw_ld)
                except Exception:
                    pass

        except Exception as e:
            now = time.time()
            if now - last_err_time > 15:
                print(f"[{format_ts()}] [WAITING] Poller: {e}")
                last_err_time = now

        time.sleep(3.0)

threading.Thread(target=background_poller, daemon=True, name="Poller").start()

# =========================================================================
#  AC / FAN HARDWARE ACTION HELPERS
# =========================================================================

def set_ac_raw_state(state, retries=1):
    try:
        res = send_to_backend(f"/targetHeatingCoolingState?value={state}&force=1", timeout=2.0).decode("utf-8")
        cache["status"]["targetHeatingCoolingState"] = int(state)
        cache["status"]["currentHeatingCoolingState"] = int(state)
        return res
    except Exception as e:
        print(f"[AC Proxy] Error setting raw AC state {state}: {e}")
        return None

def set_fan_raw_state(state, retries=1):
    endpoint = "on" if state == 1 else "off"
    try:
        res = send_to_backend(f"/lamp/fan/{endpoint}", timeout=2.0).decode("utf-8")
        cache["lamps"]["fan"] = (state == 1)
        return res
    except Exception as e:
        print(f"[AC Proxy] Error setting fan state {state}: {e}")
        return None

def get_current_ac_state() -> bool:
    global virtual_state_override
    if virtual_state_override is not None:
        return virtual_state_override != 0
    return cache["status"].get("targetHeatingCoolingState", 0) != 0

def get_current_fan_state() -> bool:
    return cache["lamps"].get("fan", False)

# =========================================================================
#  AC / FAN ALTERNATE SWAP LOOP
# =========================================================================

def ac_fan_swap_loop(minutes):
    global swap_active, swap_interval_minutes, virtual_state_override
    print(f"[AC Proxy] Starting AC / Fan swap loop (Interval: {minutes} minutes)...")

    ac_on = get_current_ac_state()
    fan_on = get_current_fan_state()

    if ac_on and not fan_on:
        print(f"[AC Proxy] Smart Swap Start: AC is ON. Running {get_ac_phase_minutes():.0f} min AC phase.")
        current_phase = "ac"
    elif fan_on and not ac_on:
        print(f"[AC Proxy] Smart Swap Start: Fan is ON. Running {swap_interval_minutes:.0f} min Fan phase.")
        current_phase = "fan"
    elif fan_on and ac_on:
        print(f"[AC Proxy] Smart Swap Start: Both ON. Holding both for {get_ac_phase_minutes():.0f} min before phasing AC off.")
        current_phase = "both"
    else:
        print("[AC Proxy] Smart Swap Start: Both OFF. Turning Fan ON and starting swap timer.")
        current_phase = "fan"
        set_fan_raw_state(1)
        virtual_state_override = 0

    last_phase_time = time.time()
    while not swap_stop_event.is_set():
        elapsed = time.time() - last_phase_time
        phase_minutes = get_ac_phase_minutes() if current_phase in ("ac", "both") else swap_interval_minutes
        target_seconds = phase_minutes * 60.0
        remaining = target_seconds - elapsed

        if remaining <= 0:
            last_phase_time = time.time()
            live_ac_on = get_current_ac_state()
            live_fan_on = get_current_fan_state()

            if current_phase == "both":
                print(f"[AC Proxy] Swap Interval reached ({phase_minutes}m): Turning AC OFF, Fan stays ON...")
                current_phase = "fan"
                set_ac_raw_state(0)
                continue

            if live_ac_on or current_phase == "ac":
                print(f"[AC Proxy] Swap Interval reached ({phase_minutes}m): Turning AC OFF, Fan ON (Phase: AC -> Fan)...")
                ac_res = set_ac_raw_state(0)
                if ac_res is None:
                    print("[AC Proxy] WARNING: Failed to turn AC OFF during swap! Retrying in 10s...")
                    last_phase_time = time.time() - (target_seconds - 10.0)
                    continue
                time.sleep(0.5)
                fan_res = set_fan_raw_state(1)
                if fan_res is None:
                    print("[AC Proxy] WARNING: Failed to turn Fan ON during swap! Retrying in 10s...")
                    last_phase_time = time.time() - (target_seconds - 10.0)
                    continue
                current_phase = "fan"
            else:
                print(f"[AC Proxy] Swap Interval reached ({phase_minutes}m): Turning Fan OFF, AC ON (Phase: Fan -> AC)...")
                fan_res = set_fan_raw_state(0)
                if fan_res is None:
                    print("[AC Proxy] WARNING: Failed to turn Fan OFF during swap! Retrying in 10s...")
                    last_phase_time = time.time() - (target_seconds - 10.0)
                    continue
                time.sleep(0.5)
                ac_res = set_ac_raw_state(2)
                if ac_res is None:
                    print("[AC Proxy] WARNING: Failed to turn AC ON during swap! Retrying in 10s...")
                    last_phase_time = time.time() - (target_seconds - 10.0)
                    continue
                current_phase = "ac"
            continue

        wait_slice = min(remaining, 1.0)
        if swap_stop_event.wait(timeout=wait_slice):
            break
        if swap_update_event.is_set():
            swap_update_event.clear()
            print(f"[AC Proxy] Swap duration updated live -> now targeting {swap_interval_minutes} minute interval.")

    print("[AC Proxy] AC / Fan swap loop stopped.")
    swap_active = False

def start_ac_fan_swap(minutes=None):
    global swap_active, swap_thread, swap_stop_event, swap_update_event, swap_interval_minutes
    if minutes is not None:
        swap_interval_minutes = max(5.0, min(240.0, float(minutes)))
    save_swap_state(True, swap_interval_minutes)
    if swap_active:
        print(f"[AC Proxy] Dynamic swap duration updated to {swap_interval_minutes} minutes.")
        swap_update_event.set()
        return
    swap_stop_event.clear()
    swap_update_event.clear()
    swap_active = True
    swap_thread = threading.Thread(target=ac_fan_swap_loop, args=(swap_interval_minutes,), daemon=True)
    swap_thread.start()

def stop_ac_fan_swap():
    global swap_active, swap_stop_event, virtual_state_override
    save_swap_state(False, swap_interval_minutes)
    if swap_active:
        print("[AC Proxy] Stopping AC / Fan swap loop...")
        swap_active = False
        swap_stop_event.set()
        virtual_state_override = None

def run_state_transition(val, target_temp, force=False):
    global is_calibrating, virtual_state_override
    with lock:
        try:
            t_val = int(float(target_temp))
            if t_val < 16 or t_val > 30:
                t_val = 24

            if val != "0":
                if not force and get_current_ac_state() and t_val == active_target_temp:
                    print(f"[AC Proxy] Redundant ON command at {t_val}°C ignored.")
                    return

                print(f"[AC Proxy] Powering ON AC -> Setting target {t_val}°C...")
                virtual_state_override = int(val)
                set_ac_raw_state(val)
                time.sleep(0.2)
                send_to_backend(f"/targetTemperature?value={t_val}")
                save_last_temp(t_val)
                print(f"[AC Proxy] Power-on sequence complete at {t_val}°C.")
            else:
                print("[AC Proxy] Turning OFF AC...")
                virtual_state_override = 0
                set_ac_raw_state(0)
        except Exception as e:
            print(f"[AC Proxy] Error in state transition: {e}")
        finally:
            is_calibrating = False
            if not swap_active:
                virtual_state_override = None

def run_hard_calibration(target_temp):
    global is_calibrating
    with lock:
        try:
            t_val = int(float(target_temp))
            if t_val < 16 or t_val > 30:
                t_val = 24
            print(f"[AC Proxy] Safe Sync -> Setting AC to {t_val}°C...")
            save_last_temp(t_val)
            send_to_backend(f"/targetTemperature?value={t_val}")
            print(f"[AC Proxy] Safe Sync Complete at {t_val}°C.")
        except Exception as e:
            print(f"[AC Proxy] Error during safe sync: {e}")
        finally:
            is_calibrating = False

# =========================================================================
#  HTTP REQUEST HANDLER (Homebridge REST API on :8880)
# =========================================================================

class ProxyHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_POST(self):
        self.do_GET()

    def do_GET(self):
        global is_calibrating, active_target_temp, virtual_state_override, fan_speed_level
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path
        query = urllib.parse.parse_qs(parsed_url.query)

        if not path.endswith("/status") and path != "/status" and path != "/download_bridge":
            print(f"[{format_ts()}] [HTTP IN] {self.client_address[0]} -> {self.path}")

        # Serve the Room PC bridge script directly
        if path == "/download_bridge":
            bridge_file = os.path.join(os.path.dirname(__file__), "room_pc", "esp_room_bridge.py")
            if os.path.exists(bridge_file):
                with open(bridge_file, "r") as f:
                    content = f.read()
                self.send_text(content)
            else:
                self.send_error(404, "Bridge script not found")
            return

        # 1. Virtual state override
        if path == "/set_virtual_state":
            st_param = query.get("state", [None])[0]
            virtual_state_override = int(st_param) if st_param is not None else None
            res_json = {
                "targetTemperature": float(active_target_temp),
                "currentTemperature": float(active_target_temp),
                "targetHeatingCoolingState": virtual_state_override if virtual_state_override is not None else 0,
                "currentHeatingCoolingState": virtual_state_override if virtual_state_override is not None else 0
            }
            self.send_json(res_json)
            return

        # 2. Sync state in software without firing IR
        if path == "/sync_ac":
            temp_param = query.get("temp", [None])[0]
            st_param = query.get("state", [None])[0]
            if temp_param is not None:
                save_last_temp(int(float(temp_param)))
            if st_param is not None:
                virtual_state_override = int(st_param)
            res_json = {
                "targetTemperature": float(active_target_temp),
                "currentTemperature": float(active_target_temp),
                "targetHeatingCoolingState": virtual_state_override if virtual_state_override is not None else 0,
                "currentHeatingCoolingState": virtual_state_override if virtual_state_override is not None else 0
            }
            self.send_json(res_json)
            return

        # 3. Thermostat status
        if path == "/status":
            if is_calibrating:
                res_json = {
                    "targetTemperature": float(active_target_temp),
                    "currentTemperature": float(active_target_temp),
                    "targetHeatingCoolingState": 2,
                    "currentHeatingCoolingState": 2
                }
            elif virtual_state_override is not None:
                res_json = {
                    "targetTemperature": float(active_target_temp),
                    "currentTemperature": float(active_target_temp),
                    "targetHeatingCoolingState": virtual_state_override,
                    "currentHeatingCoolingState": virtual_state_override
                }
            else:
                res_json = cache.get("status", {
                    "targetTemperature": float(active_target_temp),
                    "currentTemperature": float(active_target_temp),
                    "targetHeatingCoolingState": 0,
                    "currentHeatingCoolingState": 0
                })
            self.send_json(res_json)
            return

        # 4. Target Heating / Cooling State
        if path == "/targetHeatingCoolingState":
            val = query.get("value", ["0"])[0]
            target = query.get("target", [str(active_target_temp)])[0]
            raw = query.get("raw", ["0"])[0]
            try:
                t_val = int(float(target))
            except Exception:
                t_val = active_target_temp
            save_last_temp(t_val)

            if str(val) == "0" and swap_active and get_current_ac_state() and raw != "1":
                print("[AC Proxy] Manual AC OFF received -> stopping AC/Fan swap loop.")
                stop_ac_fan_swap()

            if str(val) != "0" and raw == "1":
                threading.Thread(target=set_ac_raw_state, args=(val,), daemon=True).start()
            else:
                threading.Thread(target=run_state_transition, args=(val, t_val), daemon=True).start()

            self.send_text("OK")
            return

        # 5. Target Temperature
        if path == "/targetTemperature":
            val = query.get("value", ["24"])[0]
            save_last_temp(val)
            if not is_calibrating:
                try:
                    send_to_backend(f"/targetTemperature?value={val}")
                    cache["status"]["targetTemperature"] = float(val)
                    cache["status"]["currentTemperature"] = float(val)
                except Exception as e:
                    print(f"[AC Proxy] Error setting target temp: {e}")
            self.send_text("OK")
            return

        # 6. Hard Calibration
        if path == "/calibrate":
            target = query.get("target", [str(active_target_temp)])[0]
            try:
                t_val = int(float(target))
            except Exception:
                t_val = active_target_temp
            save_last_temp(t_val)
            threading.Thread(target=run_hard_calibration, args=(t_val,), daemon=True).start()
            self.send_text("OK")
            return

        # 7. AC / Fan Swap Controls
        if path == "/ac_fan_swap/on":
            minutes_param = query.get("minutes", [None])[0]
            start_ac_fan_swap(float(minutes_param) if minutes_param is not None else None)
            self.send_text("1")
            return

        # 7. AC / Fan Swap Controls
        if path == "/ac_fan_swap/on":
            minutes_param = query.get("minutes", [None])[0]
            start_ac_fan_swap(float(minutes_param) if minutes_param is not None else None)
            self.send_text("1")
            return

        if path == "/ac_fan_swap/off":
            stop_ac_fan_swap()
            self.send_text("0")
            return

        if path == "/ac_fan_swap/status":
            self.send_text("1" if swap_active else "0")
            return

        if path == "/ac_fan_swap/duration":
            val = query.get("value", [None])[0]
            if val is not None:
                pct = max(0.0, min(100.0, float(val)))
                if pct <= 0.0:
                    stop_ac_fan_swap()
                else:
                    minutes = max(5.0, round((pct * 240.0 / 100.0) / 5.0) * 5.0)
                    start_ac_fan_swap(minutes)
            pct_response = int(min(100.0, (swap_interval_minutes / 240.0) * 100.0))
            self.send_text(str(pct_response))
            return

        if path == "/ac_fan_swap/duration/status":
            pct_response = int(min(100.0, (swap_interval_minutes / 240.0) * 100.0))
            self.send_text(str(pct_response))
            return

        # 8. Lamps Status & Actions (Instant response + Sync)
        if path == "/lamps/status":
            self.send_json(cache.get("lamps", {"main": False, "fairy": False, "fan": False, "backlight": False}))
            return

        if path.startswith("/lamp/") and path.endswith("/status"):
            parts = path.strip("/").split("/")
            if len(parts) == 3:
                lamp_name = parts[1]
                is_on = cache.get("lamps", {}).get(lamp_name, False)
                self.send_text("1" if is_on else "0")
                return

        if path.startswith("/lamp/"):
            parts = path.strip("/").split("/")
            if len(parts) == 3 and parts[2] in ("on", "off"):
                lamp_name, act = parts[1], parts[2]
                turn_on = (act == "on")
                cache["lamps"][lamp_name] = turn_on
                if lamp_name == "fan" and not turn_on and swap_active:
                    print("[AC Proxy] Fan manually turned OFF while swap active -> stopping swap.")
                    stop_ac_fan_swap()
                try:
                    res = send_to_backend(self.path).decode("utf-8")
                    print(f"[{format_ts()}] [LAMP] {lamp_name} -> {act} | serial response: {res.strip()}")
                except Exception as e:
                    print(f"[AC Proxy] Error executing {path}: {e}")
                self.send_text("1" if turn_on else "0")
                return

        # 9. LED Status & Actions
        if path == "/led/status":
            self.send_json(cache.get("led", {}))
            return

        if path == "/led/timer/status":
            self.send_text(str(cache.get("led_timer", "0")))
            return

        if path == "/led/mode/status":
            self.send_text(str(cache.get("led", {}).get("mode", 1)))
            return

        if path == "/led/speed/status":
            self.send_text(str(cache.get("led", {}).get("speed", 1)))
            return

        if path == "/led/on":
            cache["led"]["power"] = True
            threading.Thread(target=lambda: send_to_backend("/led/on"), daemon=True).start()
            self.send_text("1")
            return

        if path == "/led/off":
            cache["led"]["power"] = False
            threading.Thread(target=lambda: send_to_backend("/led/off"), daemon=True).start()
            self.send_text("0")
            return

        if path == "/led/brightness":
            val = query.get("value", ["100"])[0]
            try:
                cache["led"]["brightness"] = int(float(val))
            except Exception:
                pass
            threading.Thread(target=lambda: send_to_backend(f"/led/brightness?value={val}"), daemon=True).start()
            self.send_text(val)
            return

        if path == "/led/hue":
            val = query.get("value", ["0"])[0]
            try:
                cache["led"]["hue"] = int(float(val))
            except Exception:
                pass
            threading.Thread(target=lambda: send_to_backend(f"/led/hue?value={val}"), daemon=True).start()
            self.send_text(val)
            return

        if path == "/led/saturation":
            val = query.get("value", ["0"])[0]
            try:
                cache["led"]["saturation"] = int(float(val))
            except Exception:
                pass
            threading.Thread(target=lambda: send_to_backend(f"/led/saturation?value={val}"), daemon=True).start()
            self.send_text(val)
            return

        # 10. AC Fan & Modes status
        if path == "/ac/fan/speed/status":
            self.send_text(str(fan_speed_level * 33))
            return

        if path == "/ac/fan/status":
            self.send_text(str(fan_speed_level))
            return

        if path == "/ac/fan/speed":
            val = query.get("value", [None])[0]
            if val is not None:
                try:
                    pct = max(0, min(100, int(float(val))))
                except Exception:
                    pct = fan_speed_level * 33
                target_level = 1
                if pct > 66:
                    target_level = 3
                elif pct > 33:
                    target_level = 2
                save_fan_level(target_level)
                threading.Thread(target=lambda: send_to_backend(f"/ac/fan/speed?value={val}"), daemon=True).start()
            self.send_text(str(fan_speed_level * 33))
            return

        if path == "/ac/fan":
            try:
                res = send_to_backend("/ac/fan").decode("utf-8")
                try:
                    save_fan_level(int(res.strip()))
                except Exception:
                    pass
                self.send_text(str(fan_speed_level))
            except Exception as e:
                self.send_error(500, str(e))
            return

        if path == "/ac/fan/active":
            self.send_text("1")
            return

        if path == "/ac/swing/status":
            self.send_text(str(cache.get("ac_swing", "0")))
            return

        if path == "/ac/eco/status":
            self.send_text(str(cache.get("ac_eco", "0")))
            return

        if path == "/ac/mode/status":
            self.send_text(str(cache.get("ac_mode", "1")))
            return

        if path == "/ac/timer/status":
            self.send_text(str(cache.get("ac_timer", "0")))
            return

        # 11. Fallback for any other route
        base_clean = path.split("?", 1)[0]
        if base_clean.endswith("/status"):
            self.send_text("0")
            return

        try:
            res = send_to_backend(self.path).decode("utf-8")
            self.send_text(res)
        except Exception as e:
            self.send_error(500, str(e))

    def send_text(self, text: str):
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(str(text).encode("utf-8"))
        except (BrokenPipeError, ConnectionResetError):
            pass

    def send_json(self, data: Any):
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            if isinstance(data, (dict, list)):
                self.wfile.write(json.dumps(data, separators=(',', ':')).encode("utf-8"))
            else:
                self.wfile.write(str(data).encode("utf-8"))
        except (BrokenPipeError, ConnectionResetError):
            pass

# =========================================================================
#  MAIN ENTRY POINT
# =========================================================================

def main():
    print(f"\n=======================================================")
    print(f"  AC Proxy - Central Server Bridge                     ")
    print(f"  Listening on: http://127.0.0.1:{PORT}               ")
    print(f"  Target Room Bridge: {ROOM_BRIDGE_URL}               ")
    print(f"=======================================================\n")

    # Auto-resume swap loop in background once backend is detected
    def auto_resume_swap():
        if load_swap_state():
            print("[AC Proxy] Waiting for Room PC backend before restoring swap loop...")
            start_wait = time.time()
            while time.time() - start_wait < 60 and not is_backend_online:
                time.sleep(2.0)
            if is_backend_online:
                time.sleep(1.0)
                print(f"[AC Proxy] Restoring previously active AC / Fan swap loop ({swap_interval_minutes}m)...")
                start_ac_fan_swap(swap_interval_minutes)

    threading.Thread(target=auto_resume_swap, daemon=True).start()

    server = ThreadingHTTPServer(("", PORT), ProxyHandler)
    server.serve_forever()

if __name__ == "__main__":
    main()

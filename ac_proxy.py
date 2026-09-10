#!/usr/bin/env python3
import urllib.request
import urllib.parse
import json
import os
import time
import threading
import concurrent.futures
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

DEFAULT_ESP32_IP = os.environ.get("ESP32_IP", "")
ESP32_IP = DEFAULT_ESP32_IP
PORT = 8880
STATE_FILE = "/tmp/ac_last_temp.txt"
FAN_LEVEL_STATE_FILE = "/tmp/ac_last_fan_level.txt"
LAST_DISCOVERY_TIME = 0
DISCOVERY_COOLDOWN = 30  # seconds between discovery scans

lock = threading.Lock()
is_calibrating = False
active_target_temp = 24

# Proxy-side cache of the AC's fan speed (1=Low, 2=Medium, 3=High), reported to
# HomeKit/HA instead of the ESP32's own fan_speed_level. The ESP32 has no real
# feedback from the AC (IR is one-way) and always boots believing Low, which
# drifts from reality whenever the real fan speed doesn't match. Seeded to
# High (3) here since that's the AC's actual current speed.
fan_speed_level = 3

# AC / Fan Alternate Swap state
# swap_interval_minutes is the configurable FAN-ON duration (set via the Duration
# slider). AC-ON duration is intentionally shorter (AC draws far more power than
# the fan): it scales as 1/6 of the Fan duration, clamped to 15-45 minutes, so a
# "3 hour" swap means 3 hours Fan / 30 min AC, and a "5 hour" swap means 5 hours
# Fan / 45 min AC -- never a symmetric split, but not stuck at a fixed 30 min either.
swap_active = False
swap_interval_minutes = 30.0
AC_PHASE_MIN_MINUTES = 15.0
AC_PHASE_MAX_MINUTES = 45.0
AC_PHASE_RATIO = 1.0 / 6.0
swap_thread = None
swap_stop_event = threading.Event()
# None means "trust the ESP32's live /status". Only ever set to a real 0/2 value
# while the swap loop is actively forcing a phase; must be cleared back to None
# as soon as that phase-forcing ends, otherwise /status keeps reporting a stale
# cached state instead of what the AC/Fan are physically doing.
virtual_state_override = None

def get_ac_phase_minutes():
    """ AC-ON duration for the swap loop: 1/6 of the current Fan duration,
    clamped to 15-45 minutes. Read live (not cached) so a mid-loop duration
    change also rescales the AC phase, not just the Fan phase. """
    return max(AC_PHASE_MIN_MINUTES, min(AC_PHASE_MAX_MINUTES, swap_interval_minutes * AC_PHASE_RATIO))

def set_ac_raw_state(state):
    """ Turn AC state ON (2) or OFF (0) using targetHeatingCoolingState route on ESP32 """
    global virtual_state_override
    virtual_state_override = int(state)
    try:
        discover_esp32()
        url = f"http://{ESP32_IP}/targetHeatingCoolingState?value={state}"
        with urllib.request.urlopen(url, timeout=10) as r:
            return r.read()
    except Exception as e:
        print(f"[AC Proxy] Error setting raw AC state {state}: {e}")

def set_ac_memory_only(state):
    """ Sync state tracking memory """
    pass

def set_fan_raw_state(state):
    """ Turn Fan ON (1) or OFF (0) using sunset lamp endpoint """
    try:
        endpoint = "on" if state == 1 else "off"
        url = f"http://{ESP32_IP}/lamp/sunset/{endpoint}"
        with urllib.request.urlopen(url, timeout=10) as r:
            return r.read()
    except Exception as e:
        print(f"[AC Proxy] Error setting fan state {state}: {e}")

def get_current_ac_state():
    """ Returns True if AC is currently ON (state != 0), False otherwise """
    global virtual_state_override
    if virtual_state_override is not None:
        return virtual_state_override != 0
    try:
        discover_esp32()
        url = f"http://{ESP32_IP}/status"
        with urllib.request.urlopen(url, timeout=3) as r:
            data = json.loads(r.read().decode("utf-8"))
            return data.get("targetHeatingCoolingState", 0) != 0
    except Exception as e:
        print(f"[AC Proxy] Error reading AC status: {e}")
        return False

def get_current_fan_state():
    """ Returns True if Fan is currently ON, False otherwise """
    try:
        discover_esp32()
        url = f"http://{ESP32_IP}/lamp/sunset/status"
        with urllib.request.urlopen(url, timeout=3) as r:
            res = r.read().decode("utf-8").strip()
            return res == "1" or res.lower() == "true"
    except Exception as e:
        print(f"[AC Proxy] Error reading Fan status: {e}")
        return False

swap_update_event = threading.Event()

def ac_fan_swap_loop(minutes):
    global swap_active, swap_interval_minutes
    print(f"[AC Proxy] Starting AC / Fan swap loop (Interval: {minutes} minutes)...")
    
    ac_on = get_current_ac_state()
    fan_on = get_current_fan_state()
    
    # Smart detection: Start with whichever appliance is currently ON without issuing IR commands or resetting state
    global virtual_state_override
    if ac_on and not fan_on:
        print(f"[AC Proxy] Smart Swap Start: Detected AC is currently ON. Keeping AC ON, Fan OFF for {get_ac_phase_minutes():.0f} min AC phase.")
        current_phase = "ac"
    elif fan_on and not ac_on:
        print(f"[AC Proxy] Smart Swap Start: Detected Fan is currently ON. Keeping Fan ON, AC OFF for {swap_interval_minutes:.0f} min Fan phase.")
        current_phase = "fan"
    elif fan_on and ac_on:
        # Both ON: keep both running for one AC-phase interval first instead of
        # immediately forcing AC off. After that interval: AC off (Fan stays on).
        # From then on it's the normal Fan-phase / AC-phase alternation.
        print(f"[AC Proxy] Smart Swap Start: Both AC and Fan are ON. Keeping both ON for {get_ac_phase_minutes():.0f} min before phasing AC off.")
        current_phase = "both"
    else:
        # Both OFF: Turn Fan ON and keep AC OFF (no AC IR signals sent, it's already off)
        print("[AC Proxy] Smart Swap Start: Both appliances are OFF. Turning Fan ON and starting swap timer.")
        current_phase = "fan"
        set_fan_raw_state(1)
        # Ensure proxy software state knows AC is OFF without sending any AC IR signals
        virtual_state_override = 0
    
    last_phase_time = time.time()
    while not swap_stop_event.is_set():
        elapsed = time.time() - last_phase_time
        # "ac" (and the initial "both" hold, which ends with AC turning off) run for
        # get_ac_phase_minutes(); "fan" (and "standby") run for the configurable
        # swap_interval_minutes.
        phase_minutes = get_ac_phase_minutes() if current_phase in ("ac", "both") else swap_interval_minutes
        target_seconds = phase_minutes * 60.0
        remaining = target_seconds - elapsed
        
        if remaining <= 0:
            last_phase_time = time.time()
            # Re-check live status right when interval expires
            live_ac_on = get_current_ac_state()
            live_fan_on = get_current_fan_state()
            
            # If both are off during standby and still off, stay in standby
            if not live_ac_on and not live_fan_on and current_phase == "standby":
                print("[AC Proxy] Swap Interval reached: Both appliances still OFF. Remaining in standby.")
                continue

            # First interval of a "both were on" start: turn AC off, leave Fan on,
            # then fall into the normal AC<->Fan alternation from here on.
            if current_phase == "both":
                print(f"[AC Proxy] Swap Interval reached ({phase_minutes}m): Turning AC OFF, Fan stays ON (Phase: Both -> Fan)...")
                current_phase = "fan"
                set_ac_raw_state(0)
                continue

            # If AC is currently running (whether from initial phase or user manually turned it on), turn AC OFF and Fan ON
            if live_ac_on or current_phase == "ac":
                print(f"[AC Proxy] Swap Interval reached ({phase_minutes}m): Turning AC OFF, Fan ON (Phase: AC -> Fan)...")
                current_phase = "fan"
                set_ac_raw_state(0)
                time.sleep(1.0)
                set_fan_raw_state(1)
            else:
                print(f"[AC Proxy] Swap Interval reached ({phase_minutes}m): Turning Fan OFF, AC ON (Phase: Fan -> AC)...")
                current_phase = "ac"
                set_fan_raw_state(0)
                time.sleep(1.0)
                set_ac_raw_state(2)
            continue
            
        # Poll in short slices (instead of sleeping for the full `remaining` span)
        # so that a live duration change takes effect within ~1s instead of only
        # after whatever the OLD duration's wait was already committed to.
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
        # Clamp to the documented 5-240 minute (up to 4h) Fan-duration range regardless
        # of caller, so a bad or out-of-range value (e.g. from an HA helper with a
        # wider scale) can't silently push the swap out to an absurd duration.
        swap_interval_minutes = max(5.0, min(240.0, float(minutes)))
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
    if swap_active:
        print("[AC Proxy] Stopping AC / Fan swap loop...")
        swap_active = False
        swap_stop_event.set()
        # Release the swap's forced status override now that it no longer owns
        # AC/Fan state, so /status reports the real, live ESP32 state again.
        virtual_state_override = None

def discover_esp32():
    global ESP32_IP, LAST_DISCOVERY_TIME
    now = time.time()
    if now - LAST_DISCOVERY_TIME < DISCOVERY_COOLDOWN:
        return ESP32_IP
    LAST_DISCOVERY_TIME = now

    # First check current IP. Timeout is deliberately more forgiving than a plain
    # "is it up" ping (the ESP32 is a single-threaded device and can be briefly
    # slow to answer under Wi-Fi jitter or while mid-IR-send) -- a false negative
    # here used to fall straight through to the very slow full subnet scan below
    # for what was really just a transient blip, not an IP change.
    try:
        req = urllib.request.Request(f"http://{ESP32_IP}/status")
        with urllib.request.urlopen(req, timeout=3.0) as r:
            if r.status == 200:
                return ESP32_IP
    except Exception:
        pass

    # Check default IP if different
    if ESP32_IP != DEFAULT_ESP32_IP:
        try:
            req = urllib.request.Request(f"http://{DEFAULT_ESP32_IP}/status")
            with urllib.request.urlopen(req, timeout=1.5) as r:
                if r.status == 200:
                    ESP32_IP = DEFAULT_ESP32_IP
                    print(f"[AC Proxy] Reconnected to ESP32 at default IP: {ESP32_IP}")
                    return ESP32_IP
        except Exception:
            pass

    # Subnet scan on 192.168.1.x -- probed CONCURRENTLY. Measured on this network,
    # a sequential scan (one address at a time, 0.4s timeout each) takes ~98
    # SECONDS end to end. Since discover_esp32() runs synchronously inside every
    # status/command request's fallback path, that 98s stall was being read by
    # HomeKit/Homebridge as the accessory going unresponsive -- which is what made
    # the AC/Fan appear to randomly flip on/off every couple of minutes even
    # though nothing physically changed. Running the probes in parallel keeps the
    # worst case near the single 0.4s per-host timeout instead of 254x that.
    def probe(i):
        if i == 100:  # known non-ESP32 device that false-positives on /status
            return None
        ip = f"192.168.1.{i}"
        try:
            req = urllib.request.Request(f"http://{ip}/status")
            with urllib.request.urlopen(req, timeout=0.4) as r:
                body = r.read().decode(errors="ignore")
                if "targetTemperature" in body or "targetHeatingCoolingState" in body:
                    return ip
        except Exception:
            pass
        return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=64) as pool:
        for result in pool.map(probe, range(1, 255)):
            if result:
                ESP32_IP = result
                print(f"[AC Proxy] Discovered ESP32 at new IP: {ESP32_IP}")
                return ESP32_IP
    return ESP32_IP

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

class ProxyHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        global is_calibrating, active_target_temp, ESP32_IP, virtual_state_override, fan_speed_level
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path
        query = urllib.parse.parse_qs(parsed_url.query)

        # Force virtual software state override without sending any IR signal
        if path == "/set_virtual_state":
            st_param = query.get("state", [None])[0]
            if st_param is not None:
                virtual_state_override = int(st_param)
            else:
                virtual_state_override = None
            res_json = {
                "targetTemperature": float(active_target_temp),
                "currentTemperature": float(active_target_temp),
                "targetHeatingCoolingState": virtual_state_override if virtual_state_override is not None else 0,
                "currentHeatingCoolingState": virtual_state_override if virtual_state_override is not None else 0
            }
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(res_json).encode("utf-8"))
            return

        # 1. Status: Sync in Real-Time
        if path == "/status":
            try:
                if is_calibrating:
                    # Return target temp and COOL state consistently during calibration so HomeKit UI does not flicker or gray out
                    res_json = {
                        "targetTemperature": float(active_target_temp),
                        "currentTemperature": float(active_target_temp),
                        "targetHeatingCoolingState": 2,
                        "currentHeatingCoolingState": 2
                    }
                    data = json.dumps(res_json).encode("utf-8")
                elif virtual_state_override is not None:
                    res_json = {
                        "targetTemperature": float(active_target_temp),
                        "currentTemperature": float(active_target_temp),
                        "targetHeatingCoolingState": virtual_state_override,
                        "currentHeatingCoolingState": virtual_state_override
                    }
                    data = json.dumps(res_json).encode("utf-8")
                else:
                    url = f"http://{ESP32_IP}/status"
                    try:
                        with urllib.request.urlopen(url, timeout=3) as r:
                            data = r.read()
                    except Exception:
                        discover_esp32()
                        url = f"http://{ESP32_IP}/status"
                        with urllib.request.urlopen(url, timeout=3) as r:
                            data = r.read()

                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(data)
            except Exception as e:
                self.send_error(500, str(e))

        # 2. Intercept targetHeatingCoolingState (Power On/Off with Auto-Sync to Last Degree)
        elif path == "/targetHeatingCoolingState":
            val = query.get("value", ["0"])[0]
            target = query.get("target", [str(active_target_temp)])[0]
            try:
                t_val = int(float(target))
            except Exception:
                t_val = active_target_temp
            save_last_temp(t_val)
            
            # Auto-stop swap loop if the user manually turns the AC OFF while a swap is running --
            # the swap no longer owns AC/Fan state at that point, so it must not keep alternating
            # them behind the user's back.
            # Guard on get_current_ac_state() so this only fires on a genuine on->off
            # transition. During the swap's "fan" phase the AC is already forced off, and
            # HA periodically re-affirms that state by resending value=0 (same redundant-sync
            # quirk documented on the ESP32 side) -- without this check that resend would
            # silently kill the swap loop, leaving swap_active False so a later duration
            # change restarts from scratch instead of extending the running phase.
            if str(val) == "0" and swap_active and get_current_ac_state():
                stop_ac_fan_swap()

            threading.Thread(target=run_state_transition, args=(val, t_val), daemon=True).start()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"OK")

        # 3. Hard Calibration (Ceiling + Floor + Target sync)
        elif path == "/calibrate":
            target = query.get("target", [str(active_target_temp)])[0]
            try:
                t_val = int(float(target))
            except Exception:
                t_val = active_target_temp
            save_last_temp(t_val)
            threading.Thread(target=run_hard_calibration, args=(t_val,), daemon=True).start()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"OK")

        # 4. AC / Fan Alternate Swap Control
        elif path == "/ac_fan_swap/on":
            # No "minutes" param -> keep whatever duration is already set (last used,
            # or whatever the Duration slider set) instead of resetting it to 30.
            minutes_param = query.get("minutes", [None])[0]
            start_ac_fan_swap(float(minutes_param) if minutes_param is not None else None)
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"1")

        elif path == "/ac_fan_swap/off":
            stop_ac_fan_swap()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"0")

        elif path == "/ac_fan_swap/status":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"1" if swap_active else b"0")

        # AC / Fan Swap Duration control (0-100% mapped to 5-240 minutes of FAN-ON time,
        # 5-minute steps; the AC-ON phase scales off this via get_ac_phase_minutes()
        # (1/6 of it, clamped 15-45 min), it isn't set directly by this slider).
        # Setting it live-updates the running swap loop's Fan interval; setting
        # it to 0 stops the swap, mirroring the existing AC Shutoff Timer / LED Sleep
        # Timer pattern.
        elif path == "/ac_fan_swap/duration":
            val = query.get("value", [None])[0]
            if val is not None:
                pct = max(0.0, min(100.0, float(val)))
                if pct <= 0.0:
                    stop_ac_fan_swap()
                else:
                    minutes = max(5.0, round((pct * 240.0 / 100.0) / 5.0) * 5.0)
                    start_ac_fan_swap(minutes)
            pct_response = int(min(100.0, (swap_interval_minutes / 240.0) * 100.0))
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(str(pct_response).encode())

        elif path == "/ac_fan_swap/duration/status":
            pct_response = int(min(100.0, (swap_interval_minutes / 240.0) * 100.0))
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(str(pct_response).encode())

        # Route the Fan (lamp/sunset) power endpoints through the proxy so that turning
        # the Fan off manually can also opt the swap out, the same way the AC does.
        elif path in ("/lamp/sunset/on", "/lamp/sunset/off"):
            # Same redundant-resync guard as the AC side above: only stop the swap if the
            # Fan was actually ON before this "off" arrived. During the swap's "ac" phase
            # the Fan is already off, and HA re-affirming that state must not kill the swap.
            if path == "/lamp/sunset/off" and swap_active and get_current_fan_state():
                print("[AC Proxy] Fan manually turned OFF while swap active -> stopping swap.")
                stop_ac_fan_swap()
            try:
                url = f"http://{ESP32_IP}{path}"
                try:
                    with urllib.request.urlopen(url, timeout=10) as r:
                        data = r.read()
                except Exception:
                    discover_esp32()
                    url = f"http://{ESP32_IP}{path}"
                    with urllib.request.urlopen(url, timeout=10) as r:
                        data = r.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(data)
            except Exception as e:
                self.send_error(500, str(e))

        # 5. TargetTemperature: Save on change and forward directly
        elif path == "/targetTemperature":
            val = query.get("value", ["24"])[0]
            save_last_temp(val)
            if is_calibrating:
                # Ignore target temperature changes during calibration to prevent interrupting calibration algorithm
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"OK")
            else:
                try:
                    url = f"http://{ESP32_IP}/targetTemperature?value={val}"
                    try:
                        with urllib.request.urlopen(url, timeout=3) as r:
                            data = r.read()
                    except Exception:
                        discover_esp32()
                        url = f"http://{ESP32_IP}/targetTemperature?value={val}"
                        with urllib.request.urlopen(url, timeout=3) as r:
                            data = r.read()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain")
                    self.end_headers()
                    self.wfile.write(data)
                except Exception as e:
                    self.send_error(500, str(e))

        # 6. AC Fan Speed: report our own cache to HomeKit/HA instead of the ESP32's
        # fan_speed_level (which has no real feedback from the AC and boots believing
        # Low), while still forwarding real changes to the ESP32 so the AC itself moves.
        elif path == "/ac/fan/speed/status":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(str(fan_speed_level * 33).encode())

        elif path == "/ac/fan/speed":
            val = query.get("value", [None])[0]
            if val is not None:
                try:
                    pct = max(0, min(100, int(float(val))))
                except Exception:
                    pct = fan_speed_level * 33
                target_level = 1
                if pct > 33 and pct <= 66:
                    target_level = 2
                elif pct > 66:
                    target_level = 3
                save_fan_level(target_level)
                try:
                    url = f"http://{ESP32_IP}/ac/fan/speed?value={val}"
                    try:
                        urllib.request.urlopen(url, timeout=10).close()
                    except Exception:
                        discover_esp32()
                        urllib.request.urlopen(f"http://{ESP32_IP}/ac/fan/speed?value={val}", timeout=10).close()
                except Exception as e:
                    print(f"[AC Proxy] Error forwarding fan speed set: {e}")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(str(fan_speed_level * 33).encode())

        # Physical FAN button cycle: forward the real IR press to the ESP32, then mirror
        # its resulting level back into our cache so /ac/fan/speed/status stays in sync.
        elif path == "/ac/fan":
            try:
                url = f"http://{ESP32_IP}/ac/fan"
                try:
                    with urllib.request.urlopen(url, timeout=10) as r:
                        data = r.read()
                except Exception:
                    discover_esp32()
                    with urllib.request.urlopen(f"http://{ESP32_IP}/ac/fan", timeout=10) as r:
                        data = r.read()
                try:
                    save_fan_level(int(data.decode().strip()))
                except Exception:
                    pass
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(str(fan_speed_level).encode())
            except Exception as e:
                self.send_error(500, str(e))

        else:
            try:
                url = f"http://{ESP32_IP}{self.path}"
                try:
                    with urllib.request.urlopen(url, timeout=3) as r:
                        data = r.read()
                except Exception:
                    discover_esp32()
                    url = f"http://{ESP32_IP}{self.path}"
                    with urllib.request.urlopen(url, timeout=3) as r:
                        data = r.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(data)
            except Exception as e:
                self.send_error(500, str(e))

def run_state_transition(val, target_temp):
    global is_calibrating, ESP32_IP, virtual_state_override
    with lock:
        try:
            if val != "0":
                t_val = int(float(target_temp))
                if t_val < 16 or t_val > 30:
                    t_val = 24

                # Skip a redundant "turn ON" if the AC is already confirmed ON at this
                # exact target -- HomeKit and Home Assistant both control the same
                # accessory, and each can re-affirm an ON command the other just applied
                # (e.g. turn on from HA, then HomeKit's own sync/poll sends its own ON
                # right after). send_raw_power below is a TOGGLE on the physical remote,
                # not an explicit "set ON", so blindly re-running this routine for a
                # duplicate call would toggle the AC straight back OFF instead of doing
                # nothing, which is what a same-state command should do. Checked here
                # (holding `lock`, after any concurrent transition has fully finished)
                # so it reflects the AC's real settled state, not a mid-transition one.
                if get_current_ac_state() and t_val == active_target_temp:
                    print(f"[AC Proxy] Redundant ON command at {t_val}°C ignored (AC already ON at that target).")
                    return

                is_calibrating = True
                virtual_state_override = int(val)

                print(f"[AC Proxy] Powering ON -> Resuming & calibrating to last degree: {t_val}°C...")
                discover_esp32()

                # Step 1: Set ESP32 memory to 16.0
                try:
                    urllib.request.urlopen(f"http://{ESP32_IP}/set_state_memory?temp=16&state={val}", timeout=10).close()
                except Exception:
                    urllib.request.urlopen(f"http://{ESP32_IP}/targetTemperature?value=16", timeout=10).close()
                time.sleep(0.5)

                # Step 2: Turn on the AC
                try:
                    urllib.request.urlopen(f"http://{ESP32_IP}/send_raw_power", timeout=10).close()
                except Exception:
                    urllib.request.urlopen(f"http://{ESP32_IP}/targetHeatingCoolingState?value={val}", timeout=10).close()
                time.sleep(1.5)

                # Step 3: Go up to 31
                try:
                    urllib.request.urlopen(f"http://{ESP32_IP}/raw_temp_steps?target=31", timeout=15).close()
                except Exception:
                    urllib.request.urlopen(f"http://{ESP32_IP}/targetTemperature?value=31", timeout=15).close()
                time.sleep(1.0)

                # Step 4: Go down to 16
                try:
                    urllib.request.urlopen(f"http://{ESP32_IP}/raw_temp_steps?target=16", timeout=15).close()
                except Exception:
                    urllib.request.urlopen(f"http://{ESP32_IP}/targetTemperature?value=16", timeout=15).close()
                time.sleep(1.0)

                # Step 5: Step UP to last used degree
                try:
                    urllib.request.urlopen(f"http://{ESP32_IP}/raw_temp_steps?target={t_val}", timeout=15).close()
                except Exception:
                    urllib.request.urlopen(f"http://{ESP32_IP}/targetTemperature?value={int(t_val)}", timeout=15).close()
                print(f"[AC Proxy] Power-on calibration complete! Resumed perfectly at {t_val}°C.")
            else:
                print("[AC Proxy] Turning off AC...")
                virtual_state_override = 0
                discover_esp32()
                urllib.request.urlopen(f"http://{ESP32_IP}/targetHeatingCoolingState?value=0", timeout=10).close()
        except Exception as e:
            print(f"[AC Proxy] Error in state transition: {e}")
        finally:
            is_calibrating = False
            # Release the override once this normal (non-swap) transition is done so
            # /status goes back to trusting the ESP32's live, authoritative state.
            if not swap_active:
                virtual_state_override = None

def run_hard_calibration(target_temp):
    global is_calibrating, ESP32_IP
    with lock:
        try:
            is_calibrating = True
            t_val = int(float(target_temp))
            if t_val < 16 or t_val > 30:
                t_val = 24
            print(f"[AC Proxy] Manual Full Hard Calibration -> Floor & Ceiling (Target: {t_val}°C)...")
            discover_esp32()
            # 1. Drive to 31 (Ceiling)
            try:
                urllib.request.urlopen(f"http://{ESP32_IP}/raw_temp_steps?target=31", timeout=15).close()
            except Exception:
                urllib.request.urlopen(f"http://{ESP32_IP}/targetTemperature?value=31", timeout=15).close()
            time.sleep(1.0)
            
            # 2. Drive to 16 (Floor)
            try:
                urllib.request.urlopen(f"http://{ESP32_IP}/raw_temp_steps?target=16", timeout=15).close()
            except Exception:
                urllib.request.urlopen(f"http://{ESP32_IP}/targetTemperature?value=16", timeout=15).close()
            time.sleep(1.0)
            
            # 3. Step up to target
            try:
                urllib.request.urlopen(f"http://{ESP32_IP}/raw_temp_steps?target={t_val}", timeout=15).close()
            except Exception:
                urllib.request.urlopen(f"http://{ESP32_IP}/targetTemperature?value={int(t_val)}", timeout=15).close()
            print(f"[AC Proxy] Calibration Complete! Synchronized at {t_val}°C.")
        except Exception as e:
            print(f"[AC Proxy] Error during hard calibration: {e}")
        finally:
            is_calibrating = False

def main():
    print(f"Starting AC Proxy Server on port {PORT}...")
    # Threading server: a slow/blocking ESP32 call (e.g. mid power-on calibration,
    # or a flaky WiFi moment) must not stall every OTHER accessory's status poll --
    # a single-threaded HTTPServer processes one request at a time, so one slow
    # request queues up all the rest until they client-side timeout, which is what
    # HomeKit/HA was reporting as the Fan flickering on/off despite nothing changing.
    server = ThreadingHTTPServer(("", PORT), ProxyHandler)  # bind all interfaces
    server.serve_forever()

if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
OwnTone AirPlay Volume & Stream Manager
- Keeps volume set by user on iPhone 4S ("The Room", output ID 26612976694777) for 2 hours.
- Resets volume back to 23 after 2 hours if user changed volume.
- If user changes volume again, resets the 2-hour timer.
- Stream continues playing 24/7.
"""

import json
import os
import sys
import time
import urllib.request
import urllib.error

OWNTONE_API = "http://192.168.1.100:3689/api"
AIRPLAY_OUTPUT_ID = "26612976694777"  # "The Room" (iPhone 4S)
DEFAULT_VOLUME = 30
HOLD_DURATION = 7200  # 2 hours in seconds
STATE_FILE = "/tmp/airplay_volume_state.json"
CURL_TIMEOUT = 4

def log(msg):
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {msg}", flush=True)

def get_outputs():
    try:
        req = urllib.request.Request(f"{OWNTONE_API}/outputs")
        with urllib.request.urlopen(req, timeout=CURL_TIMEOUT) as resp:
            data = json.loads(resp.read().decode())
            return data.get("outputs", [])
    except Exception as e:
        log(f"Error fetching OwnTone outputs: {e}")
        return None

def set_output_volume(volume):
    try:
        url = f"{OWNTONE_API}/outputs/{AIRPLAY_OUTPUT_ID}"
        data = json.dumps({"volume": int(volume)}).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="PUT")
        with urllib.request.urlopen(req, timeout=CURL_TIMEOUT) as resp:
            return resp.status in (200, 204)
    except Exception as e:
        log(f"Error setting volume to {volume}: {e}")
        return False

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "last_volume": None,
        "override_active": False,
        "override_start_time": None
    }

def save_state(state):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        log(f"Error saving state: {e}")

def check_and_update_volume():
    outputs = get_outputs()
    if outputs is None:
        return

    room_output = None
    for o in outputs:
        if str(o.get("id")) == AIRPLAY_OUTPUT_ID:
            room_output = o
            break

    if not room_output:
        return

    actual_volume = room_output.get("volume")
    if actual_volume is None:
        return

    state = load_state()
    last_volume = state.get("last_volume")
    override_active = state.get("override_active", False)
    override_start_time = state.get("override_start_time")
    now = time.time()

    # Initial run check
    if last_volume is None:
        state["last_volume"] = actual_volume
        if actual_volume != DEFAULT_VOLUME:
            log(f"Initial check: volume is {actual_volume} (default is {DEFAULT_VOLUME}). Starting 2-hour hold timer.")
            state["override_active"] = True
            state["override_start_time"] = now
        else:
            state["override_active"] = False
            state["override_start_time"] = None
        save_state(state)
        return

    # Detect volume change by user
    if actual_volume != last_volume:
        log(f"Volume change detected: {last_volume} -> {actual_volume}")
        state["last_volume"] = actual_volume
        if actual_volume != DEFAULT_VOLUME:
            log(f"User changed volume to {actual_volume}. Keeping for 2 hours (until {time.strftime('%H:%M:%S', time.localtime(now + HOLD_DURATION))}).")
            state["override_active"] = True
            state["override_start_time"] = now
        else:
            log(f"Volume returned to default {DEFAULT_VOLUME}. Override cleared.")
            state["override_active"] = False
            state["override_start_time"] = None
        save_state(state)
        return

    # Check if 2 hours elapsed for active override
    if override_active and override_start_time:
        elapsed = now - override_start_time
        if elapsed >= HOLD_DURATION:
            log(f"2 hours ({int(elapsed)}s) elapsed since manual volume change. Reverting volume to {DEFAULT_VOLUME}.")
            if set_output_volume(DEFAULT_VOLUME):
                state["last_volume"] = DEFAULT_VOLUME
                state["override_active"] = False
                state["override_start_time"] = None
                save_state(state)
            else:
                log(f"Failed to reset volume to {DEFAULT_VOLUME}, will retry on next check.")

def main_loop():
    log("Starting AirPlay Volume Manager loop (poll every 5s)...")
    while True:
        try:
            check_and_update_volume()
        except Exception as e:
            log(f"Unhandled exception in volume loop: {e}")
        time.sleep(5)

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--once":
        check_and_update_volume()
    else:
        main_loop()

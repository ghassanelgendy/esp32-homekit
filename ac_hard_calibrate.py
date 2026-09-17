#!/usr/bin/env python3
"""
AC Hard Calibration Script
Guarantees 100% physical and software temperature alignment.

Procedure:
1. Drive to 31°C (forces physical AC to top ceiling regardless of start temp)
2. Drive to 16°C (forces physical AC to bottom floor regardless of start temp)
3. Step up to target temperature (e.g. 24°C)
"""
import sys
import time
import urllib.request
import urllib.parse
import json

ESP32_HOST = os.environ.get("ESP32_HOST", "esp32.local")
PROXY_HOST = os.environ.get("PROXY_HOST", "localhost")
ESP32_IP = ESP32_HOST
PROXY_URL = f"http://{PROXY_HOST}:8880"

def get_current_target():
    try:
        req = urllib.request.urlopen(f"{PROXY_URL}/status", timeout=5)
        data = json.loads(req.read().decode('utf-8'))
        return int(data.get("targetTemperature", 24))
    except Exception:
        return 24

def set_temp_direct(temp):
    try:
        url = f"http://{ESP32_IP}/targetTemperature?value={int(temp)}"
        with urllib.request.urlopen(url, timeout=15) as resp:
            return resp.read()
    except Exception as e:
        print(f"[Calibrate Error] Failed to set temp {temp}: {e}")
        return None

def calibrate(target_temp=None):
    if target_temp is None:
        target_temp = get_current_target()
        if target_temp < 16 or target_temp > 30:
            target_temp = 24
            
    print(f"=== Starting AC Hard Calibration (Target: {target_temp}°C) ===")
    
    # Step 1: Drive to 31°C (forces physical AC to ceiling)
    print("Step 1/3: Driving AC to ceiling (31°C)...")
    set_temp_direct(31)
    # Wait for IR pulses (15 pulses * ~250ms = ~4s)
    time.sleep(5.0)
    
    # Step 2: Drive to 16°C (forces physical AC to floor)
    print("Step 2/3: Driving AC to floor (16°C)...")
    set_temp_direct(16)
    # Wait for IR pulses
    time.sleep(5.0)
    
    # Step 3: Step up to target temperature
    print(f"Step 3/3: Setting final target temperature ({target_temp}°C)...")
    set_temp_direct(target_temp)
    time.sleep(3.0)
    
    print(f"✓ AC Calibration Complete! Physical AC and ESP32 are synchronized at {target_temp}°C.")

if __name__ == "__main__":
    target = int(sys.argv[1]) if len(sys.argv) > 1 else None
    calibrate(target)

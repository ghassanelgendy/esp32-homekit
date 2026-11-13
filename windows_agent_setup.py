#!/usr/bin/env python3
"""
windows_agent_setup.py - Standalone Python Micro-Agent for Windows Machine (PC / Laptop)
Can be run on Windows with Python 3:
  pip install paho-mqtt
  python windows_agent_setup.py

Listens to MQTT commands from the Homelab Mosquitto broker (192.168.1.100:1883):
  - Topic: hassagent/{DEVICE_NAME}/command/shutdown -> executes shutdown /s /t 0
  - Topic: hassagent/{DEVICE_NAME}/command/restart  -> executes shutdown /r /t 0
  - Publishes: hassagent/{DEVICE_NAME}/availability -> "online" (with LWT "offline")
"""

import sys
import os
import time
import subprocess

try:
    import paho.mqtt.client as mqtt
except ImportError:
    print("[ERROR] paho-mqtt not installed. Run: pip install paho-mqtt")
    sys.exit(1)

BROKER_IP = "192.168.1.100"
BROKER_PORT = 1883
DEVICE_NAME = sys.argv[1] if len(sys.argv) > 1 else "windows_pc"

TOPIC_AVAILABILITY = f"hassagent/{DEVICE_NAME}/availability"
TOPIC_CMD_SHUTDOWN = f"hassagent/{DEVICE_NAME}/command/shutdown"
TOPIC_CMD_RESTART = f"hassagent/{DEVICE_NAME}/command/restart"

def on_connect(client, userdata, flags, rc):
    print(f"[{DEVICE_NAME}] Connected to Mosquitto ({BROKER_IP}) with result code {rc}")
    client.publish(TOPIC_AVAILABILITY, "online", retain=True)
    client.subscribe(TOPIC_CMD_SHUTDOWN)
    client.subscribe(TOPIC_CMD_RESTART)
    print(f"[{DEVICE_NAME}] Subscribed to commands on {TOPIC_CMD_SHUTDOWN} and {TOPIC_CMD_RESTART}")

def on_message(client, userdata, msg):
    topic = msg.topic
    payload = msg.payload.decode("utf-8").strip()
    print(f"[{DEVICE_NAME}] Received message on {topic}: {payload}")

    if topic == TOPIC_CMD_SHUTDOWN:
        print(f"[{DEVICE_NAME}] Initiating SHUTDOWN...")
        client.publish(TOPIC_AVAILABILITY, "offline", retain=True)
        time.sleep(1)
        if sys.platform.startswith("win"):
            subprocess.run(["shutdown", "/s", "/t", "0"], check=False)
        else:
            subprocess.run(["systemctl", "poweroff"], check=False)

    elif topic == TOPIC_CMD_RESTART:
        print(f"[{DEVICE_NAME}] Initiating RESTART...")
        client.publish(TOPIC_AVAILABILITY, "offline", retain=True)
        time.sleep(1)
        if sys.platform.startswith("win"):
            subprocess.run(["shutdown", "/r", "/t", "0"], check=False)
        else:
            subprocess.run(["systemctl", "reboot"], check=False)

def main():
    client = mqtt.Client(client_id=f"hass_agent_{DEVICE_NAME}_{int(time.time())}")
    client.will_set(TOPIC_AVAILABILITY, "offline", retain=True)
    client.on_connect = on_connect
    client.on_message = on_message

    print(f"Connecting to broker {BROKER_IP}:{BROKER_PORT} for device '{DEVICE_NAME}'...")
    while True:
        try:
            client.connect(BROKER_IP, BROKER_PORT, 60)
            break
        except Exception as e:
            print(f"Connection failed: {e}. Retrying in 5 seconds...")
            time.sleep(5)

    client.loop_forever()

if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
linux_laptop_agent.py - Lightweight MQTT Power Control Daemon for Ubuntu Laptop (192.168.1.117)
Connects to the Homelab Mosquitto broker (192.168.1.100:1883).

Listens for:
  - Topic: hassagent/laptop/command/shutdown -> executes systemctl poweroff
  - Topic: hassagent/laptop/command/restart  -> executes systemctl reboot

Publishes:
  - Topic: hassagent/laptop/availability     -> "online" (LWT sets "offline" on disconnect)
"""

import sys
import os
import time
import subprocess

try:
    import paho.mqtt.client as mqtt
except ImportError:
    print("[ERROR] paho-mqtt not installed. Run: sudo apt install python3-paho-mqtt (or pip install paho-mqtt)", file=sys.stderr)
    sys.exit(1)

BROKER_IP = "192.168.1.100"
BROKER_PORT = 1883
DEVICE_NAME = "laptop"

TOPIC_AVAILABILITY = f"hassagent/{DEVICE_NAME}/availability"
TOPIC_CMD_SHUTDOWN = f"hassagent/{DEVICE_NAME}/command/shutdown"
TOPIC_CMD_RESTART = f"hassagent/{DEVICE_NAME}/command/restart"

def execute_action(action):
    print(f"[{DEVICE_NAME}] Executing {action}...", flush=True)
    time.sleep(1)
    if action == "shutdown":
        cmd = ["systemctl", "poweroff"]
    elif action == "restart":
        cmd = ["systemctl", "reboot"]
    else:
        return
    try:
        subprocess.run(cmd, check=False)
    except Exception as e:
        print(f"Failed to execute {cmd}: {e}", file=sys.stderr)

def on_connect(client, userdata, flags, rc):
    print(f"[{DEVICE_NAME}] Connected to Mosquitto ({BROKER_IP}) with code {rc}", flush=True)
    client.publish(TOPIC_AVAILABILITY, "online", retain=True)
    client.subscribe(TOPIC_CMD_SHUTDOWN)
    client.subscribe(TOPIC_CMD_RESTART)
    print(f"[{DEVICE_NAME}] Listening on {TOPIC_CMD_SHUTDOWN} and {TOPIC_CMD_RESTART}", flush=True)

def on_message(client, userdata, msg):
    topic = msg.topic
    print(f"[{DEVICE_NAME}] Message received on {topic}", flush=True)

    if topic == TOPIC_CMD_SHUTDOWN:
        client.publish(TOPIC_AVAILABILITY, "offline", retain=True)
        execute_action("shutdown")

    elif topic == TOPIC_CMD_RESTART:
        client.publish(TOPIC_AVAILABILITY, "offline", retain=True)
        execute_action("restart")

def main():
    client = mqtt.Client(client_id=f"ubuntu_{DEVICE_NAME}_{int(time.time())}")
    client.will_set(TOPIC_AVAILABILITY, "offline", retain=True)
    client.on_connect = on_connect
    client.on_message = on_message

    print(f"Connecting to {BROKER_IP}:{BROKER_PORT}...", flush=True)
    while True:
        try:
            client.connect(BROKER_IP, BROKER_PORT, 60)
            break
        except Exception as e:
            print(f"Connection failed: {e}. Retrying in 5 seconds...", flush=True)
            time.sleep(5)

    client.loop_forever()

if __name__ == "__main__":
    main()

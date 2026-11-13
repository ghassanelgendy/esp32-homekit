#!/usr/bin/env python3
"""
homelab_power_server.py - Central Machine Power & Bridge API for Homelab & Homebridge.
Port 5005.
"""

import os
import sys
import time
import subprocess
import threading
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
import urllib.parse
import json

PORT = 5005
shutdown_timer = None
shutdown_lock = threading.Lock()

def send_mqtt_command(topic, payload="1"):
    try:
        subprocess.run([
            "docker", "exec", "mosquitto", "mosquitto_pub",
            "-t", topic, "-m", payload
        ], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        print(f"[ERROR] send_mqtt_command failed: {e}", file=sys.stderr)

def send_wol(mac):
    try:
        mac_bytes = bytes.fromhex(mac.replace(":", "").replace("-", ""))
        packet = b'\xff' * 6 + mac_bytes * 16
        import socket
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            s.sendto(packet, ('192.168.1.255', 9))
    except Exception as e:
        print(f"[ERROR] WOL failed: {e}", file=sys.stderr)

def is_host_alive(ip):
    try:
        res = subprocess.run(["ping", "-c", "1", "-W", "1", ip],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return res.returncode == 0
    except Exception:
        return False

def execute_reboot():
    time.sleep(1)
    res = subprocess.run(["systemctl", "reboot"], check=False)
    if res.returncode != 0:
        subprocess.run([
            "docker", "exec", "owntone", "sh", "-c",
            "sync; echo s > /proc/sysrq-trigger; echo u > /proc/sysrq-trigger; echo b > /proc/sysrq-trigger"
        ], check=False)

def execute_poweroff():
    time.sleep(1)
    res = subprocess.run(["systemctl", "poweroff"], check=False)
    if res.returncode != 0:
        subprocess.run([
            "docker", "exec", "owntone", "sh", "-c",
            "sync; echo s > /proc/sysrq-trigger; echo u > /proc/sysrq-trigger; echo o > /proc/sysrq-trigger"
        ], check=False)

def execute_windows_command(action):
    # Direct SSH execution as key is already authorized on Windows PC
    try:
        cmd = "shutdown /r /f /t 0" if action == "restart" else "shutdown /s /f /t 0"
        subprocess.run([
            "ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=4",
            "ghass@192.168.1.105", cmd
        ], check=False)
    except Exception as e:
        print(f"[ERROR] Windows SSH command failed: {e}", file=sys.stderr)
    # Also publish via MQTT as secondary channel
    send_mqtt_command(f"hassagent/windows_pc/command/{action}")

class PowerHandler(BaseHTTPRequestHandler):
    def send_json(self, data, status=200):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        global shutdown_timer
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        # Homelab Host
        if path == "/status":
            self.send_json({"status": "online", "host": "192.168.1.100"})
        elif path == "/reboot":
            threading.Thread(target=execute_reboot, daemon=True).start()
            self.send_json({"result": "reboot_triggered"})
        elif path == "/shutdown" or path == "/poweroff":
            threading.Thread(target=execute_poweroff, daemon=True).start()
            self.send_json({"result": "shutdown_triggered"})
        elif path == "/timer/shutdown":
            seconds = int(query.get("seconds", [0])[0])
            with shutdown_lock:
                if shutdown_timer is not None and shutdown_timer.is_alive():
                    shutdown_timer.cancel()
                shutdown_timer = threading.Timer(seconds, execute_poweroff)
                shutdown_timer.daemon = True
                shutdown_timer.start()
            self.send_json({"result": "timer_scheduled", "seconds": seconds})
        elif path == "/timer/cancel":
            with shutdown_lock:
                if shutdown_timer is not None:
                    shutdown_timer.cancel()
                    shutdown_timer = None
            self.send_json({"result": "timer_cancelled"})

        # Windows PC endpoints
        elif path == "/pc/status":
            online = is_host_alive("192.168.1.105") or is_host_alive("192.168.1.103")
            self.send_json({"status": "online" if online else "offline"})
        elif path == "/pc/on":
            send_wol("c0:18:03:b7:8c:f0")
            self.send_json({"result": "wol_sent"})
        elif path == "/pc/off":
            threading.Thread(target=execute_windows_command, args=("shutdown",), daemon=True).start()
            self.send_json({"result": "shutdown_command_sent"})
        elif path == "/pc/restart":
            threading.Thread(target=execute_windows_command, args=("restart",), daemon=True).start()
            self.send_json({"result": "restart_command_sent"})

        # Laptop endpoints
        elif path == "/laptop/status":
            online = is_host_alive("192.168.1.117")
            self.send_json({"status": "online" if online else "offline"})
        elif path == "/laptop/on":
            send_wol("e8:fb:1c:54:48:67")
            self.send_json({"result": "wol_sent"})
        elif path == "/laptop/off":
            send_mqtt_command("hassagent/laptop/command/shutdown")
            self.send_json({"result": "shutdown_command_sent"})
        elif path == "/laptop/restart":
            send_mqtt_command("hassagent/laptop/command/restart")
            self.send_json({"result": "restart_command_sent"})

        else:
            self.send_json({"error": "not_found"}, status=404)

    def do_POST(self):
        self.do_GET()

    def log_message(self, format, *args):
        pass

if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", PORT), PowerHandler)
    server.serve_forever()

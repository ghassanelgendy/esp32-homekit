#!/usr/bin/env python3
"""
server_monitor.py - Tracks host server restarts, shutdowns, downtimes, and internet connectivity.
Writes live tracking state to Home Assistant configuration for real-time dashboards and badges.
"""

import os
import sys
import time
import json
import signal
import socket
import datetime
import subprocess

CONFIG_DIR = "/home/ghesso/homeassistant_config"
STATE_FILE = os.path.join(CONFIG_DIR, "server_tracker_state.json")
OUTPUT_FILE = os.path.join(CONFIG_DIR, "server_tracker.json")
WWW_FILE = os.path.join(CONFIG_DIR, "www", "server_tracker.json")

def format_duration(seconds):
    if seconds is None:
        return "Unknown"
    seconds = int(round(seconds))
    if seconds < 0:
        seconds = 0
    if seconds < 60:
        return f"{seconds}s"
    mins, secs = divmod(seconds, 60)
    if mins < 60:
        return f"{mins}m {secs}s" if secs else f"{mins}m"
    hours, mins = divmod(mins, 60)
    if hours < 24:
        return f"{hours}h {mins}m"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h {mins}m"

def get_system_boot_time():
    try:
        with open("/proc/stat", "r") as f:
            for line in f:
                if line.startswith("btime "):
                    return float(line.split()[1])
    except Exception:
        pass
    try:
        with open("/proc/uptime", "r") as f:
            uptime_sec = float(f.readline().split()[0])
            return time.time() - uptime_sec
    except Exception:
        pass
    return time.time()

def get_journal_boots():
    """Parses boot history from systemd journal."""
    try:
        out = subprocess.check_output(
            ["journalctl", "--list-boots", "-o", "json"],
            stderr=subprocess.DEVNULL,
            timeout=5
        )
        boots = json.loads(out)
        return boots
    except Exception:
        return []

def check_internet(timeout=1.5):
    """Fast check for internet connectivity via DNS port 53 (Cloudflare & Google & Quad9)."""
    endpoints = [
        (os.environ.get("DNS_HOST_1", "one.one.one.one"), 53),
        (os.environ.get("DNS_HOST_2", "dns.google"), 53),
        (os.environ.get("DNS_HOST_3", "dns.quad9.net"), 53),
    ]
    for host, port in endpoints:
        try:
            s = socket.create_connection((host, port), timeout=timeout)
            s.close()
            return True
        except Exception:
            continue
    return False

def atomic_write_json(filepath, data):
    tmp = f"{filepath}.tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, filepath)
    except Exception as e:
        print(f"[Error] Writing {filepath}: {e}", file=sys.stderr, flush=True)

class ServerMonitor:
    def __init__(self):
        self.running = True
        signal.signal(signal.SIGTERM, self.handle_shutdown)
        signal.signal(signal.SIGINT, self.handle_shutdown)

        self.state = self.load_state()
        self.init_boot_info()

    def load_state(self):
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, "r") as f:
                    return json.load(f)
            except Exception:
                pass
        return {
            "last_heartbeat": None,
            "clean_shutdown": False,
            "shutdown_time": None,
            "shutdown_type": "Unknown",
            "internet_status": "unknown",
            "current_outage_start": None,
            "last_disconnect": None,
            "last_duration_seconds": None,
            "last_duration_human": None,
            "outages_today_date": None,
            "outages_today_count": 0,
            "outage_history": []
        }

    def save_state(self):
        atomic_write_json(STATE_FILE, self.state)

    def handle_shutdown(self, signum, frame):
        now_dt = datetime.datetime.now(datetime.timezone.utc)
        print(f"[Monitor] Shutdown signal received ({signum}) at {now_dt.isoformat()}", flush=True)
        self.state["clean_shutdown"] = True
        self.state["shutdown_time"] = now_dt.isoformat()
        self.state["shutdown_type"] = "Clean Shutdown (Systemd)"
        self.state["last_heartbeat"] = now_dt.isoformat()
        self.save_state()
        self.write_output(now_dt)
        self.running = False
        sys.exit(0)

    def init_boot_info(self):
        btime_sec = get_system_boot_time()
        self.boot_dt = datetime.datetime.fromtimestamp(btime_sec, datetime.timezone.utc)
        self.boot_iso = self.boot_dt.isoformat()

        boots = get_journal_boots()
        self.restart_history = []
        self.last_shutdown_dt = None
        self.last_shutdown_iso = None
        self.last_downtime_sec = None
        self.shutdown_type = "Clean Shutdown"

        if boots and len(boots) >= 1:
            curr = boots[-1]
            c_start_ts = curr["first_entry"] / 1e6
            self.boot_dt = datetime.datetime.fromtimestamp(c_start_ts, datetime.timezone.utc)
            self.boot_iso = self.boot_dt.isoformat()

            if len(boots) >= 2:
                prev = boots[-2]
                prev_end_ts = prev["last_entry"] / 1e6
                self.last_shutdown_dt = datetime.datetime.fromtimestamp(prev_end_ts, datetime.timezone.utc)
                self.last_shutdown_iso = self.last_shutdown_dt.isoformat()
                self.last_downtime_sec = (self.boot_dt - self.last_shutdown_dt).total_seconds()

            # Compile recent boots history (last 10)
            for i in range(len(boots) - 1, max(-1, len(boots) - 11), -1):
                b_curr = boots[i]
                b_prev = boots[i - 1] if i > 0 else None
                s_dt = datetime.datetime.fromtimestamp(b_curr["first_entry"] / 1e6, datetime.timezone.utc)
                e_dt = datetime.datetime.fromtimestamp(b_curr["last_entry"] / 1e6, datetime.timezone.utc)
                uptime = (e_dt - s_dt).total_seconds()
                down_str = "N/A"
                if b_prev:
                    pe_dt = datetime.datetime.fromtimestamp(b_prev["last_entry"] / 1e6, datetime.timezone.utc)
                    down_str = format_duration((s_dt - pe_dt).total_seconds())

                self.restart_history.append({
                    "index": b_curr.get("index", 0),
                    "boot_time": s_dt.isoformat(),
                    "boot_time_formatted": s_dt.strftime("%b %d, %I:%M %p"),
                    "downtime_before": down_str,
                    "session_uptime": format_duration(uptime)
                })
        else:
            if self.state.get("shutdown_time"):
                try:
                    self.last_shutdown_dt = datetime.datetime.fromisoformat(self.state["shutdown_time"])
                    self.last_shutdown_iso = self.state["shutdown_time"]
                    self.last_downtime_sec = (self.boot_dt - self.last_shutdown_dt).total_seconds()
                except Exception:
                    pass
            elif self.state.get("last_heartbeat"):
                try:
                    self.last_shutdown_dt = datetime.datetime.fromisoformat(self.state["last_heartbeat"])
                    self.last_shutdown_iso = self.state["last_heartbeat"]
                    self.last_downtime_sec = (self.boot_dt - self.last_shutdown_dt).total_seconds()
                    self.shutdown_type = "Power Loss / Unclean"
                except Exception:
                    pass

        if self.state.get("shutdown_type") and self.state["shutdown_type"] not in ["Running", "Unknown"]:
            self.shutdown_type = self.state["shutdown_type"]
        elif self.state.get("clean_shutdown"):
            self.shutdown_type = "Clean Shutdown (Graceful)"
        elif self.last_downtime_sec is not None and self.last_downtime_sec < 120:
            self.shutdown_type = "Reboot (Fast Restart)"
        else:
            self.shutdown_type = "Power Loss / Long Downtime"

        self.state["clean_shutdown"] = False
        self.state["shutdown_type"] = "Running"
        self.save_state()

    def update_internet_tracking(self, is_online, now_dt):
        now_iso = now_dt.isoformat()
        today_str = now_dt.strftime("%Y-%m-%d")

        if self.state.get("outages_today_date") != today_str:
            self.state["outages_today_date"] = today_str
            self.state["outages_today_count"] = 0

        prev_status = self.state.get("internet_status", "unknown")

        if not is_online:
            if prev_status != "offline":
                print(f"[Monitor] Internet Disconnected at {now_iso}", flush=True)
                self.state["internet_status"] = "offline"
                self.state["current_outage_start"] = now_iso
                self.save_state()
        else:
            if prev_status == "offline" and self.state.get("current_outage_start"):
                try:
                    start_dt = datetime.datetime.fromisoformat(self.state["current_outage_start"])
                    duration_sec = (now_dt - start_dt).total_seconds()
                    duration_human = format_duration(duration_sec)
                    print(f"[Monitor] Internet Reconnected. Outage duration: {duration_human}", flush=True)
                    self.state["internet_status"] = "online"
                    self.state["last_disconnect"] = self.state["current_outage_start"]
                    self.state["last_duration_seconds"] = duration_sec
                    self.state["last_duration_human"] = duration_human
                    self.state["current_outage_start"] = None
                    self.state["outages_today_count"] = self.state.get("outages_today_count", 0) + 1

                    outage_entry = {
                        "start": start_dt.isoformat(),
                        "start_formatted": start_dt.strftime("%b %d, %I:%M %p"),
                        "end": now_iso,
                        "duration": duration_human,
                        "duration_seconds": round(duration_sec, 1)
                    }
                    history = self.state.get("outage_history", [])
                    history.insert(0, outage_entry)
                    self.state["outage_history"] = history[:20]
                    self.save_state()
                except Exception as e:
                    print(f"[Monitor] Error calculating outage: {e}", file=sys.stderr, flush=True)
                    self.state["internet_status"] = "online"
                    self.state["current_outage_start"] = None
                    self.save_state()
            elif prev_status != "online":
                self.state["internet_status"] = "online"
                self.save_state()

    def get_ssd_stats(self):
        info = {
            "mounted": False,
            "total_gb": 0,
            "used_gb": 0,
            "free_gb": 0,
            "percent": 0.0,
        }
        try:
            import shutil
            if os.path.exists("/mnt/ssd"):
                u = shutil.disk_usage("/mnt/ssd")
                if u.total > 0:
                    info = {
                        "mounted": True,
                        "total_gb": round(u.total / (1024**3), 1),
                        "used_gb": round(u.used / (1024**3), 1),
                        "free_gb": round(u.free / (1024**3), 1),
                        "percent": round((u.used / u.total) * 100.0, 1),
                    }
        except Exception:
            pass
        return info

    def write_output(self, now_dt):
        now_iso = now_dt.isoformat()
        uptime_sec = (now_dt - self.boot_dt).total_seconds()
        uptime_human = format_duration(uptime_sec)
        downtime_human = format_duration(self.last_downtime_sec)

        cur_outage_sec = 0
        if self.state.get("internet_status") == "offline" and self.state.get("current_outage_start"):
            try:
                s_dt = datetime.datetime.fromisoformat(self.state["current_outage_start"])
                cur_outage_sec = (now_dt - s_dt).total_seconds()
            except Exception:
                pass

        payload = {
            "server": {
                "last_restart": self.boot_iso,
                "last_restart_formatted": self.boot_dt.strftime("%b %d, %I:%M %p"),
                "uptime_seconds": round(uptime_sec, 1),
                "uptime_human": uptime_human,
                "last_shutdown": self.last_shutdown_iso,
                "last_shutdown_formatted": self.last_shutdown_dt.strftime("%b %d, %I:%M %p") if self.last_shutdown_dt else "Unknown",
                "last_downtime_seconds": round(self.last_downtime_sec, 1) if self.last_downtime_sec else 0,
                "last_downtime_human": downtime_human,
                "shutdown_type": self.shutdown_type,
                "total_reboots_tracked": len(self.restart_history)
            },
            "internet": {
                "status": self.state.get("internet_status", "online"),
                "last_disconnect": self.state.get("last_disconnect"),
                "last_disconnect_formatted": (
                    datetime.datetime.fromisoformat(self.state["last_disconnect"]).strftime("%b %d, %I:%M %p")
                    if self.state.get("last_disconnect") else "None"
                ),
                "last_duration_seconds": self.state.get("last_duration_seconds"),
                "last_duration_human": self.state.get("last_duration_human") or "None",
                "outages_today_count": self.state.get("outages_today_count", 0),
                "current_outage_duration_seconds": round(cur_outage_sec, 1),
                "current_outage_duration_human": format_duration(cur_outage_sec) if cur_outage_sec else "0s"
            },
            "restart_history": self.restart_history,
            "outage_history": self.state.get("outage_history", []),
            "ssd": self.get_ssd_stats(),
            "last_updated": now_iso
        }

        atomic_write_json(OUTPUT_FILE, payload)
        atomic_write_json(WWW_FILE, payload)

    def run(self):
        print(f"[Monitor] Server monitor running. Booted at {self.boot_iso}", flush=True)
        while self.running:
            try:
                now_dt = datetime.datetime.now(datetime.timezone.utc)
                is_online = check_internet()
                self.update_internet_tracking(is_online, now_dt)
                self.state["last_heartbeat"] = now_dt.isoformat()
                self.save_state()
                self.write_output(now_dt)
            except Exception as e:
                print(f"[Monitor] Loop error: {e}", file=sys.stderr, flush=True)

            time.sleep(10)

if __name__ == "__main__":
    monitor = ServerMonitor()
    monitor.run()

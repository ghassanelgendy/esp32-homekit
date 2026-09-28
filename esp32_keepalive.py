#!/usr/bin/env python3
import urllib.request
import time

TARGET_URL = "http://192.168.1.7/status"
INTERVAL = 2.0  # Poll every 2 seconds to keep the HTTP TCP/IP stack hot

def main():
    while True:
        try:
            req = urllib.request.Request(TARGET_URL, headers={"Connection": "close"})
            with urllib.request.urlopen(req, timeout=1.5) as r:
                _ = r.read(64)
        except Exception:
            pass
        time.sleep(INTERVAL)

if __name__ == "__main__":
    main()

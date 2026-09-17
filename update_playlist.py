#!/usr/bin/env python3
import json
import os
import socket
import subprocess
import time
import urllib.request

# Paths on host
MUSIC_DIR = "/mnt/sdc1/Music"
QURAN_DIR = "/mnt/sda5/Quran"
PLAYLIST_NAME = "quran_all"
PLAYLIST_FILE = f"{PLAYLIST_NAME}.m3u"
PLAYLIST_PATH = os.path.join(MUSIC_DIR, PLAYLIST_FILE)

OWNTONE_API = "http://192.168.1.100:3689/api"
OWNTONE_MPD_HOST = "192.168.1.100"
OWNTONE_MPD_PORT = 6600

def wait_for_port(host, port, timeout=120):
    print(f"Waiting for {host}:{port} to become available...")
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            with socket.create_connection((host, port), timeout=2):
                print(f"{host}:{port} is available.")
                return True
        except (socket.timeout, ConnectionRefusedError, OSError):
            time.sleep(2)
    print(f"Timeout waiting for {host}:{port}.")
    return False

def generate_playlist():
    tracks = []
    
    # Scan local Music dir
    for root, _, files in os.walk(MUSIC_DIR):
        if "Music/Quran" in root:
            continue
        for file in files:
            if file.lower().endswith(('.mp3', '.m4a', '.flac', '.wav')) and file != PLAYLIST_FILE:
                full_path = os.path.join(root, file)
                rel_path = os.path.relpath(full_path, MUSIC_DIR)
                tracks.append(rel_path)
                
    # Scan /mnt/sda5/Quran
    if os.path.exists(QURAN_DIR):
        for root, _, files in os.walk(QURAN_DIR):
            for file in files:
                if file.lower().endswith(('.mp3', '.m4a', '.flac', '.wav')):
                    full_path = os.path.join(root, file)
                    rel_to_quran = os.path.relpath(full_path, QURAN_DIR)
                    rel_path = os.path.join("Quran", rel_to_quran)
                    tracks.append(rel_path)
                    
    # Write playlist
    with open(PLAYLIST_PATH, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for track in sorted(tracks):
            f.write(f"{track}\n")
            
    print(f"Generated playlist with {len(tracks)} tracks.")
    return len(tracks)

def is_player_playing():
    try:
        req = urllib.request.Request(f"{OWNTONE_API}/player")
        with urllib.request.urlopen(req, timeout=4) as resp:
            data = json.loads(resp.read().decode())
            return data.get("state") == "play"
    except Exception as e:
        print(f"Player state check warning: {e}")
        return False

def configure_playback():
    print("Configuring playback...")
    
    # Enable shuffle via REST API first
    try:
        req = urllib.request.Request(f"{OWNTONE_API}/player/shuffle?mode=on", method="PUT")
        urllib.request.urlopen(req)
        print("Shuffle mode enabled.")
    except Exception as e:
        print(f"Shuffle API warning: {e}")

    # Load, repeat, play via MPD
    commands = [
        "clear",
        f"load \"file:/music/{PLAYLIST_NAME}\"",
        "repeat 1",
        "play",
        "close"
    ]
    payload = "\n".join(commands) + "\n"
    
    try:
        proc = subprocess.Popen(["nc", "-q", "2", OWNTONE_MPD_HOST, str(OWNTONE_MPD_PORT)],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True)
        stdout, _ = proc.communicate(input=payload)
        print("MPD Playlist loaded and started playing.")
    except Exception as e:
        print(f"MPD configuration error: {e}")

if __name__ == "__main__":
    # Wait for OwnTone MPD port to be open on startup before proceeding
    wait_for_port(OWNTONE_MPD_HOST, OWNTONE_MPD_PORT)
    count = generate_playlist()
    if count > 0:
        # Give OwnTone a moment to note the playlist file modification
        time.sleep(5)
        if is_player_playing():
            print("Track is currently playing. Leaving playback uninterrupted.")
        else:
            print("Nothing is currently playing. Configuring playback and shuffling...")
            configure_playback()



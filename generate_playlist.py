#!/usr/bin/env python3
import os
import glob

# Paths on host
MUSIC_DIR = "/mnt/sdc1/Music"
QURAN_DIR = "/mnt/sda5/Quran"
PLAYLIST_PATH = os.path.join(MUSIC_DIR, "quran_all.m3u")

def main():
    tracks = []
    
    # Scan files in /home/ghesso/Music
    for root, _, files in os.walk(MUSIC_DIR):
        # Skip the empty/mountpoint Quran dir to avoid duplicates/confusion on host
        if "Music/Quran" in root:
            continue
        for file in files:
            if file.lower().endswith(('.mp3', '.m4a', '.flac', '.wav')):
                full_path = os.path.join(root, file)
                # Make relative to MUSIC_DIR (which maps to /music in container)
                rel_path = os.path.relpath(full_path, MUSIC_DIR)
                tracks.append(rel_path)
                
    # Scan files in /mnt/sda5/Quran
    if os.path.exists(QURAN_DIR):
        for root, _, files in os.walk(QURAN_DIR):
            for file in files:
                if file.lower().endswith(('.mp3', '.m4a', '.flac', '.wav')):
                    full_path = os.path.join(root, file)
                    # /mnt/sda5/Quran is mounted to /music/Quran in container.
                    # So paths should start with "Quran/"
                    rel_to_quran = os.path.relpath(full_path, QURAN_DIR)
                    rel_path = os.path.join("Quran", rel_to_quran)
                    tracks.append(rel_path)
                    
    # Write M3U playlist file
    with open(PLAYLIST_PATH, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for track in sorted(tracks):
            f.write(f"{track}\n")
            
    print(f"Generated playlist with {len(tracks)} tracks at {PLAYLIST_PATH}")

if __name__ == "__main__":
    main()

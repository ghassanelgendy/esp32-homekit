#!/bin/bash
set -e

echo "=== Installing Homelab Power Agent on Ubuntu Laptop ==="
sudo apt-get update -y
sudo apt-get install -y python3 python3-pip python3-paho-mqtt

sudo cp /home/ghesso/homekit/linux_laptop_agent.py /usr/local/bin/linux_laptop_agent.py
sudo chmod +x /usr/local/bin/linux_laptop_agent.py

sudo cp /home/ghesso/homekit/laptop-power-agent.service /etc/systemd/system/laptop-power-agent.service
sudo systemctl daemon-reload
sudo systemctl enable --now laptop-power-agent.service

echo "=== Installation Complete! Service status: ==="
sudo systemctl status laptop-power-agent.service --no-pager

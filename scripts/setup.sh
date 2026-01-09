#!/bin/bash

set -e

# 0. Enable ip fowarding
sudo sysctl -w net.ipv4.ip_forward=1
sudo sysctl -w net.ipv6.conf.all.forwarding=1
echo "net.ipv4.ip_forward=1" | sudo tee -a /etc/sysctl.conf
echo "net.ipv6.conf.all.forwarding=1" | sudo tee -a /etc/sysctl.conf

# 1. Export variables from .env so the shell can use them
export $(grep -v '^#' .env | xargs)

# 2. Start containers (Docker reads .env automatically)
docker compose up -d --pull always --build --remove-orphans

# 3. Fix permissions using the variables
# This uses DEFAULT_STORAGE_MOUNT (/mnt/usb) and PUID/GUID (1000)
sudo chown -R $DEFAULT_PUID:$DEFAULT_GUID $DEFAULT_STORAGE_MOUNT

# 4. Restart services to apply changes
docker compose restart

# 5. Turn off compose
docker compose down
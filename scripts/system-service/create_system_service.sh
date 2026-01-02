#!/bin/bash

readonly SERVICE_NAME=homelab

set -e

sudo cp scripts/system-service/${SERVICE_NAME}.service /etc/systemd/system/${SERVICE_NAME}.service

# 1. Reload systemd to recognize the new file
sudo systemctl daemon-reload

# 2. Enable the service to run on startup
sudo systemctl enable ${SERVICE_NAME}.service

# 3. Start the service immediately (to test it)
sudo systemctl start ${SERVICE_NAME}.service
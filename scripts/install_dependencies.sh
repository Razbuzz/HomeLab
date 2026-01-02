#!/bin/bash

set -e
sudo apt update
sudo xargs -a apt-requirements.txt apt install -y
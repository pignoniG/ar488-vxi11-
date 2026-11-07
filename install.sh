#!/bin/sh
# Install / update the AR488 VXI-11 gateway as a systemd service.
set -e
cd "$(dirname "$0")"
sudo apt-get install -y rpcbind python3-serial
id ar488gw >/dev/null 2>&1 || sudo useradd --system --no-create-home --shell /usr/sbin/nologin -G dialout ar488gw
sudo mkdir -p /opt/ar488-vxi11
sudo rm -rf /opt/ar488-vxi11/vxi11_server
sudo cp -r ar488_vxi11.py vxi11_server /opt/ar488-vxi11/
[ -f /etc/ar488-vxi11.toml ] || sudo cp ar488-vxi11.toml /etc/ar488-vxi11.toml
sudo cp ar488-vxi11.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now rpcbind.socket rpcbind
sudo systemctl enable ar488-vxi11
sudo systemctl restart ar488-vxi11
sleep 6
systemctl --no-pager status ar488-vxi11 | head -20
rpcinfo -p | grep -E '395183|395184' || echo "WARNING: VXI-11 service not registered with rpcbind"

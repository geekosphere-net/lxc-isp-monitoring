#!/usr/bin/env bash

# Copyright (c) 2021-2026 community-scripts ORG
# Author: Devin Richards
# License: MIT | https://github.com/community-scripts/ProxmoxVE/raw/main/LICENSE
# Source: https://github.com/geekosphere-net/lxc-isp-monitoring

source /dev/stdin <<<"$FUNCTIONS_FILE_PATH"
color
verb_ip6
catch_errors
setting_up_container
network_check
update_os

msg_info "Installing Dependencies (Patience)"
$STD apt-get install -y \
  git \
  python3 \
  python3-pip \
  python3-venv \
  build-essential \
  libssl-dev \
  libffi-dev \
  ca-certificates
msg_ok "Installed Dependencies"

msg_info "Installing ${APP}"
mkdir -p /opt/pinging-monitor /var/lib/pinging-monitor
chown nobody:nogroup /var/lib/pinging-monitor

$STD git clone --depth 1 https://github.com/geekosphere-net/lxc-isp-monitoring.git /tmp/pinging-src
cp -r /tmp/pinging-src/app.py /opt/pinging-monitor/
cp -r /tmp/pinging-src/dashboard /opt/pinging-monitor/
cp /tmp/pinging-src/requirements.txt /opt/pinging-monitor/

INSTALLED_HASH=$(git -C /tmp/pinging-src rev-parse --short HEAD 2>/dev/null || echo "unknown")
echo "$INSTALLED_HASH" >/opt/pinging-monitor/version
rm -rf /tmp/pinging-src

python3 -m venv /opt/pinging-monitor/.venv
$STD /opt/pinging-monitor/.venv/bin/pip install --upgrade pip
$STD /opt/pinging-monitor/.venv/bin/pip install -r /opt/pinging-monitor/requirements.txt
msg_ok "Installed ${APP} (${INSTALLED_HASH})"

msg_info "Creating Service"
cat <<EOF >/etc/systemd/system/pinging-monitor.service
[Unit]
Description=Pinging Monitor — ISP connectivity daemon
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=nobody
WorkingDirectory=/opt/pinging-monitor
Environment="DB_PATH=/var/lib/pinging-monitor/monitor.db"
Environment="TARGET_HOST=https://pinging.net"
Environment="RETENTION_DAYS=30"
ExecStart=/opt/pinging-monitor/.venv/bin/uvicorn app:app --host 0.0.0.0 --port 8080
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF
systemctl enable -q --now pinging-monitor
msg_ok "Created Service"

motd_ssh
customize
cleanup_lxc

#!/usr/bin/env bash
# setup.sh — run this inside a fresh Debian 12 LXC to install the ISP monitor.
#
# Usage:
#   bash setup.sh
#
# The dashboard will be served at http://<lxc-ip>:8080
# Logs: journalctl -u pinging-monitor -f
set -euo pipefail

INSTALL_DIR=/opt/pinging-monitor
SERVICE=pinging-monitor
DATA_DIR=/var/lib/pinging-monitor

echo "==> Installing system dependencies..."
apt-get update -q
apt-get install -y -q \
    python3 python3-pip python3-venv \
    build-essential libssl-dev libffi-dev \
    ca-certificates curl

echo "==> Creating directories..."
mkdir -p "$DATA_DIR"
mkdir -p "$INSTALL_DIR"
chown nobody:nogroup "$DATA_DIR"

echo "==> Copying monitor files to $INSTALL_DIR..."
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cp "$SCRIPT_DIR/app.py" "$INSTALL_DIR/"
cp "$SCRIPT_DIR/requirements.txt" "$INSTALL_DIR/"
cp -r "$SCRIPT_DIR/dashboard" "$INSTALL_DIR/"

echo "==> Creating Python virtual environment..."
python3 -m venv "$INSTALL_DIR/.venv"
"$INSTALL_DIR/.venv/bin/pip" install --upgrade pip --quiet
echo "    Installing Python packages (aiortc build may take a few minutes)..."
"$INSTALL_DIR/.venv/bin/pip" install -r "$INSTALL_DIR/requirements.txt" --quiet

echo "==> Creating systemd service /etc/systemd/system/${SERVICE}.service..."
cat > "/etc/systemd/system/${SERVICE}.service" <<EOF
[Unit]
Description=ISP Monitor — pinging.net connectivity daemon
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=nobody
WorkingDirectory=$INSTALL_DIR
Environment="DB_PATH=$DATA_DIR/monitor.db"
Environment="TARGET_HOST=https://pinging.net"
Environment="RETENTION_DAYS=30"
ExecStart=$INSTALL_DIR/.venv/bin/uvicorn app:app --host 0.0.0.0 --port 8080
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now "$SERVICE"

echo ""
echo "==> Done!"
LXC_IP=$(hostname -I | awk '{print $1}')
echo "    Dashboard:  http://${LXC_IP}:8080"
echo "    Logs:       journalctl -u ${SERVICE} -f"
echo "    Database:   ${DATA_DIR}/monitor.db"
echo ""
echo "    Optional env vars (edit the [Service] section in the unit file):"
echo "      TARGET_HOST   — default: https://pinging.net"
echo "      DB_PATH       — default: ${DATA_DIR}/monitor.db"
echo "      PING_INTERVAL — default: 1.0  (seconds between HTTP/WebRTC pings)"
echo "      DNS_INTERVAL   — default: 30.0 (seconds between DNS checks)"
echo "      RETENTION_DAYS — default: 30   (days of history to keep; older rows pruned hourly)"
echo ""
echo "    Note: WebRTC pings require outbound UDP to pinging.net:8888."
echo "    If your LXC blocks UDP egress, HTTP pings still run."

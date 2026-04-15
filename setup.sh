#!/usr/bin/env bash
# setup.sh — install the ISP monitor on a Debian 12/13 machine or LXC.
#
# Usage:
#   bash setup.sh          # fresh install (run from inside the cloned repo)
#
# Dashboard: http://<ip>:8080
# Logs:      journalctl -u pinging-monitor -f
# Update:    run `update` inside the LXC
set -euo pipefail

INSTALL_DIR=/opt/pinging-monitor
SERVICE=pinging-monitor
DATA_DIR=/var/lib/pinging-monitor

# ── Fresh install ─────────────────────────────────────────────────────────────
echo "==> Installing runtime dependencies..."
apt-get update -q
apt-get install -y -q python3 python3-venv git ca-certificates

echo "==> Installing build dependencies (purged after compile)..."
apt-get install -y -q build-essential libssl-dev libffi-dev

echo "==> Creating directories..."
mkdir -p "$DATA_DIR" "$INSTALL_DIR"
chown nobody:nogroup "$DATA_DIR"

echo "==> Copying monitor files to $INSTALL_DIR..."
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cp "$SCRIPT_DIR/app.py" "$INSTALL_DIR/"
cp "$SCRIPT_DIR/requirements.txt" "$INSTALL_DIR/"
cp -r "$SCRIPT_DIR/dashboard" "$INSTALL_DIR/"
git -C "$SCRIPT_DIR" rev-parse --short HEAD 2>/dev/null \
    > "$INSTALL_DIR/version" || echo "unknown" > "$INSTALL_DIR/version"

echo "==> Creating Python virtual environment..."
python3 -m venv "$INSTALL_DIR/.venv"
"$INSTALL_DIR/.venv/bin/pip" install --upgrade pip --quiet
echo "    Installing Python packages (aiortc build may take a few minutes)..."
"$INSTALL_DIR/.venv/bin/pip" install -r "$INSTALL_DIR/requirements.txt" --quiet

echo "==> Removing build dependencies..."
apt-get purge -y --autoremove build-essential libssl-dev libffi-dev
apt-get clean
rm -rf /var/lib/apt/lists/*

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

echo "==> Capping journal size to 50 MB..."
mkdir -p /etc/systemd/journald.conf.d
cat > /etc/systemd/journald.conf.d/00-pinging-monitor.conf <<'JCONF'
[Journal]
SystemMaxUse=50M
JCONF
systemctl restart systemd-journald 2>/dev/null || true

systemctl daemon-reload
systemctl enable --now "$SERVICE"

echo "==> Installing update command..."
cat > /usr/bin/update <<'UPDATESCRIPT'
#!/usr/bin/env bash
set -euo pipefail
INSTALL_DIR=/opt/pinging-monitor
SERVICE=pinging-monitor

INSTALLED=$(cat "$INSTALL_DIR/version" 2>/dev/null || echo "unknown")
LATEST=$(git ls-remote https://github.com/geekosphere-net/lxc-isp-monitoring.git HEAD \
          | cut -c1-7)

if [[ "$INSTALLED" == "$LATEST" ]]; then
  echo "Already up to date ($INSTALLED)"
  exit 0
fi

echo "Updating $INSTALLED → $LATEST"
systemctl stop "$SERVICE"
git clone --depth 1 https://github.com/geekosphere-net/lxc-isp-monitoring.git /tmp/pinging-src
cp /tmp/pinging-src/app.py            "$INSTALL_DIR/app.py"
cp -r /tmp/pinging-src/dashboard/.   "$INSTALL_DIR/dashboard/"
cp /tmp/pinging-src/requirements.txt "$INSTALL_DIR/requirements.txt"
rm -rf /tmp/pinging-src
"$INSTALL_DIR/.venv/bin/pip" install -q -r "$INSTALL_DIR/requirements.txt"
echo "$LATEST" > "$INSTALL_DIR/version"
systemctl start "$SERVICE"
echo "Updated to $LATEST"
UPDATESCRIPT
chmod +x /usr/bin/update

echo ""
echo "==> Done!"
LXC_IP=$(hostname -I | awk '{print $1}')
echo "    Dashboard:  http://${LXC_IP}:8080"
echo "    Logs:       journalctl -u ${SERVICE} -f"
echo "    Database:   ${DATA_DIR}/monitor.db"
echo "    Update:     run 'update' inside this machine"
echo ""
echo "    Optional env vars (edit the [Service] section in the unit file):"
echo "      TARGET_HOST    — default: https://pinging.net"
echo "      DB_PATH        — default: ${DATA_DIR}/monitor.db"
echo "      PING_INTERVAL  — default: 1.0  (seconds between HTTP/WebRTC pings)"
echo "      DNS_INTERVAL   — default: 30.0 (seconds between DNS checks)"
echo "      RETENTION_DAYS — default: 30   (days of history to keep; older rows pruned hourly)"
echo ""
echo "    Note: WebRTC pings require outbound UDP to pinging.net:8888."
echo "    If your LXC blocks UDP egress, HTTP pings still run."

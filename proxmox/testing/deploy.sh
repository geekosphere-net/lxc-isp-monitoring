#!/usr/bin/env bash
# =============================================================================
# proxmox/testing/deploy.sh
#
# Standalone test deployment script for pinging-monitor.
# Mimics what the community-scripts framework does, but pulls everything
# directly from this repo — no build.func dependency.
#
# DELETE this entire proxmox/testing/ directory before submitting a PR
# to community-scripts/ProxmoxVE.
#
# Usage (run from the Proxmox host shell):
#   bash <(curl -fsSL https://raw.githubusercontent.com/geekosphere-net/lxc-isp-monitoring/main/proxmox/testing/deploy.sh)
#
# Or after cloning the repo locally:
#   bash proxmox/testing/deploy.sh
#
# To update an existing container:
#   CT_ID=<id> bash proxmox/testing/deploy.sh
# =============================================================================
set -euo pipefail

# -----------------------------------------------------------------------------
# Config — override via environment variables if needed
# -----------------------------------------------------------------------------
APP="Pinging Monitor"
REPO_URL="https://github.com/geekosphere-net/lxc-isp-monitoring.git"

CT_ID="${CT_ID:-}"                           # leave blank to auto-select next free ID
CT_HOSTNAME="${CT_HOSTNAME:-pinging}"
CT_STORAGE="${CT_STORAGE:-local-thin}"       # storage for container rootfs
CT_TMPL_STORAGE="${CT_TMPL_STORAGE:-local}"  # storage for template download
CT_DISK="${CT_DISK:-4}"                      # GB
CT_RAM="${CT_RAM:-512}"                      # MB
CT_CPU="${CT_CPU:-1}"
CT_BRIDGE="${CT_BRIDGE:-vmbr0}"
CT_OS="${CT_OS:-debian}"
CT_VERSION="${CT_VERSION:-13}"
CT_UNPRIVILEGED="${CT_UNPRIVILEGED:-1}"

# -----------------------------------------------------------------------------
# Colours
# -----------------------------------------------------------------------------
GN="\e[1;92m"; RD="\e[01;31m"; YW="\e[33m"; CL="\e[m"
BL="\e[36m"; BGN="\e[4;92m"
msg_info()  { echo -e "  ${YW}◷${CL}  ${1}..."; }
msg_ok()    { echo -e "  ${GN}✔${CL}  ${1}"; }
msg_error() { echo -e "  ${RD}✘${CL}  ${1}"; exit 1; }

# -----------------------------------------------------------------------------
# Preflight
# -----------------------------------------------------------------------------
if [[ "$(id -u)" -ne 0 ]]; then
  msg_error "Run this script as root on the Proxmox host."
fi
if ! command -v pct &>/dev/null; then
  msg_error "pct not found — this script must run on a Proxmox VE host."
fi

echo -e "\n${BL}  ===========================================${CL}"
echo -e "${BL}       ${APP} — Test Deployment${CL}"
echo -e "${BL}  ===========================================${CL}\n"

# -----------------------------------------------------------------------------
# If CT_ID is set and the container already exists, update in place and exit.
# -----------------------------------------------------------------------------
if [[ -n "$CT_ID" ]] && pct status "$CT_ID" &>/dev/null; then
  msg_info "Container ${CT_ID} already exists — running update"
  TMPUPDATE=$(mktemp /tmp/pinging-update-XXXXXX.sh)
  cat > "$TMPUPDATE" << 'UPDATE_EOF'
#!/usr/bin/env bash
set -euo pipefail
INSTALLED=$(cat /opt/pinging-monitor/version 2>/dev/null || echo "unknown")
LATEST=$(git ls-remote https://github.com/geekosphere-net/lxc-isp-monitoring.git HEAD \
          | cut -c1-7)
if [[ "$INSTALLED" == "$LATEST" ]]; then
  echo "Already up to date ($INSTALLED)"
  exit 0
fi
echo "Updating $INSTALLED → $LATEST"
systemctl stop pinging-monitor
git clone --depth 1 https://github.com/geekosphere-net/lxc-isp-monitoring.git \
          /tmp/pinging-src
cp /tmp/pinging-src/app.py            /opt/pinging-monitor/app.py
cp -r /tmp/pinging-src/dashboard/.   /opt/pinging-monitor/dashboard/
cp /tmp/pinging-src/requirements.txt /opt/pinging-monitor/requirements.txt
rm -rf /tmp/pinging-src
/opt/pinging-monitor/.venv/bin/pip install -q -r /opt/pinging-monitor/requirements.txt
echo "$LATEST" > /opt/pinging-monitor/version
systemctl start pinging-monitor
echo "Updated to $LATEST"
UPDATE_EOF

  pct push "$CT_ID" "$TMPUPDATE" /tmp/pinging-update.sh --perms 0755
  rm -f "$TMPUPDATE"
  pct exec "$CT_ID" -- bash /tmp/pinging-update.sh \
    && msg_ok "Update complete" \
    || msg_error "Update failed — scroll up for details"
  pct exec "$CT_ID" -- rm -f /tmp/pinging-update.sh
  exit 0
fi

# -----------------------------------------------------------------------------
# Pick a container ID if not set
# -----------------------------------------------------------------------------
if [[ -z "$CT_ID" ]]; then
  CT_ID=$(pvesh get /cluster/nextid 2>/dev/null || echo "")
  if [[ -z "$CT_ID" ]]; then
    msg_error "Could not determine next free container ID. Set CT_ID= manually."
  fi
fi
msg_ok "Container ID: ${CT_ID}"

# -----------------------------------------------------------------------------
# Download Debian template if not already present
# -----------------------------------------------------------------------------
msg_info "Checking for Debian ${CT_VERSION} template"
TMPL=$(pveam list "$CT_TMPL_STORAGE" 2>/dev/null | awk '{print $1}' | grep "debian-${CT_VERSION}-standard" | tail -1 || true)
if [[ -z "$TMPL" ]]; then
  msg_info "Downloading Debian ${CT_VERSION} template (this may take a moment)"
  pveam update &>/dev/null
  TMPL_NAME=$(pveam available --section system | awk '{print $2}' | grep "debian-${CT_VERSION}-standard" | tail -1)
  if [[ -z "$TMPL_NAME" ]]; then
    msg_error "Could not find a Debian ${CT_VERSION} template in the pveam catalog."
  fi
  pveam download "$CT_TMPL_STORAGE" "$TMPL_NAME" &>/dev/null
  TMPL="${CT_TMPL_STORAGE}:vztmpl/${TMPL_NAME}"
fi
msg_ok "Template: ${TMPL}"

# -----------------------------------------------------------------------------
# Create the container
# -----------------------------------------------------------------------------
msg_info "Creating LXC container ${CT_ID}"
pct create "$CT_ID" "$TMPL" \
  --hostname "$CT_HOSTNAME" \
  --storage "$CT_STORAGE" \
  --rootfs "${CT_STORAGE}:${CT_DISK}" \
  --memory "$CT_RAM" \
  --cores "$CT_CPU" \
  --net0 "name=eth0,bridge=${CT_BRIDGE},ip=dhcp,ip6=auto" \
  --unprivileged "$CT_UNPRIVILEGED" \
  --features "nesting=1" \
  --ostype "$CT_OS" \
  --start 0 \
  &>/dev/null
msg_ok "Created LXC container ${CT_ID}"

# -----------------------------------------------------------------------------
# Start the container
# -----------------------------------------------------------------------------
msg_info "Starting container"
pct start "$CT_ID"
sleep 5
msg_ok "Container started"

# -----------------------------------------------------------------------------
# Wait for network
# -----------------------------------------------------------------------------
msg_info "Waiting for network"
for i in {1..20}; do
  if pct exec "$CT_ID" -- ping -c1 -W2 8.8.8.8 &>/dev/null; then
    break
  fi
  sleep 2
  if [[ "$i" -eq 20 ]]; then
    msg_error "Container did not get network after 40s. Check bridge/DHCP."
  fi
done
msg_ok "Network reachable"

# -----------------------------------------------------------------------------
# Write self-contained install script to host temp file, push into container,
# then execute it. This avoids all bash -c quoting/escaping issues and ensures
# pct exec sees the real exit code.
# -----------------------------------------------------------------------------
msg_info "Preparing install script"
TMPSCRIPT=$(mktemp /tmp/pinging-ct-install-XXXXXX.sh)

cat > "$TMPSCRIPT" << 'INSTALL_EOF'
#!/usr/bin/env bash
set -euo pipefail

INSTALL_DIR=/opt/pinging-monitor
DATA_DIR=/var/lib/pinging-monitor
REPO_URL=https://github.com/geekosphere-net/lxc-isp-monitoring.git

echo "--- Installing runtime dependencies"
apt-get update -qq
apt-get install -y -q python3 python3-venv git ca-certificates

echo "--- Installing build dependencies (purged after compile)"
apt-get install -y -q build-essential libssl-dev libffi-dev

echo "--- Creating directories"
mkdir -p "$DATA_DIR" "$INSTALL_DIR/dashboard"
chown nobody:nogroup "$DATA_DIR"

echo "--- Cloning repository"
git clone --depth 1 "$REPO_URL" /tmp/pinging-src

echo "--- Copying application files"
cp /tmp/pinging-src/app.py "$INSTALL_DIR/"
cp -r /tmp/pinging-src/dashboard/. "$INSTALL_DIR/dashboard/"
cp /tmp/pinging-src/requirements.txt "$INSTALL_DIR/"
git -C /tmp/pinging-src rev-parse --short HEAD 2>/dev/null \
    > "$INSTALL_DIR/version" || echo "unknown" > "$INSTALL_DIR/version"
rm -rf /tmp/pinging-src

echo "--- Creating Python virtual environment"
python3 -m venv "$INSTALL_DIR/.venv"
"$INSTALL_DIR/.venv/bin/pip" install --upgrade pip --quiet
echo "--- Installing Python packages (aiortc build may take a few minutes)"
"$INSTALL_DIR/.venv/bin/pip" install -r "$INSTALL_DIR/requirements.txt" --quiet

echo "--- Removing build dependencies"
apt-get purge -y --autoremove build-essential libssl-dev libffi-dev
apt-get clean
rm -rf /var/lib/apt/lists/*

echo "--- Creating systemd service"
cat > /etc/systemd/system/pinging-monitor.service << 'UNIT'
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
UNIT

echo "--- Capping journal size to 50 MB"
mkdir -p /etc/systemd/journald.conf.d
cat > /etc/systemd/journald.conf.d/00-pinging-monitor.conf << 'JCONF'
[Journal]
SystemMaxUse=50M
JCONF
systemctl restart systemd-journald 2>/dev/null || true

systemctl daemon-reload
systemctl enable --now pinging-monitor
echo "--- Install complete"
INSTALL_EOF

msg_ok "Install script prepared"

# Push script into container and run it
msg_info "Running install inside container (aiortc build may take a few minutes)"
pct push "$CT_ID" "$TMPSCRIPT" /tmp/pinging-install.sh --perms 0755
rm -f "$TMPSCRIPT"

pct exec "$CT_ID" -- bash /tmp/pinging-install.sh \
  && msg_ok "Install complete" \
  || msg_error "Install failed — scroll up for details"

pct exec "$CT_ID" -- rm -f /tmp/pinging-install.sh

# -----------------------------------------------------------------------------
# Done
# -----------------------------------------------------------------------------
IP=$(pct exec "$CT_ID" -- hostname -I 2>/dev/null | awk '{print $1}' || echo "<container-ip>")
echo ""
msg_ok "Completed successfully!"
echo -e "\n  ${GN}${APP} is ready.${CL}"
echo -e "  ${YW}Dashboard:${CL}  ${BGN}http://${IP}:8080${CL}"
echo -e "  ${YW}Logs:${CL}       journalctl -u pinging-monitor -f  (inside CT ${CT_ID})"
echo -e "  ${YW}Update:${CL}     CT_ID=${CT_ID} bash deploy.sh\n"

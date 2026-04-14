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
# =============================================================================
set -euo pipefail

# -----------------------------------------------------------------------------
# Config — override via environment variables if needed
# -----------------------------------------------------------------------------
APP="Pinging Monitor"
REPO_RAW="https://raw.githubusercontent.com/geekosphere-net/lxc-isp-monitoring/main"
INSTALL_SCRIPT_URL="${REPO_RAW}/proxmox/install/pinging-monitor-install.sh"
INSTALL_FUNC_URL="https://raw.githubusercontent.com/community-scripts/ProxmoxVE/main/misc/install.func"

CT_ID="${CT_ID:-}"                        # leave blank to auto-select next free ID
CT_HOSTNAME="${CT_HOSTNAME:-pinging}"
CT_STORAGE="${CT_STORAGE:-local-thin}"    # storage for container rootfs
CT_TMPL_STORAGE="${CT_TMPL_STORAGE:-local}"  # storage for template download
CT_DISK="${CT_DISK:-4}"                   # GB
CT_RAM="${CT_RAM:-512}"                   # MB
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
else
  # pveam list returns "<storage>:vztmpl/<name>" already
  :
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
sleep 5   # give the container a moment to come up and get an IP
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
# Run install script inside the container
# -----------------------------------------------------------------------------
msg_info "Installing curl in container"
pct exec "$CT_ID" -- bash -c "apt-get update -qq && apt-get install -y -qq curl ca-certificates"
msg_ok "curl installed"

msg_info "Running install script"
pct exec "$CT_ID" -- bash -c "
  set -euo pipefail
  FUNCTIONS_FILE_PATH=\$(curl -fsSL ${INSTALL_FUNC_URL})
  export FUNCTIONS_FILE_PATH
  curl -fsSL ${INSTALL_SCRIPT_URL} -o /tmp/pinging-install.sh
  bash /tmp/pinging-install.sh
  rm -f /tmp/pinging-install.sh
" && msg_ok "Install complete" || msg_error "Install script failed — check output above"

# -----------------------------------------------------------------------------
# Done
# -----------------------------------------------------------------------------
IP=$(pct exec "$CT_ID" -- hostname -I 2>/dev/null | awk '{print $1}' || echo "<container-ip>")
echo ""
msg_ok "Completed successfully!"
echo -e "\n  ${GN}${APP} is ready.${CL}"
echo -e "  ${YW}Dashboard:${CL}  ${BGN}http://${IP}:8080${CL}"
echo -e "  ${YW}Logs:${CL}       journalctl -u pinging-monitor -f  (inside CT ${CT_ID})"
echo -e "  ${YW}Update:${CL}     re-run this script against the same CT_ID, or use"
echo -e "              the community-scripts update flow once merged.\n"

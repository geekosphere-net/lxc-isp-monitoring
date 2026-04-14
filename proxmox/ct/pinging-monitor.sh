#!/usr/bin/env bash
source <(curl -fsSL https://raw.githubusercontent.com/community-scripts/ProxmoxVE/main/misc/build.func)
# Copyright (c) 2021-2026 community-scripts ORG
# Author: Devin Richards
# License: MIT | https://github.com/community-scripts/ProxmoxVE/raw/main/LICENSE
# Source: https://github.com/geekosphere-net/lxc-isp-monitoring

APP="Pinging Monitor"
var_tags="${var_tags:-monitoring;networking}"
var_cpu="${var_cpu:-1}"
var_ram="${var_ram:-512}"
var_disk="${var_disk:-4}"
var_os="${var_os:-debian}"
var_version="${var_version:-13}"
var_unprivileged="${var_unprivileged:-1}"

header_info "$APP"
variables
color
catch_errors

function update_script() {
  header_info
  check_container_storage
  check_container_resources

  if [[ ! -d /opt/pinging-monitor ]]; then
    msg_error "No ${APP} Installation Found!"
    exit
  fi

  INSTALLED_HASH=$(cat /opt/pinging-monitor/version 2>/dev/null || echo "unknown")
  LATEST_HASH=$(curl -fsSL "https://api.github.com/repos/geekosphere-net/lxc-isp-monitoring/commits/main" | grep '"sha"' | head -1 | cut -d'"' -f4 | cut -c1-7)

  if [[ "$INSTALLED_HASH" == "$LATEST_HASH" ]]; then
    msg_ok "Already up to date (${INSTALLED_HASH})"
    exit
  fi

  msg_info "Stopping Service"
  systemctl stop pinging-monitor
  msg_ok "Stopped Service"

  msg_info "Updating ${APP}"
  git clone --depth 1 https://github.com/geekosphere-net/lxc-isp-monitoring.git /tmp/pinging-src &>/dev/null
  cp -r /tmp/pinging-src/app.py /opt/pinging-monitor/
  cp -r /tmp/pinging-src/dashboard /opt/pinging-monitor/
  cp /tmp/pinging-src/requirements.txt /opt/pinging-monitor/
  rm -rf /tmp/pinging-src
  /opt/pinging-monitor/.venv/bin/pip install -q -r /opt/pinging-monitor/requirements.txt
  echo "$LATEST_HASH" >/opt/pinging-monitor/version
  msg_ok "Updated ${APP} to ${LATEST_HASH}"

  msg_info "Starting Service"
  systemctl start pinging-monitor
  msg_ok "Started Service"
  msg_ok "Updated successfully!"
  exit
}

start
build_container
description

msg_ok "Completed successfully!\n"
echo -e "${CREATING}${GN}${APP} setup has been successfully initialized!${CL}"
echo -e "${INFO}${YW} Access it using the following URL:${CL}"
echo -e "${TAB}${GATEWAY}${BGN}http://${IP}:8080${CL}"

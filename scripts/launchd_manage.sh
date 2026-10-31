#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
PLIST_SRC_DIR="$ROOT_DIR/deploy/launchd"
AGENT_DIR="$HOME/Library/LaunchAgents"
BACKEND_LABEL="com.topicassistant.backend"
TUNNEL_LABEL="com.topicassistant.tunnel"
FRONTEND_LABEL="com.topicassistant.frontend"
UID_NUM="$(id -u)"

mkdir -p "$AGENT_DIR"

launchd_domain() {
  if launchctl print "gui/$UID_NUM" >/dev/null 2>&1; then
    echo "gui/$UID_NUM"
    return
  fi
  echo "user/$UID_NUM"
}

service_id() {
  local label="$1"
  echo "$(launchd_domain)/$label"
}

domain_service_id() {
  local domain="$1"
  local label="$2"
  echo "${domain}/${label}"
}

bootout_label_all_domains() {
  local label="$1"
  local gui_domain="gui/$UID_NUM"
  local user_domain="user/$UID_NUM"
  launchctl bootout "$(domain_service_id "$gui_domain" "$label")" >/dev/null 2>&1 || true
  launchctl bootout "$(domain_service_id "$user_domain" "$label")" >/dev/null 2>&1 || true
}

escape_sed_replacement() {
  printf '%s' "$1" | sed -e 's/[&|]/\\&/g'
}

render_plist() {
  local src="$1"
  local dst="$2"
  local root_escaped home_escaped
  root_escaped="$(escape_sed_replacement "$ROOT_DIR")"
  home_escaped="$(escape_sed_replacement "$HOME")"
  sed \
    -e "s|__ROOT_DIR__|$root_escaped|g" \
    -e "s|__HOME_DIR__|$home_escaped|g" \
    "$src" > "$dst"
}

install_one() {
  local label="$1"
  local src="$PLIST_SRC_DIR/$label.plist"
  local dst="$AGENT_DIR/$label.plist"
  local tmp_render

  if [[ ! -f "$src" ]]; then
    echo "$label: 缺少模板 $src"
    return 1
  fi

  tmp_render="$(mktemp "/tmp/${label}.XXXXXX.plist")"
  render_plist "$src" "$tmp_render"
  cp "$tmp_render" "$dst"
  rm -f "$tmp_render"

  local domain
  domain="$(launchd_domain)"
  bootout_label_all_domains "$label"
  if ! launchctl bootstrap "$domain" "$dst"; then
    echo "$label: bootstrap failed"
    return 1
  fi
  launchctl enable "$(service_id "$label")" >/dev/null 2>&1 || true
  launchctl kickstart -k "$(service_id "$label")" >/dev/null 2>&1 || true
  echo "$label: installed"
  return 0
}

uninstall_one() {
  local label="$1"
  local dst="$AGENT_DIR/$label.plist"
  bootout_label_all_domains "$label"
  rm -f "$dst"
}

status_one() {
  local label="$1"
  local gui_domain="gui/$UID_NUM"
  local user_domain="user/$UID_NUM"
  if launchctl print "$(domain_service_id "$gui_domain" "$label")" >/dev/null 2>&1; then
    echo "$label: loaded ($gui_domain)"
  elif launchctl print "$(domain_service_id "$user_domain" "$label")" >/dev/null 2>&1; then
    echo "$label: loaded ($user_domain)"
  else
    echo "$label: not loaded ($(launchd_domain))"
  fi
}

cmd="${1:-status}"
case "$cmd" in
  install)
    failed=0
    install_one "$BACKEND_LABEL" || failed=$((failed + 1))
    install_one "$TUNNEL_LABEL" || failed=$((failed + 1))
    install_one "$FRONTEND_LABEL" || failed=$((failed + 1))
    if [[ "$failed" -gt 0 ]]; then
      echo "launchd 安装完成，但有 $failed 个服务加载失败，请执行 ./scripts/launchd_manage.sh status 排查"
      exit 1
    fi
    echo "launchd 已安装并启用（backend + tunnel + frontend）"
    ;;
  uninstall)
    uninstall_one "$BACKEND_LABEL"
    uninstall_one "$TUNNEL_LABEL"
    uninstall_one "$FRONTEND_LABEL"
    echo "launchd 已卸载"
    ;;
  restart)
    "$0" uninstall
    "$0" install
    ;;
  status)
    status_one "$BACKEND_LABEL"
    status_one "$TUNNEL_LABEL"
    status_one "$FRONTEND_LABEL"
    ;;
  *)
    echo "用法: ./scripts/launchd_manage.sh [install|uninstall|restart|status]"
    echo "说明: install 会托管 backend + tunnel + frontend"
    exit 1
    ;;
esac

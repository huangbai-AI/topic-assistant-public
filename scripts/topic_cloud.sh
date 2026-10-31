#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
BACKEND_DIR="$ROOT_DIR/backend"
BACKEND_LOG="/tmp/topic_backend.log"
TUNNEL_LOG="/tmp/topic_tunnel.log"
BACKEND_PID_FILE="/tmp/topic_backend.pid"
TUNNEL_PID_FILE="/tmp/topic_tunnel.pid"
BACKEND_HOST="${BACKEND_HOST:-127.0.0.1}"
BACKEND_PORT="${BACKEND_PORT:-8000}"
PUBLIC_URL="${PUBLIC_URL:-https://your-topic-assistant.example.com}"
CLOUDFLARED_CONFIG="${CLOUDFLARED_CONFIG:-$HOME/.cloudflared/config.yml}"
TUNNEL_NAME="${TUNNEL_NAME:-topic-assistant}"
CLOUDFLARED_BIN="${CLOUDFLARED_BIN:-$(command -v cloudflared || true)}"
if [[ -z "$CLOUDFLARED_BIN" ]] && [[ -x "$HOME/.local/bin/cloudflared" ]]; then
  CLOUDFLARED_BIN="$HOME/.local/bin/cloudflared"
fi

export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"

PYTHON_BIN="$BACKEND_DIR/.venv/bin/python"
if [[ ! -x "$PYTHON_BIN" ]]; then
  PYTHON_BIN="$(command -v python3)"
fi
BACKEND_CMD=("$PYTHON_BIN" -m uvicorn main:app --host "$BACKEND_HOST" --port "$BACKEND_PORT")
TUNNEL_CMD=("$CLOUDFLARED_BIN" tunnel --config "$CLOUDFLARED_CONFIG" run "$TUNNEL_NAME")

print() {
  printf '[topic-cloud] %s\n' "$*"
}

is_pid_running() {
  local pid="${1:-}"
  [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null
}

read_pid() {
  local file="$1"
  if [[ -f "$file" ]]; then
    cat "$file"
  fi
}

pid_cmdline() {
  local pid="${1:-}"
  if [[ -z "$pid" ]]; then
    return
  fi
  ps -p "$pid" -o command= 2>/dev/null || true
}

is_backend_process() {
  local pid="${1:-}"
  local cmdline
  cmdline="$(pid_cmdline "$pid")"
  [[ "$cmdline" == *"$BACKEND_DIR"* ]] || {
    [[ "$cmdline" == *"uvicorn main:app"* ]] && [[ "$cmdline" == *"--port $BACKEND_PORT"* ]]
  }
}

find_backend_pid() {
  local pid
  pid="$(read_pid "$BACKEND_PID_FILE" || true)"
  if is_pid_running "$pid" && is_backend_process "$pid"; then
    echo "$pid"
    return
  fi

  for pid in $(pgrep -f "uvicorn main:app" 2>/dev/null || true); do
    if is_pid_running "$pid" && is_backend_process "$pid"; then
      echo "$pid"
      return
    fi
  done

  pid="$(lsof -tiTCP:${BACKEND_PORT} -sTCP:LISTEN 2>/dev/null | head -n 1 || true)"
  if is_pid_running "$pid" && is_backend_process "$pid"; then
    echo "$pid"
    return
  fi
  echo ""
}

find_tunnel_pid() {
  pgrep -f "cloudflared tunnel.*run ${TUNNEL_NAME}" | head -n 1 || true
}

wait_backend_ready() {
  local max_retry=20
  local i=0
  local code="000"
  while [[ $i -lt $max_retry ]]; do
    code="$(curl -s -o /tmp/topic_local_health.json -w '%{http_code}' "http://${BACKEND_HOST}:${BACKEND_PORT}/api/health" || true)"
    if [[ "$code" == "200" ]]; then
      return 0
    fi
    i=$((i + 1))
    sleep 1
  done
  return 1
}

wait_tunnel_ready() {
  local max_retry=25
  local i=0
  local code="000"
  while [[ $i -lt $max_retry ]]; do
    code="$(curl -s -o /tmp/topic_public_health.json -w '%{http_code}' "$PUBLIC_URL/api/health" || true)"
    if [[ "$code" == "200" ]]; then
      return 0
    fi
    i=$((i + 1))
    sleep 1
  done
  return 1
}

start_backend() {
  local pid
  pid="$(find_backend_pid)"
  if [[ -n "$pid" ]] && is_pid_running "$pid"; then
    echo "$pid" > "$BACKEND_PID_FILE"
    print "后端已在运行 (pid=$pid)"
    return
  fi

  local port_owner
  port_owner="$(lsof -tiTCP:${BACKEND_PORT} -sTCP:LISTEN 2>/dev/null | head -n 1 || true)"
  if [[ -n "$port_owner" ]] && is_pid_running "$port_owner" && ! is_backend_process "$port_owner"; then
    print "端口 ${BACKEND_PORT} 被其他进程占用 (pid=$port_owner)"
    print "请先释放端口后再重试：lsof -nP -iTCP:${BACKEND_PORT} -sTCP:LISTEN"
    exit 1
  fi

  print "启动后端..."
  (
    cd "$BACKEND_DIR"
    nohup "${BACKEND_CMD[@]}" > "$BACKEND_LOG" 2>&1 < /dev/null &
    echo $! > "$BACKEND_PID_FILE"
  )

  sleep 1
  pid="$(read_pid "$BACKEND_PID_FILE")"
  if ! is_pid_running "$pid"; then
    print "后端启动失败，请检查日志: $BACKEND_LOG"
    tail -n 80 "$BACKEND_LOG" || true
    exit 1
  fi
  if ! wait_backend_ready; then
    print "后端启动后健康检查失败（可能端口占用或进程异常退出）"
    tail -n 80 "$BACKEND_LOG" || true
    exit 1
  fi
  print "后端已启动并就绪 (pid=$pid)"
}

start_tunnel() {
  local pid
  pid="$(find_tunnel_pid)"
  if [[ -n "$pid" ]] && is_pid_running "$pid"; then
    echo "$pid" > "$TUNNEL_PID_FILE"
    print "隧道已在运行 (pid=$pid)"
    return
  fi

  if [[ ! -f "$CLOUDFLARED_CONFIG" ]]; then
    print "缺少 cloudflared 配置文件: $CLOUDFLARED_CONFIG"
    exit 1
  fi
  if [[ -z "$CLOUDFLARED_BIN" ]] || [[ ! -x "$CLOUDFLARED_BIN" ]]; then
    print "未找到 cloudflared 可执行文件，请设置 CLOUDFLARED_BIN 或安装到 $HOME/.local/bin"
    exit 1
  fi

  print "启动 Cloudflare Tunnel..."
  nohup "${TUNNEL_CMD[@]}" > "$TUNNEL_LOG" 2>&1 < /dev/null &
  echo $! > "$TUNNEL_PID_FILE"

  sleep 1
  pid="$(read_pid "$TUNNEL_PID_FILE")"
  if ! is_pid_running "$pid"; then
    print "隧道启动失败，请检查日志: $TUNNEL_LOG"
    tail -n 80 "$TUNNEL_LOG" || true
    exit 1
  fi
  if ! wait_tunnel_ready; then
    local code
    code="$(curl -s -o /dev/null -w '%{http_code}' "$PUBLIC_URL/api/health" || true)"
    if [[ "$code" == "530" ]]; then
      print "隧道未完成注册（530/1033），请检查 Cloudflare 隧道连接状态"
    elif [[ "$code" == "502" ]]; then
      print "隧道已连通但源站不可达（502），请检查后端监听端口"
    else
      print "公网健康检查失败 (code=$code)"
    fi
    tail -n 80 "$TUNNEL_LOG" || true
    exit 1
  fi
  print "隧道已启动并就绪 (pid=$pid)"
}

check_health() {
  print "检查本地健康接口..."
  local local_code
  local_code="$(curl -s -o /tmp/topic_local_health.json -w '%{http_code}' "http://${BACKEND_HOST}:${BACKEND_PORT}/api/health" || true)"
  if [[ "$local_code" != "200" ]]; then
    print "本地健康检查失败 (code=$local_code)"
    tail -n 60 "$BACKEND_LOG" || true
    exit 1
  fi
  print "本地健康检查通过"

  print "检查公网健康接口..."
  local public_code
  public_code="$(curl -s -o /tmp/topic_public_health.json -w '%{http_code}' "$PUBLIC_URL/api/health" || true)"
  if [[ "$public_code" != "200" ]]; then
    print "公网健康检查失败 (code=$public_code)"
    tail -n 60 "$TUNNEL_LOG" || true
    exit 1
  fi
  print "公网健康检查通过"
}

show_status() {
  local backend_pid tunnel_pid local_code public_code
  backend_pid="$(find_backend_pid)"
  tunnel_pid="$(find_tunnel_pid)"
  local_code="$(curl -s -o /dev/null -w '%{http_code}' "http://${BACKEND_HOST}:${BACKEND_PORT}/api/health" || true)"
  public_code="$(curl -s -o /dev/null -w '%{http_code}' "$PUBLIC_URL/api/health" || true)"
  local backend_listen="down"
  if lsof -tiTCP:${BACKEND_PORT} -sTCP:LISTEN >/dev/null 2>&1; then
    backend_listen="up"
  fi

  print "状态："
  print "- backend pid: ${backend_pid:-not running}"
  print "- tunnel  pid: ${tunnel_pid:-not running}"
  print "- backend listen:${backend_listen} (${BACKEND_PORT})"
  print "- local   health: $local_code"
  print "- public  health: $public_code"
  print "- cloudflared config: $CLOUDFLARED_CONFIG"
  print "- dashboard: $PUBLIC_URL/dashboard"
}

stop_all() {
  local pid

  pid="$(find_tunnel_pid)"
  if [[ -n "$pid" ]] && is_pid_running "$pid"; then
    print "停止隧道 (pid=$pid)"
    kill "$pid" || true
  else
    print "隧道未运行"
  fi

  while true; do
    pid="$(find_backend_pid)"
    if [[ -n "$pid" ]] && is_pid_running "$pid"; then
      print "停止后端 (pid=$pid)"
      kill "$pid" || true
      sleep 1
      continue
    fi
    break
  done
  print "后端未运行"

  rm -f "$BACKEND_PID_FILE" "$TUNNEL_PID_FILE"
  print "已停止"
}

start_all() {
  start_backend
  start_tunnel
  check_health
  print "启动完成：$PUBLIC_URL/dashboard"
}

usage() {
  cat <<USAGE
用法:
  ./scripts/topic_cloud.sh start    # 一键启动后端+隧道并做健康检查
  ./scripts/topic_cloud.sh stop     # 停止后端+隧道
  ./scripts/topic_cloud.sh restart  # 重启
  ./scripts/topic_cloud.sh status   # 查看运行状态

可选环境变量:
  CLOUDFLARED_CONFIG=...            # cloudflared 配置，默认 $HOME/.cloudflared/config.yml
  TUNNEL_NAME=topic-assistant       # 隧道名称
  BACKEND_HOST=127.0.0.1
  BACKEND_PORT=8000
  PUBLIC_URL=https://your-topic-assistant.example.com
USAGE
}

cmd="${1:-start}"
case "$cmd" in
  start) start_all ;;
  stop) stop_all ;;
  restart) stop_all; sleep 1; start_all ;;
  status) show_status ;;
  *) usage; exit 1 ;;
esac

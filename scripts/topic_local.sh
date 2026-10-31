#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
BACKEND_DIR="$ROOT_DIR/backend"
FRONTEND_DIR="$ROOT_DIR/frontend"

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"

BACKEND_HOST="${BACKEND_HOST:-127.0.0.1}"
BACKEND_PORT="${BACKEND_PORT:-8000}"
FRONTEND_HOST="${FRONTEND_HOST:-127.0.0.1}"
FRONTEND_PORT="${FRONTEND_PORT:-5173}"

BACKEND_URL="http://${BACKEND_HOST}:${BACKEND_PORT}"
FRONTEND_URL="http://${FRONTEND_HOST}:${FRONTEND_PORT}"

BACKEND_LOG="/tmp/topic_local_backend.log"
FRONTEND_LOG="/tmp/topic_local_frontend.log"
BACKEND_PID_FILE="/tmp/topic_local_backend.pid"
FRONTEND_PID_FILE="/tmp/topic_local_frontend.pid"

PYTHON_BIN="$BACKEND_DIR/.venv/bin/python"
if [[ ! -x "$PYTHON_BIN" ]]; then
  PYTHON_BIN="$(command -v python3)"
fi

BACKEND_CMD=("$PYTHON_BIN" -m uvicorn main:app --host "$BACKEND_HOST" --port "$BACKEND_PORT")

launchd_backend_loaded() {
  local uid
  uid="$(id -u)"
  launchctl print "gui/${uid}/com.topicassistant.backend" >/dev/null 2>&1 || \
    launchctl print "user/${uid}/com.topicassistant.backend" >/dev/null 2>&1
}

print() {
  printf '[topic-local] %s\n' "$*"
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

pid_cwd() {
  local pid="${1:-}"
  if [[ -z "$pid" ]]; then
    return
  fi
  lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -n 1 || true
}

port_pid() {
  local port="$1"
  lsof -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null | head -n 1 || true
}

is_backend_process() {
  local pid="${1:-}"
  local cmdline
  cmdline="$(pid_cmdline "$pid")"
  [[ "$cmdline" == *"$BACKEND_DIR"* ]] || [[ "$cmdline" == *"uvicorn main:app"* ]]
}

is_frontend_process() {
  local pid="${1:-}"
  local cmdline cwd
  cmdline="$(pid_cmdline "$pid")"
  cwd="$(pid_cwd "$pid")"
  [[ "$cmdline" == *"vite"* ]] || [[ "$cmdline" == *"npm run dev"* ]] || [[ "$cmdline" == *"topic_frontend_daemon.sh"* ]] || return 1
  if [[ "$cwd" == "$FRONTEND_DIR" ]]; then
    return 0
  fi
  [[ "$cmdline" == *"$FRONTEND_DIR"* ]] || [[ "$cmdline" == *"--port $FRONTEND_PORT"* ]]
}

http_ok() {
  local code="$1"
  [[ "$code" == 2* || "$code" == 3* ]]
}

wait_backend_ready() {
  local i=0
  local code="000"
  while [[ $i -lt 30 ]]; do
    code="$(curl -s -o /dev/null -w '%{http_code}' "${BACKEND_URL}/api/health" || true)"
    if [[ "$code" == "200" ]]; then
      return 0
    fi
    i=$((i + 1))
    sleep 1
  done
  return 1
}

wait_frontend_ready() {
  local i=0
  local code="000"
  while [[ $i -lt 30 ]]; do
    code="$(curl -s -o /dev/null -w '%{http_code}' "${FRONTEND_URL}" || true)"
    if http_ok "$code"; then
      return 0
    fi
    i=$((i + 1))
    sleep 1
  done
  return 1
}

ensure_frontend_deps() {
  if [[ ! -d "$FRONTEND_DIR/node_modules" ]]; then
    print "前端依赖缺失，正在安装 npm 依赖..."
    (cd "$FRONTEND_DIR" && npm install)
  fi
}

frontend_vite_bin() {
  local bin="$FRONTEND_DIR/node_modules/.bin/vite"
  if [[ -x "$bin" ]]; then
    echo "$bin"
    return
  fi
  echo ""
}

start_backend() {
  local pid
  pid="$(read_pid "$BACKEND_PID_FILE" || true)"
  if is_pid_running "$pid" && is_backend_process "$pid"; then
    print "后端已运行 (pid=$pid)"
    return
  fi

  local port_owner
  port_owner="$(port_pid "$BACKEND_PORT")"
  if [[ -n "$port_owner" ]] && is_pid_running "$port_owner" && is_backend_process "$port_owner"; then
    echo "$port_owner" > "$BACKEND_PID_FILE"
    print "后端已运行 (pid=$port_owner)"
    return
  fi
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
  pid="$(read_pid "$BACKEND_PID_FILE" || true)"
  if ! is_pid_running "$pid"; then
    print "后端启动失败，请检查日志：$BACKEND_LOG"
    tail -n 80 "$BACKEND_LOG" || true
    exit 1
  fi
  if ! wait_backend_ready; then
    print "后端健康检查失败，请检查日志：$BACKEND_LOG"
    tail -n 80 "$BACKEND_LOG" || true
    exit 1
  fi
  pid="$(port_pid "$BACKEND_PORT")"
  if [[ -n "$pid" ]]; then
    echo "$pid" > "$BACKEND_PID_FILE"
  fi
  print "后端已就绪 (pid=$pid)"
}

start_frontend() {
  local pid
  pid="$(read_pid "$FRONTEND_PID_FILE" || true)"
  if is_pid_running "$pid" && is_frontend_process "$pid"; then
    print "前端已运行 (pid=$pid)"
    return
  fi

  local port_owner
  port_owner="$(port_pid "$FRONTEND_PORT")"
  if [[ -n "$port_owner" ]] && is_pid_running "$port_owner" && is_frontend_process "$port_owner"; then
    echo "$port_owner" > "$FRONTEND_PID_FILE"
    print "前端已运行 (pid=$port_owner)"
    return
  fi
  if [[ -n "$port_owner" ]] && is_pid_running "$port_owner" && ! is_frontend_process "$port_owner"; then
    print "端口 ${FRONTEND_PORT} 被其他进程占用 (pid=$port_owner)"
    print "请先释放端口后再重试：lsof -nP -iTCP:${FRONTEND_PORT} -sTCP:LISTEN"
    exit 1
  fi

  ensure_frontend_deps
  local vite_bin
  vite_bin="$(frontend_vite_bin)"
  if [[ -z "$vite_bin" ]]; then
    print "未找到 vite 可执行文件，请先执行：cd frontend && npm install"
    exit 1
  fi

  print "启动前端..."
  (
    cd "$FRONTEND_DIR"
    nohup env CI=true "$vite_bin" --host "$FRONTEND_HOST" --port "$FRONTEND_PORT" --strictPort > "$FRONTEND_LOG" 2>&1 < /dev/null &
    echo $! > "$FRONTEND_PID_FILE"
  )
  sleep 1
  pid="$(read_pid "$FRONTEND_PID_FILE" || true)"
  if ! is_pid_running "$pid"; then
    print "前端启动失败，请检查日志：$FRONTEND_LOG"
    tail -n 80 "$FRONTEND_LOG" || true
    exit 1
  fi
  if ! wait_frontend_ready; then
    print "前端健康检查失败，请检查日志：$FRONTEND_LOG"
    tail -n 80 "$FRONTEND_LOG" || true
    exit 1
  fi
  pid="$(port_pid "$FRONTEND_PORT")"
  if [[ -n "$pid" ]]; then
    echo "$pid" > "$FRONTEND_PID_FILE"
  fi
  print "前端已就绪 (pid=$pid)"
}

stop_backend() {
  if launchd_backend_loaded; then
    print "检测到 launchd 正在托管后端，手动停止后可能会被自动拉起。"
    print "如需彻底停用：./scripts/launchd_manage.sh uninstall"
  fi
  local pid
  pid="$(read_pid "$BACKEND_PID_FILE" || true)"
  if [[ -z "$pid" ]]; then
    pid="$(port_pid "$BACKEND_PORT")"
  fi
  if is_pid_running "$pid" && is_backend_process "$pid"; then
    print "停止后端 (pid=$pid)"
    kill "$pid" || true
    sleep 1
  else
    print "后端未运行"
  fi
  rm -f "$BACKEND_PID_FILE"
}

stop_frontend() {
  local pid
  pid="$(read_pid "$FRONTEND_PID_FILE" || true)"
  if [[ -z "$pid" ]]; then
    pid="$(port_pid "$FRONTEND_PORT")"
  fi
  if is_pid_running "$pid" && is_frontend_process "$pid"; then
    print "停止前端 (pid=$pid)"
    kill "$pid" || true
    sleep 1
  else
    print "前端未运行"
  fi
  rm -f "$FRONTEND_PID_FILE"
}

check_health() {
  local backend_code frontend_code
  backend_code="$(curl -s -o /dev/null -w '%{http_code}' "${BACKEND_URL}/api/health" || true)"
  frontend_code="$(curl -s -o /dev/null -w '%{http_code}' "${FRONTEND_URL}" || true)"
  print "健康检查：backend=${backend_code} frontend=${frontend_code}"
  if [[ "$backend_code" != "200" ]]; then
    print "后端健康检查未通过，请查看：$BACKEND_LOG"
    exit 1
  fi
  if ! http_ok "$frontend_code"; then
    print "前端健康检查未通过，请查看：$FRONTEND_LOG"
    exit 1
  fi
}

open_page() {
  if command -v open >/dev/null 2>&1; then
    open "${FRONTEND_URL}" >/dev/null 2>&1 || true
  fi
}

show_status() {
  local backend_pid frontend_pid backend_code frontend_code
  backend_pid="$(port_pid "$BACKEND_PORT")"
  frontend_pid="$(port_pid "$FRONTEND_PORT")"
  backend_code="$(curl -s -o /dev/null -w '%{http_code}' "${BACKEND_URL}/api/health" || true)"
  frontend_code="$(curl -s -o /dev/null -w '%{http_code}' "${FRONTEND_URL}" || true)"
  print "状态："
  print "- backend pid: ${backend_pid:-not running} (port ${BACKEND_PORT})"
  print "- frontend pid: ${frontend_pid:-not running} (port ${FRONTEND_PORT})"
  print "- backend health: ${backend_code}"
  print "- frontend health: ${frontend_code}"
  print "- 前端地址: ${FRONTEND_URL}"
  print "- 后端地址: ${BACKEND_URL}/dashboard"
  if launchd_backend_loaded; then
    print "- launchd: backend 托管中（可能自动拉起）"
  else
    print "- launchd: backend 未托管"
  fi
}

show_logs() {
  print "==== backend log (tail 80) ===="
  tail -n 80 "$BACKEND_LOG" 2>/dev/null || true
  print "==== frontend log (tail 80) ===="
  tail -n 80 "$FRONTEND_LOG" 2>/dev/null || true
}

start_all() {
  start_backend
  start_frontend
  check_health
  if [[ "${TA_OPEN_BROWSER:-1}" == "1" ]]; then
    open_page
  fi
  print "启动完成：${FRONTEND_URL}"
}

stop_all() {
  stop_frontend
  stop_backend
  print "已停止"
}

usage() {
  cat <<USAGE
用法:
  ./scripts/topic_local.sh start    # 一键启动前端+后端
  ./scripts/topic_local.sh stop     # 停止前端+后端
  ./scripts/topic_local.sh restart  # 重启前端+后端
  ./scripts/topic_local.sh status   # 查看状态
  ./scripts/topic_local.sh logs     # 查看最近日志

可选环境变量:
  TA_OPEN_BROWSER=0                 # 启动后不自动打开浏览器
USAGE
}

cmd="${1:-start}"
case "$cmd" in
  start)
    start_all
    ;;
  stop)
    stop_all
    ;;
  restart)
    stop_all
    sleep 1
    start_all
    ;;
  status)
    show_status
    ;;
  logs)
    show_logs
    ;;
  *)
    usage
    exit 1
    ;;
esac

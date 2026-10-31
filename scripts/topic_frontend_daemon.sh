#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
FRONTEND_DIR="$ROOT_DIR/frontend"
FRONTEND_HOST="${FRONTEND_HOST:-127.0.0.1}"
FRONTEND_PORT="${FRONTEND_PORT:-5173}"

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"
export CI="true"

cd "$FRONTEND_DIR"

if [[ ! -d "node_modules" ]] || [[ ! -x "node_modules/.bin/vite" ]]; then
  echo "[topic-frontend-daemon] node_modules 缺失，正在安装依赖..."
  npm install --no-audit --no-fund
fi

exec ./node_modules/.bin/vite --host "$FRONTEND_HOST" --port "$FRONTEND_PORT" --strictPort

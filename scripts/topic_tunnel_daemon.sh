#!/usr/bin/env bash
set -euo pipefail

export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"

CONFIG_FILE="${CLOUDFLARED_CONFIG:-$HOME/.cloudflared/config.yml}"
TUNNEL_NAME="${TUNNEL_NAME:-topic-assistant}"
CLOUDFLARED_BIN="${CLOUDFLARED_BIN:-$(command -v cloudflared || true)}"
if [[ -z "$CLOUDFLARED_BIN" ]] && [[ -x "$HOME/.local/bin/cloudflared" ]]; then
  CLOUDFLARED_BIN="$HOME/.local/bin/cloudflared"
fi

if [[ ! -f "$CONFIG_FILE" ]]; then
  echo "[topic-tunnel-daemon] 缺少配置文件: $CONFIG_FILE"
  exit 1
fi
if [[ -z "$CLOUDFLARED_BIN" ]] || [[ ! -x "$CLOUDFLARED_BIN" ]]; then
  echo "[topic-tunnel-daemon] 未找到 cloudflared 可执行文件，请设置 CLOUDFLARED_BIN"
  exit 1
fi

exec "$CLOUDFLARED_BIN" tunnel --config "$CONFIG_FILE" run "$TUNNEL_NAME"

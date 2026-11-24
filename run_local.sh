#!/usr/bin/env bash
# 不使用 Docker 时在本机直接试跑（真实 UDP over loopback）。
# 前置：python3 -m pip install -r requirements.txt
# 页面地址：http://127.0.0.1:18080
set -euo pipefail
cd "$(dirname "$0")"
PY="${PYTHON:-python3}"

export TFTP_HOST=127.0.0.1
export TFTP_PORT="${TFTP_PORT:-1069}"
export STATS_HOST=127.0.0.1
export STATS_PORT="${STATS_PORT:-18001}"
export TFTP_TIMEOUT="${TFTP_TIMEOUT:-0.8}"
export TFTP_MAX_RETRIES="${TFTP_MAX_RETRIES:-4}"

$PY server.py &
SRV_PID=$!
cleanup() { kill "$SRV_PID" 2>/dev/null || true; }
trap cleanup EXIT INT TERM

sleep 0.6
TFTP_HOST=127.0.0.1 \
TFTP_PORT="$TFTP_PORT" \
STATS_URL="http://127.0.0.1:${STATS_PORT}/stats" \
WEB_HOST=127.0.0.1 \
WEB_PORT="${WEB_PORT:-18080}" \
  exec $PY web.py

#!/usr/bin/env bash
set -euo pipefail

ROOT="/root/notioncode_mcp/bridge"
PORT="${NOTION_FABLE_PORT:-8765}"

if ss -ltn 2>/dev/null | grep -q "127.0.0.1:${PORT}"; then
  echo "Notion Fable bridge already running on 127.0.0.1:${PORT}"
  exit 0
fi

cd "${ROOT}"

setsid -f /root/notioncode_mcp/.runtime/notion-agent-cli-venv/bin/python -m uvicorn \
  server:app --host 127.0.0.1 --port "${PORT}" \
  > /tmp/notion-fable-proxy.log 2>&1

sleep 2
curl -fsS "http://127.0.0.1:${PORT}/healthz"
echo

#!/usr/bin/env bash
# 起一个临时 hash 模式服务，跑契约自检 + 复现性检查，然后收拾干净。
set -euo pipefail
cd "$(dirname "$0")/.."

PY=${PY:-python3}
PORT=${PORT:-$("$PY" - <<'EOF'
import socket
s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()
EOF
)}
TMP=$(mktemp -d)
KEY="verify-$(date +%s)-key"
cleanup() {
  [[ -n "${PID:-}" ]] && kill "$PID" 2>/dev/null || true
  wait "${PID:-}" 2>/dev/null || true
  rm -rf "$TMP"
}
trap cleanup EXIT

# 默认离线 hash（免费、可复现）；VERIFY_EMBED_MODE=api 可让同一套契约跑真实 embedding
# DOTENV_DISABLE/RERANK_MODE：契约与复现性必须与开发者本地 .env 解耦（本地可能已开精排）
EMBED_MODE="${VERIFY_EMBED_MODE:-hash}" DB_PATH="$TMP/verify.db" MEMORY_SYSTEM_KEY="$KEY" \
  PORT="$PORT" HOST=127.0.0.1 STARTUP_CHECK=0 DOTENV_DISABLE=1 RERANK_MODE=none \
  "$PY" service/server.py >"$TMP/server.log" 2>&1 &
PID=$!

for _ in $(seq 1 100); do
  if curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then break; fi
  if ! kill -0 "$PID" 2>/dev/null; then
    echo "服务启动失败："; cat "$TMP/server.log"; exit 1
  fi
  sleep 0.1
done

echo "== contract_check =="
"$PY" tools/contract_check.py "http://127.0.0.1:$PORT" --key "$KEY"
echo
echo "== replay =="
"$PY" tools/replay.py "http://127.0.0.1:$PORT" 5 --key "$KEY"
echo
echo "== smoke (local) =="
"$PY" tools/smoke.py "http://127.0.0.1:$PORT" --key "$KEY" --skip-image
echo
# v2（/v2/add、/v2/search：轮次单元 + 日期/说话人抬头）走同一套契约与复现性检查
echo "== contract_check (v2) =="
"$PY" tools/contract_check.py "http://127.0.0.1:$PORT/v2" --key "$KEY"
echo
echo "== replay (v2) =="
"$PY" tools/replay.py "http://127.0.0.1:$PORT/v2" 5 --key "$KEY"
echo
echo "== smoke (v2) =="
"$PY" tools/smoke.py "http://127.0.0.1:$PORT/v2" --key "$KEY" --skip-image
echo
echo "全部通过 ✅"

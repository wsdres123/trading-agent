#!/usr/bin/env bash
# 后台常驻启动劫财AI交易。启动一次后，直接刷新 http://localhost:8601 即可，无需重复运行。
# 重复运行不会起多个实例；改了代码后浏览器刷新即生效。
set -e
cd "$(dirname "$0")"
export LD_LIBRARY_PATH="/home/lixiang/anaconda3/lib:${LD_LIBRARY_PATH:-}"

PORT=8601
FAPI_PORT=8602
PIDFILE=".data/app.pid"
LOG=".data/app.log"
FAPI_PIDFILE=".data/fastapi.pid"
FAPI_LOG=".data/fastapi.log"

# 已在运行则直接提示
if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  echo "已在运行中，PID=$(cat "$PIDFILE")  →  http://localhost:$PORT"
  echo "停止: bash stop.sh   日志: tail -f $LOG"
  exit 0
fi

# ── Redis ──────────────────────────────────────────────────────────────────
if ! redis-cli ping >/dev/null 2>&1; then
    if command -v redis-server >/dev/null 2>&1; then
        redis-server --daemonize yes --port 6379 --save ""
        sleep 1
        echo "Redis 已启动 (port 6379)"
    else
        echo "⚠ redis-server 未安装，仅用内存缓存。"
    fi
fi

# ── FastAPI (background daemon) ───────────────────────────────────────────
setsid /home/lixiang/anaconda3/bin/python3 -m uvicorn server:app \
    --host 0.0.0.0 --port "$FAPI_PORT" \
    < /dev/null > "$FAPI_LOG" 2>&1 &
echo $! > "$FAPI_PIDFILE"
sleep 2
echo "FastAPI    PID=$(cat "$FAPI_PIDFILE")  →  http://localhost:$FAPI_PORT"

# ── Streamlit (background daemon) ──────────────────────────────────────────
setsid /home/lixiang/anaconda3/bin/python3 -m streamlit.web.cli run ui.py \
  --server.port "$PORT" --server.headless true \
  --browser.gatherUsageStats false \
  < /dev/null > "$LOG" 2>&1 &
echo $! > "$PIDFILE"
sleep 2

echo "已后台启动："
echo "  Streamlit  PID=$(cat "$PIDFILE")  →  http://localhost:$PORT"
echo "  FastAPI    PID=$(cat "$FAPI_PIDFILE")  →  http://localhost:$FAPI_PORT"
echo "停止: bash stop.sh   日志: tail -f $LOG"

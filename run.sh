#!/usr/bin/env bash
# 劫财AI交易 启动脚本
# 解决 anaconda pandas/akshare 在系统 libstdc++ 下 GLIBCXX_3.4.29 缺失问题
set -e
cd "$(dirname "$0")"

export LD_LIBRARY_PATH="/home/lixiang/anaconda3/lib:${LD_LIBRARY_PATH:-}"
# 外部 shell 可能残留旧模型名，清掉让 .env 里的 QWEN_CHAT_MODEL 生效
unset QWEN_CHAT_MODEL

# ── Start Redis (if not running) ───────────────────────────────────────────
if ! redis-cli ping >/dev/null 2>&1; then
    if command -v redis-server >/dev/null 2>&1; then
        redis-server --daemonize yes --port 6379 --save ""
        sleep 1
        echo "Redis 已启动 (port 6379)"
    else
        echo "⚠ redis-server 未安装，仅用内存缓存。"
    fi
fi

# ── Start FastAPI (background) ─────────────────────────────────────────────
FAPI_PIDFILE=".data/fastapi.pid"
if [ -f "$FAPI_PIDFILE" ] && kill -0 "$(cat "$FAPI_PIDFILE")" 2>/dev/null; then
    echo "FastAPI 已在运行 PID=$(cat "$FAPI_PIDFILE")"
else
    /home/lixiang/anaconda3/bin/python3 -m uvicorn server:app \
        --host 0.0.0.0 --port 8602 \
        < /dev/null > .data/fastapi.log 2>&1 &
    echo $! > "$FAPI_PIDFILE"
    sleep 2
    echo "FastAPI 已启动 → http://localhost:8602  (PID=$(cat "$FAPI_PIDFILE"))"
fi

# ── Start Streamlit (foreground) ───────────────────────────────────────────
echo "启动 劫财AI交易 …  本机: http://localhost:8601"
for ip in $(hostname -I); do echo "  局域网访问: http://${ip}:8601"; done
exec /home/lixiang/anaconda3/bin/python3 -m streamlit.web.cli run ui.py \
    --server.port 8601 --server.headless true --server.address 0.0.0.0

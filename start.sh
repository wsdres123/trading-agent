#!/usr/bin/env bash
# 后台常驻启动劫财AI交易。启动一次后，直接刷新 http://localhost:8601 即可，无需重复运行。
# 重复运行不会起多个实例；改了代码后浏览器刷新即生效。
# 所有服务仅绑定 127.0.0.1；Redis 自动带 requirepass（密码读 .env REDIS_PASSWORD）。
set -e
cd "$(dirname "$0")"
export LD_LIBRARY_PATH="/home/lixiang/anaconda3/lib:${LD_LIBRARY_PATH:-}"

PORT=8601
FAPI_PORT=8602
PIDFILE=".data/app.pid"
LOG=".data/app.log"
FAPI_PIDFILE=".data/fastapi.pid"
FAPI_LOG=".data/fastapi.log"

# ── Redis 安全启动：仅绑 127.0.0.1 + requirepass（密码从 .env REDIS_PASSWORD 读取）──
_start_redis() {
    command -v redis-server >/dev/null 2>&1 || { echo "⚠ redis-server 未安装，仅用内存缓存。"; return; }
    local PW
    PW=$(/home/lixiang/anaconda3/bin/python3 -m config.settings --redis-password 2>/dev/null)
    if [ -z "$PW" ]; then
        echo "⚠ .env 缺少 REDIS_PASSWORD，Redis 以无密码启动（仅本机可达）。"
        redis-cli ping >/dev/null 2>&1 && return
        redis-server --daemonize yes --port 6379 --bind 127.0.0.1 --save ""
        sleep 1; echo "Redis 已启动 (port 6379, 无密码)"
        return
    fi
    if [ "$1" = "--redis-upgrade" ]; then
        redis-cli shutdown nosave 2>/dev/null || true
        sleep 0.5
        redis-server --daemonize yes --port 6379 --bind 127.0.0.1 --save "" --requirepass "$PW"
        sleep 1; echo "Redis 已重启 (port 6379, requirepass)"
        return
    fi
    if redis-cli ping >/dev/null 2>&1; then
        return  # 已在运行且无需密码 → 跳过（升级: bash start.sh --redis-upgrade）
    fi
    if redis-cli -a "$PW" ping >/dev/null 2>&1; then
        return  # 已在运行且带密码
    fi
    redis-cli shutdown nosave 2>/dev/null || true
    sleep 0.5
    redis-server --daemonize yes --port 6379 --bind 127.0.0.1 --save "" --requirepass "$PW"
    sleep 1; echo "Redis 已启动 (port 6379, requirepass)"
}

if [ "$1" = "--redis-upgrade" ]; then
    _start_redis --redis-upgrade
    exit 0
fi

# 已在运行则直接提示
if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  echo "已在运行中，PID=$(cat "$PIDFILE")  →  http://localhost:$PORT"
  echo "停止: bash stop.sh   日志: tail -f $LOG"
  exit 0
fi

_start_redis

# ── 远程访问：Tailscale 在运行时绑到 tailnet IP（100.x.y.z），否则仅本机 ──
BIND_IP="127.0.0.1"
if command -v tailscale >/dev/null 2>&1; then
    TS_IP=$(tailscale ip -4 2>/dev/null)
    if [ -n "$TS_IP" ]; then
        BIND_IP="$TS_IP"
        echo "检测到 Tailscale，服务绑定 $BIND_IP（tailnet 内其他设备可访问，本机也用此地址）"
    fi
fi
export CORS_ORIGINS="http://localhost:8601,http://$BIND_IP:8601"

# ── FastAPI (background daemon) ───────────────────────────────────────────
setsid /home/lixiang/anaconda3/bin/python3 -m uvicorn server:app \
    --host "$BIND_IP" --port "$FAPI_PORT" \
    < /dev/null > "$FAPI_LOG" 2>&1 &
echo $! > "$FAPI_PIDFILE"
sleep 2
echo "FastAPI    PID=$(cat "$FAPI_PIDFILE")  →  http://$BIND_IP:$FAPI_PORT"

# ── Streamlit (background daemon) ──────────────────────────────────────────
setsid /home/lixiang/anaconda3/bin/python3 -m streamlit.web.cli run ui.py \
  --server.port "$PORT" --server.headless true \
  --server.address "$BIND_IP" \
  --browser.gatherUsageStats false \
  < /dev/null > "$LOG" 2>&1 &
echo $! > "$PIDFILE"
sleep 2

echo "已后台启动："
echo "  Streamlit  PID=$(cat "$PIDFILE")  →  http://$BIND_IP:$PORT"
echo "  FastAPI    PID=$(cat "$FAPI_PIDFILE")  →  http://$BIND_IP:$FAPI_PORT"
echo "停止: bash stop.sh   日志: tail -f $LOG"

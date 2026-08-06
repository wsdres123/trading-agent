#!/usr/bin/env bash
# 劫财AI交易 启动脚本（前台）
# 所有服务仅绑定 127.0.0.1；Redis 自动带 requirepass（密码读 .env REDIS_PASSWORD）。
# 用法: bash run.sh                # 启动全部服务
#       bash run.sh --redis-upgrade # 仅把运行中的 Redis 升级为带密码实例（缓存清空）
set -e
cd "$(dirname "$0")"

export LD_LIBRARY_PATH="/home/lixiang/anaconda3/lib:${LD_LIBRARY_PATH:-}"
# 外部 shell 可能残留旧模型名，清掉让 .env 里的 QWEN_CHAT_MODEL 生效
unset QWEN_CHAT_MODEL

FAPI_PIDFILE=".data/fastapi.pid"

# 已在运行则直接提示（与 start.sh 一致），避免重复启动/端口冲突
if [ "$1" != "--redis-upgrade" ] && [ -f "$FAPI_PIDFILE" ] && kill -0 "$(cat "$FAPI_PIDFILE")" 2>/dev/null; then
    echo "已在运行中 PID=$(cat "$FAPI_PIDFILE") → http://localhost:8601"
    echo "重启: bash stop.sh && bash run.sh   仅升级Redis密码: bash run.sh --redis-upgrade"
    exit 0
fi

# ── Redis 安全启动：仅绑 127.0.0.1 + requirepass（密码从 .env REDIS_PASSWORD 读取）──
_start_redis() {
    command -v redis-server >/dev/null 2>&1 || { echo "⚠ redis-server 未安装，仅用内存缓存。"; return; }
    local PW
    PW=$(/home/lixiang/anaconda3/bin/python3 -m config.settings --redis-password 2>/dev/null)
    if [ -z "$PW" ]; then
        echo "⚠ .env 缺少 REDIS_PASSWORD，Redis 以无密码启动（仅本机可达）。建议: bash start.sh"
        redis-cli ping >/dev/null 2>&1 && return
        redis-server --daemonize yes --port 6379 --bind 127.0.0.1 --save ""
        sleep 1; echo "Redis 已启动 (port 6379, 无密码)"
        return
    fi
    if [ "$1" = "--redis-upgrade" ]; then
        # 强制重启为带密码实例（缓存清空）
        redis-cli shutdown nosave 2>/dev/null || true
        sleep 0.5
        redis-server --daemonize yes --port 6379 --bind 127.0.0.1 --save "" --requirepass "$PW"
        sleep 1; echo "Redis 已重启 (port 6379, requirepass)"
        return
    fi
    if redis-cli ping >/dev/null 2>&1; then
        return  # 已在运行且无需密码 → 跳过（升级请用 --redis-upgrade）
    fi
    if redis-cli -a "$PW" ping >/dev/null 2>&1; then
        return  # 已在运行且带密码
    fi
    # 实例在运行但需要密码（旧无密码实例）→ 重启为带密码实例
    redis-cli shutdown nosave 2>/dev/null || true
    sleep 0.5
    redis-server --daemonize yes --port 6379 --bind 127.0.0.1 --save "" --requirepass "$PW"
    sleep 1; echo "Redis 已启动 (port 6379, requirepass)"
}
_start_redis "$1"

[ "$1" = "--redis-upgrade" ] && exit 0

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

# ── Start FastAPI (background) ─────────────────────────────────────────────
if [ -f "$FAPI_PIDFILE" ] && kill -0 "$(cat "$FAPI_PIDFILE")" 2>/dev/null; then
    echo "FastAPI 已在运行 PID=$(cat "$FAPI_PIDFILE")"
else
    /home/lixiang/anaconda3/bin/python3 -m uvicorn server:app \
        --host "$BIND_IP" --port 8602 \
        < /dev/null > .data/fastapi.log 2>&1 &
    echo $! > "$FAPI_PIDFILE"
    sleep 2
    echo "FastAPI 已启动 → http://localhost:8602  (PID=$(cat "$FAPI_PIDFILE"))"
fi

# ── Start Streamlit (foreground) ───────────────────────────────────────────
echo "启动 劫财AI交易 …  本机: http://localhost:8601"
exec /home/lixiang/anaconda3/bin/python3 -m streamlit.web.cli run ui.py \
    --server.port 8601 --server.headless true --server.address "$BIND_IP"

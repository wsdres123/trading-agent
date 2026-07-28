#!/usr/bin/env bash
# 后台常驻启动劫财AI交易。启动一次后，直接刷新 http://localhost:8601 即可，无需重复运行。
# 重复运行不会起多个实例；改了代码后浏览器刷新即生效。
set -e
cd "$(dirname "$0")"
export LD_LIBRARY_PATH="/home/lixiang/anaconda3/lib:${LD_LIBRARY_PATH:-}"

PORT=8601
PIDFILE=".data/app.pid"
LOG=".data/app.log"

# 已在运行则直接提示
if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  echo "已在运行中，PID=$(cat "$PIDFILE")  →  http://localhost:$PORT"
  echo "停止: bash stop.sh   日志: tail -f $LOG"
  exit 0
fi

# setsid 新开会话，彻底脱离当前终端，关掉终端也不会停
setsid python3 -m streamlit.web.cli run ui.py \
  --server.port "$PORT" --server.headless true \
  --browser.gatherUsageStats false \
  < /dev/null > "$LOG" 2>&1 &
echo $! > "$PIDFILE"
sleep 2
echo "已后台启动，PID=$(cat "$PIDFILE")  →  http://localhost:$PORT"
echo "停止: bash stop.sh   日志: tail -f $LOG"

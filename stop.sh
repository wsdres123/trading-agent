#!/usr/bin/env bash
# 停止后台常驻的劫财AI交易服务。
cd "$(dirname "$0")"
PIDFILE=".data/app.pid"
if [ -f "$PIDFILE" ]; then
  PID=$(cat "$PIDFILE")
  if kill -0 "$PID" 2>/dev/null; then
    kill "$PID"
    sleep 1
    kill -0 "$PID" 2>/dev/null && kill -9 "$PID" 2>/dev/null
    echo "已停止 PID=$PID"
  else
    echo "进程已不在 (PID=$PID)"
  fi
  rm -f "$PIDFILE"
else
  echo "未找到 PID 文件，服务可能未启动。"
fi

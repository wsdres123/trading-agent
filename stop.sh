#!/usr/bin/env bash
# 停止后台常驻的劫财AI交易服务（Streamlit + FastAPI）。
cd "$(dirname "$0")"

# ── Stop Streamlit ─────────────────────────────────────────────────────────
PIDFILE=".data/app.pid"
if [ -f "$PIDFILE" ]; then
  PID=$(cat "$PIDFILE")
  if kill -0 "$PID" 2>/dev/null; then
    kill "$PID"; sleep 1
    kill -0 "$PID" 2>/dev/null && kill -9 "$PID" 2>/dev/null
    echo "已停止 Streamlit PID=$PID"
  else
    echo "Streamlit 进程已不在 (PID=$PID)"
  fi
  rm -f "$PIDFILE"
else
  echo "未找到 Streamlit PID 文件。"
fi

# ── Stop FastAPI ───────────────────────────────────────────────────────────
FAPI_PIDFILE=".data/fastapi.pid"
if [ -f "$FAPI_PIDFILE" ]; then
  PID=$(cat "$FAPI_PIDFILE")
  if kill -0 "$PID" 2>/dev/null; then
    kill "$PID"; sleep 1
    kill -0 "$PID" 2>/dev/null && kill -9 "$PID" 2>/dev/null
    echo "已停止 FastAPI PID=$PID"
  else
    echo "FastAPI 进程已不在 (PID=$PID)"
  fi
  rm -f "$FAPI_PIDFILE"
else
  echo "未找到 FastAPI PID 文件。"
fi

# ── Stop Redis（可选：bash stop.sh --redis，缓存丢失）────────────────────
if [ "$1" = "--redis" ]; then
  PW=$(/home/lixiang/anaconda3/bin/python3 -m config.settings --redis-password 2>/dev/null)
  if redis-cli ping >/dev/null 2>&1; then
    redis-cli shutdown nosave 2>/dev/null && echo "已停止 Redis"
  elif [ -n "$PW" ] && redis-cli -a "$PW" ping >/dev/null 2>&1; then
    redis-cli -a "$PW" shutdown nosave 2>/dev/null && echo "已停止 Redis"
  else
    echo "Redis 未在运行。"
  fi
fi

#!/usr/bin/env bash
# 劫财AI交易 启动脚本
# 解决 anaconda pandas/akshare 在系统 libstdc++ 下 GLIBCXX_3.4.29 缺失问题
set -e
cd "$(dirname "$0")"

export LD_LIBRARY_PATH="/home/lixiang/anaconda3/lib:${LD_LIBRARY_PATH:-}"
# 外部 shell 可能残留旧模型名，清掉让 .env 里的 QWEN_CHAT_MODEL 生效
unset QWEN_CHAT_MODEL

echo "启动 劫财AI交易 …  本机: http://localhost:8601"
for ip in $(hostname -I); do echo "  局域网访问: http://${ip}:8601"; done
exec /home/lixiang/anaconda3/bin/python3 -m streamlit.web.cli run ui.py \
    --server.port 8601 --server.headless true --server.address 0.0.0.0

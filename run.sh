#!/usr/bin/env bash
# 劫财AI交易 启动脚本
# 解决 anaconda pandas/akshare 在系统 libstdc++ 下 GLIBCXX_3.4.29 缺失问题
set -e
cd "$(dirname "$0")"

export LD_LIBRARY_PATH="/home/lixiang/anaconda3/lib:${LD_LIBRARY_PATH:-}"

echo "启动 劫财AI交易 …  http://localhost:8601"
exec /home/lixiang/anaconda3/bin/python3 -m streamlit.web.cli run ui.py --server.port 8601 --server.headless true

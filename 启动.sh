#!/bin/bash
# Buddy2api 一键启动（项目目录下的 .venv）
set -e
cd "$(dirname "$0")"

if [ ! -x ".venv/Scripts/python.exe" ]; then
  echo "[Setup] Creating project virtual environment..."
  py -3 -m venv .venv
  echo "[Setup] Installing dependencies (this may take a while)..."
  .venv/Scripts/python.exe -m pip install \
    -i https://mirrors.aliyun.com/pypi/simple/ --trusted-host mirrors.aliyun.com \
    -r requirements.txt
fi

# 默认不自动开浏览器：本机 Edge 常以管理员权限运行，非提权进程再拉起了 Edge
# 就会弹"是否要用普通权限重启现有实例"。需要自动打开时设 BUDDY_OPEN_BROWSER=1。
BROWSER_FLAG="--no-browser"
if [ "${BUDDY_OPEN_BROWSER:-}" = "1" ]; then
  BROWSER_FLAG=""
fi

echo "[Start] http://127.0.0.1:8787"
echo "[Open]  请在浏览器里手动打开这个地址（设 BUDDY_OPEN_BROWSER=1 可自动打开）"
echo "[Stop]  Ctrl+C"
echo ""
exec .venv/Scripts/python.exe server.py --port 8787 $BROWSER_FLAG "$@"

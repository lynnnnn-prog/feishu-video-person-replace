#!/bin/zsh
set -e
cd "${0:A:h}"

if ! command -v ngrok >/dev/null 2>&1; then
  echo "尚未安装 ngrok。请先访问 https://ngrok.com/download 安装并登录。"
  echo "安装完成后重新双击这个文件。"
  exit 1
fi

exec ngrok http 8080


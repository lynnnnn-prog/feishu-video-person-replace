#!/bin/zsh
set -e
cd "${0:A:h}"

if [[ ! -f .env ]]; then
  echo "缺少 .env。请先复制 .env.example 为 .env，并填写 GEMINI_API_KEY 和 WORKER_API_KEY。"
  exit 1
fi

if [[ ! -x .venv/bin/python ]]; then
  python3 -m venv .venv
  .venv/bin/python -m pip install --upgrade pip
  .venv/bin/python -m pip install -r requirements.txt
fi

mkdir -p logs
exec .venv/bin/python worker.py


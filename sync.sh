#!/bin/zsh
# 看门道门户 · 一键同步到 GitHub Pages
# 用法：./sync.sh ["提交说明"]
# 流程：重跑 build.py → 提交全部改动 → 推送到远程 main 分支
set -e
cd "$(dirname "$0")"

PYTHON="/Users/liuchang/.workbuddy/binaries/python/versions/3.13.12/bin/python3"

echo "==> 1/3 构建静态站"
"$PYTHON" build.py

echo "==> 2/3 提交改动"
git add -A
if git diff --cached --quiet; then
  echo "没有内容变化，跳过提交"
else
  git commit -m "${1:-update: 门户内容更新 $(date '+%Y-%m-%d %H:%M')}"
fi

echo "==> 3/3 推送到 GitHub"
git push origin main

echo "✅ 完成，GitHub Pages 将在 1-2 分钟内自动更新"

#!/bin/bash
# 拖放确认流：预览 → 人工确认 → 执行
set -u

KW="${1:-}"; TPL="${2:-}"; shift 2 || true
if [ -z "$KW" ] || [ "$#" -eq 0 ]; then
  echo "用法: dnd.sh <关键词> <模板> <文件...>"
  exit 2
fi

if ! command -v rnbatch >/dev/null 2>&1; then
  echo "找不到 rnbatch，请先 pip install -e \".[all]\""
  exit 2
fi

echo "=============================================="
echo " 命名词：$KW"
echo " 模板：  $TPL"
echo "=============================================="
echo

rnbatch preview "$@" --kw "$KW" --template "$TPL"
echo
printf "确认执行？输入 y 回车（其他任意键取消）： "
read -r ans
if [ "$ans" != "y" ]; then
  echo "已取消，文件未改动。"
  exit 0
fi

rnbatch apply "$@" --kw "$KW" --template "$TPL"
echo
echo "如需还原： rnbatch undo"

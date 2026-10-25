#!/bin/bash
# 拖放确认流：预览 → 人工确认 → 执行
#
# 退出码：
#   0  成功（或用户主动取消）
#   2  用法错误 / 找不到 rnbatch
#   3  预览发现目标名被占用，已中止，文件未改动
#   其他  rnbatch 自己的失败码
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
rc=$?

# 预览阶段就拦住：有冲突时一个文件都不该碰
if [ "$rc" -eq 3 ]; then
  echo
  echo "已中止：上面标 ! 的行目标名被占用，没有文件被改动。"
  echo "换个模板或关键词，或先把占位文件挪走再试。"
  exit 3
elif [ "$rc" -ne 0 ]; then
  echo
  echo "预览失败（退出码 $rc），已中止，没有文件被改动。"
  exit "$rc"
fi

echo
printf "确认执行？输入 y 回车（其他任意键取消）： "
read -r ans
if [ "$ans" != "y" ]; then
  echo "已取消，文件未改动。"
  exit 0
fi

if rnbatch apply "$@" --kw "$KW" --template "$TPL"; then
  echo
  echo "如需还原： rnbatch undo"
else
  echo
  echo "改名失败。未完成的改动已尽力回滚；可用 rnbatch logs 查看历史。"
  exit 1
fi

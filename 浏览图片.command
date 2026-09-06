#!/bin/bash
# imgbrowse 双击启动器
# 用法：
#   1. 直接双击 —— 浏览下面 DIR 指定的目录（默认是「图片」文件夹）
#   2. 把任意图片文件夹拖到终端窗口再回车 —— 浏览拖入的目录
# 也可以直接命令行运行：
#   python3 imgbrowse.py "任意图片目录"

DIR="$HOME/Pictures"
cd "$(dirname "$0")"

if [ ! -d "$DIR" ]; then
  echo "默认目录不存在: $DIR"
  echo "把图片文件夹拖到这个窗口后回车，即可浏览："
  read -r CUSTOM
  DIR="${CUSTOM:-$HOME}"
fi

exec python3 "$(dirname "$0")/imgbrowse.py" "$DIR"

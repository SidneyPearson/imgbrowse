#!/bin/bash
# 把 imgbrowse.py 打包成 macOS .app（需要在 Mac 上运行）
# 用法：./build-app.sh
# 产物：dist/imgbrowse.app 和 dist/imgbrowse-macos-arm64.zip
set -e
cd "$(dirname "$0")"

VENV=/tmp/imgbrowse-build-venv
if [ ! -x "$VENV/bin/pyinstaller" ]; then
  echo ">> 创建构建环境（首次需要下载 PyInstaller）"
  python3 -m venv "$VENV"
  "$VENV/bin/pip" install --quiet --upgrade pip pyinstaller
fi

echo ">> 打包中…"
rm -rf dist
"$VENV/bin/pyinstaller" --windowed --name imgbrowse --noconfirm \
  --distpath dist --workpath /tmp/imgbrowse-build-work \
  --specpath /tmp/imgbrowse-build-work \
  imgbrowse.py
rm -rf dist/imgbrowse   # 只保留 .app

echo ">> 压缩为 zip（保留 .app 结构与签名）"
ditto -c -k --keepParent dist/imgbrowse.app dist/imgbrowse-macos-arm64.zip

echo ">> 完成："
ls -lh dist/imgbrowse-macos-arm64.zip
echo "   发布：gh release create v1.0.0 dist/imgbrowse-macos-arm64.zip \\"
echo "     --title 'imgbrowse v1.0.0' --notes '见 README 安装说明'"

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
ICON=()
[ -f imgbrowse.icns ] && ICON=(--icon "$PWD/imgbrowse.icns")  # 必须绝对路径：specpath 在 /tmp 下
"$VENV/bin/pyinstaller" --windowed --name imgbrowse --noconfirm \
  --distpath dist --workpath /tmp/imgbrowse-build-work \
  --specpath /tmp/imgbrowse-build-work \
  "${ICON[@]}" \
  imgbrowse.py
rm -rf dist/imgbrowse   # 只保留 .app

echo ">> 压缩为 zip（保留 .app 结构与签名）"
ditto -c -k --keepParent dist/imgbrowse.app dist/imgbrowse-macos-arm64.zip

echo ">> 完成："
ls -lh dist/imgbrowse-macos-arm64.zip
echo "   发布（版本号自行递增，只保留最新一个 Release）："
echo "   gh release create vX.Y.Z dist/imgbrowse-macos-arm64.zip \\"
echo "     --title 'imgbrowse vX.Y.Z' --notes '改动说明'"
echo "   删除旧版：gh release delete <旧tag> --yes --cleanup-tag"

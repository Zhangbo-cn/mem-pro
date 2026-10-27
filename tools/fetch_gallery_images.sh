#!/usr/bin/env bash
# 下载 Mem-Gallery 官方图片（HuggingFace Ethan-Bei/Mem-Gallery，MIT，约 520 MiB / 1,490 张）
# 到 data/mem-gallery/image/<topic>/，供 `gallery_proxy.py --real-images` 使用。已存在的文件跳过。
# 注意：HF 的 ?recursive=true 列表会截断，所以逐个主题目录列。
set -euo pipefail
cd "$(dirname "$0")/.."
HF=${HF_ENDPOINT:-https://huggingface.co}
for f in data/mem-gallery/dialog/*.json; do
  t=$(basename "$f" .json)
  curl -s -m 30 "$HF/api/datasets/Ethan-Bei/Mem-Gallery/tree/main/data/image/$t" \
    | python3 -c "import json,sys;[print(x['path']) for x in json.load(sys.stdin) if x['type']=='file']"
done | xargs -P 16 -I{} sh -c 'o="data/mem-gallery/${1#data/}"; mkdir -p "$(dirname "$o")";
  [ -s "$o" ] || curl -sL -m 120 --retry 3 -o "$o" "'"$HF"'/datasets/Ethan-Bei/Mem-Gallery/resolve/main/$1"' _ {}
echo "图片: $(find data/mem-gallery/image -type f | wc -l) 张，$(du -sh data/mem-gallery/image | cut -f1)"

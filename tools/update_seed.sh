#!/usr/bin/env bash
# 更新種子 DB → VACUUM → 壓縮進 data/prices.sqlite.z → 版本提示。
#
# 這支是「發 App 新版前」跑的：讓要打包進 App 的內建行情盡量新，
# 縮短使用者首啟後要靠 feed 補的缺口。跑完把 .z 拉進 App repo（見 README）。
# 每日的線上 feed 更新走 CI（.github/workflows/update-feed.yml），不需人工。
#
# 用法：
#   tools/update_seed.sh                 # 增量：只補最近幾天（快，數分鐘）
#   tools/update_seed.sh --full          # 全量：全品項 × 整年（約 2.5 小時，可 Ctrl-C 續抓）
#   tools/update_seed.sh --compress-only # 略過抓取，只把現有 data/prices.sqlite 重壓回 .z
#   額外參數（增量模式）：--overlap N / --since YYYY-MM-DD 會轉傳給 update_seed.py
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
SQLITE="data/prices.sqlite"
OUTZ="data/prices.sqlite.z"

MODE="${1:-}"
case "$MODE" in
  --full)
    echo "▶︎ 1/3 全量抓取＋建置（約 2.5 小時；回應快取於 /tmp/seed_cache，中斷可重跑續抓）…"
    python3 tools/build_seed_db.py
    ;;
  --compress-only)
    [[ -f "$SQLITE" ]] || { echo "✗ 找不到 $SQLITE，請先跑增量或 --full"; exit 1; }
    echo "▶︎ 1/3 略過抓取（--compress-only）"
    ;;
  *)
    echo "▶︎ 1/3 增量更新（補最近幾天）…"
    python3 tools/update_seed.py "$@"
    ;;
esac

[[ -f "$SQLITE" ]] || { echo "✗ 找不到 $SQLITE"; exit 1; }

echo "▶︎ 2/3 VACUUM ＋ 壓縮 → $OUTZ"
sqlite3 "$SQLITE" "VACUUM;"
python3 - "$SQLITE" "$OUTZ" <<'PY'
import sys, zlib
data = open(sys.argv[1], "rb").read()
c = zlib.compressobj(9, zlib.DEFLATED, -15)   # raw deflate＝Apple NSData .zlib
open(sys.argv[2], "wb").write(c.compress(data) + c.flush())
PY

rows=$(sqlite3 "$SQLITE" "SELECT COUNT(*) FROM price_daily;")
dates=$(sqlite3 "$SQLITE" "SELECT MIN(trans_date)||' ~ '||MAX(trans_date) FROM price_daily;")
echo "  列數 ${rows}｜期間 ${dates}｜原始 $(du -h "$SQLITE" | cut -f1) → 壓縮 $(du -h "$OUTZ" | cut -f1)"

echo "▶︎ 3/3 版本提示"
PYV=$(grep -oE 'SEED_VERSION *= *"[0-9]+"' tools/build_seed_db.py | grep -oE '[0-9]+')
echo "  本 repo 的 SEED_VERSION=${PYV}"
echo "  ℹ️ 這個版本號必須與 App repo 的 FreshKnow/Database.swift 內 bundledSeedVersion 一致。"
echo "     只有「已安裝的舊用戶」也需要吃到這批新歷史時（例如新增了品項），兩處才要一起 +1："
echo "       - 本 repo tools/build_seed_db.py 的 SEED_VERSION"
echo "       - App repo FreshKnow/Database.swift 的 bundledSeedVersion"
echo "     單純刷新最近幾天的話不必動——那幾天舊用戶自己抓 feed 就會拿到。"
echo "✅ 完成： git add $OUTZ && git commit"
echo "   接著到 App repo 跑 tools/pull_seed.sh 把這份 .z 拉過去打包。"

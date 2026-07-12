# freshknow-data

鮮划算（FreshKnow）的**公開行情資料管線**。這裡只有開放資料與處理它的腳本，沒有 App 原始碼。

資料來源：[農業部資料開放平臺](https://data.moa.gov.tw/) 的農產品交易行情（批發市場每日成交價量）。

## 這個 repo 產出什麼

**每日 feed**（GitHub Actions 每天跑，部署到 GitHub Pages）：

```
https://1507078.github.io/freshknow-data/feed/v1/{north,central,south,east}.json
```

每區一個檔，內含該區代表市場最近 60 天的每日均價與成交量。App 以 conditional GET
（`If-None-Match`）取用——行情沒變就回 304 幾乎零流量，變了才下載一次。

格式刻意索引化，品名／日期／市場抽成索引表，`rows` 只剩數字：

```json
{ "v": 1, "region": "north", "from": "…", "to": "…",
  "dates": ["2026-05-12", …],
  "markets": [["109","台北一"], ["104","台北二"]],
  "crops":   [["LA1","甘藍-初秋"], …],
  "rows":    [[dateIdx, cropIdx, marketIdx, avgPrice, volume], …] }
```

`rows` 之外刻意不放產生時間戳：行情沒變時輸出必須逐位元組相同，ETag 才不會變、
使用者才不會在休市日（週末、颱風）白下載一次。

**種子資料庫** `data/prices.sqlite.z`：全品項 × 四區市場 × 一年歷史，壓縮後打包進 App。
App 首啟即可完整運作、完全離線也能用；feed 只負責補上「打包那天 → 今天」的缺口。

## 檔案

| 路徑 | 說明 |
|---|---|
| `data/groups.json` | 要抓取的母品項清單（農業部官方作物名）。由 App repo 的 `tools/export_groups.py` 匯出 |
| `data/prices.sqlite.z` | 種子 DB（raw deflate，對應 Apple `NSData.decompressed(using: .zlib)`） |
| `tools/build_seed_db.py` | 全量重建整年歷史（約 2.5 小時，可中斷續跑） |
| `tools/update_seed.py` | 增量補最近幾天 |
| `tools/build_feed.py` | 從 SQLite 匯出四個地區的 feed 檔 |
| `tools/update_seed.sh` | 上面幾支的包裝：更新 → VACUUM → 壓成 `.z` |
| `docs/privacy.html` | App 的隱私政策（App Store 需要公開網址） |

`data/prices.sqlite`（未壓縮的工作 DB）與 `docs/feed/`（產生物）都不進版控。

## 常用流程

**每天的 feed 更新**：不用管，CI 自己跑（台北時間早上 6 點）。手動觸發：

```sh
gh workflow run update-feed.yml
```

**發 App 新版前**，讓打包的種子盡量新：

```sh
tools/update_seed.sh          # 補到今天，壓成 data/prices.sqlite.z
git add data/prices.sqlite.z && git commit -m "Refresh seed to $(date +%F)"
git push
# 再到 App repo：tools/pull_seed.sh
```

**catalog 增刪品項後**（母品項清單變了）：

```sh
# App repo：
python3 tools/export_groups.py        # 重新匯出 groups.json 到這個 repo
# 這個 repo：
tools/update_seed.sh --full           # 全量重建（新品項需要整年歷史）
```

新增品項時，`tools/build_seed_db.py` 的 `SEED_VERSION` 與 App repo
`FreshKnow/Database.swift` 的 `bundledSeedVersion` 要一起 +1，已安裝的舊用戶才會經遷移吃到新歷史。

#!/usr/bin/env python3
"""
從 tools/prices.sqlite 匯出「每區近 N 天」的增量視窗檔 → docs/feed/v1/{region}.json

App 啟動時對自己所在地區的視窗檔發一次 conditional GET（If-None-Match）：
資料沒變就是 304 幾乎零流量，變了才下載一次，寫入本機 DB 疊在打包種子之上。
抓失敗就完全用本機資料，離線無感。

視窗的用途只是橋接「App 內建種子的日期 → 今天」這段缺口，所以天數只要大於發版間隔即可，
不必更大——視窗直接決定下載量（北部 60 天約 gzip 150KB，90 天約 225KB）。
超出視窗的使用者（久未更新 App）也不會壞：種子仍供得起一年百分位，只是中間少一段。

輸出為未壓縮 JSON——GitHub Pages（Fastly）會自動加 Content-Encoding: gzip，
URLSession 透明解壓，不需要 App 端自己處理 gzip。

用法：python3 tools/build_feed.py [--days 90]
"""
import argparse, json, os, sqlite3

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "data", "prices.sqlite")
OUTDIR = os.path.join(ROOT, "docs", "feed", "v1")
FEED_VERSION = 1

# 與 App 的 Region enum 一致（Models.swift）
REGIONS = {
    "north":   [("109", "台北一"), ("104", "台北二")],
    "central": [("400", "台中市"), ("420", "豐原區")],
    "south":   [("800", "高雄市"), ("700", "台南市場")],
    "east":    [("930", "台東市"), ("260", "宜蘭市")],
}


def build_region(db, region, markets, days):
    codes = [c for c, _ in markets]
    rows = db.execute(f"""
        SELECT trans_date, crop_code, crop_name, market_code, price_avg, volume
        FROM price_daily
        WHERE market_code IN ({','.join('?' * len(codes))})
          AND trans_date >= date((SELECT MAX(trans_date) FROM price_daily), ?)
          AND price_avg > 0
        ORDER BY trans_date, crop_code
    """, (*codes, f"-{days} days")).fetchall()

    # 索引化：品名/日期/市場重複率極高，抽成索引表可讓 rows 只剩數字，體積少一個量級。
    dates, crops, out = {}, {}, []
    for d, code, name, mcode, avg, vol in rows:
        di = dates.setdefault(d, len(dates))
        ci = crops.setdefault((code, name), len(crops))
        out.append([di, ci, codes.index(mcode), round(avg, 1), round(vol or 0, 1)])

    date_list = sorted(dates, key=dates.get)
    # 刻意不放產生時間戳：行情沒變時輸出必須逐位元組相同，ETag 才會不變、
    # App 才拿得到 304。休市日（週末、颱風）若因時間戳而每天換 ETag，使用者就白下載。
    return {
        "v": FEED_VERSION,
        "region": region,
        "from": date_list[0] if date_list else None,
        "to": date_list[-1] if date_list else None,
        "dates": date_list,
        "markets": [list(m) for m in markets],
        "crops": [list(k) for k in sorted(crops, key=crops.get)],
        "rows": out,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60,
                    help="視窗天數（需大於發版間隔），預設 60")
    ap.add_argument("--db", default=DB, help=f"來源 SQLite（預設 {DB}）")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        raise SystemExit(f"✗ 找不到 {args.db}；先跑 tools/update_seed.sh")
    db = sqlite3.connect(args.db)
    os.makedirs(OUTDIR, exist_ok=True)

    for region, markets in REGIONS.items():
        feed = build_region(db, region, markets, args.days)
        path = os.path.join(OUTDIR, f"{region}.json")
        with open(path, "w") as f:
            json.dump(feed, f, ensure_ascii=False, separators=(",", ":"))
        size = os.path.getsize(path) / 1024
        print(f"  {region:8s} {feed['from']} ~ {feed['to']}｜"
              f"{len(feed['rows']):6d} 列、{len(feed['crops']):3d} 品項｜{size:7.1f} KB")

    db.close()
    print(f"\n完成 → {OUTDIR}（GitHub Pages 會自動 gzip 傳輸，實際下載量約為上述的 1/5）")


if __name__ == "__main__":
    main()

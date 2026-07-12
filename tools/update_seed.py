#!/usr/bin/env python3
"""
增量更新種子資料庫（快）：只補「最近幾天」的行情到現有 prices.sqlite.z，不重建整年。

作法與 App 每日同步（MOAClient.fetchRecent）一致——逐市場×菜果兩類抓，不逐品項：
  缺的天數 × 8 市場 × 2 類（N04 蔬菜 / N05 水果）個請求，通常幾分鐘內完成。
逐「單日」抓以避開非會員第一頁 1000 筆上限（單日單市場單類遠低於此）。
抓到後依 catalog 白名單母品項過濾（母品項 或 母品項-子品項，避免前綴碰撞），
INSERT OR REPLACE 進現有 DB，並更新 seed_date / last_sync。

流程：解壓現有 .z → tools/prices.sqlite → 合併最近幾天 → 留給 update_seed.sh 壓回 .z。

用法：
  python3 tools/update_seed.py            # 從種子最後日期往前 2 天重疊，補到今天
  python3 tools/update_seed.py --overlap 5
  python3 tools/update_seed.py --since 2026-07-01
"""
import argparse, json, os, ssl, sqlite3, time, urllib.parse, urllib.request, zlib
from datetime import date, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = "https://data.moa.gov.tw/api/v1/AgriProductsTransType/"
GROUPS = os.path.join(ROOT, "data", "groups.json")
OUTZ = os.path.join(ROOT, "data", "prices.sqlite.z")
OUT = os.path.join(ROOT, "data", "prices.sqlite")
PAGE_LIMIT = 1000   # 非會員第一頁上限；單一回應達此值代表可能被截斷

# 與 App 的 Region enum 一致（Models.swift）
REGIONS = {
    "north":   [("台北一", "109"), ("台北二", "104")],
    "central": [("台中市", "400"), ("豐原區", "420")],
    "south":   [("高雄市", "800"), ("台南市場", "700")],
    "east":    [("台東市", "930"), ("宜蘭市", "260")],
}
TC_TYPES = ["N04", "N05"]   # 蔬菜 / 水果

_ctx = ssl.create_default_context()
_ctx.check_hostname = False
_ctx.verify_mode = ssl.CERT_NONE


def roc(d: date) -> str:
    return f"{d.year - 1911}.{d.month:02d}.{d.day:02d}"


def iso_from_roc(s: str):
    p = s.split(".")
    if len(p) != 3 or not p[0].isdigit():
        return None
    return f"{int(p[0]) + 1911:04d}-{p[1]}-{p[2]}"


def whitelist_groups():
    """母品項白名單（data/groups.json，由 App repo 匯出）。
    fetch_day 本來就把某市場某日全部品項抓回來，白名單只在寫入前過濾，不影響請求數。"""
    return json.load(open(GROUPS))


def in_whitelist(crop_name: str, groups) -> bool:
    """母品項比對，與 Database.dailySeries 同規則：完全相同或「母品項-子品項」，避免前綴碰撞。"""
    if not crop_name:
        return False
    for g in groups:
        if crop_name == g or crop_name.startswith(g + "-"):
            return True
    return False


def fetch_day(market_name: str, tc: str, day: date):
    """抓某市場、某類、某單日全部品項（不帶 CropName）。回傳 (rows, truncated)。"""
    r = roc(day)
    q = urllib.parse.urlencode({
        "Start_time": r, "End_time": r,
        "MarketName": market_name, "TcType": tc, "Page": "1",
    })
    req = urllib.request.Request(BASE + "?" + q, headers={"Accept": "application/json"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=45, context=_ctx) as resp:
                body = json.load(resp)
            data = body.get("Data") or []
            return data, len(data) >= PAGE_LIMIT
        except Exception as e:
            if attempt == 2:
                print(f"    ! 失敗 {market_name}/{tc}/{r}: {e}")
                return [], False
            time.sleep(2)


def load_db(use_existing: bool):
    """取得工作 DB。

    預設從 repo 內的 .z 解壓（單一真相來源，本機用）。
    --use-existing 則沿用現有 tools/prices.sqlite——CI 用 actions/cache 在每日 run 之間
    接力同一份 DB，只補一天；快取失效時檔案不存在，自動退回從 .z 解壓（較慢但會自我修復）。
    """
    if use_existing and os.path.exists(OUT):
        print(f"沿用現有 {OUT}（--use-existing）")
        return sqlite3.connect(OUT)
    if not os.path.exists(OUTZ):
        raise SystemExit(f"✗ 找不到 {OUTZ}；請先跑一次全量：python3 tools/build_seed_db.py")
    data = zlib.decompressobj(-15).decompress(open(OUTZ, "rb").read())
    open(OUT, "wb").write(data)
    return sqlite3.connect(OUT)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--overlap", type=int, default=2,
                    help="從種子最後日期往前重疊幾天再抓起（防漏／修正回填），預設 2")
    ap.add_argument("--since", type=str, default=None,
                    help="指定起抓日 YYYY-MM-DD（覆蓋 --overlap）")
    ap.add_argument("--use-existing", action="store_true",
                    help="沿用現有 tools/prices.sqlite 而非從 .z 解壓（CI 快取接力用）")
    args = ap.parse_args()

    db = load_db(args.use_existing)
    cur = db.cursor()
    groups = whitelist_groups()

    seed_max = cur.execute("SELECT MAX(trans_date) FROM price_daily").fetchone()[0]
    end = date.today()
    if args.since:
        start = date.fromisoformat(args.since)
    else:
        base = date.fromisoformat(seed_max) if seed_max else end - timedelta(days=7)
        start = base - timedelta(days=args.overlap)
    if start > end:
        start = end
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]

    markets = [(mn, mc) for ms in REGIONS.values() for (mn, mc) in ms]
    total_req = len(days) * len(markets) * len(TC_TYPES)
    print(f"種子最後日期 {seed_max}｜補 {days[0]} ~ {days[-1]}（{len(days)} 天）"
          f"｜{len(markets)} 市場 × {len(TC_TYPES)} 類 = {total_req} 個請求")

    before = cur.execute("SELECT COUNT(*) FROM price_daily").fetchone()[0]
    t0 = time.time()
    inserted = 0
    truncated_any = False
    n = 0
    for day in days:
        for (mname, mcode) in markets:
            for tc in TC_TYPES:
                n += 1
                data, truncated = fetch_day(mname, tc, day)
                if truncated:
                    truncated_any = True
                    print(f"    ⚠️ {mname}/{tc}/{day} 回應達 {PAGE_LIMIT} 筆，可能被截斷")
                for r in data:
                    if r.get("CropCode") in (None, "-") or r.get("CropName") == "休市":
                        continue
                    if not in_whitelist(r.get("CropName"), groups):
                        continue
                    iso = iso_from_roc(r.get("TransDate", ""))
                    if not iso:
                        continue
                    cur.execute(
                        "INSERT OR REPLACE INTO price_daily VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (iso, r.get("TcType"), r["CropCode"], r.get("CropName"),
                         r["MarketCode"], r.get("MarketName"),
                         r.get("Upper_Price"), r.get("Middle_Price"), r.get("Lower_Price"),
                         r.get("Avg_Price"), r.get("Trans_Quantity")))
                    inserted += 1
            db.commit()
        print(f"  [{n}/{total_req}] {day} done｜累計寫入 {inserted} 列", flush=True)

    # 更新種子日期戳；seed_version 不動（增量資料 App 自身每日同步也會拿到，
    # 要讓舊用戶經遷移吃到這批，才需在 build_seed_db.py 與 Database.swift 同步 +1）
    cur.execute("INSERT OR REPLACE INTO sync_meta VALUES ('seed_date', ?)", (end.isoformat(),))
    cur.execute("INSERT OR REPLACE INTO sync_meta VALUES ('last_sync', ?)",
                (end.isoformat() + "T00:00:00Z",))
    db.commit()
    after = cur.execute("SELECT COUNT(*) FROM price_daily").fetchone()[0]
    new_max = cur.execute("SELECT MAX(trans_date) FROM price_daily").fetchone()[0]
    db.close()

    m, s = divmod(int(time.time() - t0), 60)
    print(f"\n完成：寫入/覆蓋 {inserted} 列，總列數 {before} → {after}"
          f"（淨增 {after - before}），最新日期 {new_max}，耗時 {m}分{s:02d}秒")
    if truncated_any:
        print("⚠️ 有回應達 1000 筆上限，該日某市場可能不完整——需要的話用 build_seed_db.py 全量重建。")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
建置種子資料庫 prices.sqlite（全量）：抓北中南東各代表市場 × 全 catalog 母品項 × 一年歷史，
產出與 App 相同 schema 的 SQLite（打包進 App，首啟複製）。

種子必須涵蓋 catalog 的每一個母品項——使用者可在總覽頁選取任一品項，而 App 端已無線上
逐品項回填，選了卻沒歷史就只能顯示「資料不足」。

逐品項抓 → 151 母品項 × 8 市場 = 1208 個請求，約 2.5 小時。
只在「catalog 增刪品項」「要重建整年歷史」時才需要跑；平時補最近幾天用 update_seed.py。

特色：
- 進度：每 10 項印「[已完成/總數] 剩餘 N，已耗時 / 預估剩餘」。
- 可續跑：回應快取到 /tmp/seed_cache/，中斷後重跑接續，不重抓（故重跑前若要新資料，先清快取）。
- 計時：結束印總耗時、列數、檔案大小。

用法：python3 tools/build_seed_db.py
"""
import json, os, ssl, sqlite3, time, urllib.parse, urllib.request
from datetime import date, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = "https://data.moa.gov.tw/api/v1/AgriProductsTransType/"
GROUPS = os.path.join(ROOT, "data", "groups.json")
OUT = os.path.join(ROOT, "data", "prices.sqlite")
CACHE = "/tmp/seed_cache"
SEED_VERSION = "2"
DAYS = 365

# 與 App 的 Region enum 一致（Models.swift）
REGIONS = {
    "north":   [("台北一", "109"), ("台北二", "104")],
    "central": [("台中市", "400"), ("豐原區", "420")],
    "south":   [("高雄市", "800"), ("台南市場", "700")],
    "east":    [("台東市", "930"), ("宜蘭市", "260")],
}

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


def fetch(group: str, market_name: str, start: str, end: str):
    os.makedirs(CACHE, exist_ok=True)
    key = f"{market_name}_{group}".replace("/", "_")
    fp = os.path.join(CACHE, key + ".json")
    if os.path.exists(fp):
        return json.load(open(fp)).get("Data") or []
    q = urllib.parse.urlencode({
        "Start_time": start, "End_time": end,
        "CropName": group, "MarketName": market_name, "Page": "1",
    })
    req = urllib.request.Request(BASE + "?" + q, headers={"Accept": "application/json"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=45, context=_ctx) as r:
                body = json.load(r)
            json.dump(body, open(fp, "w"), ensure_ascii=False)
            return body.get("Data") or []
        except Exception as e:
            if attempt == 2:
                print(f"    ! 失敗 {market_name}/{group}: {e}")
                return []
            time.sleep(2)


def init_db(path):
    if os.path.exists(path):
        os.remove(path)
    db = sqlite3.connect(path)
    db.executescript("""
    CREATE TABLE price_daily(
        trans_date TEXT NOT NULL, tc_type TEXT, crop_code TEXT NOT NULL, crop_name TEXT,
        market_code TEXT NOT NULL, market_name TEXT,
        price_high REAL, price_mid REAL, price_low REAL, price_avg REAL, volume REAL,
        PRIMARY KEY (trans_date, market_code, crop_code));
    CREATE INDEX idx_crop_date ON price_daily(crop_name, trans_date);
    CREATE TABLE sync_meta(key TEXT PRIMARY KEY, value TEXT);
    """)
    return db


def fmt(sec):
    m, s = divmod(int(sec), 60)
    return f"{m}分{s:02d}秒"


def main():
    # 母品項清單由 App repo 的 tools/export_groups.py 從 catalog 匯出。
    # 必須涵蓋 catalog 的每一個母品項：使用者可在 App 裡選取任一品項，
    # 而 App 端已無線上逐品項回填，選了卻沒歷史就只能顯示「資料不足」。
    groups = json.load(open(GROUPS))
    jobs = [(rk, mn, mc, g) for rk, ms in REGIONS.items() for (mn, mc) in ms for g in groups]
    total = len(jobs)
    print(f"母品項 {len(groups)} × 市場 {sum(len(v) for v in REGIONS.values())} = {total} 項")

    end = date.today()
    start = end - timedelta(days=DAYS)
    end_r, start_r = roc(end), roc(start)

    db = init_db(OUT)
    cur = db.cursor()
    t0 = time.time()
    rows_total = 0
    for n, (rk, mname, mcode, group) in enumerate(jobs, 1):
        data = fetch(group, mname, start_r, end_r)
        for r in data:
            if r.get("CropCode") in (None, "-") or r.get("CropName") == "休市":
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
            rows_total += 1
        if n % 10 == 0 or n == total:
            el = time.time() - t0
            eta = el / n * (total - n)
            print(f"[{n}/{total}] 剩餘 {total - n}｜{rk}/{mname}/{group}"
                  f"｜已耗時 {fmt(el)}，預估剩 {fmt(eta)}｜累計 {rows_total} 列", flush=True)
        db.commit()

    cur.execute("INSERT OR REPLACE INTO sync_meta VALUES ('seed_version', ?)", (SEED_VERSION,))
    cur.execute("INSERT OR REPLACE INTO sync_meta VALUES ('seed_date', ?)", (end.isoformat(),))
    # last_sync 設為種子日期，App 首啟不會立刻誤判過期
    cur.execute("INSERT OR REPLACE INTO sync_meta VALUES ('last_sync', ?)", (end.isoformat() + "T00:00:00Z",))
    db.commit()
    db.close()

    size = os.path.getsize(OUT) / 1024 / 1024
    print(f"\n完成：{rows_total} 列，耗時 {fmt(time.time() - t0)}，"
          f"輸出 {OUT}（{size:.1f} MB）")


if __name__ == "__main__":
    main()

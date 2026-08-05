"""sync_mirror / parse_rows 身分比對測試 — 一律使用隔離的暫存 DB。

sync_mirror 會先全域 `UPDATE links SET active=0`，絕不可對著開發用
tracker.db 跑，因此本模組在 import tracker.db 前就把 cfg.DB_PATH 指到
暫存檔。Run:  python tests/test_sync.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from tracker import config as cfg  # noqa: E402

cfg.load()
_tmp = tempfile.mkdtemp(prefix="tracker-sync-test-")
cfg.DB_PATH = os.path.join(_tmp, "test.db")
cfg.XLSX_PATH = os.path.join(_tmp, "no-snapshot.xlsx")  # 跳過基準價快照讀取

from tracker import db  # noqa: E402

db.init()

from tracker import sheets  # noqa: E402

URL_ENC = "https://shopee.co.id/product/1111/222222?extraParams=%7B%22display_model_id%22%3A9%7D"
URL_DEC = 'https://shopee.co.id/product/1111/222222?extraParams={"display_model_id":9}'


def mkrow(r, code, h_text, h_url=None, name="item", price="10.000"):
    vals = {c: "" for c in sheets.COLS}
    vals.update({"A": code, "C": name, "E": price, "H": h_text})
    return {"row": r, "vals": vals, "h_url": h_url, "e_num": None}


def header():
    return {"row": 1, "vals": {c: "" for c in sheets.COLS}, "h_url": None, "e_num": None}


def links_of(code):
    return [dict(r) for r in db.q(
        "SELECT * FROM links WHERE product_code=? ORDER BY id", (code,))]


def main():
    # ---- parse_rows：儲存格文字優先（0801 客戶要求）-----------------------
    _, links, _ = sheets.parse_rows(
        [header(), mkrow(2, "T1 #1", "https://shopee.co.id/product/1000/2000000",
                         h_url="https://reurl.cc/x")])
    assert links[0]["raw_url"] == "https://shopee.co.id/product/1000/2000000", links[0]["raw_url"]
    _, links, _ = sheets.parse_rows(
        [header(), mkrow(2, "T1 #1", "some label text",
                         h_url="https://shopee.co.id/product/1000/2000000")])
    assert links[0]["raw_url"] == "https://shopee.co.id/product/1000/2000000", links[0]["raw_url"]
    print("parse_rows text-first OK")

    # ---- 身分重比對：同格 URL 編碼變動不得產生幻影 ------------------------
    sheets.sync_mirror([header(), mkrow(2, "T2 #1", URL_ENC)])
    r1 = links_of("T2 #1")
    assert len(r1) == 1 and r1[0]["dedupe_key"] == "1111.222222", r1
    db.x("UPDATE links SET status='valid', last_checked_at=?, last_price_idr=123 WHERE id=?",
         (db.now(), r1[0]["id"]))
    sheets.sync_mirror([header(), mkrow(2, "T2 #1", URL_DEC)])
    r2 = links_of("T2 #1")
    assert len(r2) == 1, f"編碼變動不得長出新列: {len(r2)}"
    assert r2[0]["id"] == r1[0]["id"], "必須是同一列就地更新"
    assert r2[0]["raw_url"] == URL_DEC and r2[0]["active"] == 1
    assert r2[0]["status"] == "valid" and r2[0]["last_price_idr"] == 123, "檢查歷史必須保留"
    print("identity re-match OK: raw_url 就地更新、歷史保留、無幻影")

    # ---- 幻影收斂：舊幻影對（同 dedupe_key 一 claimed 一未 claimed）-------
    ua = "https://shopee.co.id/product/1111/333333?sp_atk=aaa"
    ub = "https://shopee.co.id/product/1111/333333?sp_atk=bbb"
    db.x("""INSERT INTO links(product_code, raw_url, dedupe_key, shopid, itemid, status,
            status_detail, last_checked_at, last_price_idr, active)
            VALUES('T3 #1', ?, '1111.333333', 1111, 333333, 'high_cost', 'old detail', ?, 999, 1)""",
         (ua, db.now()))
    db.x("""INSERT INTO links(product_code, raw_url, dedupe_key, shopid, itemid,
            status, active) VALUES('T3 #1', ?, '1111.333333', 1111, 333333, 'unchecked', 1)""", (ub,))
    sheets.sync_mirror([header(), mkrow(2, "T3 #1", ub)])
    r3 = links_of("T3 #1")
    assert len(r3) == 1, f"幻影應被併掉: {[(x['raw_url'], x['active']) for x in r3]}"
    assert r3[0]["raw_url"] == ub and r3[0]["active"] == 1
    assert r3[0]["status"] == "high_cost" and r3[0]["last_price_idr"] == 999, \
        "倖存者是空殼時要接收幻影的檢查歷史"
    print("phantom converge OK: 併列 + 歷史搬移")

    # ---- 同 dedupe_key 兩列都真實在表上 → 絕不合併（KBT151 #1 案例）-------
    x1 = "https://shopee.co.id/product/1111/444444?sp_atk=x1"
    x2 = "https://shopee.co.id/product/1111/444444?sp_atk=x2"
    pull = [header(), mkrow(2, "T4 #1", x1), mkrow(3, "T4 #1", x2)]
    sheets.sync_mirror(pull)
    r4 = links_of("T4 #1")
    assert len(r4) == 2 and all(x["active"] == 1 for x in r4), r4
    sheets.sync_mirror(pull)  # 再拉一次也不得增生或合併
    r4b = links_of("T4 #1")
    assert len(r4b) == 2 and all(x["active"] == 1 for x in r4b), r4b
    assert {x["id"] for x in r4b} == {x["id"] for x in r4}
    print("dual real rows OK: 兩列皆保留、re-pull 不增生")

    print("\nALL SYNC TESTS PASSED")


if __name__ == "__main__":
    main()

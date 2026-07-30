"""Offline pipeline test: fake Shopee driver → check + find jobs end-to-end.

Run:  python tests/test_pipeline.py   (uses the dev tracker.db built by
`python -m tracker.cli import-xlsx`; resets KBT105 #14 state first)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from tracker import config as cfg, db  # noqa: E402
cfg.load()
db.init()

from tracker import checker, finder, jobs, linkparse, shopee, web  # noqa: E402

CODE = "KBT105 #14"

# 測試絕不能碰真實網路 / 真實 Sheet：dry_run 強制開（in-memory，不寫 config.json）
cfg._config["dry_run"] = True
cfg._config["auto_accept_candidates"] = True


class FakeDriver:
    def __init__(self):
        self.pdp_calls = []
        self.search_calls = []
        # keyed by itemid
        self.pdp = {}

    def get_pdp(self, url):
        ids = linkparse.parse_shopee_url(url)
        self.pdp_calls.append(ids["itemid"])
        parsed = self.pdp.get(ids["itemid"], {"exists": False, "reason": "api-error:4",
                                              "models": [], "images": []})
        return parsed, {"fake": True}

    def search(self, kw, locations_param="", page=0):
        self.search_calls.append((kw, locations_param))
        return self.search_results, {"fake": True}

    def fetch_image(self, image_id):
        return None

    def resolve_in_browser(self, url, timeout=25):
        return None


def reset():
    db.x("""UPDATE links SET status='unchecked', status_detail=NULL, last_price_idr=NULL,
            prev_price_idr=NULL, shop_location=NULL, sold=NULL, search_keyword=NULL
            WHERE product_code=?""", (CODE,))
    db.x("DELETE FROM links WHERE product_code=? AND raw_url LIKE 'https://test-short%'", (CODE,))
    db.x("DELETE FROM candidates WHERE product_code=?", (CODE,))
    db.x("DELETE FROM pending_writes WHERE product_code=?", (CODE,))
    db.x("""UPDATE products SET baseline_override_idr=NULL, baseline_idr=NULL,
            high_cost_pct=NULL, search_keyword=NULL, search_keyword_at=NULL WHERE code=?""", (CODE,))


def links_by_row():
    return {r["sheet_row_hint"]: dict(r) for r in
            db.q("SELECT * FROM links WHERE product_code=? AND active=1", (CODE,))}


def run_job_to_completion(job_id):
    job = dict(db.q1("SELECT * FROM jobs WHERE id=?", (job_id,)))
    jobs._run_job(job)
    return db.q1("SELECT state FROM jobs WHERE id=?", (job_id,))["state"]


def setup_pdp(fake, rows):
    it25, it26, it27 = rows[25]["itemid"], rows[26]["itemid"], rows[27]["itemid"]
    # row25: price rose slightly (still under threshold) → valid + update_price
    fake.pdp[it25] = {"exists": True, "unlisted": False, "title": "SisterBeauty Skintific CLEANSER",
                      "shop_name": "Sister Beauty", "images": ["imgA"], "item_status": "normal",
                      "shop_location": "Kota Tangerang", "historical_sold": 1234,
                      "models": [{"model_id": rows[25]["model_id"], "name": "Low PH 80ml FULL,CLEANSER Skintific",
                                  "price_idr": 78000, "stock": None, "in_stock": True, "image": "imgA1"}]}
    # row26: sold out
    fake.pdp[it26] = {"exists": True, "unlisted": False, "title": "[BPOM] Skintific 5X Ceramide Low pH CLEANSER",
                      "shop_name": "DeBeaute", "images": ["imgB"], "item_status": "normal",
                      "models": [{"model_id": rows[26]["model_id"], "name": "LOW PH 80ml,Skintific",
                                  "price_idr": 74703, "stock": None, "in_stock": False, "image": "imgB1"}]}
    # row27: big price jump → high cost (baseline 78000, +15% = 89700)
    fake.pdp[it27] = {"exists": True, "unlisted": False, "title": "Skintific 5x Ceramide Low Ph Facial Wash",
                      "shop_name": "Pretty Perfect", "images": ["imgC"], "item_status": "normal",
                      "models": [{"model_id": rows[27]["model_id"], "name": "80ml",
                                  "price_idr": 99999, "stock": None, "in_stock": True, "image": "imgC1"}]}


def main():
    reset()
    fake = FakeDriver()
    shopee.get_driver = lambda: fake  # monkeypatch

    rows = links_by_row()
    assert set(rows) == {25, 26, 27}, f"KBT105 #14 rows: {sorted(rows)}"
    setup_pdp(fake, rows)

    # ---- unit: _sold_to_int（import re 回歸）-----------------------------
    assert shopee._sold_to_int("10RB+ terjual") == 10000
    assert shopee._sold_to_int("1,2RB sold") == 1200
    assert shopee._sold_to_int("56 sold") == 56
    print("_sold_to_int OK (import re regression)")

    # ---- unit: build_keyword（客戶 1.2 驗收案例）-------------------------
    kw = finder.build_keyword(
        "[BPOM] Skintific 5X Ceramide Low pH CLEANSER Gentle CLEANSER For Sensitive Skin "
        "60ml | 80ml | 120ml | 15ml | Niacinamide Brightening",
        "LOW PH 80ml, Skintific", attempt=1, brand="Skintific")
    assert kw == "Skintific 5X Ceramide Low pH CLEANSER 80ml", kw
    print("build_keyword OK:", kw)

    # D 欄選項規格（香味）必須進關鍵字，品牌縮寫 FCare 不得混入；
    # 多字品牌不重複前置。客戶人工搜的是「freshcare sandalwood」。
    kw2 = finder.build_keyword("Fresh Care Aromatherapy Roll On | Minyak Angin FreshCare | Teens",
                               "Fcare sandalwood", attempt=1, brand="Fresh Care")
    assert kw2 == "Fresh Care Aromatherapy Roll On sandalwood", kw2
    # 同系列不同香味必須產生不同關鍵字（先前九款共用同一組關鍵字）
    kw3 = finder.build_keyword("FRESH CARE Aromatherapy Roll On Mix Smash Double Inhaler | Minyak",
                               "FCare- Lavender", attempt=1, brand="Fresh Care")
    assert kw2 != kw3 and "Lavender" in kw3, kw3
    # "10 ml"（空格分隔）算容量，不可在關鍵字裡出現兩次
    kw4 = finder.build_keyword("FreshCare Teens 10 ml Aroma Cherry, Bubble Gum, Passion Fruit | B",
                               "PASSION FRUIT", attempt=1, brand=None)
    assert kw4.count("10 ml") == 1 and "PASSION" in kw4, kw4
    print("build_keyword spec-token OK:", kw2, "/", kw3)

    # ---- unit: split_official（正式前 3 低價 valid，其餘備選）------------
    mk = lambda i, st, last, sheet=None: {"id": i, "active": 1, "status": st,
                                          "last_price_idr": last, "price_idr": sheet}
    ls = [mk(1, "valid", 90), mk(2, "valid", 70), mk(3, "invalid", 60),
          mk(4, "valid", 80), mk(5, "valid", 95), mk(6, "sold_out", 50), mk(7, "valid", 60)]
    off, back = web.split_official(ls, 3)
    assert [l["id"] for l in off] == [7, 2, 4], [l["id"] for l in off]
    assert {l["id"] for l in back} == {1, 3, 5, 6}, [l["id"] for l in back]
    # 備選 valid 復活變最便宜 → 重算自然進正式（計算制）
    ls[0]["last_price_idr"] = 10
    off2, _ = web.split_official(ls, 3)
    assert [l["id"] for l in off2] == [1, 7, 2]
    print("split_official OK")

    # ---- unit: migration 冪等 --------------------------------------------
    db.init()
    db.init()
    print("migration idempotent OK")

    # ---- check job（基準價固定 78000）------------------------------------
    db.x("UPDATE products SET baseline_idr=78000 WHERE code=?", (CODE,))
    job_id = jobs.create_job("check", {"product_codes": [CODE]})
    n_items = db.q1("SELECT COUNT(*) n FROM job_items WHERE job_id=?", (job_id,))["n"]
    assert n_items == 4, f"expected 3 fetch + 1 classify, got {n_items}"
    state = run_job_to_completion(job_id)
    assert state == "done", state

    rows = links_by_row()
    assert rows[25]["status"] == "valid", rows[25]["status"]
    assert rows[26]["status"] == "sold_out", rows[26]["status"]
    assert rows[27]["status"] == "high_cost", rows[27]["status"]
    assert rows[25]["last_price_idr"] == 78000
    # 地區/銷量回填（parse_pdp → links）
    assert rows[25]["shop_location"] == "Kota Tangerang", rows[25]["shop_location"]
    assert rows[25]["sold"] == 1234, rows[25]["sold"]
    # 前次價滾動：首次檢查 → 前次價 = 表上 E 欄價
    assert rows[25]["prev_price_idr"] == rows[25]["price_idr"], rows[25]["prev_price_idr"]

    writes = [dict(w) for w in db.q(
        "SELECT * FROM pending_writes WHERE product_code=? ORDER BY id", (CODE,))]
    kinds = sorted((w["kind"], db.jload(w["payload_json"])["raw_url"][:20]) for w in writes)
    upd = [w for w in writes if w["kind"] == "update_price"]
    notes = {db.jload(w["payload_json"]).get("note") for w in writes if w["kind"] == "set_note"}
    assert len(upd) == 2, f"update_price for rows 25+27 expected, got {kinds}"
    assert notes == {"sold out", "high_cost"}, notes  # 客戶 2.1 note 詞彙
    print("check pipeline OK:", [(w['kind'], w['product_code']) for w in writes])

    # ---- 價格滾動（第二次檢查）-------------------------------------------
    it25 = rows[25]["itemid"]
    fake.pdp[it25]["models"][0]["price_idr"] = 81000
    job_id = jobs.create_job("check", {"product_codes": [CODE]})
    run_job_to_completion(job_id)
    rows = links_by_row()
    assert rows[25]["prev_price_idr"] == 78000, rows[25]["prev_price_idr"]
    assert rows[25]["last_price_idr"] == 81000, rows[25]["last_price_idr"]
    fake.pdp[it25]["models"][0]["price_idr"] = 78000
    print("price rolling OK: prev=78000, last=81000")

    # ---- 基準價不變性（客戶 1.5）-----------------------------------------
    reset()
    db.x("UPDATE products SET baseline_idr=60000 WHERE code=?", (CODE,))
    job_id = jobs.create_job("check", {"product_codes": [CODE]})
    run_job_to_completion(job_id)
    rows = links_by_row()
    assert rows[25]["status"] == "high_cost", rows[25]["status"]  # 78000 > 60000*1.15
    got = db.q1("SELECT baseline_idr FROM products WHERE code=?", (CODE,))["baseline_idr"]
    assert got == 60000, f"基準價被程式改動了: {got}"
    print("baseline immutable OK: 60000 不因檢查結果變動")

    # ---- 短網址／非蝦皮 → invalid + link error（客戶 1.3/2.1）------------
    reset()
    orig_resolve = linkparse.resolve_url
    try:
        db.x("""INSERT INTO links(product_code, raw_url, page_name, variant_text, price_idr,
                active, origin, status) VALUES(?, 'https://test-short.example/abc', 'short test',
                '-', 12345, 1, 'sheet', 'unchecked')""", (CODE,))
        lid = db.q1("SELECT id FROM links WHERE raw_url='https://test-short.example/abc'")["id"]

        linkparse.resolve_url = lambda url, **kw: None  # 解析失敗（browser fallback 也回 None）
        job_id = jobs.create_job("check", {"link_ids": [lid]})
        state = run_job_to_completion(job_id)
        assert state == "done", state  # 流程不中斷
        lk = dict(db.q1("SELECT * FROM links WHERE id=?", (lid,)))
        assert lk["status"] == "invalid", lk["status"]
        note_w = [db.jload(w["payload_json"]) for w in db.q(
            "SELECT * FROM pending_writes WHERE product_code=? AND kind='set_note'", (CODE,))]
        assert any(p.get("note") == "link error" for p in note_w), note_w
        assert not db.q("SELECT 1 FROM links WHERE status='manual'"), "manual 狀態應已移除"

        # 解析成功但非蝦皮 → 同樣 invalid
        db.x("UPDATE links SET status='unchecked' WHERE id=?", (lid,))
        db.x("DELETE FROM pending_writes WHERE product_code=?", (CODE,))
        linkparse.resolve_url = lambda url, **kw: "https://www.tokopedia.com/some-item"
        job_id = jobs.create_job("check", {"link_ids": [lid]})
        state = run_job_to_completion(job_id)
        assert state == "done", state
        lk = dict(db.q1("SELECT * FROM links WHERE id=?", (lid,)))
        assert lk["status"] == "invalid", lk["status"]
        assert "非蝦皮" in (lk["status_detail"] or ""), lk["status_detail"]
    finally:
        linkparse.resolve_url = orig_resolve
    print("short-url / non-shopee OK: invalid + link error, job 不中斷")

    # ---- crash-resume ----------------------------------------------------
    reset()
    job_id = jobs.create_job("check", {"product_codes": [CODE]})
    first = db.q1("SELECT id FROM job_items WHERE job_id=? ORDER BY id LIMIT 1", (job_id,))["id"]
    db.x("UPDATE job_items SET state='done', result_json='{}' WHERE id=?", (first,))
    fake.pdp_calls.clear()
    state = run_job_to_completion(job_id)
    assert state == "done"
    assert len(fake.pdp_calls) == 2, f"done item must not re-run: {fake.pdp_calls}"
    print("resume OK: completed item skipped, only", len(fake.pdp_calls), "fetches ran")

    # ---- find job（純價格排序 + 缺幾補幾）--------------------------------
    reset()
    db.x("UPDATE links SET status='valid', last_price_idr=78000 WHERE product_code=? AND sheet_row_hint=25", (CODE,))
    db.x("UPDATE links SET status='sold_out' WHERE product_code=? AND sheet_row_hint IN (26,27)", (CODE,))
    rows = links_by_row()

    fake.search_results = [
        {"itemid": 999001, "shopid": 555001, "title": "Skintific Low pH Cleanser 80ml murah",
         "price_idr": 70000, "sold": 5000, "image": "s1", "shop_location": "Kota Tangerang", "is_ad": False},
        {"itemid": 999002, "shopid": 555002, "title": "Skintific cleanser 80ml",
         "price_idr": 65000, "sold": 50, "image": "s2", "shop_location": "Jakarta Barat", "is_ad": False},  # sold<100
        {"itemid": rows[25]["itemid"], "shopid": rows[25]["shopid"], "title": "dup of existing",
         "price_idr": 60000, "sold": 9000, "image": "s3", "shop_location": "Jakarta Timur", "is_ad": False},  # dupe
        {"itemid": 999004, "shopid": 555004, "title": "Other brand serum",
         "price_idr": 30000, "sold": 200, "image": "s4", "shop_location": "Surabaya", "is_ad": False},  # wrong loc
    ]
    # multi-variant candidate → the matched model's id flows into the URL and
    # the chosen variant name into D
    fake.pdp[999001] = {"exists": True, "unlisted": False, "title": "Skintific Low pH Cleanser 80ml murah",
                        "shop_name": "TokoMurah", "images": ["n1"], "item_status": "normal",
                        "models": [{"model_id": 776, "name": "LOW PH 15ml MINI",
                                    "price_idr": 30000, "stock": None, "in_stock": True, "image": "n0"},
                                   {"model_id": 777, "name": "LOW PH 80ml,Skintific",
                                    "price_idr": 70000, "stock": None, "in_stock": True, "image": "n1a"}]}

    cfg._config["auto_accept_candidates"] = False  # 先驗證 propose 本身
    job_id = jobs.create_job("find", {"product_codes": [CODE]})
    state = run_job_to_completion(job_id)
    assert state == "done", state
    cands = [dict(c) for c in db.q("SELECT * FROM candidates WHERE product_code=? AND state='proposed'", (CODE,))]
    assert len(cands) == 1, f"expected exactly 1 candidate: {[(c['itemid'], c['state']) for c in cands]}"
    assert cands[0]["itemid"] == 999001 and cands[0]["price_idr"] == 70000
    assert cands[0]["variant_text"] == "LOW PH 80ml,Skintific", cands[0]["variant_text"]
    assert (cands[0]["search_keyword"] or "").strip(), "候選需記錄搜尋關鍵字"
    kw_rec = db.q1("SELECT search_keyword FROM products WHERE code=?", (CODE,))["search_keyword"]
    assert (kw_rec or "").strip(), "products.search_keyword 需記錄"
    print("find pipeline OK: candidate", cands[0]["itemid"], "@", cands[0]["price_idr"], "IDR, kw:", kw_rec)

    # ---- accept → insert_link_row payload --------------------------------
    wid = finder.accept_candidate(cands[0]["id"])
    w = dict(db.q1("SELECT * FROM pending_writes WHERE id=?", (wid,)))
    p = db.jload(w["payload_json"])
    assert w["kind"] == "insert_link_row" and w["dedupe_key"] == "555001.999001"
    assert p["price_idr"] == 70000 and p["supplier"] == "TokoMurah"
    assert p["product_code"] == CODE, p.get("product_code")     # A 欄要填 product code
    assert "display_model_id%22%3A777" in p["url"]
    assert p["shop_location"] == "Kota Tangerang" and p["sold"] == 5000
    assert (p.get("search_keyword") or "").strip(), "payload 需帶關鍵字（寫 L 欄用）"
    print("accept OK:", p["url"][:80])

    # ---- 一鍵流程：check + include_find 動態展開 + auto accept ------------
    reset()
    cfg._config["auto_accept_candidates"] = True
    setup_pdp(fake, rows)
    db.x("UPDATE products SET baseline_idr=78000 WHERE code=?", (CODE,))
    job_id = jobs.create_job("check", {"product_codes": [CODE], "include_find": True})
    state = run_job_to_completion(job_id)
    assert state == "done", state
    item_kinds = [r["kind"] for r in db.q(
        "SELECT kind FROM job_items WHERE job_id=? ORDER BY id", (job_id,))]
    assert item_kinds.count("find_links") == 1, item_kinds  # 檢查完 1 valid < 3 → 動態補找
    total = db.q1("SELECT progress_total FROM jobs WHERE id=?", (job_id,))["progress_total"]
    assert total == 5, total  # 3 fetch + 1 classify + 1 find
    ins = [dict(w) for w in db.q(
        "SELECT * FROM pending_writes WHERE product_code=? AND kind='insert_link_row' AND job_id=?",
        (CODE, job_id))]
    assert len(ins) == 1, f"auto-accept 應產生 1 筆插列: {len(ins)}"
    p1 = db.jload(ins[0]["payload_json"])
    assert (p1.get("search_keyword") or "").strip()
    print("one-key OK: check→classify→find→auto-accept，動態 items:", item_kinds)

    # ---- 殘留候選過期（缺幾補幾不被歷史污染）-----------------------------
    already = db.q1("SELECT COUNT(*) n FROM candidates WHERE product_code=? AND state='proposed'",
                    (CODE,))["n"]
    assert already == 0, "auto-accept 後不應殘留 proposed"
    db.x("""INSERT INTO candidates(product_code, shopid, itemid, state, created_at)
            VALUES(?, 1, 1, 'proposed', ?)""", (CODE, db.now()))
    job_id = jobs.create_job("find", {"product_codes": [CODE]})
    run_job_to_completion(job_id)
    stale = db.q1("SELECT state FROM candidates WHERE product_code=? AND itemid=1", (CODE,))["state"]
    assert stale == "expired", stale
    print("stale candidate expiry OK")

    # ---- single-variant candidate → D = "-", URL without display_model_id -
    reset()
    db.x("UPDATE links SET status='valid', last_price_idr=78000 WHERE product_code=? AND sheet_row_hint=25", (CODE,))
    db.x("UPDATE links SET status='sold_out' WHERE product_code=? AND sheet_row_hint IN (26,27)", (CODE,))
    cfg._config["auto_accept_candidates"] = False
    fake.search_results = [
        {"itemid": 888001, "shopid": 444001, "title": "Skintific cleanser single",
         "price_idr": 68000, "sold": 3000, "image": "z1", "shop_location": "Jakarta Barat", "is_ad": False},
    ]
    fake.pdp[888001] = {"exists": True, "unlisted": False, "title": "Skintific Cleanser (no options)",
                        "shop_name": "SoloShop", "images": ["z1"], "item_status": "normal",
                        "models": [{"model_id": 900, "name": "SKINTIFIC CLEANSER",
                                    "price_idr": 68000, "stock": None, "in_stock": True, "image": "z1a"}]}
    job_id = jobs.create_job("find", {"product_codes": [CODE]})
    run_job_to_completion(job_id)
    c2 = dict(db.q1("SELECT * FROM candidates WHERE product_code=? AND itemid=888001", (CODE,)))
    assert c2["variant_text"] == "-", f"single-model D should be '-': {c2['variant_text']!r}"
    assert c2["model_id"] is None, c2["model_id"]
    wid2 = finder.accept_candidate(c2["id"])
    p2 = db.jload(dict(db.q1("SELECT * FROM pending_writes WHERE id=?", (wid2,)))["payload_json"])
    assert "display_model_id" not in p2["url"], p2["url"]
    print("single-variant OK: D='-', URL without display_model_id")

    reset()
    print("\nALL OFFLINE PIPELINE TESTS PASSED")


if __name__ == "__main__":
    main()

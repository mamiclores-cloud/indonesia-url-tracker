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

from tracker import checker, finder, jobs, linkparse, sheets, shopee, web  # noqa: E402

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
        self.pdp_raise = set()       # itemids whose PDP fetch should explode
        self.browser_resolves = {}   # short url -> final url（模擬瀏覽器追短網址）
        self.browser_calls = []

    def get_pdp(self, url):
        ids = linkparse.parse_shopee_url(url)
        self.pdp_calls.append(ids["itemid"])
        if ids["itemid"] in self.pdp_raise:
            raise RuntimeError("fake pdp crash")
        parsed = self.pdp.get(ids["itemid"], {"exists": False, "reason": "api-error:4",
                                              "models": [], "images": []})
        return parsed, {"fake": True}

    def search(self, kw, locations_param="", page=0):
        self.search_calls.append((kw, locations_param))
        return self.search_results, {"fake": True}

    def fetch_image(self, image_id):
        return None

    def resolve_in_browser(self, url, timeout=25):
        self.browser_calls.append(url)
        return self.browser_resolves.get(url)


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

    # ---- unit: split_official（0801：正式=全部 valid 依價升冪，備選=非 valid）
    mk = lambda i, st, last, sheet=None: {"id": i, "active": 1, "status": st,
                                          "last_price_idr": last, "price_idr": sheet}
    ls = [mk(1, "valid", 90), mk(2, "valid", 70), mk(3, "invalid", 60),
          mk(4, "valid", 80), mk(5, "valid", 95), mk(6, "sold_out", 50), mk(7, "valid", 60)]
    off, back = web.split_official(ls, 3)
    assert [l["id"] for l in off] == [7, 2, 4, 1, 5], [l["id"] for l in off]  # 5 valid 全進正式
    assert {l["id"] for l in back} == {3, 6}, [l["id"] for l in back]         # 備選只有非 valid
    # 備選 invalid 復活 → 重算自然進正式；超過 3 條 valid 就維持超過（0801 備註）
    ls[2]["status"] = "valid"
    off2, back2 = web.split_official(ls, 3)
    assert [l["id"] for l in off2] == [3, 7, 2, 4, 1, 5], [l["id"] for l in off2]
    assert {l["id"] for l in back2} == {6}
    print("split_official OK")

    # ---- unit: derived_note / human_note（0801：Note 由當前狀態推導）------
    for st, note in [("valid", ""), ("high_cost", "high_cost"), ("sold_out", "sold out"),
                     ("unlisted", "sold out"), ("variant_error", "sold out"),
                     ("invalid", "link error"), ("error", "link error"), ("unchecked", "")]:
        got = web.derived_note({"status": st})
        assert got == note, (st, got)
        if st in web.UI_INVALID_STATUSES:
            assert note, f"UI-invalid 狀態 {st} 必須有 Note"
    assert web.human_note({"note": "out of stock"}) == "out of stock"
    assert web.human_note({"note": "high cost"}) == ""   # 機器詞彙不當人工備註
    assert web.human_note({"note": None}) == ""
    print("derived_note OK: 所有 invalid 狀態皆有 Note")

    # ---- unit: 插列 value_batch（0801：I 欄不再寫 new，L 欄關鍵字照寫）----
    vb = sheets._insert_value_batch("ws", 30, CODE, {
        "url": "https://shopee.co.id/product/1/2", "page_name": "n", "variant_text": "-",
        "price_idr": 5, "supplier": "s", "search_keyword": "kw", "product_code": CODE})
    vb_ranges = [v["range"] for v in vb]
    assert not any("!I" in r for r in vb_ranges), vb_ranges
    assert "ws!L30" in vb_ranges, vb_ranges
    print("insert value_batch OK: no I('new'), L keyword kept")

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

    # ---- 短網址：瀏覽器 fallback 成功 → valid + url_cache（0801）----------
    reset()
    db.x("DELETE FROM url_cache WHERE short_url LIKE 'https://test-short%'")
    db.x("""INSERT INTO links(product_code, raw_url, page_name, variant_text, price_idr,
            active, origin, status) VALUES(?, 'https://test-short.example/xyz', 'browser test',
            '-', 12345, 1, 'sheet', 'unchecked')""", (CODE,))
    lid2 = db.q1("SELECT id FROM links WHERE raw_url='https://test-short.example/xyz'")["id"]
    fake.browser_calls.clear()  # 前段非蝦皮測試也會（正確地）觸發瀏覽器 fallback
    fake.browser_resolves["https://test-short.example/xyz"] = "https://shopee.co.id/product/3210/654321"
    fake.pdp[654321] = {"exists": True, "unlisted": False, "title": "Resolved item",
                        "shop_name": "ShortShop", "images": ["r1"], "item_status": "normal",
                        "shop_location": "Jakarta Barat", "historical_sold": 500,
                        "models": [{"model_id": 32100, "name": "STD", "price_idr": 12345,
                                    "stock": None, "in_stock": True, "image": "r1a"}]}
    orig_resolve = linkparse.resolve_url
    try:
        # requests 端永遠停在短網址本身（模擬 reurl.cc 擋 UA / JS 跳轉）
        linkparse.resolve_url = lambda url, **kw: url
        job_id = jobs.create_job("check", {"link_ids": [lid2]})
        assert run_job_to_completion(job_id) == "done"
        lk = dict(db.q1("SELECT * FROM links WHERE id=?", (lid2,)))
        assert lk["status"] == "valid", (lk["status"], lk["status_detail"])
        cached = db.q1("SELECT final_url FROM url_cache WHERE short_url='https://test-short.example/xyz'")
        assert cached and cached["final_url"] == "https://shopee.co.id/product/3210/654321"
        assert len(fake.browser_calls) == 1, fake.browser_calls
    finally:
        linkparse.resolve_url = orig_resolve
    # 第二次檢查：真 resolve_url 開頭就命中快取（不打網路），不再呼叫瀏覽器
    db.x("UPDATE links SET status='unchecked' WHERE id=?", (lid2,))
    job_id = jobs.create_job("check", {"link_ids": [lid2]})
    assert run_job_to_completion(job_id) == "done"
    assert db.q1("SELECT status FROM links WHERE id=?", (lid2,))["status"] == "valid"
    assert len(fake.browser_calls) == 1, "第二次應走 url_cache，不再呼叫瀏覽器"
    db.x("DELETE FROM url_cache WHERE short_url LIKE 'https://test-short%'")
    print("browser-resolve OK: 短網址經瀏覽器解析→valid，快取後免瀏覽器")

    # ---- unit: url_cache 消毒（0801：毒條目忽略、非蝦皮不入快取）----------
    db.x("INSERT OR REPLACE INTO url_cache(short_url, final_url, resolved_at) VALUES(?,?,?)",
         ("https://test-short.example/poison", "https://test-short.example/poison", db.now()))
    orig_get = linkparse.requests.get

    class _FakeResp:
        def __init__(self, url):
            self.url, self.text = url, ""
    try:
        linkparse.requests.get = lambda *a, **kw: _FakeResp("https://other.example/final")
        got = linkparse.resolve_url("https://test-short.example/poison")
        assert got == "https://other.example/final", got  # 毒快取（short→short）被忽略，走真解析
        assert db.q1("SELECT 1 FROM url_cache WHERE short_url=?",
                     ("https://other.example/final",)) is None
        row = db.q1("SELECT final_url FROM url_cache WHERE short_url=?",
                    ("https://test-short.example/poison",))
        assert row["final_url"] == "https://test-short.example/poison", "非蝦皮 final 不得入快取"
    finally:
        linkparse.requests.get = orig_get
    db.x("DELETE FROM url_cache WHERE short_url LIKE 'https://test-short%'")
    print("url_cache hygiene OK: 毒條目忽略、非蝦皮不入快取")

    # ---- variant_error → sold out；抓取例外 → error → link error（0801）--
    reset()
    rows = links_by_row()
    setup_pdp(fake, rows)
    it26v, it27v = rows[26]["itemid"], rows[27]["itemid"]
    fake.pdp[it26v] = {"exists": True, "unlisted": False, "title": "t", "shop_name": "s",
                       "images": [], "item_status": "normal",
                       "models": [{"model_id": 1, "name": "AAA", "price_idr": 100, "stock": None,
                                   "in_stock": True, "image": None},
                                  {"model_id": 2, "name": "BBB", "price_idr": 200, "stock": None,
                                   "in_stock": True, "image": None}]}
    fake.pdp_raise.add(it27v)
    try:
        job_id = jobs.create_job("check", {"product_codes": [CODE]})
        assert run_job_to_completion(job_id) == "done"
        rows = links_by_row()
        assert rows[26]["status"] == "variant_error", rows[26]["status"]
        assert rows[27]["status"] == "error", rows[27]["status"]
        assert web.ui_status(rows[26]) == "invalid" and web.ui_status(rows[27]) == "invalid"
        notes = {db.jload(w["payload_json"]).get("note") for w in db.q(
            "SELECT * FROM pending_writes WHERE product_code=? AND kind='set_note'", (CODE,))}
        assert "sold out" in notes and "link error" in notes, notes
    finally:
        fake.pdp_raise.clear()
    print("variant_error→sold out / error→link error OK（invalid 必有 Note）")

    # ---- 孤兒 accepted 不吃補找名額 + 虛擬待寫入列（0801）-----------------
    reset()
    db.x("UPDATE links SET status='valid', last_price_idr=78000 "
         "WHERE product_code=? AND sheet_row_hint IN (25,26)", (CODE,))
    db.x("UPDATE links SET status='sold_out' WHERE product_code=? AND sheet_row_hint=27", (CODE,))
    db.x("""INSERT INTO candidates(product_code, shopid, itemid, state, created_at)
            VALUES(?, 2, 2, 'accepted', ?)""", (CODE, db.now()))  # 無 pending write 背書的孤兒
    cfg._config["auto_accept_candidates"] = True
    fake.search_results = [
        {"itemid": 999001, "shopid": 555001, "title": "Skintific Low pH Cleanser 80ml murah",
         "price_idr": 70000, "sold": 5000, "image": "s1", "shop_location": "Kota Tangerang", "is_ad": False},
    ]
    fake.pdp[999001] = {"exists": True, "unlisted": False, "title": "Skintific Low pH Cleanser 80ml murah",
                        "shop_name": "TokoMurah", "images": ["n1"], "item_status": "normal",
                        "models": [{"model_id": 777, "name": "LOW PH 80ml,Skintific",
                                    "price_idr": 70000, "stock": None, "in_stock": True, "image": "n1a"}]}
    job_id = jobs.create_job("find", {"product_codes": [CODE]})
    assert run_job_to_completion(job_id) == "done"
    orphan = db.q1("SELECT state FROM candidates WHERE product_code=? AND itemid=2", (CODE,))["state"]
    assert orphan == "expired", orphan  # 舊邏輯 need=3-2-1=0 會 skip，孤兒必須先過期
    acc = [dict(c) for c in db.q(
        "SELECT * FROM candidates WHERE product_code=? AND state='accepted'", (CODE,))]
    assert [c["itemid"] for c in acc] == [999001], acc
    # 虛擬列：accepted + pending insert 寫入 → 出現在正式區資料，帶關鍵字
    vrows = web.pending_candidate_rows(CODE)
    assert len(vrows) == 1 and vrows[0]["virtual"] and vrows[0]["ui_status"] == "valid", vrows
    assert (vrows[0]["search_keyword"] or "").strip(), "虛擬列需帶搜尋關鍵字"
    # 寫入被丟棄 → 虛擬列消失
    db.x("UPDATE pending_writes SET state='discarded' "
         "WHERE product_code=? AND kind='insert_link_row'", (CODE,))
    assert web.pending_candidate_rows(CODE) == []
    # 寫入完成（candidate=written）也不顯示——由鏡射的真實列接手
    db.x("UPDATE pending_writes SET state='pending' "
         "WHERE product_code=? AND kind='insert_link_row'", (CODE,))
    db.x("UPDATE candidates SET state='written' WHERE product_code=? AND itemid=999001", (CODE,))
    assert web.pending_candidate_rows(CODE) == []
    print("orphan-accepted expiry + virtual pending rows OK")

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

    # ---- 補找價格上限：超過 high cost 門檻的候選不採用（0802）--------------
    reset()
    db.x("UPDATE products SET baseline_idr=78000, high_cost_pct=10 WHERE code=?", (CODE,))
    db.x("UPDATE links SET status='valid', last_price_idr=78000 WHERE product_code=? AND sheet_row_hint=25", (CODE,))
    db.x("UPDATE links SET status='sold_out' WHERE product_code=? AND sheet_row_hint IN (26,27)", (CODE,))
    cfg._config["auto_accept_candidates"] = False
    fake.search_results = [
        # 清單價已超過 78000×1.1=85800 → 連 PDP 都不該載入
        {"itemid": 777001, "shopid": 333001, "title": "Skintific Low pH Cleanser 80ml",
         "price_idr": 99000, "sold": 8000, "image": "p1", "shop_location": "Kota Tangerang", "is_ad": False},
        # 清單價過關但實際規格價超標 → PDP 確認後仍不採用
        {"itemid": 777002, "shopid": 333002, "title": "Skintific Low pH Cleanser 80ml",
         "price_idr": 70000, "sold": 8000, "image": "p2", "shop_location": "Kota Tangerang", "is_ad": False},
        # 完全合格
        {"itemid": 777003, "shopid": 333003, "title": "Skintific Low pH Cleanser 80ml",
         "price_idr": 71000, "sold": 8000, "image": "p3", "shop_location": "Kota Tangerang", "is_ad": False},
    ]
    for iid, mprice in ((777001, 99000), (777002, 90000), (777003, 71000)):
        fake.pdp[iid] = {"exists": True, "unlisted": False, "title": "Skintific Low pH Cleanser 80ml",
                         "shop_name": f"Shop{iid}", "images": ["p"], "item_status": "normal",
                         "models": [{"model_id": None, "name": "LOW PH 80ml,Skintific",
                                     "price_idr": mprice, "stock": None, "in_stock": True, "image": "pa"}]}
    fake.pdp_calls.clear()
    job_id = jobs.create_job("find", {"product_codes": [CODE]})
    assert run_job_to_completion(job_id) == "done"
    got = {c["itemid"] for c in db.q(
        "SELECT itemid FROM candidates WHERE product_code=? AND state='proposed'", (CODE,))}
    assert got == {777003}, got                       # 只採用門檻內的
    assert 777001 not in fake.pdp_calls, "清單價超標者不該載入 PDP"
    assert 777002 in fake.pdp_calls, "清單價過關者仍需 PDP 確認"
    res = db.jload(db.q1("SELECT result_json FROM job_items WHERE job_id=? AND kind='find_links'",
                         (job_id,))["result_json"], {})
    # need=2 但只有 1 個合格候選 → 會再往下一個地區層級找，超標者每層各記一次
    assert res["price_cap"] == 85800 and res["over_price_cap"] >= 2, res
    print("price cap OK: 清單價預篩 + 規格價複檢，只留門檻內候選")

    # ---- 單商品補找時間上限（0802）---------------------------------------
    reset()
    db.x("UPDATE products SET baseline_idr=78000 WHERE code=?", (CODE,))
    db.x("UPDATE links SET status='sold_out' WHERE product_code=?", (CODE,))
    cfg._config["find_time_budget_s"] = -1             # 期限已過，必定在第一個檢查點收工
    try:
        job_id = jobs.create_job("find", {"product_codes": [CODE]})
        assert run_job_to_completion(job_id) == "done"   # 超時不是失敗，正常收工
        res = db.jload(db.q1("SELECT result_json FROM job_items WHERE job_id=? AND kind='find_links'",
                             (job_id,))["result_json"], {})
        assert res.get("time_budget_hit") and "時間上限" in res.get("note", ""), res
        assert res["pdp_confirms"] == 0
    finally:
        cfg._config["find_time_budget_s"] = 240
    print("find time budget OK: 超時即收工並標註，任務不中斷")

    # ---- pacing 安全網（0802）--------------------------------------------
    assert shopee.safe_pacing_until() is None
    shopee.enter_safe_pacing("測試")
    assert shopee.safe_pacing_until(), "偵測到阻擋後應進入保守節奏"
    drv = shopee.ShopeeDriver()
    import unittest.mock
    with unittest.mock.patch("tracker.shopee.time.sleep") as slept:
        drv._pace()
    assert slept.call_args[0][0] >= shopee.SAFE_PACING["min_delay_s"], slept.call_args
    db.x("DELETE FROM settings WHERE key='pacing_safe_until'")
    assert shopee.safe_pacing_until() is None
    print("safe pacing OK: 阻擋後自動退回保守值、到期自動恢復")

    # ---- Sheets API 節流與重試（0802）------------------------------------
    calls = []

    class R:
        def __init__(self, code): self.status_code = code
    seq = [R(429), R(503), R(200)]
    with unittest.mock.patch("tracker.sheets.time.sleep") as slept:
        r = sheets.api_call(lambda *a, **k: (calls.append(a), seq.pop(0))[1], "url")
    assert r.status_code == 200 and len(calls) == 3, (r.status_code, len(calls))
    assert [c[0][0] for c in slept.call_args_list] == [2.0, 4.0], slept.call_args_list
    print("sheets api_call OK: 429/5xx 指數退避後成功")

    # ---- 審核頁 API：filter 沒命中絕不可退回「套用全部」（0802）------------
    from tracker import create_app
    app = create_app(start_worker=False)
    cl = app.test_client()
    n_all = db.q1("SELECT COUNT(*) n FROM pending_writes WHERE state='pending'")["n"]
    rv = cl.post("/api/writes/apply", json={"filter": {"q": "NO_SUCH_CODE_XYZ"}})
    assert rv.status_code == 400 and not rv.get_json()["ok"], rv.get_json()
    rv = cl.post("/api/writes/discard", json={"filter": {"q": "NO_SUCH_CODE_XYZ"}})
    assert rv.get_json()["discarded"] == 0, rv.get_json()
    assert db.q1("SELECT COUNT(*) n FROM pending_writes WHERE state='pending'")["n"] == n_all
    # 分頁與篩選一致性：各頁筆數加總 == 篩選總數
    import re as _re
    body = cl.get("/review?kind=set_note").get_data(as_text=True)
    total = int(_re.search(r"篩選結果 (\d+) 筆", body).group(1))
    pages = int(_re.search(r"第 \d+ / (\d+) 頁", body).group(1))
    seen = sum(cl.get(f"/review?kind=set_note&page={p}").get_data(as_text=True).count('class="sel"')
               for p in range(1, pages + 1))
    n_set_note = db.q1("SELECT COUNT(*) n FROM pending_writes WHERE kind='set_note' AND state='pending'")["n"]
    assert total == seen == n_set_note, (total, seen, n_set_note)
    print(f"review API OK: filter 空集合不誤觸全部；分頁涵蓋 {seen} 筆無重複遺漏")

    # ---- 分頁夾頁：超出範圍停在最後一頁，不出現空表（0802）----------------
    for url in ("/products?page=999", "/review?page=999", "/jobs?page=999"):
        body = cl.get(url).get_data(as_text=True)
        cur, last = _re.search(r"第 (\d+) / (\d+) 頁", body).groups()
        assert cur == last, (url, cur, last)
    jid = db.q1("SELECT id FROM jobs ORDER BY id DESC LIMIT 1")["id"]
    body = cl.get(f"/jobs/{jid}?page=999").get_data(as_text=True)
    cur, last = _re.search(r"第 (\d+) / (\d+) 頁", body).groups()
    assert cur == last, (cur, last)
    print("pagination clamp OK: 四個列表頁超出範圍都停在最後一頁")

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

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

from tracker import checker, finder, jobs, linkparse, shopee  # noqa: E402

CODE = "KBT105 #14"


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
    db.x("UPDATE links SET status='unchecked', status_detail=NULL, last_price_idr=NULL WHERE product_code=?",
         (CODE,))
    db.x("DELETE FROM candidates WHERE product_code=?", (CODE,))
    db.x("DELETE FROM pending_writes WHERE product_code=?", (CODE,))
    db.x("UPDATE products SET baseline_override_idr=NULL, high_cost_pct=NULL WHERE code=?", (CODE,))


def links_by_row():
    return {r["sheet_row_hint"]: dict(r) for r in
            db.q("SELECT * FROM links WHERE product_code=? AND active=1", (CODE,))}


def run_job_to_completion(job_id):
    job = dict(db.q1("SELECT * FROM jobs WHERE id=?", (job_id,)))
    jobs._run_job(job)
    return db.q1("SELECT state FROM jobs WHERE id=?", (job_id,))["state"]


def main():
    reset()
    fake = FakeDriver()
    shopee.get_driver = lambda: fake  # monkeypatch

    rows = links_by_row()
    assert set(rows) == {25, 26, 27}, f"KBT105 #14 rows: {sorted(rows)}"
    it25, it26, it27 = rows[25]["itemid"], rows[26]["itemid"], rows[27]["itemid"]

    # row25: price rose slightly (still under threshold) → valid + update_price
    fake.pdp[it25] = {"exists": True, "unlisted": False, "title": "SisterBeauty Skintific CLEANSER",
                      "shop_name": "Sister Beauty", "images": ["imgA"], "item_status": "normal",
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

    # ---- check job -------------------------------------------------------
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

    writes = [dict(w) for w in db.q(
        "SELECT * FROM pending_writes WHERE product_code=? ORDER BY id", (CODE,))]
    kinds = sorted((w["kind"], db.jload(w["payload_json"])["raw_url"][:20]) for w in writes)
    upd = [w for w in writes if w["kind"] == "update_price"]
    notes = {db.jload(w["payload_json"]).get("note") for w in writes if w["kind"] == "set_note"}
    assert len(upd) == 2, f"update_price for rows 25+27 expected, got {kinds}"
    assert notes == {"sold out", "high cost"}, notes
    print("check pipeline OK:", [(w['kind'], w['product_code']) for w in writes])

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

    # ---- find job --------------------------------------------------------
    reset()
    db.x("UPDATE links SET status='valid', last_price_idr=78000 WHERE product_code=? AND sheet_row_hint=25", (CODE,))
    db.x("UPDATE links SET status='sold_out' WHERE product_code=? AND sheet_row_hint IN (26,27)", (CODE,))

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

    job_id = jobs.create_job("find", {"product_codes": [CODE]})
    state = run_job_to_completion(job_id)
    assert state == "done", state
    cands = [dict(c) for c in db.q("SELECT * FROM candidates WHERE product_code=?", (CODE,))]
    assert len(cands) == 1, f"expected exactly 1 candidate: {[(c['itemid'], c['state']) for c in cands]}"
    assert cands[0]["itemid"] == 999001 and cands[0]["price_idr"] == 70000
    assert cands[0]["variant_text"] == "LOW PH 80ml,Skintific", cands[0]["variant_text"]
    print("find pipeline OK: candidate", cands[0]["itemid"], "@", cands[0]["price_idr"], "IDR")

    # ---- accept → insert_link_row payload --------------------------------
    wid = finder.accept_candidate(cands[0]["id"])
    w = dict(db.q1("SELECT * FROM pending_writes WHERE id=?", (wid,)))
    p = db.jload(w["payload_json"])
    assert w["kind"] == "insert_link_row" and w["dedupe_key"] == "555001.999001"
    assert p["price_idr"] == 70000 and p["supplier"] == "TokoMurah"
    assert p["product_code"] == CODE, p.get("product_code")     # A 欄要填 product code
    assert "display_model_id%22%3A777" in p["url"]
    print("accept OK:", p["url"][:80])

    # ---- single-variant candidate → D = "-", URL without display_model_id -
    reset()
    db.x("UPDATE links SET status='valid', last_price_idr=78000 WHERE product_code=? AND sheet_row_hint=25", (CODE,))
    db.x("UPDATE links SET status='sold_out' WHERE product_code=? AND sheet_row_hint IN (26,27)", (CODE,))
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

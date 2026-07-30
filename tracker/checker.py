"""Link checker pipeline: fetch_link (per link) then classify_product (per code).

Encodes the SPEC business rules: invalid / sold out / unlisted / high cost,
recovery re-checks, price refill, and the note clobber guard (final guard is
enforced again at write time in sheets.apply_writes).
"""
import difflib
import gzip
import json
import logging
import os
import re

from . import config as cfg
from . import db
from . import jobs
from . import linkparse
from . import shopee

log = logging.getLogger(__name__)

RAW_DIR = os.path.join(cfg.DATA_DIR, "raw")

# 客戶 2.1：Note 只用三種原因詞彙；unlisted 歸類為 sold out；連結打不開/
# 非蝦皮/解析失敗 = link error。valid 清空。（error 不寫 note，靠 status_detail）
NOTE_FOR_STATUS = {
    "invalid": "link error",
    "sold_out": "sold out",
    "unlisted": "sold out",
    "high_cost": "high_cost",
    "valid": "",
}


# ------------------------------------------------------------- expansion ---

def _checkable_links(codes=None, link_ids=None):
    """All active links. 客戶 1.3：短網址/非蝦皮不再預先跳過（manual 已移除），
    一律進 fetch 嘗試解析，解析不了才標 invalid（Note: link error），流程繼續。"""
    if link_ids:
        rows = db.q(f"SELECT * FROM links WHERE id IN ({','.join('?'*len(link_ids))}) AND active=1",
                    link_ids)
    elif codes:
        rows = db.q(f"SELECT * FROM links WHERE product_code IN ({','.join('?'*len(codes))}) AND active=1",
                    codes)
    else:
        rows = db.q("SELECT * FROM links WHERE active=1")
    return [dict(r) for r in rows]


def expand_check(params):
    links = _checkable_links(params.get("product_codes"), params.get("link_ids"))
    items = [{"kind": "fetch_link", "target": lk["id"]} for lk in links]
    codes = {lk["product_code"] for lk in links}
    # 指定商品時，就算目前 0 條可查連結也要 classify（才會觸發補找）
    codes.update(params.get("product_codes") or [])
    for code in sorted(codes):
        items.append({"kind": "classify_product", "target": code})
    return items


def expand_full_scan(params):
    params = dict(params or {})
    params["product_codes"] = None
    items = expand_check({})
    return items


# ------------------------------------------------------------- fetch link --

def _save_raw(link_id, raw):
    try:
        os.makedirs(RAW_DIR, exist_ok=True)
        with gzip.open(os.path.join(RAW_DIR, f"{link_id}.json.gz"), "wt", encoding="utf-8") as f:
            json.dump(raw, f, ensure_ascii=False)
    except OSError:
        pass


def normalize_variant(s):
    s = (s or "").lower()
    s = re.sub(r"[^\w一-鿿]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


SIZE_RE = re.compile(r"\d+\s*(?:ml|gr|g|gram|pcs|pc|sachet|kapsul|caps|l)\b", re.I)


def match_variant(variant_text, models, url_model_id=None, allow_single_model=False):
    """Pick the model a sheet row refers to. Returns (model, how) or (None, reason).

    allow_single_model (finder only): accept a candidate that lists the product
    as a single variant even when our variant_text doesn't match its name —
    a single-option listing has nothing to choose, and our "variant" is often
    just a quantity ("1pc") that other sellers don't model. Kept off for the
    checker, where mismatching an existing link's variant would corrupt prices.
    """
    if url_model_id is not None:
        for m in models:
            if m["model_id"] == url_model_id:
                return m, "display_model_id"
    vt = (variant_text or "").strip()
    if vt in ("", "-"):
        if len(models) == 1:
            return models[0], "single-model"
        if url_model_id is None:
            return None, f"無規格文字但商品有 {len(models)} 個規格"
    norm_v = normalize_variant(vt)
    scored = []
    for m in models:
        norm_m = normalize_variant(m["name"])
        if not norm_m:
            continue
        if norm_m == norm_v:
            return m, "exact"
        ratio = difflib.SequenceMatcher(None, norm_v, norm_m).ratio()
        # two-tier "a, b" — also compare the joined/reversed permutation
        parts = [p.strip() for p in vt.split(",") if p.strip()]
        if len(parts) == 2:
            rev = normalize_variant(", ".join(reversed(parts)))
            ratio = max(ratio, difflib.SequenceMatcher(None, rev, norm_m).ratio())
        scored.append((ratio, m))
    scored.sort(key=lambda t: -t[0])
    if scored and scored[0][0] >= 0.65:
        return scored[0][1], f"fuzzy:{scored[0][0]:.2f}"
    # unique size-token fallback
    sizes = SIZE_RE.findall(vt.replace(" ", ""))
    if sizes:
        tok = normalize_variant(sizes[0])
        hits = [m for m in models if tok and tok in normalize_variant(m["name"]).replace(" ", "")]
        if len(hits) == 1:
            return hits[0], f"size-token:{sizes[0]}"
    # single-variant listing: accept it unless our size clearly conflicts
    if allow_single_model and len(models) == 1:
        our = SIZE_RE.search(vt.replace(" ", ""))
        if our:
            theirs = SIZE_RE.search((models[0]["name"] or "").replace(" ", ""))
            if theirs and normalize_variant(theirs.group(0)) != normalize_variant(our.group(0)):
                return None, f"single-model 尺寸不符：{theirs.group(0)} vs {our.group(0)}"
        return models[0], "single-model-assumed"
    if url_model_id is not None:
        return None, f"display_model_id {url_model_id} 已不存在且規格文字比對失敗"
    return None, "規格文字無法對應任何選項"


def resolve_link(link, drv=None):
    """Ensure link has a usable shopee URL + ids. Returns final url or None."""
    url = link["raw_url"]
    if linkparse.is_shopee(url):
        return url
    final = linkparse.resolve_url(url)
    if final is None and drv is not None:
        final = drv.resolve_in_browser(url)
        if final:
            linkparse.cache_resolution(url, final)
    return final


def run_fetch_link(job, item):
    link = db.q1("SELECT * FROM links WHERE id=?", (item["target"],))
    if link is None:
        raise jobs.ItemFailed("link not found")
    link = dict(link)
    drv = shopee.get_driver()

    final = resolve_link(link, drv)
    if final is None:
        db.x("UPDATE links SET status='invalid', status_detail='短網址無法解析', last_checked_at=? WHERE id=?",
             (db.now(), link["id"]))
        return {"outcome": "invalid", "detail": "短網址無法解析"}
    if not linkparse.is_shopee(final):
        detail = f"非蝦皮連結: {final[:120]}"
        db.x("UPDATE links SET status='invalid', status_detail=?, last_checked_at=? WHERE id=?",
             (detail, db.now(), link["id"]))
        return {"outcome": "invalid", "detail": detail, "final": final}

    ids = linkparse.parse_shopee_url(final)
    if not ids:
        db.x("UPDATE links SET status='invalid', status_detail='URL 無法解析出商品 ID', last_checked_at=? WHERE id=?",
             (db.now(), link["id"]))
        return {"outcome": "invalid", "detail": "no ids", "final": final}
    db.x("UPDATE links SET shopid=?, itemid=?, model_id=COALESCE(?, model_id), dedupe_key=?, canonical_url=? WHERE id=?",
         (ids["shopid"], ids["itemid"], ids["model_id"],
          linkparse.dedupe_key(ids["shopid"], ids["itemid"]),
          linkparse.canonical_url(ids["shopid"], ids["itemid"], ids["model_id"]), link["id"]))

    parsed, raw = drv.get_pdp(final)
    if raw is not None:
        _save_raw(link["id"], raw)

    result = {"outcome": None, "ids": ids, "exists": parsed.get("exists", False),
              "reason": parsed.get("reason"), "unlisted": parsed.get("unlisted", False),
              "title": parsed.get("title"), "shop_name": parsed.get("shop_name"),
              "images": (parsed.get("images") or [])[:1]}
    if not parsed.get("exists"):
        result["outcome"] = "invalid"
        return result
    # 地區/銷量回填（UI 正式連結區欄位）；PDP 抓不到就保留舊值
    db.x("UPDATE links SET shop_location=COALESCE(NULLIF(?,''), shop_location), "
         "sold=COALESCE(?, sold) WHERE id=?",
         (parsed.get("shop_location") or "", parsed.get("historical_sold"), link["id"]))

    model, how = match_variant(link["variant_text"], parsed["models"], ids["model_id"])
    if model is None:
        result["outcome"] = "variant_error"
        result["detail"] = how
        return result
    result.update(outcome="ok", match_how=how, model_id=model["model_id"],
                  model_name=model["name"], price_idr=model["price_idr"],
                  stock=model["stock"], in_stock=model.get("in_stock"),
                  model_image=model.get("image"))
    return result


# --------------------------------------------------------------- classify --

def effective_baseline(code):
    """客戶 1.5：基準價 = products.baseline_idr（一次性定植於
    sheets.seed_baselines，程式永不自動變動，僅人工在 UI 修改）。
    未定植（NULL）時回傳 None → 不判 high_cost。"""
    prod = db.q1("SELECT baseline_idr FROM products WHERE code=?", (code,))
    return prod["baseline_idr"] if prod else None


def high_cost_pct(code):
    prod = db.q1("SELECT high_cost_pct FROM products WHERE code=?", (code,))
    if prod and prod["high_cost_pct"] is not None:
        return prod["high_cost_pct"]
    return float(cfg.get("high_cost_pct"))


def run_classify_product(job, item):
    code = item["target"]
    links = [dict(r) for r in db.q(
        "SELECT * FROM links WHERE product_code=? AND active=1", (code,))]
    fetches = {}
    for r in db.q("SELECT target, state, result_json FROM job_items WHERE job_id=? AND kind='fetch_link'",
                  (job["id"],)):
        fetches[r["target"]] = (r["state"], db.jload(r["result_json"], {}))

    fresh = {}       # link_id -> fetch result
    for lk in links:
        st_res = fetches.get(str(lk["id"]))
        if st_res:
            fresh[lk["id"]] = st_res

    baseline = effective_baseline(code)
    pct = high_cost_pct(code)
    threshold = baseline * (1 + pct / 100.0) if baseline else None

    summary = {"baseline": baseline, "pct": pct, "links": {}}
    for lk in links:
        got = fresh.get(lk["id"])
        if got is None:
            continue
        st, res = got
        if st == "failed":
            status, detail = "error", res.get("error", "fetch failed")
        else:
            outcome = res.get("outcome")
            if outcome == "invalid":
                status, detail = "invalid", res.get("reason") or res.get("detail") or ""
            elif outcome == "variant_error":
                status, detail = "error", res.get("detail", "variant not matched")
            elif outcome == "ok":
                price = res.get("price_idr")
                if res.get("unlisted"):
                    status, detail = "unlisted", ""
                elif res.get("in_stock") is False:
                    # only flag sold_out when Shopee explicitly says unavailable;
                    # None(unknown) is treated as available so we never false-flag
                    status, detail = "sold_out", ""
                elif price and threshold and price > threshold:
                    status = "high_cost"
                    detail = f"{price} > 基準 {baseline} +{pct}%"
                else:
                    status, detail = "valid", res.get("match_how", "")
            else:
                status, detail = "error", f"unknown outcome {outcome}"

        price = res.get("price_idr")
        # 前次/最新價滾動（客戶 2.3）：本次有抓到價才滾動；首次檢查時
        # last_price 為 NULL → 前次價自動落到表上 E 欄價
        db.x("UPDATE links SET status=?, status_detail=?, "
             "prev_price_idr=CASE WHEN ? IS NOT NULL THEN COALESCE(last_price_idr, price_idr) "
             "  ELSE prev_price_idr END, "
             "last_price_idr=COALESCE(?, last_price_idr), "
             "last_checked_at=? WHERE id=?",
             (status, detail, price, price, db.now(), lk["id"]))
        summary["links"][lk["id"]] = {"status": status, "price": price}

        # ---- pending writes ------------------------------------------------
        if price and price != lk["price_idr"] and status in ("valid", "sold_out", "unlisted", "high_cost"):
            _queue_write(job["id"], "update_price", lk, {"price_idr": price, "raw_url": lk["raw_url"]})
        target_note = NOTE_FOR_STATUS.get(status)
        if target_note is not None:
            current = (lk["note"] or "").strip()
            from .sheets import is_machine_note
            if target_note == "":
                if current and is_machine_note(current):
                    _queue_write(job["id"], "clear_note", lk, {"raw_url": lk["raw_url"]})
            elif current.lower() != target_note:
                if not current or is_machine_note(current):
                    _queue_write(job["id"], "set_note", lk,
                                 {"note": target_note, "raw_url": lk["raw_url"]})
                else:
                    db.x("UPDATE links SET status_detail=? WHERE id=?",
                         (f"note conflict：人工備註「{current}」未覆寫（判定 {target_note}）", lk["id"]))

    # 一鍵流程（客戶 2.4）：check job 帶 include_find=true（或 full_scan 預設）
    # 時，檢查完 valid 不足 3 條就動態追加 find item（缺幾補幾在 finder 內算）
    params = db.jload(job["params_json"], {})
    if params.get("include_find", job["kind"] == "full_scan"):
        n_valid = db.q1("SELECT COUNT(*) AS n FROM links WHERE product_code=? AND active=1 AND status='valid'",
                        (code,))["n"]
        if n_valid < int(cfg.get("target_links_per_product")):
            jobs.add_item(job["id"], "find_links", code)
            summary["find_queued"] = True
    return summary


def _queue_write(job_id, kind, link, payload):
    db.x("""INSERT INTO pending_writes(job_id, kind, product_code, dedupe_key, model_id,
             payload_json, state, created_at) VALUES(?,?,?,?,?,?, 'pending', ?)""",
         (job_id, kind, link["product_code"], link["dedupe_key"] or "", link["model_id"],
          db.jdump(payload), db.now()))

"""Replacement-link finder: keyword build → tiered located search → image
gate → rank (price first, sold tiebreak, sold<100 excluded) → PDP confirm →
propose candidates for review.
"""
import json
import logging
import re

from . import checker
from . import config as cfg
from . import db
from . import imagesim
from . import jobs
from . import linkparse
from . import shopee

log = logging.getLogger(__name__)

SIZE_RE = re.compile(r"\d+\s*(?:ml|gr|g|gram|pcs|pc|sachet|l)\b", re.I)

# Keyword-sanity bar (separate from candidate inclusion). Calibrated on real
# KBT105 #1 data: a correct keyword yields several ≥0.78 near-identical dHash
# hits in the top page; a wrong keyword yields at most one.
KEYWORD_CONFIRM_SIM = 0.78
KEYWORD_CONFIRM_MIN = 2

# Hard cap on PDP-confirm navigations per find run — bounds worst-case runtime
# (each PDP is paced 3–8s). A product that can't be filled stops instead of
# grinding through every location tier.
MAX_PDP_CONFIRMS = 8


def expand_find(params):
    codes = params.get("product_codes") or []
    return [{"kind": "find_links", "target": c} for c in codes]


# ------------------------------------------------------------- keywords ----

def _tokens(text):
    t = re.sub(r"\[[^\]]*\]", " ", text or "")        # [BPOM] style tags
    t = t.split("|")[0]                                # size lists after pipes
    t = re.sub(r"[^\w\s一-鿿+&.-]", " ", t)            # emoji / decorations
    t = re.sub(r"\s+", " ", t).strip()
    seen, out = set(), []
    for tok in t.split(" "):
        if not tok:
            continue
        key = tok.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(tok)
    return out


def build_keyword(title, variant_text, attempt=1, brand=None):
    """Brand + leading product words + size. The brand is critical: Shopee
    titles often bury it inside a bracketed tag ("[SKINTIFIC CERTIFIED] …")
    that _tokens strips, and without it the search returns unrelated goods."""
    toks = [t for t in _tokens(title) if not SIZE_RE.fullmatch(t)]
    n = 6 if attempt == 1 else 4
    base = toks[:n]
    size = None
    m = SIZE_RE.search((variant_text or "").replace(" ", ""))
    if m:
        size = m.group(0)
    else:
        m = SIZE_RE.search(title or "")
        if m:
            size = m.group(0)
    parts = base
    if brand and brand.strip():
        low = [t.lower() for t in base[:2]]
        if brand.strip().lower() not in low:
            parts = [brand.strip()] + base
    kw = " ".join(parts)
    if size:
        kw = f"{kw} {size}"
    return kw.strip()


def _locations_for_tier(tier):
    """Build a {"param": ..., "value": ...} location filter for this tier's
    place names, in whichever format record_locations() actually observed
    the live page using (Shopee has shipped at least two encodings)."""
    if not tier:
        return None
    recorded = cfg.get("search_locations_param") or {}
    param = recorded.get("param") if isinstance(recorded, dict) else None
    if param == "fe_filter_options":
        value = json.dumps([{"group_name": "LOCATIONS", "values": list(tier)}], separators=(",", ":"))
    else:
        # default / "locations" format: plain comma-separated place names
        value = ",".join(tier)
        param = param or "locations"
    return {"param": param, "value": value}


def _location_ok(shop_location, tier):
    if not tier:
        return True
    loc = (shop_location or "").lower()
    return any(t.lower() in loc or loc in t.lower() for t in tier if t)


# ------------------------------------------------------------- pipeline ----

def _source_link(code):
    """Best row to extract keyword/image from: valid first, then recoverable."""
    order = {"valid": 0, "high_cost": 1, "sold_out": 2, "unlisted": 3}
    rows = [dict(r) for r in db.q(
        "SELECT * FROM links WHERE product_code=? AND active=1", (code,))]
    usable = [r for r in rows if r["status"] in order and r["dedupe_key"]]
    usable.sort(key=lambda r: order[r["status"]])
    if usable:
        return usable[0], False
    # keyword-only mode: any row with C/D text
    texty = [r for r in rows if (r["page_name"] or "").strip()]
    return (texty[0] if texty else None), True


def run_find_links(job, item):
    code = item["target"]
    target_n = int(cfg.get("target_links_per_product"))
    min_sold = int(cfg.get("min_sold"))
    sim_threshold = float(db.setting_get("image_sim_calibrated") or cfg.get("image_sim_threshold"))
    drv = shopee.get_driver()

    n_valid = db.q1("SELECT COUNT(*) AS n FROM links WHERE product_code=? AND active=1 AND status='valid'",
                    (code,))["n"]
    already = db.q1("SELECT COUNT(*) AS n FROM candidates WHERE product_code=? AND state IN ('proposed','accepted')",
                    (code,))["n"]
    need = target_n - n_valid - already
    if need <= 0:
        return {"skipped": f"已有 {n_valid} 個有效連結 + {already} 個候選"}

    src, keyword_only = _source_link(code)
    if src is None:
        raise jobs.ItemFailed("此商品沒有可作為來源的連結或商品名稱")

    # --- reference data from the source link's PDP ------------------------
    ref_hashes, src_title, src_variant, src_brand = [], src["page_name"], src["variant_text"], None
    if not keyword_only:
        parsed, _ = drv.get_pdp(src["canonical_url"] or src["raw_url"])
        if parsed.get("exists"):
            src_title = parsed.get("title") or src_title
            src_brand = parsed.get("brand")
            model, _how = checker.match_variant(src["variant_text"], parsed["models"], src["model_id"])
            # Reference = the SELECTED variant's own image (from tier_variations,
            # verified to map correctly) PLUS the item cover(s). Candidate search
            # thumbnails are covers, so the cover is what usually matches; the
            # variant image guarantees we still represent OUR exact variant (and
            # covers the case where the cover is a different variant or null).
            # best_similarity takes the MAX, and exact-variant correctness is
            # separately enforced at PDP confirm below.
            img_ids = []
            if model and model.get("image"):
                img_ids.append(model["image"])
            for im in (parsed.get("images") or [])[:2]:
                if im and im not in img_ids:
                    img_ids.append(im)
            for iid in img_ids[:3]:
                path = drv.fetch_image(iid)
                if path:
                    ref_hashes += [imagesim.dhash(path), imagesim.dhash_mirrored(path)]
        else:
            keyword_only = True

    # dedupe only against links CURRENTLY in the sheet (active=1) — a link the
    # user removed from the sheet should be findable again (SPEC: 與現有連結不重複)
    existing = {r["dedupe_key"] for r in db.q(
        "SELECT dedupe_key FROM links WHERE product_code=? AND dedupe_key!='' AND active=1", (code,))}
    seen_cand = {f'{r["shopid"]}.{r["itemid"]}' for r in db.q(
        "SELECT shopid, itemid FROM candidates WHERE product_code=?", (code,))}

    summary = {"attempts": [], "proposed": 0, "keyword_only": keyword_only}
    proposed = 0
    pdp_confirms = 0        # bound total PDP navigations so a run can't drag on

    for attempt in (1, 2):
        kw = build_keyword(src_title, src_variant, attempt, brand=src_brand)
        if not kw:
            continue
        attempt_log = {"keyword": kw, "tiers": []}
        summary["attempts"].append(attempt_log)
        keyword_bad = False

        for tier_idx, tier in enumerate(cfg.get("location_tiers")):
            results, _raw = drv.search(kw, _locations_for_tier(tier))
            tier_log = {"tier": tier_idx, "results": len(results), "passed": 0}
            attempt_log["tiers"].append(tier_log)
            if not results:
                continue

            # if the locations param didn't take effect, post-filter instead
            if tier and results and not any(_location_ok(r["shop_location"], tier) for r in results):
                tier_log["note"] = "location filter ineffective"
            pool = [r for r in results if _location_ok(r["shop_location"], tier)] if tier else results

            # Two separate bars (calibrated on real KBT105 #1 data):
            #  - inclusion (sim_threshold): keep a candidate for ranking; loose so
            #    cheap genuine matches aren't dropped.
            #  - confirm (KEYWORD_CONFIRM_SIM): "are we even looking at the right
            #    product?" strict, needs a few near-identical hits in the top page;
            #    unrelated goods rarely score this high, so this catches bad keywords.
            gated = []
            confirm_hits = 0
            for idx, r in enumerate(pool):
                sim = None
                if ref_hashes and r.get("image"):
                    path = drv.fetch_image(r["image"])
                    sim = imagesim.best_similarity(ref_hashes, imagesim.dhash(path)) if path else None
                r["sim"] = sim
                if ref_hashes:
                    if idx < 12 and sim is not None and sim >= KEYWORD_CONFIRM_SIM:
                        confirm_hits += 1
                    if sim is not None and sim >= sim_threshold:
                        gated.append(r)
                else:
                    gated.append(r)
            if ref_hashes and confirm_hits < KEYWORD_CONFIRM_MIN and len(pool) >= 5:
                # keyword likely wrong (SPEC: 相似度不足代表關鍵字錯誤)
                keyword_bad = True
                tier_log["note"] = (f"keyword check: only {confirm_hits} near-identical "
                                    f"(≥{KEYWORD_CONFIRM_SIM}) in top 12")
                break

            cands = [r for r in gated
                     if f'{r["shopid"]}.{r["itemid"]}' not in existing
                     and f'{r["shopid"]}.{r["itemid"]}' not in seen_cand
                     and (r["sold"] or 0) >= min_sold
                     and r.get("price_idr")
                     and not r.get("is_sold_out")]
            cands.sort(key=lambda r: (r["price_idr"], -(r["sold"] or 0)))
            tier_log["passed"] = len(cands)

            # --- PDP confirm best candidates until quota ------------------
            # Only confirm the cheapest few per tier and cap the run total, so a
            # product that can't be filled doesn't navigate dozens of PDPs.
            for r in cands[:max(need * 3, 4)]:
                if proposed >= need or pdp_confirms >= MAX_PDP_CONFIRMS:
                    break
                url = linkparse.canonical_url(r["shopid"], r["itemid"])
                pdp_confirms += 1
                parsed, _ = drv.get_pdp(url)
                if not parsed.get("exists") or parsed.get("unlisted"):
                    continue
                model, how = checker.match_variant(src_variant, parsed["models"], allow_single_model=True)
                if model is None or not model.get("price_idr") or model.get("in_stock") is False:
                    continue
                # D 欄：只有 1 個 model = 沒有可選規格 → "-"（SPEC：無選項顯示 -）；
                # 內部 model 名（如空字串或「SKINTIFIC EYE CREAM」）不是使用者可選項目
                n_models = len(parsed.get("models") or [])
                variant_for_d = model["name"] if (n_models > 1 and (model.get("name") or "").strip()) else "-"
                db.x("""INSERT INTO candidates(product_code, shopid, itemid, model_id, title,
                         variant_text, price_idr, sold, shop_name, shop_location, image_sim,
                         tier, state, note, created_at)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'proposed',?,?)""",
                     (code, r["shopid"], r["itemid"], model["model_id"] if n_models > 1 else None,
                      parsed.get("title") or r["title"], variant_for_d, model["price_idr"],
                      r["sold"], parsed.get("shop_name") or "", r["shop_location"],
                      r.get("sim"), tier_idx,
                      "需人工確認（無圖片驗證）" if keyword_only else "", db.now()))
                seen_cand.add(f'{r["shopid"]}.{r["itemid"]}')
                proposed += 1

            if proposed >= need or pdp_confirms >= MAX_PDP_CONFIRMS:
                break
        if proposed >= need or pdp_confirms >= MAX_PDP_CONFIRMS or not keyword_bad:
            break

    summary["proposed"] = proposed
    summary["pdp_confirms"] = pdp_confirms
    if proposed == 0:
        db.x("UPDATE products SET updated_at=? WHERE code=?", (db.now(), code))
        summary["note"] = ("關鍵字比對失敗，需人工設定關鍵字" if keyword_bad
                           else "第一頁無合格結果")
    if cfg.get("auto_accept_candidates"):
        for r in db.q("SELECT id FROM candidates WHERE product_code=? AND state='proposed'", (code,)):
            accept_candidate(r["id"], job_id=job["id"])
    return summary


def accept_candidate(cand_id, job_id=None):
    c = db.q1("SELECT * FROM candidates WHERE id=?", (cand_id,))
    if c is None or c["state"] not in ("proposed", "accepted"):
        return None
    url = linkparse.canonical_url(c["shopid"], c["itemid"], c["model_id"])
    payload = {
        "url": url, "product_code": c["product_code"],
        "page_name": c["title"], "variant_text": c["variant_text"],
        "price_idr": c["price_idr"], "supplier": c["shop_name"],
        "shopid": c["shopid"], "itemid": c["itemid"], "candidate_id": c["id"],
    }
    cur = db.x("""INSERT INTO pending_writes(job_id, kind, product_code, dedupe_key, model_id,
                   payload_json, state, created_at)
                  VALUES(?, 'insert_link_row', ?, ?, ?, ?, 'pending', ?)""",
               (job_id, c["product_code"], linkparse.dedupe_key(c["shopid"], c["itemid"]),
                c["model_id"], db.jdump(payload), db.now()))
    db.x("UPDATE candidates SET state='accepted' WHERE id=?", (cand_id,))
    return cur.lastrowid


def reject_candidate(cand_id):
    db.x("UPDATE candidates SET state='rejected' WHERE id=?", (cand_id,))

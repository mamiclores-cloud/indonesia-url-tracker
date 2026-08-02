"""Replacement-link finder: keyword build → tiered located search → image
gate → rank (price first, sold tiebreak, sold<100 excluded) → PDP confirm →
propose candidates for review.
"""
import difflib
import json
import logging
import re
import time

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
# ...or this many title-matching results in the top page confirm the keyword —
# more reliable than image similarity for multi-variant/scent product lines.
KEYWORD_TITLE_MIN = 3

# Hard cap on PDP-confirm navigations per find run — bounds worst-case runtime
# (each PDP is paced 3–8s). A product that can't be filled stops instead of
# grinding through every location tier.
MAX_PDP_CONFIRMS = 15


def expand_find(params):
    codes = params.get("product_codes") or []
    return [{"kind": "find_links", "target": c} for c in codes]


# ------------------------------------------------------------- keywords ----

def _tokens(text):
    t = re.sub(r"\[[^\]]*\]", " ", text or "")        # [BPOM] style tags
    t = re.sub(r"(\d+)\s+(ml|gr|g|gram|pcs|pc|sachet|l)\b", r"\1\2", t, flags=re.I)
    # Text after a pipe is usually a size list, but some sheet rows lead with the
    # shop name instead ("Serafina♣ | [SCARLET] WHITENING BODY SCRUB …") — cutting
    # there would leave the keyword with no product words at all.
    head = t.split("|")[0]
    if len(head.split()) >= 3:
        t = head
    t = re.sub(r"[^\w\s一-鿿+&.-]", " ", t)            # emoji / decorations
    t = re.sub(r"\s+", " ", t).strip()
    seen, out = set(), []
    for tok in t.split(" "):
        if not tok or not re.search(r"\w", tok):   # drop pure punctuation ("-")
            continue
        key = tok.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(tok)
    return out


def _spec_tokens(variant_text, brand, base, limit=2):
    """D 欄的「選項規格」詞（客戶 1.2：品牌＋選項規格＋容量）。

    同系列商品的 C 欄往往完全相同（九款 Fresh Care 香味共用一個標題），
    分辨它們的唯一線索在 D 欄：sandalwood / lavender / citrus。沒有這些詞，
    九個商品會用同一組關鍵字搜尋，找回來的必然是錯的香味。

    排除四類雜訊：容量（另外附加）、長度<4 的縮寫（SC / SCT / FW）、品牌或
    其縮寫（FCare→FreshCare 相似度 0.71）、以及標題開頭已有的詞。取最後
    limit 個——規格詞在 D 欄通常押後（「Minyak Angin Citrus」的 Citrus、
    「SC Sabun Charming」的 Charming）。
    """
    bnorm = checker.normalize_variant(brand).replace(" ", "") if brand else ""
    base_norm = [checker.normalize_variant(t) for t in base]
    out, seen = [], set()
    for tok in re.split(r"[^\w一-鿿]+", variant_text or ""):
        if len(tok) < 4 or SIZE_RE.fullmatch(tok):
            continue
        n = checker.normalize_variant(tok)
        if not n or n in seen:
            continue
        if bnorm and (n in bnorm or bnorm in n
                      or difflib.SequenceMatcher(None, n, bnorm).ratio() >= 0.6):
            continue
        # 標題開頭已有的詞（含縮寫變體，如標題的 Care 對上 D 欄的 FCare）
        if any(b == n or difflib.SequenceMatcher(None, n, b).ratio() >= 0.8
               for b in base_norm):
            continue
        seen.add(n)
        out.append(tok)
    return out[-limit:]


def build_keyword(title, variant_text, attempt=1, brand=None):
    """Brand + leading product words + size（客戶 1.2：品牌＋商品名＋容量）。
    The brand is critical: Shopee titles often bury it inside a bracketed tag
    ("[SKINTIFIC CERTIFIED] …") that _tokens strips, and without it the search
    returns unrelated goods."""
    toks = [t for t in _tokens(title) if not SIZE_RE.fullmatch(t)]
    # C 欄常見「賣家名 - 商品名」前綴；已知品牌時，從品牌出現處起算，
    # 把前面的賣家名裁掉（客戶 1.2：應先判斷品牌）
    if brand and brand.strip():
        b = brand.strip().lower()
        for i, t in enumerate(toks):
            if t.lower() == b:
                if i:
                    toks = toks[i:]
                break
    n = 6 if attempt == 1 else 4
    base = toks[:n]
    size = None
    m = SIZE_RE.search(variant_text or "")
    if m:
        size = m.group(0)
    else:
        m = SIZE_RE.search(title or "")
        if m:
            size = m.group(0)
    parts = base + _spec_tokens(variant_text, brand, base)
    if brand and brand.strip():
        # Compare space-stripped so a multi-word brand ("Fresh Care") isn't
        # prepended again onto a title that already starts with it.
        bkey = checker.normalize_variant(brand).replace(" ", "")
        head = checker.normalize_variant(" ".join(base[:3])).replace(" ", "")
        if bkey and not head.startswith(bkey):
            parts = [brand.strip()] + parts
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


def _confirm_rank(r):
    """PDP-confirm order（客戶 1.1）：純價格由低到高、同價銷量高者先，
    依序開商品頁確認規格。相似度只作納入門檻，不參與排序。"""
    return (r["price_idr"], -(r["sold"] or 0))


def _product_tokens(keyword, brand):
    """Distinctive (len≥4, non-brand, non-size) product words from the keyword.
    Used to reject same-brand-different-product results that the image gate
    can't tell apart (e.g. Fresh Care 'Aromatherapy Roll On' vs 'Press & Relax').

    Brand exclusion is fragment-aware: a brand written 'FRESHCARE' still drops
    the 'fresh'/'care' tokens that a split 'Fresh Care' title yields."""
    bnorm = checker.normalize_variant(brand).replace(" ", "") if brand else ""
    out = []
    for t in checker.normalize_variant(keyword).split():
        if len(t) < 4 or SIZE_RE.fullmatch(t):
            continue
        if bnorm and (t in bnorm or bnorm in t):   # brand or a fragment of it
            continue
        out.append(t)
    return out


def _title_has_product(title, ptokens):
    """True if the candidate title shares at least one distinctive product word.
    Lenient (≥1) so terse genuine titles still pass, but enough to drop a
    sibling product that shares only the brand name. Matches per word-token
    (a product token may be a substring of a title token, e.g. roll⊂rollon)."""
    if not ptokens:
        return True
    title_tokens = checker.normalize_variant(title).split()
    return any(any(pt in tt for tt in title_tokens) for pt in ptokens)


# ------------------------------------------------------------- pipeline ----

def _source_link(code):
    """Best row to extract keyword/image from.

    Prefer ORIGINAL sheet links over auto-found ones: the human-curated sheet
    row is the authoritative identity of the product, whereas a prior bad
    auto-find (e.g. a keyword-only match with no image check) could seed the
    keyword for the WRONG product and cascade. Within each origin, prefer the
    healthiest status.
    """
    order = {"valid": 0, "high_cost": 1, "sold_out": 2, "unlisted": 3}
    rows = [dict(r) for r in db.q(
        "SELECT * FROM links WHERE product_code=? AND active=1", (code,))]
    usable = [r for r in rows if r["status"] in order and r["dedupe_key"]]
    usable.sort(key=lambda r: (0 if r["origin"] == "sheet" else 1, order[r["status"]]))
    if usable:
        return usable[0], False
    # keyword-only mode: any ORIGINAL sheet row with C/D text (avoid seeding
    # from a found row); fall back to any text row if none.
    texty = [r for r in rows if (r["page_name"] or "").strip()]
    texty.sort(key=lambda r: 0 if r["origin"] == "sheet" else 1)
    return (texty[0] if texty else None), True


def run_find_links(job, item):
    code = item["target"]
    target_n = int(cfg.get("target_links_per_product"))
    min_sold = int(cfg.get("min_sold"))
    sim_threshold = float(db.setting_get("image_sim_calibrated") or cfg.get("image_sim_threshold"))
    drv = shopee.get_driver()

    deadline = time.monotonic() + float(cfg.get("find_time_budget_s") or 0 or 1e9)

    n_valid = db.q1("SELECT COUNT(*) AS n FROM links WHERE product_code=? AND active=1 AND status='valid'",
                    (code,))["n"]
    # 客戶 Q&A：缺幾補幾。殘留的舊 proposed 候選會佔用名額（造成「缺 1 找 3」
    # 的假象），先全部標 expired；只有已 accept 未寫入的仍算在途數量。
    db.x("UPDATE candidates SET state='expired' WHERE product_code=? AND state='proposed'", (code,))
    # 孤兒 accepted（對應的 insert 寫入已被丟棄/失敗/寫完）不再佔名額，
    # 否則 need 會被永久壓縮成「缺 2 只找 1」。寫完的由 links 鏡射列接手擋重複。
    db.x("""UPDATE candidates SET state='expired'
            WHERE product_code=? AND state='accepted'
              AND NOT EXISTS (SELECT 1 FROM pending_writes w
                              WHERE w.kind='insert_link_row' AND w.state='pending'
                                AND w.product_code=candidates.product_code
                                AND w.dedupe_key = candidates.shopid || '.' || candidates.itemid)""",
         (code,))
    already = db.q1("SELECT COUNT(*) AS n FROM candidates WHERE product_code=? AND state='accepted'",
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
            # 客戶 1.2：關鍵字以表上 C 欄文字為基底（人工整理過的商品名）；
            # PDP title 只在 C 欄空白時補位，brand 仍取自 PDP
            if not (src_title or "").strip():
                src_title = parsed.get("title") or ""
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

    if not src_brand:
        # D 欄逗號尾段常是品牌（例「LOW PH 80ml, Skintific」→ Skintific）。
        # 僅接受單一字的尾段——多字尾段（如「CLEANSER Skintific」）多半是規格
        parts = [p.strip() for p in (src_variant or "").split(",") if p.strip()]
        if len(parts) >= 2 and len(parts[-1].split()) == 1 and not SIZE_RE.search(parts[-1]):
            src_brand = parts[-1]

    # dedupe only against links CURRENTLY in the sheet (active=1) — a link the
    # user removed from the sheet should be findable again (SPEC: 與現有連結不重複)
    existing = {r["dedupe_key"] for r in db.q(
        "SELECT dedupe_key FROM links WHERE product_code=? AND dedupe_key!='' AND active=1", (code,))}
    # expired（僅是過期候選）可重新提案；rejected 是人工否決、仍要擋
    seen_cand = {f'{r["shopid"]}.{r["itemid"]}' for r in db.q(
        "SELECT shopid, itemid FROM candidates WHERE product_code=? AND state!='expired'", (code,))}

    # 0802：補找只收「補上去就會是 valid」的連結——價格超過 high cost 門檻的
    # 候選寫進表後下次檢查必然變 high_cost 掉回備選區（實測占已採用候選 31%）。
    # 搜尋清單價是該賣場最低規格價，是實際規格價的下界，連它都超標就一定超標。
    baseline = checker.effective_baseline(code)
    pct = checker.high_cost_pct(code)
    price_cap = baseline * (1 + pct / 100.0) if baseline else None

    summary = {"attempts": [], "proposed": 0, "keyword_only": keyword_only,
               "price_cap": int(price_cap) if price_cap else None}
    proposed = 0
    pdp_confirms = 0        # bound total PDP navigations so a run can't drag on
    over_cap = 0            # 因價格超過門檻而略過的候選數（觀察用）

    for attempt in (1, 2):
        if time.monotonic() > deadline:
            summary["time_budget_hit"] = True
            break
        kw = build_keyword(src_title, src_variant, attempt, brand=src_brand)
        if not kw:
            continue
        # 記錄最後實際用於搜尋的關鍵字（客戶 1.2 驗證用；找不到連結時也看得到）
        db.x("UPDATE products SET search_keyword=?, search_keyword_at=? WHERE code=?",
             (kw, db.now(), code))
        # distinctive tokens from the cleaned keyword, NOT the raw title (the raw
        # title carries generic words like "minyak angin" that sibling products
        # also have, which would let them slip through)
        ptokens = _product_tokens(kw, src_brand)
        attempt_log = {"keyword": kw, "tiers": []}
        summary["attempts"].append(attempt_log)
        keyword_bad = False

        for tier_idx, tier in enumerate(cfg.get("location_tiers")):
            if time.monotonic() > deadline:
                summary["time_budget_hit"] = True
                break
            results, _raw = drv.search(kw, _locations_for_tier(tier))
            tier_log = {"tier": tier_idx, "results": len(results), "passed": 0}
            attempt_log["tiers"].append(tier_log)
            if not results:
                continue

            # if the locations param didn't take effect, post-filter instead
            if tier and results and not any(_location_ok(r["shop_location"], tier) for r in results):
                tier_log["note"] = "location filter ineffective"
            pool = [r for r in results if _location_ok(r["shop_location"], tier)] if tier else results

            # Product identity uses TWO signals; either one confirms the keyword
            # is finding our product (image alone is unreliable for multi-scent
            # lines whose variants differ from our reference photo):
            #  - title gate: candidate title carries a distinctive product word
            #    (stable, brand-siblings like Press&Relax fail it)
            #  - image band (KEYWORD_CONFIRM_SIM): near-identical photo in top page
            # A candidate is included only if it passes the title gate AND clears
            # the (loose) image inclusion threshold; ranking then prefers the
            # higher-similarity, cheaper ones.
            gated = []
            confirm_hits = 0
            title_hits = 0
            for idx, r in enumerate(pool):
                title_ok = _title_has_product(r["title"], ptokens)
                if idx < 12 and title_ok:
                    title_hits += 1
                sim = None
                if ref_hashes and r.get("image"):
                    path = drv.fetch_image(r["image"])
                    sim = imagesim.best_similarity(ref_hashes, imagesim.dhash(path)) if path else None
                r["sim"] = sim
                if idx < 12 and sim is not None and sim >= KEYWORD_CONFIRM_SIM:
                    confirm_hits += 1
                if title_ok and (not ref_hashes or (sim is not None and sim >= sim_threshold)):
                    gated.append(r)

            keyword_ok = confirm_hits >= KEYWORD_CONFIRM_MIN or title_hits >= KEYWORD_TITLE_MIN
            if not keyword_ok and len(pool) >= 5:
                # search isn't returning our product (bad/too-generic keyword)
                keyword_bad = True
                tier_log["note"] = (f"keyword check failed: image≥{KEYWORD_CONFIRM_SIM} "
                                    f"hits={confirm_hits}, title hits={title_hits}")
                break

            cands = [r for r in gated
                     if f'{r["shopid"]}.{r["itemid"]}' not in existing
                     and f'{r["shopid"]}.{r["itemid"]}' not in seen_cand
                     and (r["sold"] or 0) >= min_sold
                     and r.get("price_idr")
                     and not r.get("is_sold_out")]
            if price_cap:
                keep = [r for r in cands if r["price_idr"] <= price_cap]
                over_cap += len(cands) - len(keep)
                cands = keep
            # Confirm order: by similarity band first (most-likely-genuine),
            # cheapest within each band. A low inclusion threshold otherwise
            # floods the pool with loose matches and the cheapest-first rule
            # burns the PDP-confirm budget on junk before reaching real matches.
            cands.sort(key=_confirm_rank)
            tier_log["passed"] = len(cands)

            # --- PDP confirm best candidates until quota ------------------
            # Only confirm the cheapest few per tier and cap the run total, so a
            # product that can't be filled doesn't navigate dozens of PDPs.
            for r in cands[:max(need * 3, 4)]:
                if proposed >= need or pdp_confirms >= MAX_PDP_CONFIRMS:
                    break
                if time.monotonic() > deadline:
                    summary["time_budget_hit"] = True
                    break
                url = linkparse.canonical_url(r["shopid"], r["itemid"])
                pdp_confirms += 1
                parsed, _ = drv.get_pdp(url)
                if not parsed.get("exists") or parsed.get("unlisted"):
                    continue
                model, how = checker.match_variant(src_variant, parsed["models"], allow_single_model=True)
                if model is None or not model.get("price_idr") or model.get("in_stock") is False:
                    continue
                # 實際選到的規格價才是會回填 E 欄的價；超過門檻就不採用
                if price_cap and model["price_idr"] > price_cap:
                    over_cap += 1
                    continue
                # D 欄：只有 1 個 model = 沒有可選規格 → "-"（SPEC：無選項顯示 -）；
                # 內部 model 名（如空字串或「SKINTIFIC EYE CREAM」）不是使用者可選項目
                n_models = len(parsed.get("models") or [])
                variant_for_d = model["name"] if (n_models > 1 and (model.get("name") or "").strip()) else "-"
                db.x("""INSERT INTO candidates(product_code, shopid, itemid, model_id, title,
                         variant_text, price_idr, sold, shop_name, shop_location, image_sim,
                         tier, state, note, search_keyword, created_at)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'proposed',?,?,?)""",
                     (code, r["shopid"], r["itemid"], model["model_id"] if n_models > 1 else None,
                      parsed.get("title") or r["title"], variant_for_d, model["price_idr"],
                      r["sold"], parsed.get("shop_name") or "", r["shop_location"],
                      r.get("sim"), tier_idx,
                      "需人工確認（無圖片驗證）" if keyword_only else "", kw, db.now()))
                seen_cand.add(f'{r["shopid"]}.{r["itemid"]}')
                proposed += 1

            if proposed >= need or pdp_confirms >= MAX_PDP_CONFIRMS:
                break
        if proposed >= need or pdp_confirms >= MAX_PDP_CONFIRMS or not keyword_bad:
            break

    summary["proposed"] = proposed
    summary["pdp_confirms"] = pdp_confirms
    summary["over_price_cap"] = over_cap
    if proposed == 0:
        db.x("UPDATE products SET updated_at=? WHERE code=?", (db.now(), code))
        if summary.get("time_budget_hit"):
            summary["note"] = f"達單商品時間上限（{cfg.get('find_time_budget_s')} 秒），下次執行再補"
        elif over_cap and not keyword_bad:
            summary["note"] = f"第一頁結果都超過 high cost 門檻（略過 {over_cap} 個）"
        else:
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
        "shop_location": c["shop_location"], "sold": c["sold"],
        "search_keyword": c["search_keyword"],
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

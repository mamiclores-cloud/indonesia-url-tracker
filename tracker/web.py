import logging
import os
import threading

from flask import Blueprint, jsonify, redirect, render_template, request

from . import config as cfg
from . import db
from . import jobs as jobs_mod

log = logging.getLogger(__name__)
bp = Blueprint("web", __name__)

_sync_state = {"running": False, "error": None, "result": None}


# ------------------------------------------------------------------ pages --

@bp.get("/")
def index():
    return redirect("/products")


PRODUCT_SQL = """
SELECT p.code, p.item_name, p.baseline_idr, p.high_cost_pct,
  COUNT(l.id) AS n_links,
  COALESCE(SUM(CASE WHEN l.status='valid' THEN 1 ELSE 0 END),0) AS n_valid,
  COALESCE(SUM(CASE WHEN l.status IN ('invalid','sold_out','unlisted','variant_error','high_cost') THEN 1 ELSE 0 END),0) AS n_bad,
  COALESCE(SUM(CASE WHEN l.status='error' THEN 1 ELSE 0 END),0) AS n_error,
  COALESCE(SUM(CASE WHEN l.status='unchecked' THEN 1 ELSE 0 END),0) AS n_unchecked,
  COALESCE(SUM(CASE WHEN l.status_detail LIKE 'note conflict%' THEN 1 ELSE 0 END),0) AS n_conflict,
  MIN(CASE WHEN l.price_idr>0 THEN l.price_idr END) AS min_price,
  MIN(CASE WHEN l.status='valid' AND COALESCE(l.last_price_idr,l.price_idr)>0
        THEN COALESCE(l.last_price_idr,l.price_idr) END) AS min_valid_price,
  MAX(l.last_checked_at) AS last_checked
FROM products p LEFT JOIN links l ON l.product_code=p.code AND l.active=1
GROUP BY p.code ORDER BY p.code
"""


@bp.get("/products")
def products():
    q = (request.args.get("q") or "").strip().lower()
    flt = request.args.get("filter") or ""
    per = 200
    rows = [dict(r) for r in db.q(PRODUCT_SQL)]
    if q:
        rows = [r for r in rows if q in r["code"].lower() or q in (r["item_name"] or "").lower()]
    if flt == "few_valid":
        target = int(cfg.get("target_links_per_product"))
        rows = [r for r in rows if r["n_valid"] < target]
    elif flt == "conflict":
        rows = [r for r in rows if r["n_conflict"] > 0]
    elif flt == "error":
        rows = [r for r in rows if r["n_error"] > 0]
    elif flt == "unchecked":
        rows = [r for r in rows if r["n_unchecked"] > 0]
    elif flt == "bad":
        rows = [r for r in rows if r["n_bad"] > 0]
    total = len(rows)
    pages, page, off = _paginate(total, per, int(request.args.get("page", 1)))
    rows = rows[off: off + per]
    return render_template("products.html", rows=rows, q=q, flt=flt,
                           page=page, pages=pages, total=total,
                           target=int(cfg.get("target_links_per_product")))


# UI 狀態二值化（客戶 2.1）：畫面只顯示 valid / invalid，細分原因在 Note 與
# status_detail；unchecked 顯示中性標籤。內部 status 細分保留（復活判斷用）。
UI_INVALID_STATUSES = {"invalid", "sold_out", "unlisted", "variant_error", "high_cost", "error"}


def ui_status(link):
    if link["status"] == "valid":
        return "valid"
    if link["status"] in UI_INVALID_STATUSES:
        return "invalid"
    return "unchecked"


def derived_note(link):
    """0801 修正單：Note 欄以「當前判定」為真相來源，不顯示表上 I 欄舊值
    （dry_run 下舊值永遠不更新，會出現 valid 卻掛 high cost 的矛盾）。"""
    from .checker import NOTE_FOR_STATUS
    return NOTE_FOR_STATUS.get(link["status"], "")


def human_note(link):
    """I 欄若是人工備註（非機器詞彙）則保留顯示；機器詞彙一律改由
    derived_note 呈現，避免新舊判定不一致。"""
    from .sheets import is_machine_note
    raw = (link["note"] or "").strip()
    return raw if raw and not is_machine_note(raw) else ""


_PRICE_KEY = lambda l: (l["last_price_idr"] or l["price_idr"] or (1 << 62))


def split_official(links, target=None):
    """0801 修正單：正式連結 = 全部 valid（可超過目標數，例如備選區兩條復活
    後 4 條 valid 就維持 4 條），依最新價升冪；備選區 = 其餘 active 連結
    （invalid 各狀態 + unchecked）。純計算不落地。target 參數保留簽名相容。"""
    act = [l for l in links if l["active"]]
    official = sorted([l for l in act if l["status"] == "valid"], key=_PRICE_KEY)
    backup = [l for l in act if l["status"] != "valid"]
    return official, backup


def pending_candidate_rows(code):
    """已找到但尚未寫入表的候選 → 正式區虛擬列（帶「待寫入表」標籤）。
    dry_run 期間也能看到補找結果與搜尋關鍵字。proposed 一律顯示；accepted
    需仍有 pending 的 insert_link_row 背書（與 finder 的孤兒判定同一條件）。
    寫入完成 state='written' 即消失，由鏡射出的真實列取代。"""
    from . import linkparse
    rows = db.q("""SELECT c.* FROM candidates c WHERE c.product_code=? AND (
                     c.state='proposed' OR (c.state='accepted' AND EXISTS (
                       SELECT 1 FROM pending_writes w
                       WHERE w.kind='insert_link_row' AND w.state='pending'
                         AND w.product_code=c.product_code
                         AND w.dedupe_key = c.shopid || '.' || c.itemid)))""", (code,))
    active_keys = {r["dedupe_key"] for r in db.q(
        "SELECT dedupe_key FROM links WHERE product_code=? AND active=1 AND dedupe_key!=''",
        (code,))}
    out = []
    for c in rows:
        c = dict(c)
        if f"{c['shopid']}.{c['itemid']}" in active_keys:
            continue  # 已寫入並鏡射成真實列，避免雙顯示
        out.append({
            "id": f"cand-{c['id']}", "virtual": True, "sheet_row_hint": None,
            "raw_url": None,
            "canonical_url": linkparse.canonical_url(c["shopid"], c["itemid"], c["model_id"]),
            "page_name": c["title"], "variant_text": c["variant_text"],
            "supplier": c["shop_name"], "shop_location": c["shop_location"],
            "sold": c["sold"], "price_idr": c["price_idr"],
            "prev_price_idr": None, "last_price_idr": c["price_idr"],
            "status": "valid", "ui_status": "valid", "status_detail": "",
            "note": "", "search_keyword": c["search_keyword"],
            "last_checked_at": c["created_at"], "active": 1,
        })
    return out


@bp.get("/product/<path:code>")
def product(code):
    prod = db.q1("SELECT * FROM products WHERE code=?", (code,))
    if prod is None:
        return f"無此商品 {code}", 404
    links = [dict(r) for r in db.q(
        "SELECT * FROM links WHERE product_code=? ORDER BY active DESC, sheet_row_hint", (code,))]
    for l in links:
        l["ui_status"] = ui_status(l)
    target = int(cfg.get("target_links_per_product"))
    official, backup = split_official(links, target)
    virtual = pending_candidate_rows(code)
    n_real = len(official)
    official = sorted(official + virtual, key=_PRICE_KEY)
    for l in official + backup:
        l["ui_note"] = derived_note(l)
        l["human_note"] = human_note(l)
    return render_template("product.html", p=dict(prod), links=links,
                           official=official, backup=backup,
                           n_real=n_real, n_virtual=len(virtual),
                           target=target, global_pct=cfg.get("high_cost_pct"))


def _paginate(total, per, requested):
    """(頁數, 夾在有效範圍內的頁碼, offset)。超出範圍就停在最後一頁，
    不要顯示「第 99 / 6 頁」加一張空表。"""
    pages = (total + per - 1) // per
    page = min(max(1, requested), pages) if pages else 1
    return pages, page, (page - 1) * per


@bp.get("/jobs")
def jobs_page():
    per = 50
    total = db.q1("SELECT COUNT(*) n FROM jobs")["n"]
    pages, page, off = _paginate(total, per, int(request.args.get("page", 1)))
    rows = [dict(r) for r in db.q(
        "SELECT * FROM jobs ORDER BY id DESC LIMIT ? OFFSET ?", (per, off))]
    return render_template("jobs.html", rows=rows, page=page, total=total, pages=pages)


ITEM_KINDS = ("fetch_link", "classify_product", "find_links", "apply_writes")
ITEM_STATES = ("pending", "running", "done", "failed")


@bp.get("/jobs/<int:job_id>")
def job_detail(job_id):
    job = db.q1("SELECT * FROM jobs WHERE id=?", (job_id,))
    if job is None:
        return "無此任務", 404
    per = 200
    kind = request.args.get("kind") or ""
    state = request.args.get("state") or ""
    where, params = ["job_id=?"], [job_id]
    if kind in ITEM_KINDS:
        where.append("kind=?")
        params.append(kind)
    if state in ITEM_STATES:
        where.append("state=?")
        params.append(state)
    where = " AND ".join(where)
    total = db.q1(f"SELECT COUNT(*) n FROM job_items WHERE {where}", params)["n"]
    pages, page, off = _paginate(total, per, int(request.args.get("page", 1)))
    # 失敗優先仍是預設排序（要先看到問題），分頁在同一個排序上切
    items = [dict(r) for r in db.q(
        f"SELECT * FROM job_items WHERE {where} ORDER BY (state='failed') DESC, id DESC "
        f"LIMIT ? OFFSET ?", params + [per, off])]
    counts = {(r["kind"], r["state"]): r["n"] for r in db.q(
        "SELECT kind, state, COUNT(*) n FROM job_items WHERE job_id=? GROUP BY kind, state",
        (job_id,))}
    by_kind, by_state = {}, {}
    for (k, s), n in counts.items():
        by_kind[k] = by_kind.get(k, 0) + n
        by_state[s] = by_state.get(s, 0) + n
    return render_template("job_detail.html", job=dict(job), items=items,
                           page=page, pages=pages, total=total,
                           flt={"kind": kind, "state": state},
                           by_kind=by_kind, by_state=by_state)


WRITE_KINDS = ("update_price", "set_note", "clear_note", "insert_link_row")


def _write_filters(args):
    """審核頁的篩選條件 → (where 片段, 參數)。同一組條件同時給列表、計數與
    「套用目前篩選的全部」使用，三者才不會各算各的。"""
    where, params = ["state IN ('pending','failed')"], []
    kind = args.get("kind") or ""
    state = args.get("state") or ""
    q = (args.get("q") or "").strip()
    if kind in WRITE_KINDS:
        where.append("kind=?")
        params.append(kind)
    if state in ("pending", "failed"):
        where.append("state=?")
        params.append(state)
    if q:
        where.append("product_code LIKE ?")
        params.append(f"%{q}%")
    return " AND ".join(where), params, {"kind": kind, "state": state, "q": q}


@bp.get("/review")
def review():
    per = 200
    where, params, flt = _write_filters(request.args)
    total = db.q1(f"SELECT COUNT(*) n FROM pending_writes WHERE {where}", params)["n"]
    pages, page, off = _paginate(total, per, int(request.args.get("page", 1)))
    n_pending = db.q1(f"SELECT COUNT(*) n FROM pending_writes WHERE {where} AND state='pending'",
                      params)["n"]
    writes = [dict(r) for r in db.q(
        f"SELECT * FROM pending_writes WHERE {where} ORDER BY id DESC LIMIT ? OFFSET ?",
        params + [per, off])]
    for w in writes:
        w["payload"] = db.jload(w["payload_json"], {})
    cands = [dict(r) for r in db.q(
        "SELECT * FROM candidates WHERE state='proposed' ORDER BY product_code, price_idr LIMIT 300")]
    counts = {r["kind"]: r["n"] for r in db.q(
        "SELECT kind, COUNT(*) n FROM pending_writes WHERE state='pending' GROUP BY kind")}
    return render_template("review.html", writes=writes, cands=cands, flt=flt,
                           page=page, pages=pages, total=total,
                           n_pending=n_pending, counts=counts,
                           all_pending=db.q1(
                               "SELECT COUNT(*) n FROM pending_writes WHERE state='pending'")["n"])


@bp.get("/settings")
def settings():
    return render_template("settings.html", cfg=cfg.all_values(),
                           last_sync=db.setting_get("last_sync_at"))


# -------------------------------------------------------------------- api --

ACTIVE_JOB_STATES = "('pending','running','paused_captcha','paused_login','paused_user')"


def _status_payload():
    """狀態列的真相來源。

    0803：這裡原本取 `ORDER BY id DESC LIMIT 1`，也就是「id 最大的活躍任務」。
    使用者在 full_scan #270 跑到一半時建立 apply #273，狀態列就一路顯示
    「任務#273 pending 0/1」，真正在跑的 #270 完全隱形——看起來像卡死，其實
    是排隊。改成回報「實際在執行的那一個」＋佇列長度＋被擋的那一個。
    """
    from . import shopee, sheets
    running = db.q1(f"SELECT * FROM jobs WHERE state='running' ORDER BY id LIMIT 1")
    blocked_job = db.q1("SELECT * FROM jobs WHERE state IN ('paused_captcha','paused_login') "
                        "ORDER BY id LIMIT 1")
    paused_user = db.q1("SELECT * FROM jobs WHERE state='paused_user' ORDER BY id LIMIT 1")
    pending = db.q1("SELECT * FROM jobs WHERE state='pending' ORDER BY id LIMIT 1")
    n_queued = db.q1(f"SELECT COUNT(*) AS n FROM jobs WHERE state IN {ACTIVE_JOB_STATES}")["n"]
    cur = blocked_job or running or paused_user or pending
    block = shopee.block_status()
    return {
        "chrome": shopee.driver_status(),
        "google": sheets.auth_status(),
        "job": dict(cur) if cur else None,
        "running_job": dict(running) if running else None,
        "queued_jobs": n_queued,
        "block": {"blocked": block["blocked"], "since": block["since"],
                  "reason": block["reason"], "probe_fails": block["probe_fails"],
                  "count_today": block["count_today"],
                  "warn": block["count_today"] >= shopee.BLOCK_WARN_COUNT},
        "dry_run": bool(cfg.get("dry_run")),
        "last_sync": db.setting_get("last_sync_at"),
        "pending_writes": db.q1("SELECT COUNT(*) AS n FROM pending_writes WHERE state='pending'")["n"],
        "proposed_candidates": db.q1("SELECT COUNT(*) AS n FROM candidates WHERE state='proposed'")["n"],
        "sync": dict(_sync_state),
    }


@bp.get("/api/status")
def api_status():
    """前端每 3 秒打一次。任何子系統故障都不得讓它 500——app.js 收到非 JSON
    就整個 pollStatus 放棄，狀態列、驗證碼橫幅、審核徽章會一起失明。"""
    try:
        return jsonify(_status_payload())
    except Exception as e:
        log.exception("api_status failed")
        return jsonify({"chrome": {"chrome_alive": False, "work_tab": False},
                        "google": {"ok": False, "reason": str(e)},
                        "job": None, "running_job": None, "queued_jobs": 0,
                        "block": {"blocked": False, "count_today": 0, "warn": False},
                        "dry_run": bool(cfg.get("dry_run")), "pending_writes": 0,
                        "proposed_candidates": 0, "sync": dict(_sync_state),
                        "status_error": f"{type(e).__name__}: {e}"})


@bp.post("/api/sync/pull")
def api_pull():
    if _sync_state["running"]:
        return jsonify({"ok": False, "error": "同步進行中"}), 409

    def run():
        from . import sheets
        _sync_state.update(running=True, error=None, result=None)
        try:
            _sync_state["result"] = sheets.pull_and_sync()
        except Exception as e:
            log.exception("pull failed")
            _sync_state["error"] = str(e)
        finally:
            _sync_state["running"] = False

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"ok": True})


@bp.post("/api/xlsx/import")
def api_xlsx():
    from . import sheets
    try:
        res = sheets.import_xlsx()
        return jsonify({"ok": True, "result": res})
    except Exception as e:
        log.exception("xlsx import failed")
        return jsonify({"ok": False, "error": str(e)}), 500


@bp.post("/api/jobs")
def api_create_job():
    data = request.get_json(force=True)
    kind = data.get("kind")
    if kind not in ("check", "find", "full_scan", "apply"):
        return jsonify({"ok": False, "error": f"未知任務類型 {kind}"}), 400
    params = {k: data.get(k) for k in ("product_codes", "link_ids", "write_ids", "include_find")
              if data.get(k) is not None}
    job_id = jobs_mod.create_job(kind, params)
    return jsonify({"ok": True, "job_id": job_id})


@bp.post("/api/jobs/<int:job_id>/<action>")
def api_job_action(job_id, action):
    if action == "retry_failed":
        n = jobs_mod.retry_failed_items(job_id)
        return jsonify({"ok": True, "retried": n, "state": jobs_mod.job_state(job_id)})
    fn = {"pause": jobs_mod.request_pause, "resume": jobs_mod.request_resume,
          "stop": jobs_mod.request_stop}.get(action)
    if fn is None:
        return jsonify({"ok": False}), 400
    fn(job_id)
    return jsonify({"ok": True, "state": jobs_mod.job_state(job_id)})


@bp.post("/api/products/<path:code>/baseline")
def api_baseline(code):
    data = request.get_json(force=True)
    vals, params = [], []
    # 客戶 1.5：基準價僅人工可改，任何值都接受（含低於表上最低價）
    for key in ("baseline_idr", "high_cost_pct"):
        if key in data:
            v = data[key]
            v = None if v in ("", None) else float(v)
            if key == "baseline_idr" and v is not None:
                v = int(v)
            vals.append(f"{key}=?")
            params.append(v)
    if not vals:
        return jsonify({"ok": False}), 400
    params += [db.now(), code]
    db.x(f"UPDATE products SET {','.join(vals)}, updated_at=? WHERE code=?", params)
    return jsonify({"ok": True})


@bp.post("/api/writes/apply")
def api_writes_apply():
    """ids / filter / 兩者皆無（＝全部待寫入）三選一。

    注意：帶了 filter 卻沒命中時必須直接回錯，絕不可退回「套用全部」——
    那會把使用者以為只有幾筆的操作變成整批寫入 Sheet。
    """
    data = request.get_json(force=True) if request.data else {}
    ids = data.get("ids")
    if not ids and "filter" in data:
        where, params, _ = _write_filters(data["filter"])
        ids = [r["id"] for r in db.q(
            f"SELECT id FROM pending_writes WHERE {where} AND state='pending'", params)]
        if not ids:
            return jsonify({"ok": False, "error": "篩選結果沒有可套用的項目"}), 400
    elif not ids:
        ids = [r["id"] for r in db.q("SELECT id FROM pending_writes WHERE state='pending'")]
    if not ids:
        return jsonify({"ok": False, "error": "沒有待寫入項目"}), 400
    job_id = jobs_mod.create_job("apply", {"write_ids": ids})
    return jsonify({"ok": True, "job_id": job_id})


@bp.post("/api/writes/discard")
def api_writes_discard():
    data = request.get_json(force=True)
    ids = data.get("ids") or []
    if not ids and data.get("filter"):
        # 跨頁全選捨棄：與列表同一組條件，只動 pending（failed 留著給人看原因）
        where, params, _ = _write_filters(data["filter"])
        n = db.x(f"UPDATE pending_writes SET state='discarded' "
                 f"WHERE {where} AND state='pending'", params).rowcount
        return jsonify({"ok": True, "discarded": n})
    if ids:
        db.x(f"UPDATE pending_writes SET state='discarded' WHERE id IN ({','.join('?'*len(ids))})", ids)
    return jsonify({"ok": True, "discarded": len(ids)})


@bp.post("/api/candidates/<int:cand_id>/<action>")
def api_candidate(cand_id, action):
    from . import finder
    if action == "accept":
        finder.accept_candidate(cand_id)
    elif action == "reject":
        finder.reject_candidate(cand_id)
    else:
        return jsonify({"ok": False}), 400
    return jsonify({"ok": True})


@bp.post("/api/chrome/launch")
def api_chrome():
    from . import cdp
    ok = cdp.ensure_chrome()
    return jsonify({"ok": ok})


@bp.post("/api/google/connect")
def api_google_connect():
    from . import sheets
    try:
        sheets.start_auth_flow()
        return jsonify({"ok": True})
    except sheets.GoogleAuthNeeded as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@bp.post("/api/locations/record")
def api_loc_record():
    from . import shopee
    shopee.record_locations()
    return jsonify({"ok": True})


@bp.get("/api/locations/status")
def api_loc_status():
    from . import shopee
    return jsonify(shopee.recording_status())


@bp.post("/api/config")
def api_config():
    data = request.get_json(force=True)
    allowed = {"high_cost_pct", "min_sold", "image_sim_threshold", "dry_run",
               "auto_accept_candidates", "record_keyword_to_sheet",
               "worksheet_name", "sheet_id", "chrome_path",
               "idr_per_twd_divisor", "target_links_per_product", "pacing",
               "find_time_budget_s", "search_locations_param",
               "work_block_hours", "work_break_minutes"}
    updates = {k: v for k, v in data.items() if k in allowed}
    if "high_cost_pct" in updates:
        updates["high_cost_pct"] = float(updates["high_cost_pct"])
    for k in ("min_sold", "target_links_per_product", "idr_per_twd_divisor",
              "find_time_budget_s", "work_break_minutes"):
        if k in updates:
            updates[k] = int(updates[k])
    for k in ("image_sim_threshold", "work_block_hours"):
        if k in updates:
            updates[k] = float(updates[k])
    cfg.set_values(updates)
    return jsonify({"ok": True, "config": cfg.all_values()})


@bp.post("/api/shutdown")
def api_shutdown():
    def die():
        log.info("shutdown requested from UI")
        os._exit(0)
    threading.Timer(0.5, die).start()
    return jsonify({"ok": True})

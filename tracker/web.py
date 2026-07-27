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
SELECT p.code, p.item_name, p.baseline_override_idr, p.high_cost_pct,
  COUNT(l.id) AS n_links,
  COALESCE(SUM(CASE WHEN l.status='valid' THEN 1 ELSE 0 END),0) AS n_valid,
  COALESCE(SUM(CASE WHEN l.status IN ('invalid','sold_out','unlisted','high_cost') THEN 1 ELSE 0 END),0) AS n_bad,
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
    page = max(1, int(request.args.get("page", 1)))
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
    rows = rows[(page - 1) * per: page * per]
    return render_template("products.html", rows=rows, q=q, flt=flt,
                           page=page, pages=(total + per - 1) // per, total=total,
                           target=int(cfg.get("target_links_per_product")))


@bp.get("/product/<path:code>")
def product(code):
    prod = db.q1("SELECT * FROM products WHERE code=?", (code,))
    if prod is None:
        return f"無此商品 {code}", 404
    links = [dict(r) for r in db.q(
        "SELECT * FROM links WHERE product_code=? ORDER BY active DESC, sheet_row_hint", (code,))]
    cands = [dict(r) for r in db.q(
        "SELECT * FROM candidates WHERE product_code=? ORDER BY state='proposed' DESC, id DESC", (code,))]
    valid_prices = [l["last_price_idr"] or l["price_idr"] for l in links
                    if l["active"] and l["status"] == "valid" and (l["last_price_idr"] or l["price_idr"])]
    auto_baseline = min(valid_prices) if valid_prices else None
    return render_template("product.html", p=dict(prod), links=links, cands=cands,
                           auto_baseline=auto_baseline,
                           global_pct=cfg.get("high_cost_pct"))


@bp.get("/jobs")
def jobs_page():
    rows = [dict(r) for r in db.q("SELECT * FROM jobs ORDER BY id DESC LIMIT 50")]
    return render_template("jobs.html", rows=rows)


@bp.get("/jobs/<int:job_id>")
def job_detail(job_id):
    job = db.q1("SELECT * FROM jobs WHERE id=?", (job_id,))
    if job is None:
        return "無此任務", 404
    items = [dict(r) for r in db.q(
        "SELECT * FROM job_items WHERE job_id=? ORDER BY (state='failed') DESC, id DESC LIMIT 300",
        (job_id,))]
    return render_template("job_detail.html", job=dict(job), items=items)


@bp.get("/review")
def review():
    writes = [dict(r) for r in db.q(
        "SELECT * FROM pending_writes WHERE state IN ('pending','failed') ORDER BY id DESC LIMIT 500")]
    for w in writes:
        w["payload"] = db.jload(w["payload_json"], {})
    cands = [dict(r) for r in db.q(
        "SELECT * FROM candidates WHERE state='proposed' ORDER BY product_code, price_idr LIMIT 300")]
    return render_template("review.html", writes=writes, cands=cands)


@bp.get("/settings")
def settings():
    return render_template("settings.html", cfg=cfg.all_values(),
                           last_sync=db.setting_get("last_sync_at"))


# -------------------------------------------------------------------- api --

@bp.get("/api/status")
def api_status():
    from . import shopee, sheets
    cur = db.q1("SELECT * FROM jobs WHERE state IN ('pending','running','paused_captcha','paused_login','paused_user') "
                "ORDER BY id DESC LIMIT 1")
    n_pending_writes = db.q1("SELECT COUNT(*) AS n FROM pending_writes WHERE state='pending'")["n"]
    n_proposed = db.q1("SELECT COUNT(*) AS n FROM candidates WHERE state='proposed'")["n"]
    return jsonify({
        "chrome": shopee.driver_status(),
        "google": sheets.auth_status(),
        "job": dict(cur) if cur else None,
        "dry_run": bool(cfg.get("dry_run")),
        "last_sync": db.setting_get("last_sync_at"),
        "pending_writes": n_pending_writes,
        "proposed_candidates": n_proposed,
        "sync": dict(_sync_state),
    })


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
    for key in ("baseline_override_idr", "high_cost_pct"):
        if key in data:
            v = data[key]
            v = None if v in ("", None) else float(v)
            if key == "baseline_override_idr" and v is not None:
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
    data = request.get_json(force=True) if request.data else {}
    ids = data.get("ids")
    if not ids:
        ids = [r["id"] for r in db.q("SELECT id FROM pending_writes WHERE state='pending'")]
    if not ids:
        return jsonify({"ok": False, "error": "沒有待寫入項目"}), 400
    job_id = jobs_mod.create_job("apply", {"write_ids": ids})
    return jsonify({"ok": True, "job_id": job_id})


@bp.post("/api/writes/discard")
def api_writes_discard():
    data = request.get_json(force=True)
    ids = data.get("ids") or []
    if ids:
        db.x(f"UPDATE pending_writes SET state='discarded' WHERE id IN ({','.join('?'*len(ids))})", ids)
    return jsonify({"ok": True})


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
               "auto_accept_candidates", "worksheet_name", "sheet_id", "chrome_path",
               "idr_per_twd_divisor", "target_links_per_product", "pacing",
               "search_locations_param"}
    updates = {k: v for k, v in data.items() if k in allowed}
    if "high_cost_pct" in updates:
        updates["high_cost_pct"] = float(updates["high_cost_pct"])
    for k in ("min_sold", "target_links_per_product", "idr_per_twd_divisor"):
        if k in updates:
            updates[k] = int(updates[k])
    if "image_sim_threshold" in updates:
        updates["image_sim_threshold"] = float(updates["image_sim_threshold"])
    cfg.set_values(updates)
    return jsonify({"ok": True, "config": cfg.all_values()})


@bp.post("/api/shutdown")
def api_shutdown():
    def die():
        log.info("shutdown requested from UI")
        os._exit(0)
    threading.Timer(0.5, die).start()
    return jsonify({"ok": True})

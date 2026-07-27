"""Single background worker executing jobs item-by-item.

Every job_item update is its own transaction, which is what makes jobs
crash-resumable: after a restart, orphaned jobs go to paused_user and the
Resume button re-enters at the first still-pending item.
"""
import logging
import threading
import time

from . import config as cfg
from . import db
from . import shopee

log = logging.getLogger(__name__)

_wake = threading.Event()

TERMINAL = ("done", "stopped", "failed")


class ItemFailed(Exception):
    pass


def create_job(kind, params=None):
    from . import checker, finder, sheets_apply
    expanders = {
        "check": checker.expand_check,
        "find": finder.expand_find,
        "full_scan": checker.expand_full_scan,
        "apply": sheets_apply.expand_apply,
    }
    cur = db.x("INSERT INTO jobs(kind, state, params_json, created_at, updated_at) VALUES(?,?,?,?,?)",
               (kind, "pending", db.jdump(params or {}), db.now(), db.now()))
    job_id = cur.lastrowid
    items = expanders[kind](params or {})
    c = db.conn()
    with c:
        for it in items:
            c.execute("INSERT INTO job_items(job_id, kind, target, state, updated_at) VALUES(?,?,?,?,?)",
                      (job_id, it["kind"], str(it["target"]), "pending", db.now()))
        c.execute("UPDATE jobs SET progress_total=? WHERE id=?", (len(items), job_id))
    _wake.set()
    return job_id


def add_item(job_id, kind, target):
    db.x("INSERT INTO job_items(job_id, kind, target, state, updated_at) VALUES(?,?,?,?,?)",
         (job_id, kind, str(target), "pending", db.now()))
    db.x("UPDATE jobs SET progress_total=progress_total+1 WHERE id=?", (job_id,))


def set_state(job_id, state, message=None):
    db.x("UPDATE jobs SET state=?, message=COALESCE(?, message), updated_at=? WHERE id=?",
         (state, message, db.now(), job_id))
    _wake.set()


def job_state(job_id):
    row = db.q1("SELECT state FROM jobs WHERE id=?", (job_id,))
    return row["state"] if row else "stopped"


def recover_orphan_jobs():
    db.x("UPDATE jobs SET state='paused_user', message='程式重啟，請按 Resume 續跑', updated_at=? "
         "WHERE state IN ('running','paused_captcha','paused_login')", (db.now(),))


def request_pause(job_id):
    if job_state(job_id) in ("running", "pending"):
        set_state(job_id, "paused_user", "已由使用者暫停")


def request_resume(job_id):
    if job_state(job_id) in ("paused_user", "paused_captcha", "paused_login"):
        set_state(job_id, "running", "續跑中")


def request_stop(job_id):
    if job_state(job_id) not in TERMINAL:
        set_state(job_id, "stopped", "已由使用者停止")


# ---------------------------------------------------------------- worker ---

def worker_loop():
    log.info("job worker started")
    while True:
        try:
            job = db.q1("SELECT * FROM jobs WHERE state IN ('pending','running') ORDER BY id LIMIT 1")
            if job is None:
                _wake.wait(2)
                _wake.clear()
                continue
            _run_job(dict(job))
        except Exception:
            log.exception("worker loop error")
            time.sleep(3)


def _wait_while_paused(job_id):
    """Block while the job is paused; return the state that ended the wait."""
    while True:
        st = job_state(job_id)
        if st not in ("paused_user", "paused_captcha", "paused_login"):
            return st
        time.sleep(2)


def _run_job(job):
    from . import checker, finder, sheets_apply
    runners = {
        "fetch_link": checker.run_fetch_link,
        "classify_product": checker.run_classify_product,
        "find_links": finder.run_find_links,
        "apply_writes": sheets_apply.run_apply_writes,
    }
    job_id = job["id"]
    set_state(job_id, "running", "執行中")
    log.info("job %s (%s) starting", job_id, job["kind"])

    while True:
        st = job_state(job_id)
        if st in ("paused_user", "paused_captcha", "paused_login"):
            st = _wait_while_paused(job_id)
        if st in TERMINAL:
            break
        item = db.q1("SELECT * FROM job_items WHERE job_id=? AND state='pending' ORDER BY id LIMIT 1",
                     (job_id,))
        if item is None:
            set_state(job_id, "done", "完成")
            break
        item = dict(item)
        db.x("UPDATE job_items SET state='running', updated_at=? WHERE id=?", (db.now(), item["id"]))
        try:
            result = runners[item["kind"]](job, item)
            db.x("UPDATE job_items SET state='done', result_json=?, updated_at=? WHERE id=?",
                 (db.jdump(result or {}), db.now(), item["id"]))
        except shopee.CaptchaDetected as e:
            log.warning("captcha detected: %s", e)
            db.x("UPDATE job_items SET state='pending', updated_at=? WHERE id=?", (db.now(), item["id"]))
            set_state(job_id, "paused_captcha", "偵測到驗證碼：請到 Chrome 視窗完成驗證後按 Resume")
            continue
        except shopee.LoginNeeded as e:
            log.warning("login needed: %s", e)
            db.x("UPDATE job_items SET state='pending', updated_at=? WHERE id=?", (db.now(), item["id"]))
            set_state(job_id, "paused_login", "蝦皮未登入：請到 Chrome 視窗登入後按 Resume")
            continue
        except ItemFailed as e:
            db.x("UPDATE job_items SET state='failed', result_json=?, updated_at=? WHERE id=?",
                 (db.jdump({"error": str(e)}), db.now(), item["id"]))
        except Exception as e:
            log.exception("item %s failed", item["id"])
            db.x("UPDATE job_items SET state='failed', result_json=?, updated_at=? WHERE id=?",
                 (db.jdump({"error": f"{type(e).__name__}: {e}"}), db.now(), item["id"]))
        db.x("UPDATE jobs SET progress_done=progress_done+1, updated_at=? WHERE id=?",
             (db.now(), job_id))

    # dry-run off → auto-apply this job's pending writes
    final = job_state(job_id)
    if final == "done" and job["kind"] in ("check", "find", "full_scan") and not cfg.get("dry_run"):
        rows = db.q("SELECT id FROM pending_writes WHERE job_id=? AND state='pending'", (job_id,))
        if rows:
            create_job("apply", {"write_ids": [r["id"] for r in rows]})
    log.info("job %s finished with state=%s", job_id, job_state(job_id))

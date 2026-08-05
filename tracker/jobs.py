"""Background workers executing jobs item-by-item.

Two workers, split by what they contend for:

  * shopee worker — check / find / full_scan。共用同一個 Chrome 工作分頁與
    節奏控制，所以**必須**嚴格序列化，一次只跑一個任務。
  * sheet worker  — apply。只碰 Google Sheets API，跟 Shopee 沒有任何共用
    資源，沒有理由排在幾十小時的掃描後面（0803：apply #273 因為排在 full_scan
    #270 後面，pending 了 6 小時 26 分）。

兩者都遵守同一個全域封鎖旗標：一旦 Shopee 端被擋，整個系統一起停手，期間
不寫任何判定、也不寫表，等人解驗證碼或等探測通過。

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

TERMINAL = ("done", "stopped", "failed")
PAUSED = ("paused_user", "paused_captcha", "paused_login")

SHOPEE_KINDS = ("check", "find", "full_scan")
SHEET_KINDS = ("apply",)

# 每個 worker 一個喚醒事件——共用一個的話，某個 worker 的 clear() 會吃掉
# 另一個的喚醒訊號。
_wakers = []
_wakers_lock = threading.Lock()


def _new_waker():
    ev = threading.Event()
    with _wakers_lock:
        _wakers.append(ev)
    return ev


def _wake_all():
    with _wakers_lock:
        for ev in list(_wakers):
            ev.set()


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
    _wake_all()
    return job_id


def add_item(job_id, kind, target):
    db.x("INSERT INTO job_items(job_id, kind, target, state, updated_at) VALUES(?,?,?,?,?)",
         (job_id, kind, str(target), "pending", db.now()))
    db.x("UPDATE jobs SET progress_total=progress_total+1 WHERE id=?", (job_id,))


def set_state(job_id, state, message=None):
    db.x("UPDATE jobs SET state=?, message=COALESCE(?, message), updated_at=? WHERE id=?",
         (state, message, db.now(), job_id))
    _wake_all()


def job_state(job_id):
    row = db.q1("SELECT state FROM jobs WHERE id=?", (job_id,))
    return row["state"] if row else "stopped"


def recover_orphan_jobs():
    db.x("UPDATE jobs SET state='paused_user', message='程式重啟，請按 Resume 續跑', updated_at=? "
         "WHERE state IN ('running','paused_captcha','paused_login')", (db.now(),))


def request_pause(job_id):
    if job_state(job_id) in ("running", "pending"):
        set_state(job_id, "paused_user", "已由使用者暫停")
        shopee.interrupt_waits()   # 長休中也要立刻有反應


def request_resume(job_id):
    if job_state(job_id) in PAUSED:
        # 人工續跑 = 人已經處理過驗證碼／登入，封鎖視為解除
        shopee.clear_blocked()
        set_state(job_id, "running", "續跑中")


def request_stop(job_id):
    if job_state(job_id) not in TERMINAL:
        set_state(job_id, "stopped", "已由使用者停止")
        shopee.interrupt_waits()


def retry_failed_items(job_id):
    """把該任務的失敗項打回 pending 重跑（Q7：不自動重試，由人決定）。

    自動重試在系統性故障下會把每個項目都跑兩次（Chrome 掛掉 → 5843 項全部
    重試一次 = 白燒一倍時間），所以這裡刻意做成手動觸發。
    """
    n = db.x("UPDATE job_items SET state='pending', updated_at=? WHERE job_id=? AND state='failed'",
             (db.now(), job_id)).rowcount
    if n:
        db.x("UPDATE jobs SET state='pending', progress_done=MAX(progress_done-?, 0), "
             "message=?, updated_at=? WHERE id=?",
             (n, f"重跑 {n} 個失敗項", db.now(), job_id))
        _wake_all()
    return n


# ---------------------------------------------------------------- workers ---

def worker_loop(kinds, name):
    waker = _new_waker()
    log.info("%s worker started (kinds=%s)", name, ",".join(kinds))
    placeholders = ",".join("?" * len(kinds))
    sql = ("SELECT * FROM jobs WHERE state IN ('pending','running') "
           f"AND kind IN ({placeholders}) ORDER BY id LIMIT 1")
    while True:
        try:
            # 全域封鎖：兩個 worker 一起停手。apply 雖然不碰 Shopee，但被擋
            # 期間產生的判定不可信，讓它繼續寫表等於把污染送出門。
            if shopee.is_blocked():
                waker.wait(5)
                waker.clear()
                continue
            job = db.q1(sql, kinds)
            if job is None:
                waker.wait(2)
                waker.clear()
                continue
            _run_job(dict(job))
        except Exception:
            log.exception("%s worker loop error", name)
            time.sleep(3)


def blocked_watcher_loop():
    """被擋期間定期探測，通過就自動續跑（Q2）。

    跨夜批次不該因為晚上九點的一次驗證碼就整夜停擺。探測間隔 10 分鐘，相對
    於工作時每 8 秒一次導覽是兩個數量級的降幅，就算沒過也不構成額外壓力。

    連續失敗 PROBE_MAX_FAILS 次（約 2 小時）就停止探測、純等人——那通常代表
    是需要人手動滑的驗證碼，再探測也不會過。
    """
    last = None
    while True:
        time.sleep(5)
        try:
            if not shopee.is_blocked():
                last = None
                continue
            if db.q1("SELECT 1 AS x FROM jobs WHERE state='paused_login' LIMIT 1"):
                continue        # 登入只能靠人，探測沒有意義
            if shopee.block_status()["probe_fails"] >= shopee.PROBE_MAX_FAILS:
                continue
            if last is None:
                last = time.monotonic()     # 剛被擋，先等一個間隔再探
                continue
            if time.monotonic() - last < shopee.PROBE_INTERVAL_S:
                continue
            last = time.monotonic()
            if shopee.probe_now():
                shopee.clear_blocked()
                n = db.x("UPDATE jobs SET state='running', message='封鎖解除，自動續跑', "
                         "updated_at=? WHERE state='paused_captcha'", (db.now(),)).rowcount
                log.info("探測通過，封鎖解除，自動續跑 %s 個任務", n)
                _wake_all()
            else:
                log.info("探測未通過（連續第 %s 次）", shopee.note_probe_failure())
        except Exception:
            log.exception("blocked watcher error")


def start_workers():
    threading.Thread(target=worker_loop, args=(SHOPEE_KINDS, "shopee"),
                     name="job-worker-shopee", daemon=True).start()
    threading.Thread(target=worker_loop, args=(SHEET_KINDS, "sheet"),
                     name="job-worker-sheet", daemon=True).start()
    threading.Thread(target=blocked_watcher_loop, name="blocked-watcher", daemon=True).start()


# -------------------------------------------------------------- execution ---

def _finish_job(job_id):
    """三級收尾（Q7）：跑完 97.3% 的大批次不該跟「完全沒做到事」長得一樣。"""
    counts = {r["state"]: r["n"] for r in db.q(
        "SELECT state, COUNT(*) n FROM job_items WHERE job_id=? GROUP BY state", (job_id,))}
    done, failed = counts.get("done", 0), counts.get("failed", 0)
    if failed and not done:
        # 挑第一個「有記到錯誤訊息」的失敗項——訊息才是人真正需要看的東西
        err = ""
        for r in db.q("SELECT result_json FROM job_items WHERE job_id=? AND state='failed' "
                      "ORDER BY id LIMIT 20", (job_id,)):
            err = (db.jload(r["result_json"], {}) or {}).get("error", "")
            if err:
                break
        set_state(job_id, "failed", f"失敗：{err}"[:300] if err else "全部項目失敗")
    elif failed:
        set_state(job_id, "done", f"完成（{failed} 項失敗）")
    else:
        set_state(job_id, "done", "完成")


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
    shopee.clear_interrupt()
    log.info("job %s (%s) starting", job_id, job["kind"])

    while True:
        st = job_state(job_id)
        if st in PAUSED:
            # 讓出 worker。暫停的任務原地空轉會佔死執行緒，後面的任務全部
            # 排隊等它——這是 0803 apply 卡住的成因之一。
            return
        if st in TERMINAL:
            break
        if shopee.is_blocked():
            set_state(job_id, "running", "系統偵測到封鎖，暫停執行中")
            return
        item = db.q1("SELECT * FROM job_items WHERE job_id=? AND state='pending' ORDER BY id LIMIT 1",
                     (job_id,))
        if item is None:
            _finish_job(job_id)
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

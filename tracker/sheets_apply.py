"""Apply-writes job glue (kept separate so jobs.py avoids importing sheets
eagerly — sheets pulls in google libs which need credentials at call time)."""
from . import db


def expand_apply(params):
    return [{"kind": "apply_writes", "target": "batch"}]


def run_apply_writes(job, item):
    from . import sheets, jobs
    # 授權與存取權都要先過：寫到一半才發現 token 失效或沒分享，會留下半套的表
    st = sheets.auth_status()
    if not st.get("ok"):
        raise jobs.ItemFailed(f"Google 授權未就緒（{st.get('mode')}）：{st.get('reason')}")
    ok, err = sheets.check_access(force=True)
    if not ok:
        raise jobs.ItemFailed(f"無法存取試算表：{err}")
    params = db.jload(job["params_json"], {})
    write_ids = params.get("write_ids") or [
        r["id"] for r in db.q("SELECT id FROM pending_writes WHERE state='pending'")]
    if not write_ids:
        return {"written": 0, "failed": 0, "note": "no pending writes"}

    def progress(done, total):
        db.x("UPDATE jobs SET progress_done=?, progress_total=?, updated_at=? WHERE id=?",
             (done, total, db.now(), job["id"]))

    return sheets.apply_writes(write_ids, progress=progress)

"""Apply-writes job glue (kept separate so jobs.py avoids importing sheets
eagerly — sheets pulls in google libs which need credentials at call time)."""
from . import db


def expand_apply(params):
    return [{"kind": "apply_writes", "target": "batch"}]


def run_apply_writes(job, item):
    from . import sheets, jobs
    params = db.jload(job["params_json"], {})
    write_ids = params.get("write_ids") or [
        r["id"] for r in db.q("SELECT id FROM pending_writes WHERE state='pending'")]
    if not write_ids:
        return {"written": 0, "failed": 0, "note": "no pending writes"}

    def progress(done, total):
        db.x("UPDATE jobs SET progress_done=?, progress_total=?, updated_at=? WHERE id=?",
             (done, total, db.now(), job["id"]))

    return sheets.apply_writes(write_ids, progress=progress)

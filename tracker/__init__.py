import logging
import logging.handlers
import os
import threading

from flask import Flask

from . import config as cfg
from . import db


def setup_logging():
    os.makedirs(cfg.LOG_DIR, exist_ok=True)
    root = logging.getLogger()
    if root.handlers:
        return
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    fh = logging.handlers.RotatingFileHandler(
        os.path.join(cfg.LOG_DIR, "app.log"), maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    root.addHandler(fh)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root.addHandler(sh)


def create_app(start_worker=True):
    setup_logging()
    cfg.load()
    db.init()

    # 基準價一次性定植（客戶規則：取「未執行查詢前」表上最低價，之後僅人工可改）
    try:
        from . import sheets
        sheets.seed_baselines()
    except Exception:
        logging.getLogger(__name__).exception("baseline seeding failed")

    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config["JSON_AS_ASCII"] = False

    from .web import bp
    app.register_blueprint(bp)

    if start_worker:
        from . import jobs
        jobs.recover_orphan_jobs()
        t = threading.Thread(target=jobs.worker_loop, name="job-worker", daemon=True)
        t.start()

    return app

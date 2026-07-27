import datetime
import json
import sqlite3
import threading

from . import config as cfg

_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
  code TEXT PRIMARY KEY,
  item_name TEXT,
  baseline_override_idr INTEGER,
  high_cost_pct REAL,
  updated_at TEXT
);

CREATE TABLE IF NOT EXISTS links (
  id INTEGER PRIMARY KEY,
  product_code TEXT NOT NULL,
  sheet_row_hint INTEGER,
  raw_url TEXT NOT NULL,
  canonical_url TEXT,
  shopid INTEGER,
  itemid INTEGER,
  model_id INTEGER,
  dedupe_key TEXT DEFAULT '',
  page_name TEXT,
  variant_text TEXT,
  price_idr INTEGER,
  supplier TEXT,
  note TEXT,
  status TEXT DEFAULT 'unchecked',
  status_detail TEXT,
  last_price_idr INTEGER,
  last_checked_at TEXT,
  origin TEXT DEFAULT 'sheet',
  active INTEGER DEFAULT 1,
  UNIQUE(product_code, raw_url)
);
CREATE INDEX IF NOT EXISTS idx_links_code ON links(product_code);
CREATE INDEX IF NOT EXISTS idx_links_dedupe ON links(dedupe_key);

CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'pending',
  params_json TEXT,
  progress_done INTEGER DEFAULT 0,
  progress_total INTEGER DEFAULT 0,
  message TEXT,
  created_at TEXT,
  updated_at TEXT
);

CREATE TABLE IF NOT EXISTS job_items (
  id INTEGER PRIMARY KEY,
  job_id INTEGER NOT NULL REFERENCES jobs(id),
  kind TEXT NOT NULL,
  target TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'pending',
  result_json TEXT,
  updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_job_items_job ON job_items(job_id, state);

CREATE TABLE IF NOT EXISTS pending_writes (
  id INTEGER PRIMARY KEY,
  job_id INTEGER,
  kind TEXT NOT NULL,
  product_code TEXT,
  dedupe_key TEXT,
  model_id INTEGER,
  payload_json TEXT,
  state TEXT NOT NULL DEFAULT 'pending',
  error TEXT,
  created_at TEXT,
  written_at TEXT
);

CREATE TABLE IF NOT EXISTS candidates (
  id INTEGER PRIMARY KEY,
  product_code TEXT NOT NULL,
  shopid INTEGER,
  itemid INTEGER,
  model_id INTEGER,
  title TEXT,
  variant_text TEXT,
  price_idr INTEGER,
  sold INTEGER,
  shop_name TEXT,
  shop_location TEXT,
  image_sim REAL,
  tier INTEGER,
  state TEXT NOT NULL DEFAULT 'proposed',
  note TEXT,
  created_at TEXT
);

CREATE TABLE IF NOT EXISTS url_cache (
  short_url TEXT PRIMARY KEY,
  final_url TEXT,
  resolved_at TEXT
);

CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT
);
"""


def now():
    return datetime.datetime.now().isoformat(timespec="seconds")


def conn() -> sqlite3.Connection:
    c = getattr(_local, "conn", None)
    if c is None:
        c = sqlite3.connect(cfg.DB_PATH, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA foreign_keys=ON")
        _local.conn = c
    return c


def init():
    c = conn()
    c.executescript(SCHEMA)
    c.commit()


def q(sql, params=()):
    return conn().execute(sql, params).fetchall()


def q1(sql, params=()):
    return conn().execute(sql, params).fetchone()


def x(sql, params=()):
    c = conn()
    cur = c.execute(sql, params)
    c.commit()
    return cur


def setting_get(key, default=None):
    row = q1("SELECT value FROM settings WHERE key=?", (key,))
    return row["value"] if row else default


def setting_set(key, value):
    x("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
      (key, str(value)))


def jdump(obj):
    return json.dumps(obj, ensure_ascii=False)


def jload(s, default=None):
    if not s:
        return default
    try:
        return json.loads(s)
    except ValueError:
        return default

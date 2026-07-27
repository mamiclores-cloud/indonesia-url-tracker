import json
import os
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
LOG_DIR = os.path.join(DATA_DIR, "logs")
IMAGE_DIR = os.path.join(DATA_DIR, "images")
CHROME_PROFILE_DIR = os.path.join(DATA_DIR, "chrome-profile")
DB_PATH = os.path.join(DATA_DIR, "tracker.db")
CONFIG_PATH = os.path.join(ROOT, "config.json")
CLIENT_SECRET_PATH = os.path.join(DATA_DIR, "client_secret.json")
GOOGLE_TOKEN_PATH = os.path.join(DATA_DIR, "google_token.json")
XLSX_PATH = os.path.join(ROOT, "印尼商品連結 Indo Shopee Purchase Link 的副本.xlsx")

DEFAULTS = {
    "port": 8765,
    "sheet_id": "1DOTTepXR8OZ2wskuiz98ZE6yeplmGp0Ezk0SX_Z_tOI",
    "worksheet_name": "purchase link",
    "idr_per_twd_divisor": 470,
    "high_cost_pct": 15,
    "min_sold": 100,
    "image_sim_threshold": 0.75,
    "target_links_per_product": 3,
    "dry_run": True,
    "auto_accept_candidates": False,
    "chrome_path": "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
    "chrome_debug_port": 9222,
    "pacing": {"min_delay_s": 3, "max_delay_s": 8, "long_pause_every": 25, "long_pause_s": 60},
    "location_tiers": [
        ["Kota Tangerang", "Kab. Tangerang", "Kota Tangerang Selatan",
         "Jakarta Barat", "Jakarta Pusat", "Jakarta Selatan", "Jakarta Timur", "Jakarta Utara"],
        ["Jabodetabek"],
        ["DKI Jakarta", "Banten"],
        ["Jawa Barat"],
        []
    ],
    "search_locations_param": {},  # {"param": "locations"|"fe_filter_options", "value": str}
}

_lock = threading.Lock()
_config = {}


def load():
    global _config
    with _lock:
        data = {}
        if os.path.exists(CONFIG_PATH):
            try:
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except (ValueError, OSError):
                data = {}
        merged = dict(DEFAULTS)
        merged.update(data)
        _config = merged
        if merged != data:
            _save_locked()
        for d in (DATA_DIR, LOG_DIR, IMAGE_DIR):
            os.makedirs(d, exist_ok=True)
    return _config


def _save_locked():
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(_config, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_PATH)


def get(key, default=None):
    if not _config:
        load()
    return _config.get(key, default)


def set_values(values: dict):
    with _lock:
        _config.update(values)
        _save_locked()


def all_values():
    if not _config:
        load()
    return dict(_config)

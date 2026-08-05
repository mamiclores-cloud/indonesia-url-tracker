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
# 存在就優先使用（不過期、不需瀏覽器授權）；試算表要分享給裡面的 client_email
SERVICE_ACCOUNT_PATH = os.path.join(DATA_DIR, "service_account.json")
XLSX_PATH = os.path.join(ROOT, "印尼商品連結 Indo Shopee Purchase Link 的副本.xlsx")

DEFAULTS = {
    "port": 8765,
    "sheet_id": "1DOTTepXR8OZ2wskuiz98ZE6yeplmGp0Ezk0SX_Z_tOI",
    "worksheet_name": "purchase link",
    "idr_per_twd_divisor": 470,
    "high_cost_pct": 15,
    "min_sold": 100,
    "image_sim_threshold": 0.6,
    "target_links_per_product": 3,
    # 單一商品補找的牆鐘上限（秒）。到點就用已補到的收工，剩下的下次再補，
    # 避免個別難找的商品把整批掃描拖住。
    "find_time_budget_s": 240,
    "dry_run": False,
    "auto_accept_candidates": True,
    "record_keyword_to_sheet": True,
    "chrome_path": "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
    "chrome_debug_port": 9222,
    # 0803 事故後回退到這組：1-3s/50/30s 把每次導覽的平均間隔從 ~7.9s 壓到
    # ~2.6s（導覽速率約 470 次/小時），連續跑 29.6 小時後被 Shopee 攔截；
    # 3-8s/25/60s 則是實測 22 小時 5,450 次導覽零驗證碼的那一組。
    "pacing": {"min_delay_s": 3, "max_delay_s": 8, "long_pause_every": 25, "long_pause_s": 60},
    # 連續工作 N 小時就強制休息 M 分鐘。事故的兩個變因（速率、連續時長）裡，
    # 連續時長是從未被驗證過的那一個——舊節奏只跑過 22 小時，這次是在第
    # 29.6 小時被擋。放慢節奏不能取代休息。
    "work_block_hours": 4,
    "work_break_minutes": 10,
    # City-level names only — Shopee's location filter and shop_location fields
    # are city-level, so region names like "Jabodetabek"/"Jawa Barat" match
    # nothing and waste a whole tier. Ordered inner→outer from Jakarta; last
    # tier is unfiltered (nationwide).
    "location_tiers": [
        ["Kota Tangerang", "Kab. Tangerang", "Kota Tangerang Selatan",
         "Jakarta Barat", "Jakarta Pusat", "Jakarta Selatan", "Jakarta Timur", "Jakarta Utara"],
        ["Kota Bekasi", "Kab. Bekasi", "Kota Depok", "Kota Bogor", "Kab. Bogor", "Kota Tangerang Selatan"],
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
